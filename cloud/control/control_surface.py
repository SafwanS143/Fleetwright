#!/usr/bin/env python3
"""Operator control surface: the human-facing end of the cloud->device loop.

The remediator closes the loop automatically; this is the same downlink, driven by a person. A served
web panel lists each device, publishes an OTA command on `fleet/<id>/cmd` on one click, and shows the
round trip land: the device's ack on `fleet/<id>/ack` plus the live telemetry rate, so one action
produces a visible behaviour change (set_rate 10->50 Hz makes the rate meter jump) and its confirmation.

    open http://localhost:9099            # the panel
    POST /cmd?device=fleet-edge-01&cmd=set_rate&hz=25
    POST /cmd?device=fleet-edge-01&cmd=reboot

Ack round-trip time is measured here (publish -> ack) so it spans the whole path: broker, gateway,
UART, firmware, and back. Command/ack counters and that latency are exported on /metrics.
"""

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

BROKER_HOST = os.environ.get("FLEET_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("FLEET_BROKER_PORT", "1883"))
HTTP_PORT = int(os.environ.get("FLEET_CONTROL_PORT", "9099"))
# Always show the real device even before it has published, so the panel isn't empty on a fresh start.
DEFAULT_DEVICE = os.environ.get("FLEET_DEVICE_ID", "fleet-edge-01")
# Window the observed rate is measured over. A few seconds smooths the per-message jitter without
# lagging a real rate change long enough to hide it during a demo.
RATE_WINDOW = float(os.environ.get("FLEET_RATE_WINDOW", "4"))
# set_rate presets the panel offers; must sit inside the firmware's accepted 1..50 Hz range.
RATE_PRESETS = [1, 10, 25, 50]

CMDS_SENT = Counter("fleet_control_commands_total", "Commands published from the control surface.",
                    ["device", "cmd"])
ACKS = Counter("fleet_control_acks_total", "Acks received for control-surface commands.",
               ["device", "cmd", "ok"])
ACK_RTT = Histogram("fleet_control_ack_rtt_seconds", "Command publish -> ack round-trip time.",
                    ["cmd"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5))


def log(msg):
    print(f"[control] {msg}", flush=True)


class Fleet:
    """Live view of every device the panel knows about, built from what arrives on MQTT."""

    def __init__(self):
        self.lock = threading.Lock()
        self.devices: dict[str, dict] = {}
        self.get(DEFAULT_DEVICE)

    def get(self, device: str) -> dict:
        d = self.devices.get(device)
        if d is None:
            # `pending` holds the last command awaiting an ack (for RTT); `ts` is a sliding window of
            # telemetry arrival times, trimmed to RATE_WINDOW on read, to derive the observed Hz.
            d = {"status": "unknown", "ts": deque(), "pending": None, "last_ack": None}
            self.devices[device] = d
        return d

    def note_telemetry(self, device: str):
        with self.lock:
            self.get(device)["ts"].append(time.monotonic())

    def note_status(self, device: str, status: str):
        with self.lock:
            self.get(device)["status"] = status

    def note_command(self, device: str, cmd: str, hz):
        with self.lock:
            self.get(device)["pending"] = {"cmd": cmd, "hz": hz, "sent": time.monotonic()}

    def note_ack(self, device: str, ack: dict):
        with self.lock:
            d = self.get(device)
            cmd = ack.get("cmd", "?")
            ok = bool(ack.get("ok"))
            rtt = None
            pending = d["pending"]
            # Only match an ack we're waiting on: pair by command name so a device-initiated ack (or a
            # remediator reboot) doesn't get charged a bogus RTT against our unrelated pending command.
            if pending and pending["cmd"] == cmd:
                rtt = time.monotonic() - pending["sent"]
                d["pending"] = None
                ACK_RTT.labels(cmd=cmd).observe(rtt)
            d["last_ack"] = {"cmd": cmd, "ok": ok, "hz": ack.get("hz"),
                             "rtt_ms": round(rtt * 1000) if rtt is not None else None,
                             "at": time.monotonic()}
            ACKS.labels(device=device, cmd=cmd, ok=str(ok).lower()).inc()

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        out = []
        with self.lock:
            for device, d in sorted(self.devices.items()):
                ts = d["ts"]
                while ts and now - ts[0] > RATE_WINDOW:
                    ts.popleft()
                hz = round(len(ts) / RATE_WINDOW, 1)
                ack = d["last_ack"]
                out.append({
                    "device": device,
                    "status": d["status"],
                    "hz": hz,
                    "pending": d["pending"]["cmd"] if d["pending"] else None,
                    "last_ack": ({**{k: ack[k] for k in ("cmd", "ok", "hz", "rtt_ms")},
                                  "age_s": round(now - ack["at"], 1)} if ack else None),
                })
        return out


# ── MQTT ──────────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log(f"connect failed: {reason_code}")
        return
    # One subscription per signal we render: rate (telemetry), liveness (status), confirmation (ack).
    for topic in ("fleet/+/telemetry", "fleet/+/status", "fleet/+/ack"):
        client.subscribe(topic, qos=1)
    log(f"connected to {BROKER_HOST}:{BROKER_PORT}; watching telemetry/status/ack")


def on_message(client, userdata, msg):
    fleet: Fleet = userdata
    parts = msg.topic.split("/")
    if len(parts) < 3:
        return
    device, kind = parts[1], parts[2]
    if kind == "telemetry":
        fleet.note_telemetry(device)
        return
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if kind == "status":
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


CONTROL_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Fleet control surface</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;background:#0f1116;color:#e6e6e6}
 h1{font-size:1.3rem} .sub{opacity:.6;font-size:.85rem;margin-top:-.5rem}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem;margin-top:1rem}
 .card{border-radius:10px;padding:1rem;background:#171a21;border-left:5px solid #3fb950}
 .card.offline,.card.lwt{border-color:#f85149} .card.unknown{border-color:#6e7681}
 .hz{font-size:2rem;font-weight:700;margin:.2rem 0} .hz span{font-size:.9rem;font-weight:400;opacity:.6}
 .status{font-size:.8rem;opacity:.8;text-transform:uppercase;letter-spacing:.04em}
 button{cursor:pointer;border:0;border-radius:6px;padding:.4rem .7rem;margin:.15rem;font-size:.85rem;
  background:#2a2f3a;color:#e6e6e6} button:hover{background:#3a4150} .reboot{background:#5a2a2f}
 .reboot:hover{background:#7a353c} .ack{font-size:.8rem;margin-top:.7rem;min-height:1.1em;opacity:.9}
 .ack.ok{color:#3fb950} .ack.no{color:#f85149} .pending{color:#d29922}
</style></head><body>
<h1>Fleet control surface</h1>
<div class=sub>Push an OTA command; watch the rate change and the device ack land.</div>
<div id=grid class=grid></div>
<script>
const PRESETS=__PRESETS__;
async function load(){
  const s=await (await fetch('/state')).json();
  const g=document.getElementById('grid'); g.innerHTML='';
  for(const d of s){
    const c=document.createElement('div'); c.className='card '+d.status;
    let h=`<div class=status>${d.device} &middot; ${d.status}</div>`;
    h+=`<div class=hz>${d.hz.toFixed(1)}<span> Hz observed</span></div>`;
    for(const hz of PRESETS) h+=`<button onclick="cmd('${d.device}','set_rate',${hz})">${hz} Hz</button>`;
    h+=`<button class=reboot onclick="cmd('${d.device}','reboot',0)">reboot</button>`;
    let a='';
    if(d.pending) a=`<span class=pending>${d.pending} sent, awaiting ack&hellip;</span>`;
    else if(d.last_ack){const k=d.last_ack; const rtt=k.rtt_ms!=null?`, ${k.rtt_ms} ms rtt`:'';
      a=`<span class="${k.ok?'ok':'no'}">${k.cmd} ${k.ok?'ok':'rejected'}`+
        `${k.hz!=null&&k.cmd==='set_rate'?' ('+k.hz+' Hz)':''}${rtt} &middot; ${k.age_s}s ago</span>`;}
    h+=`<div class=ack>${a}</div>`;
    c.innerHTML=h; g.appendChild(c);
  }
}
async function cmd(dev,c,hz){await fetch(`/cmd?device=${dev}&cmd=${c}&hz=${hz}`,{method:'POST'});setTimeout(load,150);}
load(); setInterval(load,1000);
</script></body></html>""".replace("__PRESETS__", json.dumps(RATE_PRESETS))


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
                self._reply(200, CONTROL_PAGE, "text/html; charset=utf-8")
            elif path == "/state":
                self._reply(200, json.dumps(fleet.snapshot()), "application/json")
            elif path == "/metrics":
                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

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
            pass  # the command lines are the only output we want

    return Handler


def main():
    fleet = Fleet()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="fleet-control", userdata=fleet)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), make_handler(client, fleet))
    log(f"panel on :{HTTP_PORT} (/, /state, /cmd, /metrics); default device {DEFAULT_DEVICE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
