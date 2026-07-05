# Interview Notes — "Defend it cold"

This file is the actual point of the project. For every chunk, I write — **in my own words** — the
answers to the design questions an interviewer would probe. If the code runs but I can't answer these
cold, the chunk isn't done.

Rule: write the answer _after_ building, from memory, then check it. The gaps you find while writing
are exactly the gaps an interviewer will find.

---

## Chunk 1 — Repo, architecture, the gateway pattern

**Q: Why the gateway pattern? Why not have the device talk to the cloud directly?**

> _(your answer here — the 30-second systems-thinking pitch. Hit: F401RE has no network stack
> (USB/UART/I²C/SPI/GPIO only); a constrained MCU behind a connectivity gateway is the correct design;
> it's exactly how an ECU sits behind a telematics unit in a vehicle.)_

**Q: Give the 30-second end-to-end architecture pitch out loud.**

> _(your answer here — say it out loud until it's smooth, then write the version that came out.)_

--- We need a gateway here because the STM32 F401RE doesn't have a network stack, it only communicates thru SPI/I2C/GPIO/UART. It needs something in between like a Raspberry Pi (in this case) to communicate with it and upload it to the cloud, since the Pi has connectivity. This is very similar to an ECU connecting to a telematics unit.

> Sections below are stubs — fill each in as you complete the chunk.

## Chunk 2 — Pi bring-up

_TCP vs UDP for SSH (why TCP, port 22); what systemd is; systemctl/journalctl; what DNS resolves._

1 - TCP vs UDP:
TCP ensures all packets are sent in order, and resends if missing. This is slower but necessary for ssh commands, compared to UDP which is faster but doesn't guarantee packet integrity

2 - What systemd is:
systemd is the startup control in the OS of the Raspberry Pi. It controls all the services that run, including SSH. If bugs are encountered, then the logs for systemd will greatly benefit debugging (systemctl - logs for SSH status/journalctl - actual SSH instructions ran)

3 - The DNS resolves the issue of remembering the IP of something. For example fleet-gw is easy to remember but 192.193.13.27 is hard. The DNS solves that by tracking and translating IPs to links. The mDNS is the local network version of that, where a device would respond when scanned for a certain name.

## Firmware defends — tiering note

_Reweighted 2026-07-04: Tesla SRE interviews probe systems/reliability reasoning, not firmware
trivia. Chunks 3-9 below are tiered so I know exactly how much to internalize — Interview-critical
gets full depth, One-liner gets a single sentence, and low-signal register/math/formula detail is
either compressed or dropped. Chunks 1-2 are untouched (already right-sized)._

**Tier 1 — Interview-critical, full depth, wherever it lives:**

1. Gateway pattern — Chunk 1 above (constrained MCU with no networking behind a connectivity
   gateway = ECU behind a telematics unit).
2. Heartbeat + sequence numbers — Chunk 9 below (seq# → packet-loss detection; heartbeat →
   telemetry-freshness SLI).
3. Firmware as hop-one of the telemetry troubleshooting chain — Chunk 8 below (is it the sensor,
   the I²C read, or the UART framing that broke?).

## Chunk 3 — Nucleo toolchain

**One-liner tier:**

- What ST-LINK is: a separate on-board debug chip that talks SWD to the F401RE so STM32CubeIDE can
  flash and debug it.
- Bare-metal vs OS: bare-metal means my code owns the whole chip with nothing underneath it; an OS
  would add process scheduling and memory management in between.
- Flashing: goes through the ST-LINK, which unlocks the flash controller, erases what it needs to,
  and writes the binary in.

## Chunk 4 — I²C bring-up

**One-liner tier:**

- What I²C is: a two-wire (SDA/SCL) master/slave bus using 7-bit addressing.
- WHO_AM_I as a part-identity check: my "MPU-6050" breakout returned 0x70, not 0x68 — it's
  a relabeled MPU-6500. Reading the ID register caught a silent BOM substitution before it
  could confuse later debugging (register-map compatible for what we do, so no code impact).

  I2C is a 2 wire communication interface between a master (STM32) and a slave (sensors), talking synchronously thru the SCL signal, with data going thru the SDA signal.

## Chunk 5 — Read the IMU

**One-liner tier:**

- Why configure before reading: the sensor's scale/mode registers have to be set before raw
  readings mean anything.

  The sensor is set to SLEEP mode by default, and the raw values are in terms of g (9.81), so the sensor must be turned on and scaled, as do all sensors after purchase.

## Chunk 6 — BME280 on the same bus

**One-liner tier:**

- Two devices, one I²C bus: they coexist fine since each has its own 7-bit address, no extra wiring
  needed.

## Chunk 7 — Non-blocking sampling

**One-liner tier:**

- SysTick: a hardware timer interrupt used to drive periodic sampling without blocking the main
  loop.
- Why non-blocking matters: a blocking delay would stall sensor reads and desync sampling from real
  time.

## Chunk 8 — JSON-line telemetry over UART

**One-liner tier:**

- UART vs I²C: UART is asynchronous and point-to-point with no shared clock line; I²C is a
  synchronous multi-device bus.

**Interview-critical tier:**

- Why newline-delimited JSON, and why this is hop-one of the troubleshooting chain: if telemetry
  stops, the newline-delimited framing is the first thing that tells me whether the fault is the
  sensor, the I²C read, or UART framing itself — a corrupted/missing delimiter points at UART, a
  bad value points upstream at the read or the sensor.

## Chunk 9 — Heartbeat + sequence numbers

_Why seq# (packet-loss detection); why heartbeat (basis of the freshness SLI, liveness ≠ payload)._

<!-- Phases 2-7 stubs added as you reach them. -->
