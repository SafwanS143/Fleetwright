#!/usr/bin/env python3
"""SQLite incident store + timeline (Chunk 24).

The alerting path so far is stateless: Alertmanager routes notifications, but nothing REMEMBERS —
"how many incidents did sim-01 have this week, how long did each last, did that anomaly flap?" has no
answer once the Slack scrollback ages out. This service is the memory: it consumes the same Alertmanager
webhook the log sink does (every receiver fans out here with `send_resolved: true`), and turns the
alert stream into incident ROWS with a lifecycle:

    firing   → open an incident (or reopen a just-resolved one — see reopen window below)
    resolved → close it, record the duration

Incidents come in three scopes, derived from the labels Alertmanager forwards — the store must handle
all of them, they are different kinds of event:
    channel — per-channel anomaly (device + channel labels): one sensor stream misbehaving
    device  — per-device staleness (device label only): one box went quiet
    fleet   — availability / error-rate / pipeline (no device label): systemic, one row for the event,
              NOT one per device (inhibition in Chunk 23 mutes the per-device noise; here the fleet
              incident is the single record of it)

Identity & dedup: Alertmanager's `fingerprint` (a stable hash of the alert's label set) is the natural
key. A repeat notification for a still-open incident (group_interval/repeat_interval re-sends) is a
no-op — store-level dedup mirroring Alertmanager's notification-level dedup. A re-fire arriving within
FLEET_REOPEN_WINDOW of the resolve REOPENS the same row instead of minting a new one: a fault that
flaps past the rule-level hysteresis (`keep_firing_for`, Chunk 23) still records as ONE operational
episode with a `reopened` event in its timeline, not a stack of two-minute incidents. The window
defaults to 300s to match Alertmanager's group_interval — the two layers agree on what "the same
episode" means.

One failure mode needs explicit handling: an alert whose RESOLVED notification never arrives — seen
live in testing when a per-device Stale alert resolved while the fleet-availability critical was still
inhibiting it (inhibition mutes resolves too), and equally possible across an Alertmanager restart.
Without a backstop that incident is open forever. So a janitor sweep expires open incidents not heard
from in FLEET_INCIDENT_EXPIRY (default 5h — deliberately past Alertmanager's 4h repeat_interval, since
any still-firing, un-muted alert re-notifies at least that often; an open incident silent for longer
is either resolved-unheard or muted because a covering incident owns the page).

Storage is SQLite on a named volume: incidents are the system's history, so they must survive
`compose down` — and on restart the open ones are rehydrated into the gauge below. Grafana gets the
timeline via Prometheus, not by reading the DB: the store exports `fleet_incident_active` (1 while
open, 0 after) which a state-timeline panel renders directly, plus open-count/opened/reopened/TTR
series. That keeps Grafana plugin-free and reuses the scrape path everything else already uses; the
full rows + per-incident event timeline are served as JSON at /incidents for inspection.

Stdlib + prometheus_client only.
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

PORT = int(os.environ.get("FLEET_INCIDENT_PORT", "9096"))
DB_PATH = os.environ.get("FLEET_INCIDENT_DB", "/data/incidents.db")
# A re-fire of the same fingerprint within this many seconds of its resolve reopens the incident
# instead of opening a new one. Matches Alertmanager's group_interval (5m) so the store and the
# notifier draw the "same episode vs. new episode" line in the same place.
REOPEN_WINDOW = float(os.environ.get("FLEET_REOPEN_WINDOW", "300"))
# Janitor: open incidents not heard from (no firing notification) in this long are force-closed as
# 'expired' — the swallowed-resolve backstop. Must exceed repeat_interval (4h), or still-firing
# incidents would expire between re-notifications.
EXPIRY = float(os.environ.get("FLEET_INCIDENT_EXPIRY", "18000"))

# ── metrics ──────────────────────────────────────────────────────────────────
# The timeline primitive: 1 while an incident on this (alert, device, channel) is open. Grafana's
# state-timeline panel draws this directly — red span = open incident, at full scrape resolution.
INCIDENT_ACTIVE = Gauge(
    "fleet_incident_active",
    "1 while an incident is open for this alert/device/channel, 0 once resolved.",
    ["alertname", "device", "channel", "severity", "scope"],
)
OPEN_BY_SEVERITY = Gauge(
    "fleet_incidents_open",
    "Number of currently open incidents.",
    ["severity"],
)
OPENED = Counter(
    "fleet_incidents_opened_total",
    "Incidents opened.",
    ["severity", "scope"],
)
REOPENED = Counter(
    "fleet_incidents_reopened_total",
    "Incidents reopened by a re-fire inside the reopen window (flap indicator).",
    ["severity", "scope"],
)
# Time-to-recovery per resolved incident. sum/count of this histogram IS the MTTR panel; buckets sized
# for incident scales (minutes→hours), not request latency.
TTR = Histogram(
    "fleet_incident_ttr_seconds",
    "Open→resolved duration of each incident (time to recovery).",
    buckets=(30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400, 28800),
)

# ── storage ──────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint  TEXT NOT NULL,           -- Alertmanager's stable hash of the label set (identity)
  alertname    TEXT NOT NULL,
  scope        TEXT NOT NULL,           -- fleet | device | channel
  device       TEXT,                    -- NULL for fleet-scoped incidents
  channel      TEXT,                    -- NULL unless channel-scoped (anomaly)
  severity     TEXT,
  team         TEXT,
  sli          TEXT,
  summary      TEXT,
  status       TEXT NOT NULL DEFAULT 'open',   -- open | resolved
  opened_at    REAL NOT NULL,           -- unix seconds; survives reopens (episode start, not last flap)
  resolved_at  REAL,
  last_seen_at REAL,                    -- last firing notification; drives the expiry sweep
  reopen_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents (fingerprint, status);

CREATE TABLE IF NOT EXISTS incident_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  ts          REAL NOT NULL,
  kind        TEXT NOT NULL,            -- opened | reopened | resolved
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_incident ON incident_events (incident_id, ts);
"""

# ThreadingHTTPServer handles each POST on its own thread; one connection + one lock serializes all
# DB work. Incident traffic is a few rows a minute — a lock, not a pool, is the right size.
LOCK = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(SCHEMA)
db.execute("PRAGMA journal_mode=WAL")  # webhook writes + /incidents reads without blocking each other
db.commit()

_seen_severities = {"warning", "critical"}


def parse_ts(value):
    """RFC3339 from Alertmanager → unix seconds. Go's zero time ("0001-01-01...") means 'not set'
    (endsAt while still firing) → None. Go can emit nanosecond fractions; trim to microseconds for
    fromisoformat."""
    if not value:
        return None
    try:
        trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(trimmed)
    except ValueError:
        return None
    if dt.year < 2000:
        return None
    return dt.timestamp()


def log(action, row_id, severity, scope, alertname, attribution, note=""):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(
        f"[incidents {now}] {action:8} #{row_id:<4} sev={severity:8} scope={scope:8} "
        f"{alertname} <{attribution}>{' :: ' + note if note else ''}",
        flush=True,
    )


def add_event(incident_id, ts, kind, note):
    db.execute(
        "INSERT INTO incident_events (incident_id, ts, kind, note) VALUES (?, ?, ?, ?)",
        (incident_id, ts, kind, note),
    )


def refresh_open_gauge():
    counts = dict.fromkeys(_seen_severities, 0)
    for row in db.execute(
        "SELECT severity, COUNT(*) AS n FROM incidents WHERE status='open' GROUP BY severity"
    ):
        counts[row["severity"] or "none"] = row["n"]
    for sev, n in counts.items():
        _seen_severities.add(sev)
        OPEN_BY_SEVERITY.labels(severity=sev).set(n)


def handle_alert(alert):
    """Apply one webhook alert to the store. Caller holds LOCK."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status")
    fingerprint = alert.get("fingerprint") or json.dumps(labels, sort_keys=True)

    alertname = labels.get("alertname", "unknown")
    device = labels.get("device")
    channel = labels.get("channel")
    severity = labels.get("severity", "none")
    # Scope from label shape: channel ⊂ device ⊂ fleet. Fleet-scoped alerts carry no device label,
    # so a systemic event is ONE incident row, never N per-device rows.
    scope = "channel" if channel else ("device" if device else "fleet")
    attribution = f"{device or 'fleet'}{'/' + channel if channel else ''}"
    summary = annotations.get("summary", "")
    now = time.time()

    open_row = db.execute(
        "SELECT * FROM incidents WHERE fingerprint=? AND status='open' ORDER BY id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()

    if status == "firing":
        if open_row:
            # Already tracked: group_interval/repeat_interval re-notifications are not new state —
            # but they are proof of life, which is what keeps the expiry sweep off this incident.
            db.execute("UPDATE incidents SET last_seen_at=? WHERE id=?", (now, open_row["id"]))
            return
        # startsAt is when the alert began firing — truer than webhook arrival, which group_wait delays.
        started = parse_ts(alert.get("startsAt")) or now

        recent = db.execute(
            "SELECT * FROM incidents WHERE fingerprint=? AND status='resolved' "
            "ORDER BY resolved_at DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if recent and recent["resolved_at"] and now - recent["resolved_at"] <= REOPEN_WINDOW:
            gap = int(now - recent["resolved_at"])
            db.execute(
                "UPDATE incidents SET status='open', resolved_at=NULL, last_seen_at=?, "
                "reopen_count=reopen_count+1 WHERE id=?",
                (now, recent["id"]),
            )
            add_event(recent["id"], now, "reopened",
                      f"re-fired {gap}s after resolve (reopen window {int(REOPEN_WINDOW)}s)")
            REOPENED.labels(severity=severity, scope=scope).inc()
            log("REOPENED", recent["id"], severity, scope, alertname, attribution,
                f"flap: quiet {gap}s")
        else:
            cur = db.execute(
                "INSERT INTO incidents (fingerprint, alertname, scope, device, channel, severity, "
                "team, sli, summary, status, opened_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
                (fingerprint, alertname, scope, device, channel, severity,
                 labels.get("team"), labels.get("sli"), summary, started, now),
            )
            add_event(cur.lastrowid, started, "opened", summary)
            OPENED.labels(severity=severity, scope=scope).inc()
            log("OPENED", cur.lastrowid, severity, scope, alertname, attribution, summary)
        INCIDENT_ACTIVE.labels(
            alertname=alertname, device=device or "", channel=channel or "",
            severity=severity, scope=scope,
        ).set(1)

    elif status == "resolved":
        if not open_row:
            # Resolve for something we never opened (store deployed mid-incident, or a replayed
            # notification for an already-closed row) — nothing to do.
            return
        ended = parse_ts(alert.get("endsAt")) or now
        db.execute(
            "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?",
            (ended, open_row["id"]),
        )
        # Duration spans the whole episode including reopens/quiet gaps — deliberately: the fault was
        # not fixed during the flap's quiet phases, so they count against recovery time.
        duration = max(ended - open_row["opened_at"], 0.0)
        add_event(open_row["id"], ended, "resolved", f"after {int(duration)}s")
        TTR.observe(duration)
        INCIDENT_ACTIVE.labels(
            alertname=alertname, device=device or "", channel=channel or "",
            severity=open_row["severity"] or "none", scope=open_row["scope"],
        ).set(0)
        log("RESOLVED", open_row["id"], severity, scope, alertname, attribution,
            f"after {int(duration)}s")


def expire_stale_incidents():
    """The swallowed-resolve backstop: force-close open incidents with no firing notification in
    EXPIRY seconds. Any live, un-muted alert re-notifies at least every repeat_interval (4h < EXPIRY),
    so silence this long means the resolve never reached us (inhibited at resolve time, Alertmanager
    restart) — or the alert is muted by a covering incident, which then owns the record. Expired
    closures do NOT feed the TTR histogram: the real recovery time is unknown, and inventing one
    would corrupt MTTR."""
    now = time.time()
    with LOCK:
        rows = db.execute(
            "SELECT * FROM incidents WHERE status='open' AND COALESCE(last_seen_at, opened_at) < ?",
            (now - EXPIRY,),
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?", (now, row["id"])
            )
            silent = int(now - (row["last_seen_at"] or row["opened_at"]))
            add_event(row["id"], now, "expired",
                      f"no notification for {silent}s (> expiry {int(EXPIRY)}s); resolve assumed lost")
            INCIDENT_ACTIVE.labels(
                alertname=row["alertname"], device=row["device"] or "",
                channel=row["channel"] or "", severity=row["severity"] or "none",
                scope=row["scope"],
            ).set(0)
            log("EXPIRED", row["id"], row["severity"] or "none", row["scope"], row["alertname"],
                f"{row['device'] or 'fleet'}{'/' + row['channel'] if row['channel'] else ''}",
                f"silent {silent}s")
        if rows:
            refresh_open_gauge()
        db.commit()


def janitor_loop():
    while True:
        time.sleep(60)
        try:
            expire_stale_incidents()
        except Exception as exc:  # never let the janitor kill the store
            print(f"[incidents] janitor error: {exc}", flush=True)


def rehydrate():
    """On restart, open incidents in the DB are still open in the world — put them back in the gauge
    so the timeline doesn't show a phantom recovery, and prime the open-count series so they exist
    (a labeled gauge has no series until first set, and 'no data' is not 0)."""
    with LOCK:
        rows = db.execute("SELECT * FROM incidents WHERE status='open'").fetchall()
        for row in rows:
            INCIDENT_ACTIVE.labels(
                alertname=row["alertname"], device=row["device"] or "",
                channel=row["channel"] or "", severity=row["severity"] or "none",
                scope=row["scope"],
            ).set(1)
        refresh_open_gauge()
        db.commit()
    if rows:
        print(f"[incidents] rehydrated {len(rows)} open incident(s) from {DB_PATH}", flush=True)


def recent_incidents(limit=100):
    """Caller holds LOCK. Newest-first rows with their event timeline nested."""
    incidents = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM incidents ORDER BY opened_at DESC LIMIT ?", (limit,)
        )
    ]
    for incident in incidents:
        incident["events"] = [
            dict(ev)
            for ev in db.execute(
                "SELECT ts, kind, note FROM incident_events WHERE incident_id=? ORDER BY ts",
                (incident["id"],),
            )
        ]
    return incidents


# ── HTTP surface ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body, content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def do_POST(self):
        if self.path != "/alert":
            self._reply(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reply(400, "bad json")
            return
        # Alertmanager batches a whole group per POST; each member alert advances its own incident.
        with LOCK:
            for alert in payload.get("alerts", []):
                handle_alert(alert)
            refresh_open_gauge()
            db.commit()
        self._reply(200, "ok")

    def do_GET(self):
        if self.path == "/metrics":
            self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
        elif self.path.startswith("/incidents"):
            with LOCK:
                body = json.dumps(recent_incidents(), indent=2)
            self._reply(200, body, "application/json")
        else:
            self._reply(200, "incident-store ok")

    def log_message(self, *args):
        pass  # lifecycle lines above are the only output we want


if __name__ == "__main__":
    rehydrate()
    threading.Thread(target=janitor_loop, daemon=True).start()
    print(
        f"[incidents] store at {DB_PATH}; webhook /alert, JSON /incidents, /metrics on :{PORT} "
        f"(reopen window {int(REOPEN_WINDOW)}s, expiry {int(EXPIRY)}s)",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
