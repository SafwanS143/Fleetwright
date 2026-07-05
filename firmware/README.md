# firmware/

Bare-metal firmware for the **STM32 Nucleo F401RE**.

Reads the **MPU-6500** (IMU, I²C `0x68`) and **BME280** (temp/humidity/pressure, I²C `0x76`) off a
shared I²C bus, samples at a fixed rate off a SysTick/timer tick (non-blocking), frames each sample as
a newline-delimited JSON line, and streams it over UART to the gateway. Also handles the OTA downlink:
parses inbound config commands and acks them back up the telemetry path.

**Toolchain:** STM32CubeIDE (or PlatformIO), flashed over the on-board ST-LINK.

One newline-delimited JSON object per sample, streamed at 10 Hz over USART2 (the
ST-LINK virtual COM port, 115200 8N1). Values are in engineering units:

```
{"id":"fleet-edge-01","seq":42,"ts":4200,"temp":26.70,"humidity":54.63,
 "pressure":971.14,"ax":0.026,"ay":0.009,"az":1.001,"gx":0.73,"gy":-0.90,"gz":0.06}
```

- `seq` — monotonic message counter (lets the gateway detect gaps/packet loss)
- `ts` — device uptime in ms (`HAL_GetTick`)
- `temp` °C, `humidity` %RH, `pressure` hPa, `ax..az` g, `gx..gz` °/s

> Floats are formatted without newlib-nano's `%f` (avoids pulling float `printf`
> into the image); see `fmt_fixed()` in `Core/Src/main.c`.
