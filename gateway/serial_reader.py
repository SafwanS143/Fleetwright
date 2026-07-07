#!/usr/bin/env python3
"""
gateway/serial_reader.py  --  Chunk 10 (Pi reads serial)

Reads NDJSON telemetry from the Nucleo over the ST-LINK virtual COM port,
buffers raw bytes until a newline, parses each complete line as JSON, and
reconnects automatically if the Nucleo is unplugged/replugged.

Run on the Pi:  python3 gateway/serial_reader.py   (Ctrl-C to stop)
"""
import json
import time
import serial
from serial import SerialException
import os

# Prefer the stable by-id symlink over /dev/ttyACM0 -- ACM numbering can change
# on replug, the by-id path does not. Find yours with:
#   ls -l /dev/serial/by-id/
PORT = os.environ.get("FLEET_SERIAL_PORT", "/dev/ttyACM0")
BAUD = 115200
READ_TIMEOUT = 1.0             # seconds; read() returns after this even with no data
RECONNECT_DELAY = 2.0          # seconds between reconnect attempts


def open_serial() -> serial.Serial:
    """Block until the port opens, then return the handle. Retries forever so
    the gateway can be started before the Nucleo is plugged in."""
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=READ_TIMEOUT)
            print(f"[serial] connected {PORT} @ {BAUD}")
            return ser
        except (SerialException, OSError) as e:
            print(f"[serial] {PORT} not ready ({e}); retry in {RECONNECT_DELAY}s")
            time.sleep(RECONNECT_DELAY)


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


def handle_line(raw: bytes):
    """Parse one complete line as JSON. A malformed line is logged and dropped,
    not fatal -- a single corrupt frame must not take the gateway down."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[parse] skipping bad line: {raw!r}")
        return
    print(msg)


def main():
    ser = open_serial()
    buf = bytearray()
    while True:
        try:
            chunk = ser.read(256)     # up to 256 bytes, or fewer after timeout
        except (SerialException, OSError) as e:
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
        for line in extract_lines(buf):
            handle_line(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[serial] stopped")