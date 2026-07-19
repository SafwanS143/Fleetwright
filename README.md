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
│                      │   OTA cmd /cmd  │  (containerized)      │   OTA downlink  │  anomaly detect + alerts  │
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
- **Reliability:** two-detector anomaly detection (robust z-score + Isolation Forest, scikit-learn), Slack alerting, SQLite incident store
- **Platform:** Docker Compose → k3s, Helm, ArgoCD (GitOps), Terraform, GitHub Actions

---

## Architecture

The rendered end-to-end diagram and the **troubleshooting spine** — every hop from sensor to Grafana
with its failure mode and detection signal — live in [docs/architecture.md](docs/architecture.md). The
short version: bare-metal firmware frames sensor readings as newline-delimited JSON over UART; a
containerized Raspberry Pi gateway parses, buffers, and republishes them over MQTT; and the cloud stack
scrapes, dashboards, detects anomalies, alerts, and records incidents.

## SLIs / SLOs / Error budgets

Three service-level indicators, each recorded as a `fleet:...` series and enforced by an SLO threshold
in [cloud/prometheus/rules/fleet_slos.yml](cloud/prometheus/rules/fleet_slos.yml).

| SLI | Definition (PromQL) | SLO target | Why this number |
| --- | --- | --- | --- |
| **Telemetry freshness** (per device) | `time() − fleet_last_message_timestamp_seconds` | fresh (`< 10s`) ≥ 99% of the time | At 10 Hz the nominal gap is 0.1s, so 10s ≈ 100 missed messages — long enough to ride out a reconnect or gateway restart, short enough to catch a real outage fast. |
| **Fleet availability** | `avg(fleet:device_up:bool)` — fraction of devices currently fresh | ≥ 95% of the fleet fresh | Tolerates a single device blipping offline without paging; a wider dip means a systemic problem (broker, gateway, network), not one device. |
| **Ingest error rate** | malformed frames ÷ frames received, over 5m | `< 0.1%` (99.9% parse cleanly) | Machine-generated NDJSON should essentially always parse; anything above 0.1% points at framing corruption on the UART/serial hop, not normal operation. |

**Not an SLO objective: completeness.** Packet loss (sequence-number gaps) is tracked on the dashboard
but deliberately kept out of the error-rate SLI. Telemetry publishes at QoS 0, which trades delivery
guarantees for liveness — so a dropped sample in a 10 Hz stream is expected, not a budget-consuming error.

**Error budget.** Each SLO implies a budget of `1 − target`: 99% freshness ≈ 7.2 h/device/month of
allowed staleness; 99.9% clean-parse ≈ 0.1% of frames. The budget is the room to absorb reconnects,
deploys, and blips before a target is breached — when it runs out, reliability work takes priority over
new features. These thresholds feed the alert rules, which route by severity through Alertmanager to
Slack + the incident store.

**Golden signals.** How these metrics map onto the four golden signals (latency / traffic / errors /
saturation) — and which signal is only partially covered and why — is written up in
[docs/golden-signals.md](docs/golden-signals.md).

## Runbooks

One page per alert in [docs/runbooks/](docs/runbooks/), linked from each alert's `runbook_url` so
whoever is paged lands on the fix. Covers device offline, channel anomaly (fault vs. regime change),
fleet availability, ingest pipeline down, and ingest errors — each with diagnose-in-order steps, the
known-good remediation, and when to escalate.

## Postmortems

> _Upcoming: one full blameless postmortem for a simulated incident (timeline, impact, root cause, what
> worked, action items), in [docs/](docs/)._

---

## Status

🚧 **In active development.** Building chunk-by-chunk; see the layout above for what's wired up so far.
