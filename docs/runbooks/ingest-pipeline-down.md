# Runbook — Ingest pipeline down (`FleetIngestPipelineDown`)

**Severity:** critical · **Owner:** platform · **SLI:** pipeline (operational, not one of the three SLOs)

## What it means
Prometheus can't scrape the MQTT→Prometheus bridge (`up{job="fleet-bridge"} == 0`). Every other SLI —
freshness, availability, error rate — is now **blind**, because they're all derived from metrics the
bridge exports. This is the highest-signal alert in the system: it inhibits the freshness/availability/
anomaly alerts, since those would all fire for a reason you can't act on per-device.

## Diagnose
1. **Bridge container.** `docker compose ps bridge`; `docker compose logs bridge`.
   - Crash-looping → read the traceback (bad broker host, dependency, OOM).
   - Up but unscraped → step 2.
2. **Can Prometheus reach it?** http://localhost:9090/targets → `fleet-bridge` error text (connection
   refused, timeout, DNS). Confirm the service name/port in `prometheus.yml` matches compose.
3. **Is the bridge actually serving metrics?** From another container/host on the network,
   `curl http://bridge:8000/metrics`. Empty/refused → the exporter thread didn't start; check logs.

## Remediate
- Bridge down/looping → `docker compose up -d --build bridge`.
- Wrong target config → fix `prometheus/prometheus.yml`, then `docker compose kill -s SIGHUP prometheus`.
- Recovery shows as `fleet-bridge` returning to UP on the targets page; the inhibited alerts clear on
  their own once real data flows again.

## Note
Bridge death is deliberately **not** a device-liveness signal — it's a separate fault domain caught by
Prometheus's own `up` metric, which is why this alert exists instead of relying on freshness.
