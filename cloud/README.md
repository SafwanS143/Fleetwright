# cloud/

Cloud-side observability and reliability stack.

- **MQTT→Prometheus bridge** — subscribes to telemetry, exposes `/metrics` (counters, gauges,
  histograms) for Prometheus to scrape.
- **Mosquitto** — MQTT broker (QoS, retained status, Last-Will-and-Testament).
- **Prometheus + Grafana** — scraping, dashboards, per-device + fleet-overview boards.
- **Isolation Forest** — per-channel unsupervised anomaly detection (scikit-learn).
- **Alerting** — severity-routed Slack alerts on SLO breach / anomaly, with dedup + suppression.
- **Incident store** — SQLite, open-on-trip / close-on-recovery, with a Grafana timeline.
- **Self-healing** — observe → diff → act remediation loop.

Stood up first on **Docker Compose**, then migrated to **k3s** (Deployments/Services, liveness/
readiness probes), packaged with **Helm**, and synced via **ArgoCD** (GitOps).

## Running the stack (Chunk 16)

```bash
cd cloud
docker compose up -d --build          # mosquitto + bridge + prometheus + grafana
```

- Grafana: http://localhost:3000 (admin/admin) → dashboard **Fleet → Fleet — Per-Device**
- Prometheus targets: http://localhost:9090/targets (the `fleet-bridge` job should be UP)
- No Pi on this box? Feed synthetic data:
  `pip install paho-mqtt && python tools/fake_telemetry.py --devices 3 --drop 0.02`
- Real Pi gateway: point it at this host — `FLEET_BROKER_HOST=<laptop-ip>` — and it publishes into the same broker.

Layout: [`docker-compose.yml`](docker-compose.yml), [`mosquitto/`](mosquitto/),
[`prometheus/`](prometheus/), [`grafana/provisioning`](grafana/provisioning) (datasource + dashboard
provider), [`grafana/dashboards`](grafana/dashboards) (the per-device board, Chunk 17).

## SLIs / SLOs (Chunk 18)

[`prometheus/rules/fleet_slos.yml`](prometheus/rules/fleet_slos.yml) defines the SLIs as `fleet:...`
recording rules (freshness, availability, ingest error rate) and the SLO targets as alert thresholds.
Targets and rationale are documented in the [root README](../README.md#slis--slos--error-budgets).

- Recording rules / SLI series: http://localhost:9090/graph (e.g. `fleet:availability:ratio`)
- SLO alerts (pending/firing): http://localhost:9090/alerts

There's no Alertmanager yet — breaches only surface on the Prometheus **/alerts** page. Routing them to
Slack with severity, dedup, and suppression is Chunk 22. After editing the rules, reload Prometheus:
`docker compose kill -s SIGHUP prometheus` (or restart the container).
