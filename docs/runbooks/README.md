# Runbooks

One page per alert. Each alert rule carries a `runbook_url` annotation pointing here, so whoever gets
paged lands on the fix, not a blank Slack message.

A runbook exists to cut **MTTR** (mean time to recovery): it turns "someone senior remembers how this
works" into a checklist anyone on-call can follow — diagnose in the same order every time, take the
known-good action, and know when to escalate.

| Alert | Severity | Runbook |
| --- | --- | --- |
| `FleetDeviceStale` | warning | [device-offline.md](device-offline.md) |
| `FleetChannelAnomaly` | warning | [channel-anomaly.md](channel-anomaly.md) |
| `FleetAvailabilityBelowSLO` | critical | [fleet-availability.md](fleet-availability.md) |
| `FleetIngestPipelineDown` | critical | [ingest-pipeline-down.md](ingest-pipeline-down.md) |
| `FleetHighErrorRate` | warning | [ingest-errors.md](ingest-errors.md) |
| `FleetRemediationExhausted` | critical | [remediation-exhausted.md](remediation-exhausted.md) |

**Fast links:** Grafana http://localhost:3000 · Prometheus alerts http://localhost:9090/alerts ·
targets http://localhost:9090/targets · Alertmanager http://localhost:9093 · open incidents
http://localhost:9096/incidents
