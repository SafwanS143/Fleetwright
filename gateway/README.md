# gateway/

Connectivity gateway, runs on a **Raspberry Pi**.

Reads JSON-line telemetry off the serial port (`/dev/ttyACM0`) with `pyserial`, buffering partial
reads until a newline and surviving Nucleo unplug/replug. Publishes to MQTT
(`fleet/<id>/telemetry`) with `paho-mqtt`. A **bounded ring buffer** provides store-and-forward: when
the broker is unreachable, telemetry is buffered and flushed on reconnect (bounded for memory safety /
backpressure on a 2 GB Pi). Subscribes to `fleet/<id>/cmd` and relays OTA commands down to the Nucleo
over UART.

Containerized, with the serial device passed through into the container.

**Stack:** Python, `pyserial`, `paho-mqtt`, Docker.

> Scaffold only — serial reader, ring buffer, and MQTT client land in Phase 2 (Chunks 10–14).
