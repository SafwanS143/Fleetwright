#!/usr/bin/env python3
"""MQTT→Prometheus exporter: subscribes to telemetry, exposes /metrics for Prometheus to scrape."""

import json
import math
import os
import signal
import time

import paho.mqtt.client as mqtt
from prometheus_client import Counter, Gauge, Histogram, start_http_server

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

# Per-device prior arrival, used only to compute the gap. Single-writer (on_message thread), so no lock.
_last_arrival: dict[str, float] = {}


# ── MQTT callbacks (paho-mqtt 2.x / CallbackAPIVersion.VERSION2) ─────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[bridge] connect failed: {reason_code}", flush=True)
        return
    # Subscribe on every (re)connect: subscriptions are per-session and dropped on reconnect.
    client.subscribe(TELEMETRY_TOPIC, qos=1)
    print(f"[bridge] connected; subscribed to {TELEMETRY_TOPIC}", flush=True)


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


def main():
    # Daemon thread serving /metrics; runs independently of the MQTT loop and only reads the metrics.
    start_http_server(METRICS_PORT)
    print(f"[bridge] /metrics on :{METRICS_PORT}", flush=True)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="fleet-bridge",
    )
    client.on_connect = on_connect
    client.on_message = on_message

    # No Last-Will: bridge death is caught by Prometheus's `up` metric, a different fault domain than device liveness.
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)

    # Clean SIGTERM/SIGINT shutdown so `docker stop` exits 0 instead of looking like a crash.
    def _shutdown(signum, frame):
        print(f"[bridge] signal {signum}; disconnecting", flush=True)
        client.disconnect()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # loop_forever (not loop_start): blocks and handles reconnects; main thread has nothing else to do.
    client.loop_forever()
    print("[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
