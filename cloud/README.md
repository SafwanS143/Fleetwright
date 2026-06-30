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

> Scaffold only — bridge and stack land in Phase 3 onward (Chunk 15+).
