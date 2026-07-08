#!/usr/bin/env python3
"""
gateway/serial_reader.py  --  Chunk 12 (QoS, retained status, last-will)

Reads NDJSON telemetry from the Nucleo over the ST-LINK virtual COM port,
buffers raw bytes until a newline, parses each complete line as JSON, and
publishes it to the Mosquitto broker. Publishes a retained connection-state
message on connect, and registers a Last-Will so an ungraceful death marks
the gateway offline without any code of ours running.

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

# Identity must be known BEFORE connect, because the Last-Will topic is sent
# inside the CONNECT packet -- before a single telemetry line has been read.
# So identity is configuration, not payload.
DEVICE_ID = os.environ.get("FLEET_DEVICE_ID", "fleet-edge-01")

TOPIC_TELEMETRY = f"fleet/{DEVICE_ID}/telemetry"
TOPIC_HEARTBEAT = f"fleet/{DEVICE_ID}/heartbeat"
TOPIC_STATUS    = f"fleet/{DEVICE_ID}/status"   # retained connection state ONLY

# 30s => broker declares us dead after ~45s of silence (1.5x keepalive).
# That figure IS the MTTD floor for gateway death: nothing downstream can know
# sooner, because the fact doesn't exist until the broker publishes our will.
# Chosen against the availability SLO, not taken from paho's 60s default.
MQTT_KEEPALIVE = 30

STATUS_ONLINE   = json.dumps({"id": DEVICE_ID, "state": "online"})
STATUS_LWT      = json.dumps({"id": DEVICE_ID, "state": "offline", "reason": "lwt"})
STATUS_SHUTDOWN = json.dumps({"id": DEVICE_ID, "state": "offline", "reason": "shutdown"})


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


def on_connect(client, userdata, connect_flags, reason_code, properties):
    """Fires on EVERY successful connection, including paho's auto-reconnects.
    That is exactly why the retained 'online' publish lives here and not inline
    after connect(): a gateway that reconnects after a network blip must restore
    its own status, or the broker keeps serving the retained 'offline' will."""
    if reason_code != 0:
        print(f"[mqtt] connect failed: {reason_code}")
        return
    print(f"[mqtt] connected to {BROKER_HOST}:{BROKER_PORT}")
    # QoS 1 + retain: state must arrive, and must be there for late subscribers.
    client.publish(TOPIC_STATUS, STATUS_ONLINE, qos=1, retain=True)
    print(f"[mqtt] {TOPIC_STATUS} online (retained)")


def make_mqtt_client() -> mqtt.Client:
    """Connect to the broker and start a background network thread.
    loop_start() runs paho's I/O on its own thread so our serial read loop
    stays in control -- loop_forever() would block and starve the reader.
    CallbackAPIVersion.VERSION2 is mandatory in paho-mqtt 2.x."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect

    # The will travels INSIDE the CONNECT packet, so it must be registered
    # before any connect call. Retained, because a subscriber that arrives
    # after our death must not be served the stale 'online' message.
    client.will_set(TOPIC_STATUS, STATUS_LWT, qos=1, retain=True)

    # connect_async + loop_start defers the connect to paho's thread and retries,
    # so a broker that's down at boot doesn't crash the gateway (symmetric with open_serial).
    client.connect_async(BROKER_HOST, BROKER_PORT, MQTT_KEEPALIVE)
    client.loop_start()
    print(f"[mqtt] connecting to {BROKER_HOST}:{BROKER_PORT} (keepalive {MQTT_KEEPALIVE}s)")
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

    # /status is reserved for retained CONNECTION state (online/offline).
    # The firmware heartbeat is a different fault domain -- device liveness,
    # not link liveness -- so it gets its own topic. Publishing it to /status
    # would clobber the retained will marker.
    topic = TOPIC_HEARTBEAT if msg.get("type") == "heartbeat" else TOPIC_TELEMETRY

    # QoS 0: telemetry is a sample of a continuous signal. A lost sample is a
    # gap I can DETECT via seq#, and a retransmitted 400ms-old accel reading is
    # worthless -- freshness is the SLI, not completeness. QoS 1 would also
    # duplicate messages and corrupt the Prometheus counters in Chunk 15.
    client.publish(topic, json.dumps(msg), qos=0, retain=False)
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
        # A CLEAN disconnect suppresses the will -- the broker assumes we meant
        # to leave. So we must overwrite the retained status ourselves, or it
        # says 'online' forever after a graceful Ctrl-C.
        info = client.publish(TOPIC_STATUS, STATUS_SHUTDOWN, qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=2.0)   # network thread is still alive here
        except Exception:
            pass
        print(f"[mqtt] {TOPIC_STATUS} offline (retained)")
        client.loop_stop()
        client.disconnect()
        print("[mqtt] disconnected")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[serial] stopped")