#!/usr/bin/env python3
"""
gateway/serial_reader.py  --  Chunk 13 (bounded ring buffer / store-and-forward)

Reads NDJSON telemetry from the Nucleo over the ST-LINK virtual COM port,
buffers raw bytes until a newline, parses each complete line as JSON, and
publishes it to the Mosquitto broker. Publishes a retained connection-state
message on connect, and registers a Last-Will so an ungraceful death marks
the gateway offline without any code of ours running.

Chunk 13: telemetry no longer publishes directly. It goes into a bounded ring
buffer, and a rate-limited drain is the ONLY publisher. A broker outage now
costs latency instead of data.

Run on the Pi:  python3 gateway/serial_reader.py   (Ctrl-C to stop)
"""
import json
import time
import serial
import os
from collections import deque
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

# --- Chunk 13: store-and-forward ---------------------------------------------
# BOUNDED, because an unbounded queue does not prevent loss -- it converts a
# small bounded loss into an OOM kill on a 2GB Pi, which loses the queue AND the
# gateway. Bounded means the loss is capped, counted, and logged. That is
# backpressure: the Nucleo cannot be told to slow down, so we decide in advance
# what we sacrifice.
#
# Sizing: 10 Hz x ~230 B/frame. 6000 frames = 10 min of outage = ~1.4 MB
# resident. Sized to the outage class we intend to survive (broker restart,
# network blip) -- NOT to an all-day outage, whose backlog Prometheus could not
# use anyway (it stamps samples at scrape time, so a flush does not backfill).
BUFFER_MAXLEN = int(os.environ.get("FLEET_BUFFER_MAXLEN", "6000"))

# Max frames published per pass through the read loop. A greedy flush would not
# call ser.read() while draining, the kernel's serial input buffer would overrun,
# and we would lose LIVE data while frantically saving DEAD data. At ~9 loop
# passes/s this drains ~450 msg/s against a 10 msg/s ingest: the backlog clears
# ~45x faster than it fills, and the reader is serviced every pass.
DRAIN_BATCH = int(os.environ.get("FLEET_DRAIN_BATCH", "50"))

STAT_INTERVAL = 10.0           # seconds between [stat] lines


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
    userdata["connected"] = True
    print(f"[mqtt] connected to {BROKER_HOST}:{BROKER_PORT} "
          f"(buffered={len(userdata['buf'])})")
    # QoS 1 + retain: state must arrive, and must be there for late subscribers.
    client.publish(TOPIC_STATUS, STATUS_ONLINE, qos=1, retain=True)
    print(f"[mqtt] {TOPIC_STATUS} online (retained)")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    """The only place `connected` goes False. Everything downstream (drain,
    heartbeat) reads this flag rather than asking paho, so there is exactly one
    source of truth for link state."""
    userdata["connected"] = False
    print(f"[mqtt] disconnected ({reason_code}); buffering telemetry")


def make_mqtt_client(state: dict) -> mqtt.Client:
    """Connect to the broker and start a background network thread.
    loop_start() runs paho's I/O on its own thread so our serial read loop
    stays in control -- loop_forever() would block and starve the reader.
    CallbackAPIVersion.VERSION2 is mandatory in paho-mqtt 2.x.

    `state` is handed to paho as userdata so the callbacks can flip `connected`
    and read the buffer depth without a module-level global."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=state)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

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


def enqueue(state: dict, payload: str):
    """Append telemetry to the ring buffer, counting evictions.

    deque(maxlen=N) evicts the oldest element SILENTLY, and a silent drop is the
    exact failure this chunk exists to fix. So the eviction is counted before the
    append, or the loss is invisible.

    Drop-oldest is correct for telemetry: the newest sample is the most valuable
    one, because the dashboard and the anomaly detector both care about now. An
    audit log or a ledger would drop the NEWEST instead and refuse the write.
    """
    buf = state["buf"]
    if len(buf) == buf.maxlen:
        state["dropped"] += 1
        if state["dropped"] == 1 or state["dropped"] % 500 == 0:
            print(f"[buffer] FULL -- evicted {state['dropped']} oldest frames")
    buf.append(payload)


def drain(client: mqtt.Client, state: dict) -> int:
    """The ONLY publisher of telemetry. Pops FIFO, up to DRAIN_BATCH per pass.

    A frame is popped only AFTER paho accepts it; on failure it stays at the head
    and is retried next pass. FIFO + head-retry means the stream is never
    reordered, so `seq` stays monotonic and the consumer's gap detection keeps
    working across a flush.

    Note what MQTT_ERR_SUCCESS actually means at QoS 0: paho accepted the frame
    into its socket buffer. NOT that the broker received it. This buffer covers
    DISCONNECTED loss; in-flight loss is covered by seq-gap detection downstream.
    """
    buf = state["buf"]
    # No per-frame print. Logging every message at 10 Hz buries the [stat] line,
    # fills the disk, and -- while clearing a 6000-frame backlog to a remote
    # stdout -- would itself starve the read loop. Only report when catching up.
    backlog = len(buf) > DRAIN_BATCH
    sent = 0
    while buf and sent < DRAIN_BATCH and state["connected"]:
        info = client.publish(TOPIC_TELEMETRY, buf[0], qos=0, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            break                         # leave it at the head; retry next pass
        buf.popleft()
        sent += 1
        state["published"] += 1
    if backlog:
        print(f"[drain] flushed {sent}, {len(buf)} remaining")
    return sent


def publish_heartbeat(client: mqtt.Client, state: dict, payload: str):
    """Heartbeats are NEVER buffered.

    The freshness SLI is `now - last_received`, measured downstream. Replaying a
    stale heartbeat after an outage would report the device as fresh across a
    window in which the control plane demonstrably could not see it -- the system
    would manufacture a lie about its own availability. A heartbeat is only
    meaningful at the instant it arrives; if it cannot be delivered now, destroy it.

    Which is also why it is QoS 0, not the QoS 1 Chunk 12 first gave it: paho
    QUEUES QoS>0 publishes while disconnected and replays them on reconnect --
    exactly the replay this function exists to prevent. A liveness token needs
    the QoS level that does not retransmit.
    """
    if not state["connected"]:
        state["hb_dropped"] += 1
        return
    client.publish(TOPIC_HEARTBEAT, payload, qos=0, retain=False)
    print(f"[mqtt] {TOPIC_HEARTBEAT} {payload}")


def handle_line(raw: bytes, client: mqtt.Client, state: dict):
    """Parse one complete line as JSON, then route it. A malformed line is logged
    and dropped BEFORE it reaches the buffer or MQTT -- only valid telemetry
    enters the pipeline, so parse/framing failures stay upstream and never leave
    the gateway. A single corrupt frame must not take it down."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        state["parse_errors"] += 1
        print(f"[parse] skipping bad line: {raw!r}")
        return

    payload = json.dumps(msg)

    # /status is reserved for retained CONNECTION state (online/offline).
    # The firmware heartbeat is a different fault domain -- device liveness,
    # not link liveness -- so it gets its own topic. Publishing it to /status
    # would clobber the retained will marker.
    if msg.get("type") == "heartbeat":
        publish_heartbeat(client, state, payload)   # bypasses the buffer, by design
    else:
        enqueue(state, payload)                     # the buffer is the only publisher


def main():
    state = {
        "buf": deque(maxlen=BUFFER_MAXLEN),
        "connected": False,
        "dropped": 0,        # telemetry frames evicted by a full buffer
        "hb_dropped": 0,     # heartbeats destroyed while disconnected (by design, not a bug)
        "published": 0,
        "parse_errors": 0,
    }

    ser = open_serial()
    client = make_mqtt_client(state)
    buf = bytearray()
    last_stat = time.monotonic()

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

            if chunk:
                buf.extend(chunk)
                if len(buf) > MAX_LINE and b"\n" not in buf:
                    print(f"[serial] no newline in {len(buf)}B; framing fault, dropping buffer")
                    buf.clear()
                for line in extract_lines(buf):
                    handle_line(line, client, state)

            # Runs every pass, INCLUDING the read-timeout path -- a backlog must
            # keep flushing even when the Nucleo has gone quiet. No-ops when the
            # queue is empty or the link is down.
            drain(client, state)

            now = time.monotonic()
            if now - last_stat >= STAT_INTERVAL:
                print(f"[stat] depth={len(state['buf'])} pub={state['published']} "
                      f"dropped={state['dropped']} hb_dropped={state['hb_dropped']} "
                      f"errs={state['parse_errors']} connected={state['connected']}")
                last_stat = now
    finally:
        # Cleanup runs in IMPORTANCE order, and anything that can raise goes LAST.
        # Learned the hard way: a print() into a dead pipe (`| grep`, killed by the
        # same Ctrl-C) raised BrokenPipeError here and skipped disconnect(). No
        # DISCONNECT packet was sent, so the broker treated a planned shutdown as
        # an ungraceful death and fired the will -- the retained status flipped
        # from reason:shutdown to reason:lwt. A crash in the LOGGING path silently
        # rewrote the incident record. Logging is I/O; I/O fails; cleanup handlers
        # do their real work before they narrate it.
        try:
            # A CLEAN disconnect suppresses the will -- the broker assumes we meant
            # to leave. So we must overwrite the retained status ourselves, or it
            # says 'online' forever after a graceful Ctrl-C.
            info = client.publish(TOPIC_STATUS, STATUS_SHUTDOWN, qos=1, retain=True)
            info.wait_for_publish(timeout=2.0)   # network thread is still alive here
        except Exception:
            pass
        client.loop_stop()
        client.disconnect()          # sends DISCONNECT -- this is what suppresses the will
        try:
            ser.close()              # an unclosed port is "resource busy" on next start
        except Exception:
            pass

        # Cosmetics only, past this point. Nothing below can affect broker state.
        try:
            print(f"[mqtt] {TOPIC_STATUS} offline (retained)")
            # The buffer is memory-only: it survives a BROKER outage, not our own
            # death. Durability across a gateway restart means a disk-backed queue
            # (SQLite/WAL) -- deliberately out of scope, and worth saying out loud.
            if state["buf"]:
                print(f"[buffer] {len(state['buf'])} frames lost on exit (memory-only)")
            print(f"[mqtt] disconnected -- pub={state['published']} "
                  f"dropped={state['dropped']} hb_dropped={state['hb_dropped']} "
                  f"errs={state['parse_errors']}")
        except BrokenPipeError:
            pass                     # stdout's reader is gone; the state above is committed


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        try:
            print("\n[serial] stopped")
        except BrokenPipeError:
            pass