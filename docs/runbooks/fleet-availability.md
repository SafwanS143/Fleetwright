# Runbook — Fleet availability below SLO (`FleetAvailabilityBelowSLO`)

**Severity:** critical · **Owner:** platform · **SLI:** availability (≥ 95% of devices fresh)

## What it means
More than 5% of the fleet is stale at once. This is a **systemic** signal — one device going quiet can't
trip it, so the cause is almost always the shared path (broker, bridge, network), not any single device.
While this fires, per-device `FleetDeviceStale` warnings are inhibited on purpose so you get one page,
not one per device.

## Diagnose (shared path first)
1. **Is the pipeline up?** http://localhost:9090/targets — is `fleet-bridge` UP? If down, this is really
   [ingest-pipeline-down.md](ingest-pipeline-down.md); that critical inhibits this one.
2. **Is the broker up?** `docker compose ps mosquitto`; `docker compose logs mosquitto`. A broker restart
   drops every subscriber at once → fleet-wide staleness.
3. **Bridge healthy?** `docker compose logs bridge` — connected and subscribed? A crashed/looping bridge
   stops updating last-seen for everyone.
4. **Network / host.** If broker and bridge are both fine, check host resources (`docker stats`) and the
   network between gateways and the broker.

## Remediate
- Broker down → `docker compose up -d mosquitto`; gateways reconnect and flush their ring buffers.
- Bridge down/looping → `docker compose up -d bridge` (or restart); freshness recovers within one scrape.
- Confirm recovery on the availability panel; the alert resolves when ≥ 95% are fresh again and the
  incident closes.

## Escalate
If broker, bridge, and host are healthy but availability stays low, you likely have a fleet-wide
gateway/network outage upstream of the cloud — escalate to whoever owns the device network.
