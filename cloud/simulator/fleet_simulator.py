#!/usr/bin/env python3
"""Simulated device fleet: N independent telemetry streams with occasional faults, over MQTT.

Each device has its own baseline (so streams look distinct, not cloned), publishes at a fixed rate
under its own id, and drifts through a small state machine — normal, a channel anomaly, or an offline
outage — so the fleet naturally exercises freshness, availability, anomaly detection, and incidents
without anyone injecting by hand. This is the real workload the fleet dashboard and self-heal sit on.

    python fleet_simulator.py --devices 5           # or set FLEET_SIM_* env and run the container
"""

import argparse
import json
import math
import os
import random
import time

import paho.mqtt.client as mqtt

# Offset added to a channel while it's in an anomaly — large enough to clear the fitted normal band.
ANOMALY_OFFSETS = {"temperature": 14.0, "humidity": 40.0, "pressure": 6.0, "accel": 1.0}


def env(name, default):
    return os.environ.get(name, default)


class Device:
    """One simulated device: a distinct baseline plus a normal/anomaly/offline state machine."""

    def __init__(self, device_id, rng):
        self.id = device_id
        self.rng = rng
        # Per-device baselines so no two streams are identical.
        self.temp0 = rng.uniform(19.0, 25.0)
        self.humid0 = rng.uniform(38.0, 52.0)
        self.press0 = rng.uniform(1008.0, 1018.0)
        self.phase = rng.uniform(0.0, math.tau)
        self.seq = 0
        self.mode = "normal"          # normal | anomaly | offline
        self.mode_until = 0.0
        self.anomaly_channel = None

    def _maybe_transition(self, now, warmup_over, p_anomaly, p_offline):
        """Advance the state machine. Events only start after warm-up so detectors baseline on clean data."""
        if now < self.mode_until:
            return
        if self.mode != "normal":
            # An event just ended.
            print(f"[sim] {self.id}: {self.mode} -> normal", flush=True)
            self.mode, self.anomaly_channel = "normal", None
            return
        if not warmup_over:
            return
        roll = self.rng.random()
        if roll < p_offline:
            self.mode = "offline"
            self.mode_until = now + self.rng.uniform(30.0, 90.0)
            print(f"[sim] {self.id}: normal -> offline ({int(self.mode_until - now)}s)", flush=True)
        elif roll < p_offline + p_anomaly:
            self.mode = "anomaly"
            self.anomaly_channel = self.rng.choice(list(ANOMALY_OFFSETS))
            self.mode_until = now + self.rng.uniform(25.0, 60.0)
            print(f"[sim] {self.id}: normal -> anomaly on {self.anomaly_channel} "
                  f"({int(self.mode_until - now)}s)", flush=True)

    def sample(self, t):
        """Build one telemetry payload for this tick, applying the anomaly offset if active."""
        off = ANOMALY_OFFSETS[self.anomaly_channel] if self.mode == "anomaly" else 0.0
        temp = self.temp0 + 2 * math.sin(t / 5 + self.phase) + self.rng.uniform(-0.1, 0.1)
        humidity = self.humid0 + 5 * math.sin(t / 7 + self.phase) + self.rng.uniform(-0.2, 0.2)
        pressure = self.press0 + self.rng.uniform(-0.5, 0.5)
        az = 1.0 + self.rng.uniform(-0.02, 0.02)
        temp += off if self.anomaly_channel == "temperature" else 0.0
        humidity += off if self.anomaly_channel == "humidity" else 0.0
        pressure += off if self.anomaly_channel == "pressure" else 0.0
        az += off if self.anomaly_channel == "accel" else 0.0
        return {
            "id": self.id, "seq": self.seq, "ts": time.time(),
            "temp": round(temp, 2), "humidity": round(humidity, 2), "pressure": round(pressure, 2),
            "ax": round(self.rng.uniform(-0.02, 0.02), 3),
            "ay": round(self.rng.uniform(-0.02, 0.02), 3),
            "az": round(az, 3),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=env("FLEET_BROKER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("FLEET_BROKER_PORT", "1883")))
    ap.add_argument("--devices", type=int, default=int(env("FLEET_SIM_DEVICES", "5")))
    ap.add_argument("--rate", type=float, default=float(env("FLEET_SIM_RATE", "10")),
                    help="messages/sec per device")
    ap.add_argument("--drop", type=float, default=float(env("FLEET_SIM_DROP", "0.005")),
                    help="fraction of messages dropped (packet loss)")
    ap.add_argument("--anomaly-rate", type=float, default=float(env("FLEET_SIM_ANOMALY_RATE", "0.004")),
                    help="probability per device per second of starting an anomaly")
    ap.add_argument("--offline-rate", type=float, default=float(env("FLEET_SIM_OFFLINE_RATE", "0.002")),
                    help="probability per device per second of going offline")
    ap.add_argument("--warmup", type=float, default=float(env("FLEET_SIM_WARMUP", "40")),
                    help="seconds of clean data before any faults, so detectors baseline first")
    ap.add_argument("--seed", type=int, default=int(env("FLEET_SIM_SEED", "0")) or None)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    devices = [Device(f"sim-{i:02d}", random.Random(rng.random())) for i in range(1, args.devices + 1)]

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="fleet-sim")
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    period = 1.0 / args.rate
    # Per-tick event probabilities derived from the per-second rates.
    p_anomaly = args.anomaly_rate * period
    p_offline = args.offline_rate * period
    start = time.time()
    print(f"[sim] {len(devices)} devices at {args.rate} Hz -> {args.host}:{args.port} "
          f"(warmup {args.warmup:.0f}s); Ctrl-C to stop", flush=True)

    t = 0.0
    try:
        while True:
            now = time.time()
            warmup_over = now - start >= args.warmup
            for dev in devices:
                dev._maybe_transition(now, warmup_over, p_anomaly, p_offline)
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


if __name__ == "__main__":
    main()
