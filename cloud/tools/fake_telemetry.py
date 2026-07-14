#!/usr/bin/env python3
"""Dev-only telemetry generator so the cloud stack has data on a laptop with no Pi attached.

This is NOT part of the product path — the Pi gateway is the real publisher, and Chunk 26 replaces
this with a proper multi-device fleet simulator. It exists purely to verify the Chunk 16/17 stack
(Prometheus scraping, Grafana panels) end to end without hardware.

    pip install paho-mqtt
    python fake_telemetry.py --devices 3 --rate 10          # 3 devices, 10 Hz each
    python fake_telemetry.py --drop 0.02                     # skip ~2% to exercise the packet-loss panel
"""
import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--devices", type=int, default=1)
    ap.add_argument("--rate", type=float, default=10.0, help="messages/sec per device")
    ap.add_argument("--drop", type=float, default=0.0,
                    help="fraction of messages to skip publishing (fakes packet loss)")
    args = ap.parse_args()

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="fake-telemetry")
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    ids = [f"sim-{i:02d}" for i in range(1, args.devices + 1)]
    seq = {d: 0 for d in ids}
    period = 1.0 / args.rate
    print(f"publishing {args.rate} Hz x {len(ids)} device(s) to {args.host}:{args.port} "
          f"(drop={args.drop:.0%}); Ctrl-C to stop", flush=True)

    t = 0.0
    try:
        while True:
            for d in ids:
                # Advance seq FIRST, then maybe skip the publish: the bridge sees the gap in seq and
                # counts it as loss — exactly how a real dropped packet looks downstream.
                seq[d] += 1
                if args.drop and random.random() < args.drop:
                    continue
                payload = {
                    "id": d,
                    "seq": seq[d],
                    "ts": time.time(),
                    "temp": round(22 + 2 * math.sin(t / 5) + random.uniform(-0.1, 0.1), 2),
                    "humidity": round(45 + 5 * math.sin(t / 7) + random.uniform(-0.2, 0.2), 2),
                    "pressure": round(1013 + random.uniform(-0.5, 0.5), 2),
                    "ax": round(random.uniform(-0.02, 0.02), 3),
                    "ay": round(random.uniform(-0.02, 0.02), 3),
                    "az": round(1.0 + random.uniform(-0.02, 0.02), 3),  # ~1 g rest = gravity on Z
                }
                client.publish(f"fleet/{d}/telemetry", json.dumps(payload), qos=0)
            t += period
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
