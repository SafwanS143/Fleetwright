#!/usr/bin/env python3
"""MQTT→Prometheus exporter: subscribes to telemetry, exposes /metrics for Prometheus to scrape."""

import json
import math
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ── config (env-driven) ──────────────────────────────────────────────────────
BROKER_HOST = os.environ.get("FLEET_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("FLEET_BROKER_PORT", "1883"))
METRICS_PORT = int(os.environ.get("FLEET_METRICS_PORT", "8000"))
# '+' matches one topic level, so this catches every device without enumerating IDs.
TELEMETRY_TOPIC = os.environ.get("FLEET_TELEMETRY_TOPIC", "fleet/+/telemetry")

# ── metrics ───────────────────────────────────────────────────────────────
# Counters: monotonic; read via rate() in PromQL, never as a raw value.
MESSAGES = Counter(
    "fleet_messages_total",
    "Telemetry messages received and successfully parsed.",
    ["device"],
)
ERRORS = Counter(
    "fleet_message_errors_total",
    "Payloads that arrived but could not be decoded/parsed.",
    ["device", "reason"],  # 'reason' is a bounded set of failure kinds
)

# Gauges: current-value snapshots that move both ways.
TEMPERATURE = Gauge("fleet_temperature_celsius", "Last temperature reading.", ["device"])
HUMIDITY = Gauge("fleet_humidity_percent", "Last relative humidity reading.", ["device"])
PRESSURE = Gauge("fleet_pressure_hpa", "Last barometric pressure reading.", ["device"])
ACCEL_MAG = Gauge(
    "fleet_accel_magnitude_g",
    "Last accelerometer magnitude sqrt(ax^2+ay^2+az^2); ~1.0 at rest (gravity).",
    ["device"],
)
GYRO_MAG = Gauge(
    "fleet_gyro_magnitude_dps",
    "Last gyro magnitude sqrt(gx^2+gy^2+gz^2); ~0 at rest, spikes on a twist/rotation.",
    ["device"],
)

# Freshness primitive: expose last-seen timestamp; age = time()-<this> at query time, so it climbs on its own when a device dies.
LAST_MESSAGE_TS = Gauge(
    "fleet_last_message_timestamp_seconds",
    "Bridge wall-clock time the last message from this device was received.",
    ["device"],
)

# Buckets hand-sized around the 10 Hz (~0.1s) operating point so p95 resolves there.
INTERMESSAGE_GAP = Histogram(
    "fleet_intermessage_gap_seconds",
    "Gap between consecutive telemetry messages from a device.",
    ["device"],
    buckets=(0.05, 0.075, 0.1, 0.125, 0.15, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0),
)

# Packet loss from the firmware's monotonic seq: a jump larger than 1 between consecutive messages
# means the gateway/broker dropped the ones in between. A counter, not a gauge, because loss is
# cumulative — rate() it in PromQL for a loss-per-second signal.
SEQUENCE_GAPS = Counter(
    "fleet_sequence_gaps_total",
    "Telemetry messages skipped, inferred from gaps in the firmware sequence number.",
    ["device"],
)

# Broker connectivity as a metric, not as a probe result: an outage has to stay visible to Prometheus
# rather than quietly removing this exporter from service.
BROKER_CONNECTED = Gauge(
    "fleet_broker_connected",
    "1 while this service holds a live MQTT broker connection.",
)
BROKER_CONNECTED.set(0)

# Per-device prior arrival, used only to compute the gap. Single-writer (on_message thread), so no lock.
_last_arrival: dict[str, float] = {}
# Per-device prior seq, used only to detect skipped messages. Same single-writer thread, no lock.
_last_seq: dict[str, int] = {}


# ── health ───────────────────────────────────────────────────────────────────
class Health:
    """What the kubelet's probes read. Written by the MQTT thread, read by the HTTP thread."""

    def __init__(self):
        self.subscribed = False     # flipped by the first successful subscribe, never back
        self.loop_running = True    # cleared if paho's network loop ever returns

    def live(self):
        # Only process-local state: a broker outage is not something a restart can fix, so it must
        # never reach this. Restarting on a dependency failure just turns one outage into two.
        return self.loop_running

    def ready(self):
        # Warm-up gate only. It deliberately stays ready across later broker flaps: pulling the
        # exporter from the Service would take the staleness signal with it and hide the outage.
        return self.subscribed


# ── MQTT callbacks (paho-mqtt 2.x / CallbackAPIVersion.VERSION2) ─────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[bridge] connect failed: {reason_code}", flush=True)
        return
    # Subscribe on every (re)connect: subscriptions are per-session and dropped on reconnect.
    client.subscribe(TELEMETRY_TOPIC, qos=1)
    userdata.subscribed = True
    BROKER_CONNECTED.set(1)
    print(f"[bridge] connected; subscribed to {TELEMETRY_TOPIC}", flush=True)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    BROKER_CONNECTED.set(0)
    print(f"[bridge] disconnected ({reason_code}); paho will retry", flush=True)


def on_message(client, userdata, msg):
    now = time.time()

    # Identity comes from the topic (already routed by the broker), not the payload.
    parts = msg.topic.split("/")
    device = parts[1] if len(parts) >= 2 else "unknown"

    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Bad frame is a countable failure; tag it and keep the loop alive.
        ERRORS.labels(device=device, reason="decode").inc()
        return

    # Count + stamp freshness first: liveness is independent of sensor payload (even a heartbeat proves alive).
    MESSAGES.labels(device=device).inc()
    LAST_MESSAGE_TS.labels(device=device).set(now)

    # First message per device only seeds the baseline — no gap to observe yet.
    prev = _last_arrival.get(device)
    if prev is not None:
        INTERMESSAGE_GAP.labels(device=device).observe(now - prev)
    _last_arrival[device] = now

    # Packet loss from the seq counter. Only count a forward jump > 1; a backwards/reset seq means the
    # device rebooted (seq restarts at 0), so reseed instead of logging a huge bogus gap.
    seq = data.get("seq")
    if isinstance(seq, int):
        prev_seq = _last_seq.get(device)
        if prev_seq is not None and seq > prev_seq + 1:
            SEQUENCE_GAPS.labels(device=device).inc(seq - prev_seq - 1)
        _last_seq[device] = seq

    # Sensor fields are optional (heartbeats omit them); never default to 0.0 — a phantom 0 fakes a reading.
    if "temp" in data:
        TEMPERATURE.labels(device=device).set(data["temp"])
    if "humidity" in data:
        HUMIDITY.labels(device=device).set(data["humidity"])
    if "pressure" in data:
        PRESSURE.labels(device=device).set(data["pressure"])
    if all(k in data for k in ("ax", "ay", "az")):
        mag = math.sqrt(data["ax"] ** 2 + data["ay"] ** 2 + data["az"] ** 2)
        ACCEL_MAG.labels(device=device).set(mag)
    if all(k in data for k in ("gx", "gy", "gz")):
        gmag = math.sqrt(data["gx"] ** 2 + data["gy"] ** 2 + data["gz"] ** 2)
        GYRO_MAG.labels(device=device).set(gmag)


def make_handler(health):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

        def do_GET(self):
            if self.path == "/metrics":
                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif self.path == "/healthz":
                ok = health.live()
                self._reply(200 if ok else 503, "ok" if ok else "mqtt loop stopped")
            elif self.path == "/readyz":
                ok = health.ready()
                self._reply(200 if ok else 503, "ready" if ok else "no broker subscription yet")
            else:
                self._reply(404, "not found")

        def log_message(self, *args):
            pass  # probe traffic every few seconds would drown the lifecycle lines

    return Handler


def mqtt_loop(client, health):
    """Owns paho's network loop. If it ever returns, the process can't recover — say so on /healthz."""
    try:
        # retry_first_connection: without it paho re-raises when the broker isn't up yet, turning a
        # dependency that is merely slow to start into a crash loop.
        client.loop_forever(retry_first_connection=True)
    finally:
        health.loop_running = False
        BROKER_CONNECTED.set(0)


def main():
    health = Health()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="fleet-bridge",
        userdata=health,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # No Last-Will: bridge death is caught by Prometheus's `up` metric, a different fault domain than device liveness.
    # connect_async, so a broker that isn't up yet leaves us unready instead of crash-looping.
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)

    server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), make_handler(health))
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    threading.Thread(target=mqtt_loop, args=(client, health), daemon=True, name="mqtt").start()
    print(f"[bridge] /metrics, /healthz, /readyz on :{METRICS_PORT}", flush=True)

    # Clean SIGTERM/SIGINT shutdown so a stop exits 0 instead of looking like a crash.
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    stopping.wait()
    client.disconnect()
    server.shutdown()
    print("[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
