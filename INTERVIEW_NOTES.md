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

## Chunk 3 — Nucleo toolchain

_What ST-LINK is; bare-metal vs running an OS; how flashing writes to MCU flash._

1 - What is ST-LINK

ST-LINK is a seperate microcontroller on the STM32 that allows for the STM32CubeIDE to reach the F401RE chip. It has it's own translator and communicates thru Serial Wire Debug (SWD). This is how you can debug and run code on the chip itself.

2 - Bare metal is no OS layer underneath your code, your code owns the entire chip. OS means the OS runs processes, scheduling, manages memory etc.

3 - Flashing goes thru the ST-LINK, unlocks the flash controller, erases the flash memory it needs to erase, and pastes in the binary of your code.

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
