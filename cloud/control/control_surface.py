#!/usr/bin/env python3
"""Mission Control: the operator cockpit for the fleet, and the human-driven end of the cloud->device loop.

One screen that comes alive when the fleet does. It subscribes to MQTT for real-time telemetry, status,
and command acks, and polls Prometheus / the incident store / the remediator for the derived state those
services own (anomaly scores, open incidents, self-healing activity). It streams a merged snapshot to the
browser over Server-Sent Events at video frame rate, so a physical fault — twist the IMU — is visible end
to end live: the gyro trace spikes, the anomaly pill flares, an incident scrolls into the event feed, and
the remediator's reboot (or one you click here) brings it back to green.

The same panel publishes OTA commands on `fleet/<id>/cmd` and shows each command's ack + round-trip time,
so it doubles as proof the downlink is a real bidirectional control channel, not just a dashboard.

    open http://localhost:9099            # the cockpit
    GET  /stream                          # SSE snapshot feed
    POST /cmd?device=fleet-edge-01&cmd=set_rate&hz=25
    GET  /state | /metrics | /healthz
"""

import json
import math
import os
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import paho.mqtt.client as mqtt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

BROKER_HOST = os.environ.get("FLEET_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("FLEET_BROKER_PORT", "1883"))
HTTP_PORT = int(os.environ.get("FLEET_CONTROL_PORT", "9099"))
DEFAULT_DEVICE = os.environ.get("FLEET_DEVICE_ID", "fleet-edge-01")
RATE_WINDOW = float(os.environ.get("FLEET_RATE_WINDOW", "4"))
RATE_PRESETS = [1, 10, 25, 50]

# Derived-state sources. Each is optional: if one is unreachable the cockpit degrades to what it has,
# it never blocks the live MQTT path. localhost defaults let it run outside compose against port-forwards.
PROM_URL = os.environ.get("FLEET_PROM_URL", "http://prometheus:9090")
INCIDENTS_URL = os.environ.get("FLEET_INCIDENTS_URL", "http://incidents:9096")
REMEDIATOR_URL = os.environ.get("FLEET_REMEDIATOR_URL", "http://remediator:9098")
POLL_INTERVAL = float(os.environ.get("FLEET_POLL_INTERVAL", "1.5"))
SSE_INTERVAL = float(os.environ.get("FLEET_SSE_INTERVAL", "0.1"))   # 10 Hz stream to the browser
STALE_AFTER = float(os.environ.get("FLEET_STALE_AFTER", "10"))      # freshness SLO: degraded past this

# Channels rendered as anomaly pills, in display order, with a friendly short label.
CHANNELS = [("temperature", "temp"), ("humidity", "humid"), ("pressure", "press"),
            ("accel_magnitude", "accel"), ("gyro_magnitude", "gyro")]

CMDS_SENT = Counter("fleet_control_commands_total", "Commands published from the control surface.",
                    ["device", "cmd"])
ACKS = Counter("fleet_control_acks_total", "Acks received for control-surface commands.",
               ["device", "cmd", "ok"])
ACK_RTT = Histogram("fleet_control_ack_rtt_seconds", "Command publish -> ack round-trip time.",
                    ["cmd"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5))


def log(msg):
    print(f"[control] {msg}", flush=True)


class Fleet:
    """Single source of truth for the cockpit, updated from three writers (MQTT thread, poller thread,
    HTTP command handlers) under one lock. The SSE loop only ever reads a snapshot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.devices: dict[str, dict] = {}
        self.events: deque = deque(maxlen=200)     # narration feed, newest appended
        self.incidents: list[dict] = []            # active incidents from the incident store
        self.remediations: list[dict] = []         # what the remediator is currently working
        self._event_seq = 0
        self.get(DEFAULT_DEVICE)

    def get(self, device: str) -> dict:
        d = self.devices.get(device)
        if d is None:
            d = {
                "status": "unknown",
                "ts": deque(),                 # telemetry arrival times, for observed Hz
                "last_ts": None,               # wall clock of last telemetry, for freshness
                "telem": {},                   # last temp/humidity/pressure/accel/gyro
                "anomaly": {},                 # channel -> {score, flag}, from Prometheus
                "bands": {},                   # channel -> {lo, hi}, from Prometheus
                "pending": None,
                "last_ack": None,
            }
            self.devices[device] = d
        return d

    def event(self, kind: str, msg: str):
        """Append one narration event. Caller holds the lock."""
        self._event_seq += 1
        self.events.append({"id": self._event_seq, "t": time.time(), "kind": kind, "msg": msg})

    # ── MQTT writers (real-time) ──────────────────────────────────────────────
    def note_telemetry(self, device: str, data: dict):
        with self.lock:
            d = self.get(device)
            d["ts"].append(time.monotonic())
            d["last_ts"] = time.time()
            t = d["telem"]
            for src, dst in (("temp", "temp"), ("humidity", "humidity"), ("pressure", "pressure")):
                if src in data:
                    t[dst] = data[src]
            if all(k in data for k in ("ax", "ay", "az")):
                t["accel"] = math.sqrt(data["ax"] ** 2 + data["ay"] ** 2 + data["az"] ** 2)
            if all(k in data for k in ("gx", "gy", "gz")):
                t["gyro"] = math.sqrt(data["gx"] ** 2 + data["gy"] ** 2 + data["gz"] ** 2)

    def note_status(self, device: str, status: str):
        with self.lock:
            d = self.get(device)
            if d["status"] != status:
                self.event("status", f"{device} → {status}")
            d["status"] = status

    def note_command(self, device: str, cmd: str, hz):
        with self.lock:
            self.get(device)["pending"] = {"cmd": cmd, "hz": hz, "sent": time.monotonic()}
            label = f"{cmd} {hz} Hz" if cmd == "set_rate" else cmd
            self.event("cmd", f"{device} ← {label}")

    def note_ack(self, device: str, ack: dict):
        with self.lock:
            d = self.get(device)
            cmd, ok = ack.get("cmd", "?"), bool(ack.get("ok"))
            rtt = None
            pending = d["pending"]
            if pending and pending["cmd"] == cmd:   # only charge RTT to a command we're awaiting
                rtt = time.monotonic() - pending["sent"]
                d["pending"] = None
                ACK_RTT.labels(cmd=cmd).observe(rtt)
            d["last_ack"] = {"cmd": cmd, "ok": ok, "hz": ack.get("hz"),
                             "rtt_ms": round(rtt * 1000) if rtt is not None else None,
                             "at": time.monotonic()}
            ACKS.labels(device=device, cmd=cmd, ok=str(ok).lower()).inc()
            verdict = "ok" if ok else "rejected"
            extra = f" ({ack.get('hz')} Hz)" if cmd == "set_rate" and ok else ""
            rttx = f", {round(rtt * 1000)} ms" if rtt is not None else ""
            self.event("ack" if ok else "reject", f"{device} ✓ {cmd} {verdict}{extra}{rttx}")

    # ── poller writer (derived state) ─────────────────────────────────────────
    def apply_anomaly(self, scores, flags, lows, highs):
        with self.lock:
            for device, chans in scores.items():
                d = self.get(device)
                for channel, score in chans.items():
                    flag = bool(flags.get(device, {}).get(channel, 0.0))
                    prev = d["anomaly"].get(channel, {}).get("flag", False)
                    d["anomaly"][channel] = {"score": score, "flag": flag}
                    if flag and not prev:
                        self.event("anomaly", f"{device} anomaly on {channel} (score {score:.1f})")
                    elif prev and not flag:
                        self.event("clear", f"{device} {channel} back to normal")
            for device, chans in lows.items():
                d = self.get(device)
                for channel, lo in chans.items():
                    d["bands"].setdefault(channel, {})["lo"] = lo
            for device, chans in highs.items():
                d = self.get(device)
                for channel, hi in chans.items():
                    d["bands"].setdefault(channel, {})["hi"] = hi

    def apply_incidents(self, incidents):
        active = [i for i in incidents if i.get("status") == "open"]
        with self.lock:
            known = {i["id"]: i for i in self.incidents}
            for inc in active:
                if inc["id"] not in known:
                    who = inc.get("device") or "fleet"
                    ch = f"/{inc['channel']}" if inc.get("channel") else ""
                    self.event("incident", f"incident #{inc['id']} OPEN [{inc.get('severity','?')}] "
                                           f"{who}{ch}: {inc.get('summary') or inc.get('alertname','')}")
            now_ids = {i["id"] for i in active}
            for inc_id, inc in known.items():
                if inc_id not in now_ids:
                    self.event("resolve", f"incident #{inc_id} RESOLVED")
            self.incidents = active

    def apply_remediations(self, targets):
        with self.lock:
            prev = {(t["kind"], t["target"], t["reason"]): t for t in self.remediations}
            for t in targets:
                key = (t.get("kind"), t.get("target"), t.get("reason"))
                p = prev.get(key)
                if p is None:
                    self.event("remediate", f"remediator: acting on {t['target']} ({t['reason']})")
                elif t.get("attempts", 0) > p.get("attempts", 0):
                    self.event("remediate", f"remediator: retry {t['target']} "
                                           f"(#{t['attempts']})")
                elif t.get("exhausted") and not p.get("exhausted"):
                    self.event("escalate", f"remediator: ESCALATED {t['target']} — paging a human")
            self.remediations = targets

    # ── reader ────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        now_m, now_w = time.monotonic(), time.time()
        devices, ingest = [], 0.0
        online = 0
        anomalies_now = 0
        with self.lock:
            for device, d in sorted(self.devices.items()):
                ts = d["ts"]
                while ts and now_m - ts[0] > RATE_WINDOW:
                    ts.popleft()
                hz = round(len(ts) / RATE_WINDOW, 1)
                ingest += hz
                fresh = round(now_w - d["last_ts"], 1) if d["last_ts"] else None
                # Derive a headline status: explicit offline wins; else stale telemetry = degraded.
                status = d["status"]
                if status not in ("offline", "lwt") and fresh is not None and fresh > STALE_AFTER:
                    status = "degraded"
                if status == "online" and fresh is not None and fresh <= STALE_AFTER:
                    online += 1
                flagged = [c for c, a in d["anomaly"].items() if a.get("flag")]
                anomalies_now += len(flagged)
                ack = d["last_ack"]
                devices.append({
                    "device": device,
                    "status": status,
                    "hz": hz,
                    "fresh_s": fresh,
                    "telem": d["telem"],
                    "anomaly": d["anomaly"],
                    "bands": d["bands"],
                    "pending": d["pending"]["cmd"] if d["pending"] else None,
                    "last_ack": ({**{k: ack[k] for k in ("cmd", "ok", "hz", "rtt_ms")},
                                  "age_s": round(now_m - ack["at"], 1)} if ack else None),
                })
            events = [dict(e) for e in self.events]
            incidents = [dict(i) for i in self.incidents]
            remediations = [dict(r) for r in self.remediations]
        return {
            "t": now_w,
            "summary": {
                "online": online, "total": len(devices), "ingest_hz": round(ingest, 1),
                "incidents": len(incidents), "anomalies": anomalies_now,
            },
            "devices": devices,
            "events": events,
            "incidents": incidents,
            "remediations": remediations,
            "presets": RATE_PRESETS,
        }


# ── Prometheus / service poller ─────────────────────────────────────────────
def _http_json(url, timeout=2.5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _prom_instant(expr):
    """One instant query -> {device: {channel: value}} for the fleet_anomaly_*/baseline series."""
    out: dict[str, dict[str, float]] = {}
    try:
        data = _http_json(f"{PROM_URL}/api/v1/query?query={quote(expr)}")
    except Exception:
        return out
    if data.get("status") != "success":
        return out
    for series in data["data"]["result"]:
        m = series["metric"]
        device, channel = m.get("device"), m.get("channel")
        if not device or not channel:
            continue
        try:
            out.setdefault(device, {})[channel] = float(series["value"][1])
        except (ValueError, IndexError):
            continue
    return out


def poller(fleet: Fleet):
    """Refresh derived state on a slow cadence (Prometheus scrapes at 5s anyway; polling faster buys
    nothing). Every source is independent and best-effort so one being down never stalls the others."""
    while True:
        scores = _prom_instant('fleet_anomaly_score{detector="zscore"}')
        flags = _prom_instant('fleet_anomaly_flag{detector="zscore"}')
        lows = _prom_instant("fleet_channel_baseline_lower")
        highs = _prom_instant("fleet_channel_baseline_upper")
        fleet.apply_anomaly(scores, flags, lows, highs)

        try:
            fleet.apply_incidents(_http_json(f"{INCIDENTS_URL}/incidents"))
        except Exception:
            pass
        try:
            fleet.apply_remediations(_http_json(f"{REMEDIATOR_URL}/state"))
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ── MQTT ────────────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log(f"connect failed: {reason_code}")
        return
    for topic in ("fleet/+/telemetry", "fleet/+/status", "fleet/+/ack"):
        client.subscribe(topic, qos=1)
    log(f"connected to {BROKER_HOST}:{BROKER_PORT}; watching telemetry/status/ack")


def on_message(client, userdata, msg):
    fleet: Fleet = userdata
    parts = msg.topic.split("/")
    if len(parts) < 3:
        return
    device, kind = parts[1], parts[2]
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if kind == "telemetry":
        fleet.note_telemetry(device, payload)
    elif kind == "status":
        fleet.note_status(device, payload.get("state", "unknown"))
    elif kind == "ack":
        fleet.note_ack(device, payload)


def publish_command(client, fleet: Fleet, device: str, cmd: str, hz):
    """Publish one downlink command. QoS 1 so the broker won't silently drop it; the firmware's commands
    are idempotent, so a QoS-1 duplicate is harmless."""
    body = {"cmd": cmd, "source": "control-surface"}
    if cmd == "set_rate":
        body["hz"] = hz
    client.publish(f"fleet/{device}/cmd", json.dumps(body), qos=1)
    fleet.note_command(device, cmd, hz)
    CMDS_SENT.labels(device=device, cmd=cmd).inc()
    log(f"{device}: sent {cmd}" + (f" hz={hz}" if cmd == "set_rate" else ""))


# ── HTTP ─────────────────────────────────────────────────────────────────────
def make_handler(client, fleet: Fleet):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode() if isinstance(body, str) else body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._reply(200, COCKPIT_PAGE, "text/html; charset=utf-8")
            elif path == "/stream":
                self._stream()
            elif path == "/state":
                self._reply(200, json.dumps(fleet.snapshot()), "application/json")
            elif path == "/metrics":
                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif path in ("/healthz", "/readyz"):
                # Both are process-level on purpose: the cockpit is built to degrade when a source is
                # down, so a broker or Prometheus outage must not take the operator's page offline.
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def _stream(self):
            # SSE: hold the connection open and push a snapshot per frame. HTTP/1.0 (the default) closes
            # on the client's disconnect, which surfaces as a write error below and ends the loop.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    frame = f"data: {json.dumps(fleet.snapshot())}\n\n"
                    self.wfile.write(frame.encode())
                    self.wfile.flush()
                    time.sleep(SSE_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, ValueError):
                return  # client navigated away / reloaded

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/cmd":
                self._reply(404, "not found")
                return
            q = parse_qs(parsed.query)
            device = q.get("device", [""])[0]
            cmd = q.get("cmd", [""])[0]
            if not device or cmd not in ("set_rate", "reboot"):
                self._reply(400, "need device and cmd=set_rate|reboot")
                return
            hz = None
            if cmd == "set_rate":
                try:
                    hz = int(q.get("hz", ["0"])[0])
                except ValueError:
                    hz = 0
                if not 1 <= hz <= 50:   # mirror the firmware's accepted range, reject early
                    self._reply(400, "hz must be 1..50")
                    return
            publish_command(client, fleet, device, cmd, hz)
            self._reply(200, f"{device}: {cmd}" + (f" {hz} Hz" if hz else ""))

        def log_message(self, *args):
            pass

    return Handler


def main():
    fleet = Fleet()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="fleet-control", userdata=fleet)
    client.on_connect = on_connect
    client.on_message = on_message
    # Async so the cockpit still serves (degraded) when the broker isn't up yet, instead of crash-looping.
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    threading.Thread(target=poller, args=(fleet,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), make_handler(client, fleet))
    log(f"Mission Control on :{HTTP_PORT} (/, /stream, /cmd, /state, /metrics); default {DEFAULT_DEVICE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


# ── cockpit page (self-contained: inline CSS + vanilla JS + canvas charts, no CDN) ──────────────
COCKPIT_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleetwright · Mission Control</title>
<style>
 :root{--bg:#0a0c11;--panel:#12151d;--panel2:#171b24;--line:#232936;--txt:#dbe3ef;--dim:#8592a6;
  --grn:#3fb950;--amb:#d29922;--red:#f85149;--blu:#58a6ff;--cyn:#39d0d8;--vio:#a371f7}
 *{box-sizing:border-box} html,body{margin:0;height:100%}
 body{background:radial-gradient(1200px 600px at 70% -10%,#141a26 0,var(--bg) 60%);color:var(--txt);
  font:14px/1.4 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
 .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 header{display:flex;align-items:center;gap:1rem;padding:.8rem 1.2rem;border-bottom:1px solid var(--line);
  background:rgba(10,12,17,.7);backdrop-filter:blur(6px);position:sticky;top:0;z-index:5}
 .brand{font-weight:800;letter-spacing:.14em;font-size:.9rem}
 .brand b{color:var(--grn)} .dot{width:8px;height:8px;border-radius:50%;background:var(--red);
  box-shadow:0 0 8px currentColor;display:inline-block} .dot.on{background:var(--grn)}
 .clock{margin-left:auto;color:var(--dim)}
 .chips{display:flex;gap:.6rem;flex-wrap:wrap}
 .chip{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.3rem .7rem;
  display:flex;flex-direction:column;min-width:76px} .chip .k{font-size:.62rem;color:var(--dim);
  text-transform:uppercase;letter-spacing:.08em} .chip .v{font-weight:700;font-size:1.1rem}
 .chip.warn .v{color:var(--amb)} .chip.bad .v{color:var(--red)} .chip.good .v{color:var(--grn)}
 main{display:grid;grid-template-columns:1fr 380px;gap:1rem;padding:1rem;align-items:start}
 @media(max-width:1100px){main{grid-template-columns:1fr}}
 .devices{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:1rem}
 .card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:14px;padding:1rem;border-top:3px solid var(--grn)}
 .card.degraded{border-top-color:var(--amb)} .card.offline,.card.lwt{border-top-color:var(--red)}
 .card.unknown{border-top-color:#3a4150}
 .card.degraded,.card.offline,.card.lwt{animation:pulse 2s infinite}
 @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(210,153,34,.0)}50%{box-shadow:0 0 24px 0 rgba(210,153,34,.10)}}
 .card.offline,.card.lwt{animation-name:pulseR}
 @keyframes pulseR{0%,100%{box-shadow:0 0 0 0 rgba(248,81,73,0)}50%{box-shadow:0 0 26px 0 rgba(248,81,73,.14)}}
 .chead{display:flex;align-items:center;gap:.6rem} .cid{font-weight:700;letter-spacing:.02em}
 .badge{font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;padding:.15rem .5rem;border-radius:999px;
  border:1px solid currentColor} .b-online{color:var(--grn)} .b-degraded{color:var(--amb)}
 .b-offline,.b-lwt{color:var(--red)} .b-unknown{color:var(--dim)}
 .meta{margin-left:auto;text-align:right;color:var(--dim);font-size:.72rem;line-height:1.25}
 .meta b{color:var(--txt);font-size:.95rem}
 .charts{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin:.7rem 0}
 .ch{background:#0c0f16;border:1px solid var(--line);border-radius:10px;padding:.4rem .5rem}
 .ch .lab{display:flex;justify-content:space-between;font-size:.66rem;color:var(--dim)}
 .ch .lab b{color:var(--txt);font-size:.8rem} .ch canvas{width:100%;height:64px;display:block}
 .pills{display:flex;gap:.35rem;flex-wrap:wrap;margin:.2rem 0 .7rem}
 .pill{font-size:.66rem;padding:.2rem .5rem;border-radius:6px;background:#0c0f16;border:1px solid var(--line);
  color:var(--dim)} .pill.hot{color:#fff;background:rgba(248,81,73,.18);border-color:var(--red);
  box-shadow:0 0 10px rgba(248,81,73,.3);animation:pulseR 1.4s infinite}
 .pill.warm{color:var(--amb);border-color:rgba(210,153,34,.5)}
 .ctrls{display:flex;gap:.3rem;flex-wrap:wrap;align-items:center}
 button{cursor:pointer;border:1px solid var(--line);border-radius:7px;padding:.35rem .6rem;font-size:.78rem;
  background:#1c222d;color:var(--txt);font-family:inherit} button:hover{background:#28303d;border-color:#3a4553}
 button.reboot{margin-left:auto;color:#ffb4ae;border-color:#5a2a2f;background:#2a1416}
 button.reboot:hover{background:#3a1a1d}
 .ack{margin-top:.6rem;min-height:1.15em;font-size:.74rem} .ack .ok{color:var(--grn)} .ack .no{color:var(--red)}
 .ack .pend{color:var(--amb)}
 .rail{display:flex;flex-direction:column;gap:1rem;position:sticky;top:64px}
 .box{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
 .box h3{margin:0;padding:.6rem .9rem;font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--dim);border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
 .feed{max-height:52vh;overflow:auto;padding:.3rem 0}
 .ev{display:flex;gap:.5rem;padding:.22rem .9rem;font-size:.74rem;align-items:baseline}
 .ev .ts{color:var(--dim);flex:0 0 58px} .ev .msg{color:var(--txt)}
 .ev.anomaly .msg,.ev.reject .msg{color:var(--amb)} .ev.incident .msg,.ev.escalate .msg{color:var(--red)}
 .ev.remediate .msg{color:var(--blu)} .ev.resolve .msg,.ev.clear .msg,.ev.ack .msg{color:var(--grn)}
 .ev.cmd .msg{color:var(--cyn)} .ev.status .msg{color:var(--vio)}
 .il{padding:.4rem .9rem;font-size:.76rem;border-bottom:1px solid var(--line)} .il:last-child{border:0}
 .il .sev{font-size:.6rem;text-transform:uppercase;padding:.05rem .4rem;border-radius:4px;margin-right:.4rem}
 .sev.critical{background:rgba(248,81,73,.2);color:var(--red)} .sev.warning{background:rgba(210,153,34,.2);color:var(--amb)}
 .empty{padding:.7rem .9rem;color:var(--dim);font-size:.74rem}
</style></head><body>
<header>
 <span class="dot" id="live"></span>
 <span class="brand"><b>FLEETWRIGHT</b> · MISSION CONTROL</span>
 <div class="chips" id="chips"></div>
 <span class="clock mono" id="clock"></span>
</header>
<main>
 <section class="devices" id="devices"></section>
 <aside class="rail">
  <div class="box"><h3>Event feed <span id="evn" class="mono"></span></h3><div class="feed" id="feed"></div></div>
  <div class="box"><h3>Active incidents</h3><div id="incs"></div></div>
  <div class="box"><h3>Self-healing</h3><div id="rems"></div></div>
 </aside>
</main>
<script>
const HIST=200, cards={}, hist={};
const q=s=>document.querySelector(s), el=(t,c)=>{const e=document.createElement(t);if(c)e.className=c;return e;};
const clk=()=>q('#clock').textContent=new Date().toISOString().substr(11,8)+' UTC';
setInterval(clk,1000);clk();

function fmt(v,d=1){return v==null?'—':(+v).toFixed(d);}
function tsStr(t){return new Date(t*1000).toISOString().substr(11,8);}

function drawChart(cv,data,band,unit){
 const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
 if(cv.width!==w*dpr){cv.width=w*dpr;cv.height=h*dpr;}
 const c=cv.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,w,h);
 if(!data.length){return;}
 let lo=Math.min(...data),hi=Math.max(...data);
 if(band){if(band.lo!=null)lo=Math.min(lo,band.lo);if(band.hi!=null)hi=Math.max(hi,band.hi);}
 const pad=(hi-lo)*0.15||1;lo-=pad;hi+=pad;const rng=hi-lo||1;
 const x=i=>i/(HIST-1)*w, y=v=>h-(v-lo)/rng*h;
 c.strokeStyle='#1c2230';c.lineWidth=1;for(let g=1;g<3;g++){const yy=h*g/3;c.beginPath();c.moveTo(0,yy);c.lineTo(w,yy);c.stroke();}
 if(band&&band.lo!=null&&band.hi!=null){c.fillStyle='rgba(63,185,80,.10)';c.fillRect(0,y(band.hi),w,y(band.lo)-y(band.hi));
  c.strokeStyle='rgba(63,185,80,.4)';c.setLineDash([4,4]);c.beginPath();c.moveTo(0,y(band.hi));c.lineTo(w,y(band.hi));
  c.moveTo(0,y(band.lo));c.lineTo(w,y(band.lo));c.stroke();c.setLineDash([]);}
 const last=data[data.length-1], out=band&&band.hi!=null&&last>band.hi;
 const off=HIST-data.length;c.beginPath();data.forEach((v,i)=>{const xx=x(i+off),yy=y(v);i?c.lineTo(xx,yy):c.moveTo(xx,yy);});
 c.strokeStyle=out?'#f85149':'#3fb950';c.lineWidth=1.8;c.stroke();
 c.lineTo(x(HIST-1),h);c.lineTo(x(off),h);c.closePath();
 c.fillStyle=out?'rgba(248,81,73,.12)':'rgba(63,185,80,.10)';c.fill();
 c.fillStyle=out?'#f85149':'#dbe3ef';c.font='11px ui-monospace,monospace';c.textAlign='right';
 c.fillText(fmt(last,unit==='g'?2:1)+' '+unit,w-4,12);
}

function ensureCard(d){
 let card=cards[d.device];if(card)return card;
 const c=el('div','card');c.dataset.id=d.device;
 c.innerHTML=`<div class="chead"><span class="cid mono"></span><span class="badge"></span>
   <div class="meta"><b class="hz"></b> Hz<br><span class="fresh"></span></div></div>
  <div class="charts">
   <div class="ch"><div class="lab"><span>ACCEL</span><b class="la"></b></div><canvas class="ca"></canvas></div>
   <div class="ch"><div class="lab"><span>GYRO</span><b class="lg"></b></div><canvas class="cg"></canvas></div>
  </div>
  <div class="pills"></div><div class="ctrls"></div><div class="ack mono"></div>`;
 const ct=c.querySelector('.ctrls');
 for(const hz of (window._presets||[1,10,25,50])){const b=el('button');b.textContent=hz+' Hz';
  b.onclick=()=>cmd(d.device,'set_rate',hz);ct.appendChild(b);}
 const rb=el('button','reboot');rb.textContent='⏻ reboot';rb.onclick=()=>cmd(d.device,'reboot',0);ct.appendChild(rb);
 q('#devices').appendChild(c);cards[d.device]=c;hist[d.device]={accel:[],gyro:[]};return c;
}

function push(arr,v){if(v==null)v=arr.length?arr[arr.length-1]:0;arr.push(v);if(arr.length>HIST)arr.shift();}

function renderCard(d){
 const c=ensureCard(d),H=hist[d.device];
 push(H.accel,d.telem.accel);push(H.gyro,d.telem.gyro);
 c.className='card '+d.status;
 c.querySelector('.cid').textContent=d.device;
 const bd=c.querySelector('.badge');bd.textContent=d.status;bd.className='badge b-'+d.status;
 c.querySelector('.hz').textContent=fmt(d.hz);
 c.querySelector('.fresh').textContent=d.fresh_s==null?'no data':('seen '+fmt(d.fresh_s)+'s ago');
 c.querySelector('.la').textContent=fmt(d.telem.accel,2)+' g';
 c.querySelector('.lg').textContent=fmt(d.telem.gyro,1)+' dps';
 drawChart(c.querySelector('.ca'),H.accel,d.bands.accel_magnitude,'g');
 drawChart(c.querySelector('.cg'),H.gyro,d.bands.gyro_magnitude,'dps');
 const pl=c.querySelector('.pills');pl.innerHTML='';
 for(const [ch,label] of window._channels){const a=d.anomaly[ch]||{};const p=el('span','pill');
  p.className='pill'+(a.flag?' hot':(a.score>0.7?' warm':''));
  p.textContent=label+(a.score!=null?' '+a.score.toFixed(1):'');pl.appendChild(p);}
 const ak=c.querySelector('.ack');
 if(d.pending)ak.innerHTML=`<span class="pend">▸ ${d.pending} sent — awaiting ack…</span>`;
 else if(d.last_ack){const k=d.last_ack,r=k.rtt_ms!=null?`, ${k.rtt_ms} ms rtt`:'';
  ak.innerHTML=`<span class="${k.ok?'ok':'no'}">✓ ${k.cmd} ${k.ok?'ok':'rejected'}`+
   `${k.hz!=null&&k.cmd==='set_rate'?' ('+k.hz+' Hz)':''}${r} · ${k.age_s}s ago</span>`;}
 else ak.innerHTML='';
}

let lastEv=0;
function render(s){
 window._presets=s.presets;window._channels=[["temperature","temp"],["humidity","humid"],
  ["pressure","press"],["accel_magnitude","accel"],["gyro_magnitude","gyro"]];
 const sm=s.summary;
 q('#chips').innerHTML=
  chip('Devices',sm.online+'/'+sm.total,sm.online<sm.total?'warn':'good')+
  chip('Ingest',sm.ingest_hz+' Hz','')+
  chip('Incidents',sm.incidents,sm.incidents?'bad':'good')+
  chip('Anomalies',sm.anomalies,sm.anomalies?'warn':'good');
 const ids=new Set(s.devices.map(d=>d.device));
 for(const id in cards)if(!ids.has(id)){cards[id].remove();delete cards[id];delete hist[id];}
 s.devices.forEach(renderCard);

 const feed=q('#feed');
 if(s.events.length&&s.events[s.events.length-1].id!==lastEv){
  lastEv=s.events[s.events.length-1].id;
  feed.innerHTML=s.events.slice(-80).reverse().map(e=>
   `<div class="ev ${e.kind}"><span class="ts mono">${tsStr(e.t)}</span><span class="msg">${esc(e.msg)}</span></div>`).join('');
  q('#evn').textContent=s.events.length;
 }
 const inc=q('#incs');inc.innerHTML=s.incidents.length?s.incidents.map(i=>{
  const who=(i.device||'fleet')+(i.channel?'/'+i.channel:'');
  return `<div class="il"><span class="sev ${i.severity}">${i.severity||'?'}</span>`+
   `<b>#${i.id}</b> ${esc(who)} — ${esc(i.summary||i.alertname||'')}</div>`;}).join(''):'<div class="empty">none — fleet healthy</div>';
 const rem=q('#rems');rem.innerHTML=s.remediations.length?s.remediations.map(r=>
  `<div class="il">${esc(r.target)} · ${esc(r.reason)} · ${r.action||'act'} #${r.attempts}${r.exhausted?' <b style="color:var(--red)">ESCALATED</b>':''}</div>`).join(''):'<div class="empty">idle — nothing to heal</div>';
}
function chip(k,v,cls){return `<div class="chip ${cls}"><span class="k">${k}</span><span class="v">${v}</span></div>`;}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function cmd(dev,c,hz){await fetch(`/cmd?device=${dev}&cmd=${c}&hz=${hz}`,{method:'POST'});}

function connect(){
 const es=new EventSource('/stream');
 es.onopen=()=>q('#live').classList.add('on');
 es.onerror=()=>q('#live').classList.remove('on');
 es.onmessage=e=>{try{render(JSON.parse(e.data));}catch(x){}};
}
connect();
</script></body></html>"""


if __name__ == "__main__":
    main()
