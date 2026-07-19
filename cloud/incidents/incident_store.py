#!/usr/bin/env python3
"""SQLite incident store: turns the Alertmanager alert stream into incident rows with a lifecycle.

Consumes the same Alertmanager webhook as the log sink (firing opens an incident, resolved closes it
and records the duration) and exports fleet_incident_* metrics that Prometheus scrapes and Grafana
renders as a timeline. Stdlib + prometheus_client only.
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
# A re-fire within this long of a resolve reopens the same incident. Matches Alertmanager's
# group_interval so store and notifier agree on "same episode vs. new episode".
REOPEN_WINDOW = float(os.environ.get("FLEET_REOPEN_WINDOW", "300"))
# Force-close open incidents silent this long (swallowed-resolve backstop). Must exceed repeat_interval
# so a still-firing alert always re-notifies first.
EXPIRY = float(os.environ.get("FLEET_INCIDENT_EXPIRY", "18000"))

# ── metrics ──────────────────────────────────────────────────────────────────
# 1 while an incident is open — the state-timeline panel draws this directly.
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
# sum/count of this histogram is the MTTR panel; buckets sized for minutes→hours, not request latency.
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
  device       TEXT,
  channel      TEXT,
  severity     TEXT,
  team         TEXT,
  sli          TEXT,
  summary      TEXT,
  status       TEXT NOT NULL DEFAULT 'open',   -- open | resolved
  opened_at    REAL NOT NULL,           -- episode start; survives reopens
  resolved_at  REAL,
  last_seen_at REAL,                    -- last firing notification; drives the expiry sweep
  reopen_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents (fingerprint, status);

CREATE TABLE IF NOT EXISTS incident_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  ts          REAL NOT NULL,
  kind        TEXT NOT NULL,            -- opened | reopened | resolved | expired
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_incident ON incident_events (incident_id, ts);
"""

# One connection + one lock serializes all DB work; incident traffic is a few rows a minute.
LOCK = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(SCHEMA)
db.execute("PRAGMA journal_mode=WAL")
db.commit()

_seen_severities = {"warning", "critical"}


def parse_ts(value):
    """RFC3339 → unix seconds. Go's zero time (year < 2000) means 'not set' → None; trim
    sub-microsecond fractions that fromisoformat rejects."""
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
    # Scope from label shape: a fleet-wide alert carries no device label, so it's one row not N.
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
            # Re-notification of a tracked incident: not new state, but proof of life for the sweep.
            db.execute("UPDATE incidents SET last_seen_at=? WHERE id=?", (now, open_row["id"]))
            return
        # startsAt is truer than webhook arrival, which group_wait delays.
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
            # Resolve for something we never opened (deployed mid-incident, or a replay) — ignore.
            return
        ended = parse_ts(alert.get("endsAt")) or now
        db.execute(
            "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?",
            (ended, open_row["id"]),
        )
        # Duration spans the whole episode, flap quiet-gaps included — the fault wasn't fixed then.
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
    """Force-close open incidents silent longer than EXPIRY: their resolve was lost (inhibited at
    resolve time, or an Alertmanager restart). Expired closures don't feed TTR — recovery time is
    unknown, and inventing one would corrupt MTTR."""
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
    """On restart, open incidents are still open in the world — reprime the gauges so the timeline
    doesn't show a phantom recovery ('no data' is not 0)."""
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
        # Alertmanager batches a whole group per POST; each member advances its own incident.
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
        pass  # suppress the default access log; our lifecycle lines are the only output


if __name__ == "__main__":
    rehydrate()
    threading.Thread(target=janitor_loop, daemon=True).start()
    print(
        f"[incidents] store at {DB_PATH}; webhook /alert, JSON /incidents, /metrics on :{PORT} "
        f"(reopen window {int(REOPEN_WINDOW)}s, expiry {int(EXPIRY)}s)",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
