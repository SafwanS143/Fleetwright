#!/usr/bin/env python3
"""Anomaly-detection service (Chunk 20): a second MQTT subscriber that scores telemetry per channel
with two detectors and exposes the scores on /metrics for Prometheus.

Deliberately a SEPARATE service from the bridge rather than bolted into it. Pub/sub means another
consumer just subscribes — the gateway and the bridge don't change and don't even know it exists
(the Chunk 11 payoff). It also keeps the bridge a pure exporter and lets anomaly detection fail,
restart, or scale on its own fault domain without touching the ingest path.

Neither detector is wired to alerting here. Chunk 20 exposes both so they can be compared; Chunk 21 is
the evaluation that picks one for the alerting path.
"""

import json
import math
import os
import signal
import time

import paho.mqtt.client as mqtt
from prometheus_client import Gauge, start_http_server

from detectors import ChannelDetectors

# ── config (env-driven) ──────────────────────────────────────────────────────
BROKER_HOST = os.environ.get("FLEET_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("FLEET_BROKER_PORT", "1883"))
METRICS_PORT = int(os.environ.get("FLEET_METRICS_PORT", "8001"))  # 8001: the bridge already owns 8000
TELEMETRY_TOPIC = os.environ.get("FLEET_TELEMETRY_TOPIC", "fleet/+/telemetry")

# Detector knobs. Chunk 21 tunes these; exposing them as env keeps that a config change, not a rebuild.
BASELINE = int(os.environ.get("FLEET_ANOMALY_BASELINE", "300"))       # warm-up samples (~30s at 10 Hz)
SIGMA = float(os.environ.get("FLEET_ZSCORE_SIGMA", "3.5"))            # z-score trip threshold
CONTAMINATION = float(os.environ.get("FLEET_IF_CONTAMINATION", "0.01"))
N_ESTIMATORS = int(os.environ.get("FLEET_IF_ESTIMATORS", "100"))

# Chunk 21 decision: the z-score/MAD baseline enters the alerting path (0.000 FP vs IF's 5.6% on a 10 Hz
# stream — the deciding axis; see evaluate.py and docs/detector-evaluation.md). Both scores stay exported
# for the dashboard; Chunk 22 wires *this* detector's flag to paging.
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


def _get(device: str, channel: str) -> ChannelDetectors:
    key = (device, channel)
    det = _detectors.get(key)
    if det is None:
        det = ChannelDetectors(
            baseline=BASELINE, sigma=SIGMA,
            contamination=CONTAMINATION, n_estimators=N_ESTIMATORS,
        )
        _detectors[key] = det
        MODEL_READY.labels(device=device, channel=channel).set(0)
    return det


# ── MQTT callbacks (paho-mqtt 2.x / CallbackAPIVersion.VERSION2) ─────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[anomaly] connect failed: {reason_code}", flush=True)
        return
    client.subscribe(TELEMETRY_TOPIC, qos=1)
    print(f"[anomaly] connected; subscribed to {TELEMETRY_TOPIC}", flush=True)


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


def main():
    # Daemon thread serving /metrics; runs independently of the MQTT loop and only reads the gauges.
    start_http_server(METRICS_PORT)
    print(f"[anomaly] /metrics on :{METRICS_PORT}; baseline={BASELINE}, "
          f"sigma={SIGMA}, contamination={CONTAMINATION}", flush=True)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="fleet-anomaly",
    )
    client.on_connect = on_connect
    client.on_message = on_message

    # No Last-Will: this service dying is caught by Prometheus's `up` metric, a different fault domain
    # than device liveness (same reasoning as the bridge).
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)

    def _shutdown(signum, frame):
        print(f"[anomaly] signal {signum}; disconnecting", flush=True)
        client.disconnect()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    client.loop_forever()
    print("[anomaly] stopped", flush=True)


if __name__ == "__main__":
    main()
