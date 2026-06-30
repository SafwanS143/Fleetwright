# firmware/

Bare-metal firmware for the **STM32 Nucleo F401RE**.

Reads the **MPU-6050** (IMU, I²C `0x68`) and **BME280** (temp/humidity/pressure, I²C `0x76`) off a
shared I²C bus, samples at a fixed rate off a SysTick/timer tick (non-blocking), frames each sample as
a newline-delimited JSON line, and streams it over UART to the gateway. Also handles the OTA downlink:
parses inbound config commands and acks them back up the telemetry path.

**Toolchain:** STM32CubeIDE (or PlatformIO), flashed over the on-board ST-LINK.

```
{ "id": "...", "seq": 0, "ts": 0, "temp": 0.0, "humidity": 0.0,
  "pressure": 0.0, "ax": 0, "ay": 0, "az": 0, "gx": 0, "gy": 0, "gz": 0 }
```

> Scaffold only — drivers and sampling loop land in Phase 1 (Chunks 4–9).
