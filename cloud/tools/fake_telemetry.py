#!/usr/bin/env python3
"""Dev-only telemetry generator so the cloud stack has data on a laptop with no Pi attached.

This is NOT part of the product path — the Pi gateway is the real publisher, and Chunk 26 replaces
this with a proper multi-device fleet simulator. It exists purely to verify the Chunk 16/17 stack
(Prometheus scraping, Grafana panels) end to end without hardware.

    pip install paho-mqtt
    python fake_telemetry.py --devices 3 --rate 10          # 3 devices, 10 Hz each
    python fake_telemetry.py --drop 0.02                     # skip ~2% to exercise the packet-loss panel
    python fake_telemetry.py --anomaly temp                  # trip the Chunk 20 detectors on one channel
"""
import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt

# Chunk 20 demo only: how far to shove a channel out of its normal band when injecting. Each is many
# baseline-sigmas past the ~0.1–0.2 unit noise the generator produces, so both detectors trip clearly.
ANOMALY_OFFSETS = {"temp": 8.0, "humidity": 25.0, "pressure": 6.0, "accel": 1.0}


def build_sample(t, channel, off):
    """One synthetic sample dict. `off` is added to `channel` (0.0 = clean); accel offsets az so the
    magnitude sqrt(ax²+ay²+az²) rises like a shake/vibration."""
    temp = 22 + 2 * math.sin(t / 5) + random.uniform(-0.1, 0.1)
    humidity = 45 + 5 * math.sin(t / 7) + random.uniform(-0.2, 0.2)
    pressure = 1013 + random.uniform(-0.5, 0.5)
    az = 1.0 + random.uniform(-0.02, 0.02)  # ~1 g rest = gravity on Z
    temp += off if channel == "temp" else 0.0
    humidity += off if channel == "humidity" else 0.0
    pressure += off if channel == "pressure" else 0.0
    az += off if channel == "accel" else 0.0
    return {
        "temp": round(temp, 2),
        "humidity": round(humidity, 2),
        "pressure": round(pressure, 2),
        "ax": round(random.uniform(-0.02, 0.02), 3),
        "ay": round(random.uniform(-0.02, 0.02), 3),
        "az": round(az, 3),
    }


def is_injecting(args, elapsed):
    """True while the square-wave injection is in an anomalous window. After the warm-up delay it
    alternates anomalous / normal windows of `anomaly-hold` seconds; the first window is anomalous."""
    if not args.anomaly:
        return False
    since_start = elapsed - args.anomaly_start
    return since_start >= 0 and int(since_start // args.anomaly_hold) % 2 == 0


def publish_round(client, ids, seq, t, injecting, target, args):
    """Publish one message per device for this tick."""
    for d in ids:
        # Advance seq FIRST, then maybe skip the publish: the bridge sees the gap in seq and counts it
        # as loss — exactly how a real dropped packet looks downstream.
        seq[d] += 1
        if args.drop and random.random() < args.drop:
            continue
        # Offset only the injected channel, only on the target device, only while injecting.
        off = ANOMALY_OFFSETS[args.anomaly] if (injecting and d == target) else 0.0
        payload = {"id": d, "seq": seq[d], "ts": time.time(), **build_sample(t, args.anomaly, off)}
        client.publish(f"fleet/{d}/telemetry", json.dumps(payload), qos=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--devices", type=int, default=1)
    ap.add_argument("--rate", type=float, default=10.0, help="messages/sec per device")
    ap.add_argument("--drop", type=float, default=0.0,
                    help="fraction of messages to skip publishing (fakes packet loss)")
    ap.add_argument("--anomaly", choices=list(ANOMALY_OFFSETS),
                    help="Chunk 20 demo: after a warm-up delay, periodically push this channel out of "
                         "its normal band on one device so both anomaly detectors trip")
    ap.add_argument("--anomaly-device",
                    help="device id to disturb (default: the first device)")
    ap.add_argument("--anomaly-start", type=float, default=35.0,
                    help="seconds to wait before injecting, so the baseline window fits on clean data first")
    ap.add_argument("--anomaly-hold", type=float, default=15.0,
                    help="length of each anomalous window; injection alternates on/off every hold seconds")
    args = ap.parse_args()

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="fake-telemetry")
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    ids = [f"sim-{i:02d}" for i in range(1, args.devices + 1)]
    seq = {d: 0 for d in ids}
    period = 1.0 / args.rate
    anomaly_target = args.anomaly_device or ids[0]
    print(f"publishing {args.rate} Hz x {len(ids)} device(s) to {args.host}:{args.port} "
          f"(drop={args.drop:.0%}); Ctrl-C to stop", flush=True)
    if args.anomaly:
        print(f"anomaly: '{args.anomaly}' on {anomaly_target}, starting at t+{args.anomaly_start:.0f}s, "
              f"toggling every {args.anomaly_hold:.0f}s", flush=True)

    # Injection is a square wave that trips both detectors once fitted, then recovers — which also
    # exercises the later self-heal path. See is_injecting() for the timing.
    start_wall = time.time()
    prev_injecting = False
    t = 0.0
    try:
        while True:
            injecting = is_injecting(args, time.time() - start_wall)
            if injecting != prev_injecting:
                print(f"[t+{time.time() - start_wall:5.0f}s] anomaly {'ON' if injecting else 'off'} "
                      f"({args.anomaly} @ {anomaly_target})", flush=True)
                prev_injecting = injecting

            publish_round(client, ids, seq, t, injecting, anomaly_target, args)
            t += period
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
