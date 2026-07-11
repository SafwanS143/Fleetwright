#!/usr/bin/env python3
"""
gateway/serial_reader.py  --  Chunk 14 (containerized gateway)

Reads NDJSON telemetry from the Nucleo over serial, buffers to newline, parses each
line as JSON, and publishes to Mosquitto. Publishes a retained connection-state
message on connect and registers a Last-Will so an ungraceful death marks us offline.

Run on the Pi:  docker compose up -d   (or python3 gateway/serial_reader.py)
"""
import json
import time
import serial
import os
import signal
import sys
from collections import deque
import paho.mqtt.client as mqtt


def log(*args, **kwargs):
    """Wraps print() because stdout writes can fail. When piped (`| grep`) and the
    reader dies, the next write raises BrokenPipeError; inside a paho callback that
    would abort disconnect() mid-packet, fire the will, and log a planned shutdown as
    an ungraceful death. (Under Docker, PYTHONUNBUFFERED=1 keeps `docker logs` live.)"""
    try:
        print(*args, **kwargs)
    except BrokenPipeError:
        pass


def _on_sigterm(signum, frame):
    """Turn SIGTERM into KeyboardInterrupt so the finally block runs (writes
    reason:shutdown). PEP 475 interrupts a blocking ser.read()/sleep to deliver it."""
    raise KeyboardInterrupt


# Prefer the stable by-id symlink over /dev/ttyACM0 (ACM numbering changes on replug).
# Works in-container only because /dev is bind-mounted from the host, where udev makes
# the symlink; `--device` would freeze the (major,minor) node and die on a replug.
PORT = os.environ.get("FLEET_SERIAL_PORT", "/dev/ttyACM0")
BAUD = 115200
READ_TIMEOUT = 1.0             # seconds; read() returns after this even with no data
RECONNECT_DELAY = 2.0          # seconds between reconnect attempts
MAX_LINE = 4096                # a real telemetry line is ~200B; larger w/o a newline = framing fault

# Env-driven so the broker can move (Pi -> cloud, Chunk 16) with no code change. Host
# networking today: 127.0.0.1 reaches the Pi's Mosquitto; a Compose service name under bridge.
BROKER_HOST = os.environ.get("FLEET_MQTT_HOST", "localhost")
BROKER_PORT = int(os.environ.get("FLEET_MQTT_PORT", "1883"))

# Identity must exist before connect: the will topic ships inside the CONNECT packet. So it's config, not payload.
DEVICE_ID = os.environ.get("FLEET_DEVICE_ID", "fleet-edge-01")

TOPIC_TELEMETRY = f"fleet/{DEVICE_ID}/telemetry"
TOPIC_HEARTBEAT = f"fleet/{DEVICE_ID}/heartbeat"
TOPIC_STATUS    = f"fleet/{DEVICE_ID}/status"   # retained connection state ONLY

# 30s => broker declares us dead after ~45s (1.5x keepalive). That's the MTTD floor for
# gateway death: nothing downstream knows sooner. Chosen against the availability SLO.
MQTT_KEEPALIVE = 30

STATUS_ONLINE   = json.dumps({"id": DEVICE_ID, "state": "online"})
STATUS_LWT      = json.dumps({"id": DEVICE_ID, "state": "offline", "reason": "lwt"})
STATUS_SHUTDOWN = json.dumps({"id": DEVICE_ID, "state": "offline", "reason": "shutdown"})

# --- Chunk 13: store-and-forward ---------------------------------------------
# Bounded: an unbounded queue trades a small capped loss for an OOM kill that loses the
# queue AND the gateway. Bounded = loss is capped, counted, logged (backpressure).
# 6000 frames @ 10 Hz x ~230B ~= 10 min / ~1.4 MB: sized for a broker restart or blip,
# not an all-day outage (Prometheus stamps at scrape time, so a late flush can't backfill).
# If a cgroup mem_limit is ever set it must exceed BUFFER_MAXLEN*frame_size + interpreter,
# or the OOM killer reintroduces the exact failure the bound prevents.
BUFFER_MAXLEN = int(os.environ.get("FLEET_BUFFER_MAXLEN", "6000"))

# Cap frames/pass so a flush never starves ser.read() (which would overrun the kernel's
# serial buffer -- losing live data to save dead data). ~450 msg/s drain vs 10 msg/s ingest.
DRAIN_BATCH = int(os.environ.get("FLEET_DRAIN_BATCH", "50"))

STAT_INTERVAL = 10.0           # seconds between [stat] lines


def open_serial() -> serial.Serial:
    """Block until the port opens; retries forever so the gateway can start before the
    Nucleo is plugged in. Correct only because /dev is the host's live devtmpfs, so this
    loop sees udev recreate the by-id symlink on replug (a `--device` node would not)."""
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=READ_TIMEOUT)
            # Flush pre-connect bytes: the first are usually a mid-line fragment that
            # won't parse. Freshness over stale backlog -- ties to the freshness SLI.
            ser.reset_input_buffer()
            log(f"[serial] connected {PORT} @ {BAUD}")
            return ser
        except OSError as e:   # serial.SerialException subclasses OSError
            # EPERM (vs ENOENT) = node exists but the device cgroup denied the open:
            # a container-config fault, not wiring; host chmod won't fix it.
            log(f"[serial] {PORT} not ready ({e}); retry in {RECONNECT_DELAY}s")
            time.sleep(RECONNECT_DELAY)


def on_connect(client, userdata, connect_flags, reason_code, properties):
    """Fires on every (re)connect, so the retained 'online' publish lives here: after a
    blip we must overwrite our own retained 'offline' will, not leave it serving stale."""
    if reason_code != 0:
        log(f"[mqtt] connect failed: {reason_code}")
        return
    userdata["connected"] = True
    log(f"[mqtt] connected to {BROKER_HOST}:{BROKER_PORT} "
        f"(buffered={len(userdata['buf'])})")
    # QoS 1 + retain: state must arrive, and must be there for late subscribers.
    client.publish(TOPIC_STATUS, STATUS_ONLINE, qos=1, retain=True)
    log(f"[mqtt] {TOPIC_STATUS} online (retained)")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    """The only place `connected` goes False -- one source of truth for link state that
    everything downstream (drain, heartbeat) reads instead of asking paho."""
    userdata["connected"] = False
    log(f"[mqtt] disconnected ({reason_code}); buffering telemetry")


def make_mqtt_client(state: dict) -> mqtt.Client:
    """Build the client. loop_start() runs paho I/O on its own thread so the serial read
    loop keeps control. `state` is passed as userdata so callbacks share link state."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=state)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    # Will ships inside CONNECT, so register before connecting. Retained so a subscriber
    # arriving after our death isn't served the stale 'online'.
    client.will_set(TOPIC_STATUS, STATUS_LWT, qos=1, retain=True)

    # connect_async + loop_start: connect happens on paho's thread and retries, so a
    # broker down at boot doesn't crash (and crash-loop) the container under `restart:`.
    client.connect_async(BROKER_HOST, BROKER_PORT, MQTT_KEEPALIVE)
    client.loop_start()
    log(f"[mqtt] connecting to {BROKER_HOST}:{BROKER_PORT} (keepalive {MQTT_KEEPALIVE}s)")
    return client


def extract_lines(buf: bytearray):
    """Yield each complete newline-terminated line in order; the trailing partial line
    stays in buf until the rest arrives on a later read."""
    while b"\n" in buf:
        idx = buf.index(b"\n")
        line = bytes(buf[:idx]).strip()
        del buf[:idx + 1]         # drop the line AND its newline; keep the tail
        if line:
            yield line


def enqueue(state: dict, payload: str):
    """Append to the ring buffer, counting evictions first -- deque(maxlen) drops the
    oldest silently, and a silent drop is the bug this chunk fixes. Drop-oldest is right
    for telemetry: the newest sample is the most valuable (an audit log would drop newest)."""
    buf = state["buf"]
    if len(buf) == buf.maxlen:
        state["dropped"] += 1
        if state["dropped"] == 1 or state["dropped"] % 500 == 0:
            log(f"[buffer] FULL -- evicted {state['dropped']} oldest frames")
    buf.append(payload)


def drain(client: mqtt.Client, state: dict) -> int:
    """The only publisher of telemetry. Pops FIFO up to DRAIN_BATCH/pass, and only after
    paho accepts a frame -- on failure it stays at the head and retries, so seq stays
    monotonic. Note QoS 0 success = accepted into paho's socket buffer, NOT broker-received;
    this buffer covers DISCONNECTED loss, in-flight loss is caught by seq-gap detection."""
    buf = state["buf"]
    # No per-frame log: at 10 Hz it buries [stat], fills disk, and could starve the read
    # loop while flushing a 6000-frame backlog. Only report when catching up.
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
        log(f"[drain] flushed {sent}, {len(buf)} remaining")
    return sent


def publish_heartbeat(client: mqtt.Client, state: dict, payload: str):
    """Heartbeats are never buffered: freshness is `now - last_received`, so replaying a
    stale heartbeat after an outage would fake availability. QoS 0 (not 1) for the same
    reason -- paho queues and replays QoS>0 publishes on reconnect, the replay we forbid."""
    if not state["connected"]:
        state["hb_dropped"] += 1
        return
    client.publish(TOPIC_HEARTBEAT, payload, qos=0, retain=False)
    log(f"[mqtt] {TOPIC_HEARTBEAT} {payload}")


def handle_line(raw: bytes, client: mqtt.Client, state: dict):
    """Parse one line as JSON and route it. Malformed lines are counted and dropped
    before the buffer or MQTT, so framing/parse faults stay upstream of the gateway."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        state["parse_errors"] += 1
        log(f"[parse] skipping bad line: {raw!r}")
        return

    payload = json.dumps(msg)

    # Heartbeat gets its own topic (device liveness != link liveness); publishing it to
    # /status would clobber the retained will marker.
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
                log(f"[serial] lost device ({e}); reconnecting")
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
                    log(f"[serial] no newline in {len(buf)}B; framing fault, dropping buffer")
                    buf.clear()
                for line in extract_lines(buf):
                    handle_line(line, client, state)

            # Every pass, including the read-timeout path, so a backlog keeps flushing
            # when the Nucleo goes quiet. No-ops when the queue is empty or link is down.
            drain(client, state)

            now = time.monotonic()
            if now - last_stat >= STAT_INTERVAL:
                log(f"[stat] depth={len(state['buf'])} pub={state['published']} "
                    f"dropped={state['dropped']} hb_dropped={state['hb_dropped']} "
                    f"errs={state['parse_errors']} connected={state['connected']}")
                last_stat = now
    finally:
        # Cleanup in importance order, raisy work last: a print() into a dead pipe once
        # raised here and skipped disconnect(), so the will fired and the retained status
        # flipped shutdown->lwt. Also on a clock -- `docker stop` SIGKILLs after the grace
        # period, so every bounded wait below (2.0s) must sum to well under it.
        try:
            # A clean disconnect suppresses the will, so overwrite the retained status
            # ourselves or it says 'online' forever after a graceful Ctrl-C.
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

        # Cosmetics only past here -- nothing below can affect broker state.
        log(f"[mqtt] {TOPIC_STATUS} offline (retained)")
        # Buffer is memory-only: survives a broker outage, not our own restart. Durability
        # across a gateway restart needs a disk-backed queue on a volume -- out of scope.
        if state["buf"]:
            log(f"[buffer] {len(state['buf'])} frames lost on exit (memory-only)")
        log(f"[mqtt] disconnected -- pub={state['published']} "
            f"dropped={state['dropped']} hb_dropped={state['hb_dropped']} "
            f"errs={state['parse_errors']}")


if __name__ == "__main__":
    # Register before main() so an early `docker stop` still unwinds. As PID 1 there's no
    # default handler -- without this, SIGTERM is dropped and docker SIGKILLs us after the
    # grace period, skipping the finally block and firing the will.
    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        main()
    except KeyboardInterrupt:
        log("\n[serial] stopped")
    finally:
        # CPython flushes stdout at exit, after our code runs; if the pipe is dead that
        # raises uncatchably. Redirect the fd to /dev/null so the final flush is harmless.
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
