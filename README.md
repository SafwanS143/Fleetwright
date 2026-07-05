# Fleetwright

**An end-to-end fleet reliability platform — from bare-metal firmware to a GitOps-managed observability cloud.**

Fleetwright instruments a fleet of edge devices (real hardware + simulated peers), streams their
telemetry through a connectivity gateway into a cloud observability stack, and applies real SRE
practice on top of it: SLIs/SLOs and error budgets, anomaly detection, symptom-based alerting,
incident lifecycle tracking, runbooks, blameless postmortems, and automated self-healing — plus an
over-the-air control path that closes the loop from cloud back down to the device.

The point of the project isn't any single component. It's **reliability across the hardware/software
boundary**: being able to stand in front of the whole chain and explain how you'd detect, diagnose,
and recover a failure at *every hop* — firmware → UART → gateway → MQTT → broker → bridge → Prometheus
→ Grafana.

---

## Why a gateway architecture?

The edge device is an **STM32 Nucleo F401RE** — a constrained microcontroller with no network stack
(USB / UART / I²C / SPI / GPIO only). It cannot talk to the cloud directly. So it sits behind a
**Raspberry Pi connectivity gateway** that does the protocol translation (UART → MQTT) and the
store-and-forward buffering.

This is not a workaround — it's the correct design, and it mirrors how a real vehicle is built: a
constrained **ECU** sits behind a **telematics control unit** that owns connectivity. The same shape
shows up across embedded fleets everywhere.

```
┌─────────────────────┐      UART       ┌──────────────────────┐      MQTT       ┌──────────────────────────┐
│  STM32 Nucleo F401RE │  JSON lines     │  Raspberry Pi gateway │  pub/sub        │  Cloud observability      │
│  ──────────────────  │ ───────────────▶│  ───────────────────  │ ───────────────▶│  ──────────────────────   │
│  MPU-6500 (IMU)  I²C │                 │  pyserial parse       │                 │  Mosquitto broker         │
│  BME280 (env)    I²C │                 │  bounded ring buffer  │                 │  MQTT→Prometheus bridge   │
│  bare-metal sampling │◀─────────────── │  paho-mqtt client     │◀─────────────── │  Prometheus + Grafana     │
│                      │   OTA cmd /cmd  │  (containerized)      │   OTA downlink  │  Isolation Forest + alerts│
└─────────────────────┘                 └──────────────────────┘                 └──────────────────────────┘
```

Full rendered diagram: [docs/architecture.md](docs/architecture.md)

---

## Repository layout

| Path           | What lives here                                                                 |
| -------------- | ------------------------------------------------------------------------------- |
| [firmware/](firmware/) | Bare-metal STM32 firmware: I²C sensor drivers, sampling loop, JSON-over-UART telemetry, OTA command handling. |
| [gateway/](gateway/)   | Raspberry Pi gateway: serial reader, store-and-forward ring buffer, MQTT publisher, containerized. |
| [cloud/](cloud/)       | Observability + reliability stack: MQTT→Prometheus bridge, Compose/k3s manifests, anomaly detection, alerting, incident store. |
| [docs/](docs/)         | Architecture diagram, runbooks, postmortems, SLO definitions.                   |
| [INTERVIEW_NOTES.md](INTERVIEW_NOTES.md) | The "defend it cold" prep — every design decision in my own words. |

---

## Tech stack

- **Firmware:** C, bare-metal STM32 (HAL), I²C, UART
- **Gateway:** Python (pyserial, paho-mqtt), Docker
- **Transport:** MQTT (Mosquitto)
- **Observability:** Prometheus, Grafana
- **Reliability:** Isolation Forest anomaly detection (scikit-learn), Slack alerting, SQLite incident store
- **Platform:** Docker Compose → k3s, Helm, ArgoCD (GitOps), Terraform, GitHub Actions

---

## Architecture

> _Placeholder — filled in as the build progresses. The end-to-end diagram lives in
> [docs/architecture.md](docs/architecture.md); this section will carry the narrative walkthrough of
> each hop and its failure modes._

## SLIs / SLOs / Error budgets

> _Placeholder — to be defined in Chunk 18. Three explicit SLIs are planned:_
> - **Telemetry freshness** — how recently the last sample arrived (`now − last_seen`).
> - **Availability** — fraction of devices reporting within the freshness window.
> - **Error rate** — malformed / dropped messages as a fraction of total.
>
> _Each will carry a stated SLO target, a justification, and an error-budget policy._

## Runbooks

> _Placeholder — to be written in Chunk 25. Planned: "device offline → diagnose & recover",
> "vibration anomaly → interpret & act". They'll live in [docs/](docs/)._

## Postmortems

> _Placeholder — to be written in Chunk 29. One full blameless postmortem for a simulated incident
> (timeline, impact, root cause, what worked, action items)._

---

## Status

🚧 **In active development.** Building chunk-by-chunk; see the layout above for what's wired up so far.
