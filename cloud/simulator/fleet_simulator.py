#!/usr/bin/env python3
"""Simulated device fleet: N independent telemetry streams you inject faults into on demand.

Each device has its own baseline (so streams look distinct, not cloned) and publishes at a fixed rate
under its own id. The fleet is healthy and silent by default — faults are triggered manually, either
from the served control page (http://localhost:9097) or with a curl, so nothing pages you at random.

    curl -X POST "localhost:9097/fault?device=sim-01&type=anomaly&channel=temperature&duration=90"
    curl -X POST "localhost:9097/fault?device=sim-02&type=offline"
    curl -X POST "localhost:9097/clear?device=sim-01"      # or /clear for the whole fleet
"""

import argparse
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from random import Random
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt

# Offset added to a channel while it's in an anomaly — large enough to clear the fitted normal band.
ANOMALY_OFFSETS = {"temperature": 14.0, "humidity": 40.0, "pressure": 6.0, "accel": 1.0}

STATE_LOCK = threading.Lock()


def env(name, default):
    return os.environ.get(name, default)


class Device:
    """One simulated device: a distinct baseline plus a manually-controlled fault mode."""

    def __init__(self, device_id, rng):
        self.id = device_id
        self.rng = rng
        self.temp0 = rng.uniform(19.0, 25.0)
        self.humid0 = rng.uniform(38.0, 52.0)
        self.press0 = rng.uniform(1008.0, 1018.0)
        self.phase = rng.uniform(0.0, math.tau)
        self.seq = 0
        self.mode = "normal"          # normal | anomaly | offline
        self.channel = None           # which channel, when mode == anomaly
        self.until = 0.0              # auto-clear time; 0 = sticky until cleared

    def set_fault(self, mode, channel, duration):
        self.mode = mode
        self.channel = channel if mode == "anomaly" else None
        self.until = (time.time() + duration) if duration > 0 else 0.0

    def clear(self):
        self.mode, self.channel, self.until = "normal", None, 0.0

    def maybe_autoclear(self, now):
        if self.mode != "normal" and self.until and now >= self.until:
            print(f"[sim] {self.id}: {self.mode} -> normal (duration elapsed)", flush=True)
            self.clear()

    def sample(self, t):
        off = ANOMALY_OFFSETS[self.channel] if self.mode == "anomaly" else 0.0
        temp = self.temp0 + 2 * math.sin(t / 5 + self.phase) + self.rng.uniform(-0.1, 0.1)
        humidity = self.humid0 + 5 * math.sin(t / 7 + self.phase) + self.rng.uniform(-0.2, 0.2)
        pressure = self.press0 + self.rng.uniform(-0.5, 0.5)
        az = 1.0 + self.rng.uniform(-0.02, 0.02)
        temp += off if self.channel == "temperature" else 0.0
        humidity += off if self.channel == "humidity" else 0.0
        pressure += off if self.channel == "pressure" else 0.0
        az += off if self.channel == "accel" else 0.0
        return {
            "id": self.id, "seq": self.seq, "ts": time.time(),
            "temp": round(temp, 2), "humidity": round(humidity, 2), "pressure": round(pressure, 2),
            "ax": round(self.rng.uniform(-0.02, 0.02), 3),
            "ay": round(self.rng.uniform(-0.02, 0.02), 3),
            "az": round(az, 3),
        }


CONTROL_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Fleet fault control</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;background:#0f1116;color:#e6e6e6}
 h1{font-size:1.3rem} button{cursor:pointer;border:0;border-radius:6px;padding:.35rem .6rem;
  margin:.15rem;font-size:.8rem;background:#2a2f3a;color:#e6e6e6} button:hover{background:#3a4150}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:1rem;margin-top:1rem}
 .card{border-radius:10px;padding:1rem;background:#171a21;border-left:5px solid #3fb950}
 .card.anomaly{border-color:#d29922} .card.offline{border-color:#f85149}
 .mode{font-size:.85rem;opacity:.8;margin:.2rem 0 .6rem} .clear{background:#40474f}
 .top{background:#40474f;padding:.5rem .9rem}
</style></head><body>
<h1>Fleet fault control</h1>
<button class="top" onclick="clearAll()">Clear all faults</button>
<div id="grid" class="grid"></div>
<script>
const CH=['temperature','humidity','pressure','accel'];
async function load(){
  const s=await (await fetch('/state')).json();
  const g=document.getElementById('grid'); g.innerHTML='';
  for(const d of s.devices){
    const c=document.createElement('div'); c.className='card '+d.mode;
    let h=`<b>${d.id}</b><div class=mode>${d.mode}${d.channel?(' / '+d.channel):''}</div>`;
    for(const ch of CH) h+=`<button onclick="fault('${d.id}','anomaly','${ch}')">${ch}</button>`;
    h+=`<button onclick="fault('${d.id}','offline','')">offline</button>`;
    h+=`<button class=clear onclick="clr('${d.id}')">clear</button>`;
    c.innerHTML=h; g.appendChild(c);
  }
}
async function fault(dev,type,ch){await fetch(`/fault?device=${dev}&type=${type}&channel=${ch}`,{method:'POST'});load();}
async function clr(dev){await fetch(`/clear?device=${dev}`,{method:'POST'});load();}
async function clearAll(){await fetch('/clear',{method:'POST'});load();}
load(); setInterval(load,2000);
</script></body></html>"""


def make_handler(devices):
    """Build the control-server request handler bound to the device table."""

    def apply_fault(params):
        dev = devices.get((params.get("device", [""])[0]))
        if not dev:
            return 404, "unknown device"
        ftype = params.get("type", ["anomaly"])[0]
        channel = params.get("channel", [""])[0] or "temperature"
        if ftype == "anomaly" and channel not in ANOMALY_OFFSETS:
            return 400, f"channel must be one of {list(ANOMALY_OFFSETS)}"
        if ftype not in ("anomaly", "offline"):
            return 400, "type must be anomaly|offline"
        try:
            duration = float(params.get("duration", ["0"])[0])
        except ValueError:
            return 400, "duration must be a number (seconds; 0 = until cleared)"
        with STATE_LOCK:
            dev.set_fault(ftype, channel, duration)
        note = f"{ftype}{'/' + channel if ftype == 'anomaly' else ''}"
        note += f" for {int(duration)}s" if duration > 0 else " (until cleared)"
        print(f"[sim] {dev.id}: fault set -> {note}", flush=True)
        return 200, f"{dev.id}: {note}"

    def apply_clear(params):
        target = params.get("device", [""])[0]
        with STATE_LOCK:
            targets = [devices[target]] if target in devices else (list(devices.values()) if not target else [])
            if target and not targets:
                return 404, "unknown device"
            for dev in targets:
                dev.clear()
        print(f"[sim] cleared {target or 'all devices'}", flush=True)
        return 200, f"cleared {target or 'all'}"

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode() if isinstance(body, str) else body)

        def _route(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path == "/fault":
                self._reply(*apply_fault(params))
            elif parsed.path == "/clear":
                self._reply(*apply_clear(params))
            elif parsed.path == "/state":
                with STATE_LOCK:
                    body = json.dumps({"devices": [
                        {"id": d.id, "mode": d.mode, "channel": d.channel, "seq": d.seq}
                        for d in devices.values()
                    ]})
                self._reply(200, body, "application/json")
            elif parsed.path == "/":
                self._reply(200, CONTROL_PAGE, "text/html; charset=utf-8")
            else:
                self._reply(404, "not found")

        do_GET = _route
        do_POST = _route

        def log_message(self, *args):
            pass  # our fault lines are the only output we want

    return Handler


# Downlink command topic the self-healing controller (and, later, the OTA control plane) publishes to.
CMD_TOPIC = "fleet/+/cmd"


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[sim] connect failed: {reason_code}", flush=True)
        return
    client.subscribe(CMD_TOPIC, qos=1)
    print(f"[sim] subscribed to {CMD_TOPIC} (command downlink)", flush=True)


def on_command(client, userdata, msg):
    """Apply a downlink command to one device. A reboot clears the injected fault and restarts the
    sequence — the simulated equivalent of power-cycling a wedged unit back to a known-good state."""
    devices = userdata
    parts = msg.topic.split("/")
    dev = devices.get(parts[1]) if len(parts) >= 3 else None
    if not dev:
        return
    try:
        cmd = json.loads(msg.payload.decode("utf-8")).get("cmd")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return
    if cmd in ("reboot", "reset", "clear"):
        with STATE_LOCK:
            was = dev.mode
            dev.clear()
            dev.seq = 0   # a reboot restarts the sequence; the bridge reads a seq reset as a reboot
        print(f"[sim] {dev.id}: '{cmd}' downlink -> {was} cleared, rebooted", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=env("FLEET_BROKER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("FLEET_BROKER_PORT", "1883")))
    ap.add_argument("--devices", type=int, default=int(env("FLEET_SIM_DEVICES", "5")))
    ap.add_argument("--rate", type=float, default=float(env("FLEET_SIM_RATE", "10")),
                    help="messages/sec per device")
    ap.add_argument("--drop", type=float, default=float(env("FLEET_SIM_DROP", "0.005")),
                    help="fraction of messages dropped (baseline packet loss)")
    ap.add_argument("--control-port", type=int, default=int(env("FLEET_SIM_CONTROL_PORT", "9097")))
    ap.add_argument("--seed", type=int, default=int(env("FLEET_SIM_SEED", "0")) or None)
    args = ap.parse_args()

    rng = Random(args.seed)
    devices = {f"sim-{i:02d}": Device(f"sim-{i:02d}", Random(rng.random()))
               for i in range(1, args.devices + 1)}

    server = ThreadingHTTPServer(("0.0.0.0", args.control_port), make_handler(devices))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="fleet-sim")
    client.user_data_set(devices)
    client.on_connect = on_connect
    client.on_message = on_command
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    period = 1.0 / args.rate
    print(f"[sim] {len(devices)} devices at {args.rate} Hz -> {args.host}:{args.port}; "
          f"control on :{args.control_port} (page + /fault + /clear). Ctrl-C to stop", flush=True)

    t = 0.0
    try:
        while True:
            now = time.time()
            with STATE_LOCK:
                snapshot = list(devices.values())
            for dev in snapshot:
                dev.maybe_autoclear(now)
                dev.seq += 1
                if dev.mode == "offline":
                    continue
                if args.drop and dev.rng.random() < args.drop:
                    continue
                client.publish(f"fleet/{dev.id}/telemetry", json.dumps(dev.sample(t)), qos=0)
            t += period
            time.sleep(period)
    except KeyboardInterrupt:
        print("\n[sim] stopping", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()
        server.shutdown()


if __name__ == "__main__":
    main()
