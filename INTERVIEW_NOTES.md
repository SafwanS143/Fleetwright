# Interview Notes — "Defend it cold"

This file is the actual point of the project. For every chunk, I write — **in my own words** — the
answers to the design questions an interviewer would probe. If the code runs but I can't answer these
cold, the chunk isn't done.

Rule: write the answer *after* building, from memory, then check it. The gaps you find while writing
are exactly the gaps an interviewer will find.

---

## Chunk 1 — Repo, architecture, the gateway pattern

**Q: Why the gateway pattern? Why not have the device talk to the cloud directly?**

> _(your answer here — the 30-second systems-thinking pitch. Hit: F401RE has no network stack
> (USB/UART/I²C/SPI/GPIO only); a constrained MCU behind a connectivity gateway is the correct design;
> it's exactly how an ECU sits behind a telematics unit in a vehicle.)_

**Q: Give the 30-second end-to-end architecture pitch out loud.**

> _(your answer here — say it out loud until it's smooth, then write the version that came out.)_

---

> Sections below are stubs — fill each in as you complete the chunk.

## Chunk 2 — Pi bring-up
_TCP vs UDP for SSH (why TCP, port 22); what systemd is; systemctl/journalctl; what DNS resolves._

## Chunk 3 — Nucleo toolchain
_What ST-LINK is; bare-metal vs running an OS; how flashing writes to MCU flash._

## Chunk 4 — I²C bring-up
_What I²C is (SDA/SCL, master/slave, 7-bit addressing); why pull-ups (open-drain); why MPU @ 0x68._

## Chunk 5 — Read the IMU
_Two's-complement 16-bit registers; sensitivity scale → physical units; why configure before reading._

## Chunk 6 — BME280 on the same bus
_Two devices on one I²C bus (different addresses); why raw BME readings need compensation._

## Chunk 7 — Non-blocking sampling
_SysTick; why non-blocking timing matters; RTOS background (context switching, stacks, MSP/PSP)._

## Chunk 8 — JSON-line telemetry over UART
_UART vs I²C (async, no clock line, point-to-point); baud rate; why newline-delimited JSON._

## Chunk 9 — Heartbeat + sequence numbers
_Why seq# (packet-loss detection); why heartbeat (basis of the freshness SLI, liveness ≠ payload)._

<!-- Phases 2-7 stubs added as you reach them. -->
