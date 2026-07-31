#!/usr/bin/env python3
"""Anomaly-detection service: a second MQTT subscriber that scores telemetry per channel with two
detectors and exposes the scores on /metrics for Prometheus.

Deliberately a SEPARATE service from the bridge rather than bolted into it. Pub/sub means another
consumer just subscribes — the gateway and the bridge don't change and don't even know it exists. It
also keeps the bridge a pure exporter and lets anomaly detection fail, restart, or scale on its own
fault domain without touching the ingest path.

Both detectors are exported so they stay comparable on the dashboard; only the one named by
FLEET_ALERTING_DETECTOR feeds the alerting path.
"""

import json
import math
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from detectors import ChannelDetectors

# ── config (env-driven) ──────────────────────────────────────────────────────
BROKER_HOST = os.environ.get("FLEET_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("FLEET_BROKER_PORT", "1883"))
METRICS_PORT = int(os.environ.get("FLEET_METRICS_PORT", "8001"))  # 8001: the bridge already owns 8000
TELEMETRY_TOPIC = os.environ.get("FLEET_TELEMETRY_TOPIC", "fleet/+/telemetry")

# Detector knobs. Exposing them as env keeps tuning a config change, not a rebuild.
BASELINE = int(os.environ.get("FLEET_ANOMALY_BASELINE", "300"))       # warm-up samples (~30s at 10 Hz)
SIGMA = float(os.environ.get("FLEET_ZSCORE_SIGMA", "3.5"))            # z-score trip threshold
CONTAMINATION = float(os.environ.get("FLEET_IF_CONTAMINATION", "0.01"))
N_ESTIMATORS = int(os.environ.get("FLEET_IF_ESTIMATORS", "100"))

# Per-channel noise floor on the z-score spread, in the channel's own units. A near-constant real
# signal (a still BME280) drives MAD toward zero; without a physical floor the band is a sliver and
# normal jitter/self-heating drift trips a false anomaly. Override per channel via env.
# Floors sit at roughly each sensor's own datasheet accuracy: below that a "change" isn't physically
# resolvable, so it can't be a real anomaly worth paging on.
_MIN_STD_DEFAULTS = {
    "temperature": 0.25,      # °C   (~BME280 typ temp accuracy)
    "humidity": 0.8,          # %RH  (~BME280 humidity accuracy ±3%)
    "pressure": 0.16,         # hPa  (~4-5x relative-pressure noise)
    "accel_magnitude": 0.02,  # g
    "gyro_magnitude": 3.0,    # dps  (well above the sub-1 dps resting noise, below a real hand-twist)
}


def _min_std(channel: str) -> float:
    default = _MIN_STD_DEFAULTS.get(channel, 0.0)
    return float(os.environ.get(f"FLEET_MIN_STD_{channel.upper()}", default))

# The z-score/MAD baseline owns the alerting path: 0.000 FP vs Isolation Forest's 5.6% on a 10 Hz
# stream was the deciding axis (see evaluate.py and docs/detector-evaluation.md).
ALERTING_DETECTOR = os.environ.get("FLEET_ALERTING_DETECTOR", "zscore")

# ── metrics ───────────────────────────────────────────────────────────────
# Normalized so 1.0 is each detector's own trip line (see detectors.py); comparable on one axis.
ANOMALY_SCORE = Gauge(
    "fleet_anomaly_score",
    "Normalized anomaly score per detector; 1.0 = that detector's trip threshold, higher = more anomalous.",
    ["device", "channel", "detector"],
)
ANOMALY_FLAG = Gauge(
    "fleet_anomaly_flag",
    "1 when the detector's score is past its threshold for this sample, else 0.",
    ["device", "channel", "detector"],
)
# Statistical detector's normal range in the channel's own units — for shading a band under the signal.
BAND_LOWER = Gauge(
    "fleet_channel_baseline_lower",
    "Lower edge of the z-score/MAD normal band (raw channel units).",
    ["device", "channel"],
)
BAND_UPPER = Gauge(
    "fleet_channel_baseline_upper",
    "Upper edge of the z-score/MAD normal band (raw channel units).",
    ["device", "channel"],
)
MODEL_READY = Gauge(
    "fleet_anomaly_model_ready",
    "1 once both detectors are fitted for this channel (warm-up complete), else 0.",
    ["device", "channel"],
)
# Broker connectivity as a metric, not as a probe result — same reasoning as the bridge.
BROKER_CONNECTED = Gauge(
    "fleet_broker_connected",
    "1 while this service holds a live MQTT broker connection.",
)
BROKER_CONNECTED.set(0)

# Per (device, channel) detector pair. Written only from the on_message thread, so no lock (same
# single-writer discipline as the bridge). The /metrics server thread only ever reads the gauges.
_detectors: dict[tuple[str, str], ChannelDetectors] = {}
_fit_logged: set[tuple[str, str]] = set()


def _channels(data: dict):
    """Yield (channel, value) for each sensor field present. Heartbeats omit these, so they're skipped;
    never default a missing field to 0.0 — a phantom zero would look like a huge anomaly."""
    if "temp" in data:
        yield "temperature", float(data["temp"])
    if "humidity" in data:
        yield "humidity", float(data["humidity"])
    if "pressure" in data:
        yield "pressure", float(data["pressure"])
    if all(k in data for k in ("ax", "ay", "az")):
        yield "accel_magnitude", math.sqrt(data["ax"] ** 2 + data["ay"] ** 2 + data["az"] ** 2)
    # Rotation (a twist) barely moves accel magnitude — gravity's vector length is conserved — so it
    # only shows up on the gyro. Score gyro magnitude too, or a spin is invisible to anomaly detection.
    if all(k in data for k in ("gx", "gy", "gz")):
        yield "gyro_magnitude", math.sqrt(data["gx"] ** 2 + data["gy"] ** 2 + data["gz"] ** 2)


def _get(device: str, channel: str) -> ChannelDetectors:
    key = (device, channel)
    det = _detectors.get(key)
    if det is None:
        det = ChannelDetectors(
            baseline=BASELINE, sigma=SIGMA, min_std=_min_std(channel),
            contamination=CONTAMINATION, n_estimators=N_ESTIMATORS,
        )
        _detectors[key] = det
        MODEL_READY.labels(device=device, channel=channel).set(0)
    return det


# ── health ───────────────────────────────────────────────────────────────────
class Health:
    """What the kubelet's probes read. Written by the MQTT thread, read by the HTTP thread."""

    def __init__(self):
        self.subscribed = False     # flipped by the first successful subscribe, never back
        self.loop_running = True    # cleared if paho's network loop ever returns

    def live(self):
        # Process-local only: a broker outage isn't fixed by a restart, so it must not fail liveness.
        return self.loop_running

    def ready(self):
        # Warm-up gate is the subscription, not the models: a fitting detector still has scores worth
        # scraping, and model state is already exported as fleet_anomaly_model_ready.
        return self.subscribed


# ── MQTT callbacks (paho-mqtt 2.x / CallbackAPIVersion.VERSION2) ─────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[anomaly] connect failed: {reason_code}", flush=True)
        return
    client.subscribe(TELEMETRY_TOPIC, qos=1)
    userdata.subscribed = True
    BROKER_CONNECTED.set(1)
    print(f"[anomaly] connected; subscribed to {TELEMETRY_TOPIC}", flush=True)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    BROKER_CONNECTED.set(0)
    print(f"[anomaly] disconnected ({reason_code}); paho will retry", flush=True)


def on_message(client, userdata, msg):
    # Identity comes from the topic (already routed by the broker), not the payload.
    parts = msg.topic.split("/")
    device = parts[1] if len(parts) >= 2 else "unknown"

    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Counting bad frames is the bridge's job; the detector just skips them.
        return

    for channel, value in _channels(data):
        det = _get(device, channel)
        result = det.update(value)

        # Reflect warm-up state on every message so the gauge flips to 1 the instant the fit lands.
        MODEL_READY.labels(device=device, channel=channel).set(1 if det.ready else 0)
        if result is None:
            continue  # still warming up (or this was the sample that completed the baseline)

        key = (device, channel)
        if key not in _fit_logged:
            _fit_logged.add(key)
            print(f"[anomaly] fitted {device}/{channel} on {BASELINE} baseline samples", flush=True)

        for detector in ("zscore", "iforest"):
            r = result[detector]
            ANOMALY_SCORE.labels(device=device, channel=channel, detector=detector).set(r["score"])
            ANOMALY_FLAG.labels(device=device, channel=channel, detector=detector).set(1 if r["flag"] else 0)
        BAND_LOWER.labels(device=device, channel=channel).set(result["band_lower"])
        BAND_UPPER.labels(device=device, channel=channel).set(result["band_upper"])


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
        client_id="fleet-anomaly",
        userdata=health,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # No Last-Will: this service dying is caught by Prometheus's `up` metric, a different fault domain
    # than device liveness (same reasoning as the bridge). connect_async so a broker that isn't up yet
    # leaves us unready instead of crash-looping.
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)

    server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), make_handler(health))
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    threading.Thread(target=mqtt_loop, args=(client, health), daemon=True, name="mqtt").start()
    print(f"[anomaly] /metrics, /healthz, /readyz on :{METRICS_PORT}; baseline={BASELINE}, "
          f"sigma={SIGMA}, contamination={CONTAMINATION}", flush=True)

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    stopping.wait()
    client.disconnect()
    server.shutdown()
    print("[anomaly] stopped", flush=True)


if __name__ == "__main__":
    main()
