#!/usr/bin/env python3
"""
gateway/serial_reader.py  --  Chunk 11 (Pi reads serial -> publishes to MQTT)

Reads NDJSON telemetry from the Nucleo over the ST-LINK virtual COM port,
buffers raw bytes until a newline, parses each complete line as JSON, and
publishes each valid message to the Mosquitto broker. Reconnects the serial
side automatically if the Nucleo is unplugged/replugged.

Run on the Pi:  python3 gateway/serial_reader.py   (Ctrl-C to stop)
"""
import json
import time
import serial
import os
import paho.mqtt.client as mqtt

# Prefer the stable by-id symlink over /dev/ttyACM0 -- ACM numbering can change
# on replug, the by-id path does not. Find yours with:
#   ls -l /dev/serial/by-id/
PORT = os.environ.get("FLEET_SERIAL_PORT", "/dev/ttyACM0")
BAUD = 115200
READ_TIMEOUT = 1.0             # seconds; read() returns after this even with no data
RECONNECT_DELAY = 2.0          # seconds between reconnect attempts
MAX_LINE = 4096                # a real telemetry line is ~200B; larger w/o a newline = framing fault

# Broker host/port are env vars for the same reason PORT is: when the broker
# migrates from the Pi to the cloud in Chunk 16, the same committed file works
# by changing an env var, not the code.
BROKER_HOST = os.environ.get("FLEET_MQTT_HOST", "localhost")
BROKER_PORT = int(os.environ.get("FLEET_MQTT_PORT", "1883"))
MQTT_KEEPALIVE = 60            # seconds; paho pings the broker this often to stay alive


def open_serial() -> serial.Serial:
    """Block until the port opens, then return the handle. Retries forever so
    the gateway can be started before the Nucleo is plugged in."""
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=READ_TIMEOUT)
            # Discard bytes that accumulated in the OS buffer before we started
            # reading. The first bytes are almost always a mid-line fragment
            # (no opening '{'), which would fail to parse. Flushing chooses
            # freshness over a stale backlog -- ties to the freshness SLI.
            ser.reset_input_buffer()
            print(f"[serial] connected {PORT} @ {BAUD}")
            return ser
        except OSError as e:   # serial.SerialException subclasses OSError
            print(f"[serial] {PORT} not ready ({e}); retry in {RECONNECT_DELAY}s")
            time.sleep(RECONNECT_DELAY)


def make_mqtt_client() -> mqtt.Client:
    """Connect to the broker and start a background network thread.
    loop_start() runs paho's I/O on its own thread so our serial read loop
    stays in control -- loop_forever() would block and starve the reader.
    CallbackAPIVersion.VERSION2 is mandatory in paho-mqtt 2.x."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    # connect_async + loop_start defers the connect to paho's thread and retries,
    # so a broker that's down at boot doesn't crash the gateway (symmetric with open_serial).
    client.connect_async(BROKER_HOST, BROKER_PORT, MQTT_KEEPALIVE)
    client.loop_start()
    print(f"[mqtt] connecting to {BROKER_HOST}:{BROKER_PORT}")
    return client


def extract_lines(buf: bytearray):
    """Pull every complete newline-terminated line out of buf, in order.
    Whatever follows the last newline is a PARTIAL line -- it stays in buf
    until the rest of it arrives on a later read, then gets parsed."""
    while b"\n" in buf:
        idx = buf.index(b"\n")
        line = bytes(buf[:idx]).strip()
        del buf[:idx + 1]         # drop the line AND its newline; keep the tail
        if line:
            yield line


def handle_line(raw: bytes, client: mqtt.Client):
    """Parse one complete line as JSON, then publish it to MQTT. A malformed
    line is logged and dropped BEFORE any publish -- only valid telemetry
    reaches the broker, so parse/framing failures stay upstream of MQTT and
    never leave the gateway. A single corrupt frame must not take it down."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[parse] skipping bad line: {raw!r}")
        return

    device_id = msg.get("id", "unknown")
    # Heartbeats go to .../status, telemetry to .../telemetry. Splitting them
    # sets up retained-status + last-will in Chunk 12 and shows topic hierarchy.
    subtopic = "status" if msg.get("type") == "heartbeat" else "telemetry"
    topic = f"fleet/{device_id}/{subtopic}"
    client.publish(topic, json.dumps(msg))     # QoS 0 (default) -- QoS is Chunk 12
    print(f"[mqtt] {topic} {msg}")


def main():
    ser = open_serial()
    client = make_mqtt_client()
    buf = bytearray()
    try:
        while True:
            try:
                chunk = ser.read(256)     # up to 256 bytes, or fewer after timeout
            except OSError as e:          # serial.SerialException subclasses OSError
                print(f"[serial] lost device ({e}); reconnecting")
                try:
                    ser.close()
                except Exception:
                    pass
                buf.clear()               # abandon the half-received line
                ser = open_serial()
                continue

            if not chunk:
                continue                  # timeout, no bytes this round

            buf.extend(chunk)
            if len(buf) > MAX_LINE and b"\n" not in buf:
                print(f"[serial] no newline in {len(buf)}B; framing fault, dropping buffer")
                buf.clear()
            for line in extract_lines(buf):
                handle_line(line, client)
    finally:
        # Clean shutdown even on Ctrl-C: stop the network thread and disconnect
        # so we don't leave a half-open connection on the broker.
        client.loop_stop()
        client.disconnect()
        print("[mqtt] disconnected")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[serial] stopped")