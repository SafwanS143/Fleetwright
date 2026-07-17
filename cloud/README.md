# cloud/

Cloud-side observability and reliability stack.

- **MQTT→Prometheus bridge** — subscribes to telemetry, exposes `/metrics` (counters, gauges,
  histograms) for Prometheus to scrape.
- **Mosquitto** — MQTT broker (QoS, retained status, Last-Will-and-Testament).
- **Prometheus + Grafana** — scraping, dashboards, per-device + fleet-overview boards.
- **Anomaly detection** — two per-channel detectors (robust z-score / MAD baseline + Isolation Forest)
  scored in parallel; Chunk 21 evaluates them and picks one for the alerting path.
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

## Anomaly detection (Chunk 20)

The [`anomaly/`](anomaly/) service is a **second** MQTT subscriber (independent of the bridge — that's
the point of pub/sub) that runs **two** detectors per channel: a robust z-score / MAD baseline and an
Isolation Forest. Both fit once on a warm-up window (~300 samples ≈ 30s at 10 Hz), then score live and
export `fleet_anomaly_score` / `fleet_anomaly_flag` (normalized so `1.0` is each detector's trip line),
plus the statistical band (`fleet_channel_baseline_lower/upper`). Neither is wired to alerting yet —
Chunk 21 evaluates them on false positives / detection latency and picks one.

- Dashboard: Grafana → **Fleet → Fleet — Anomaly Detection**
- See both detectors trip: warm up on clean data, then inject a perturbation on one channel:
  `python tools/fake_telemetry.py --devices 3 --anomaly temp`
  (waits ~35s so the baseline fits first, then toggles the anomaly on/off every 15s)
- Detector knobs (baseline size, z-score sigma, IF contamination) are env vars on the `anomaly` service
  in [`docker-compose.yml`](docker-compose.yml) — Chunk 21 tuning is a config change, not a rebuild.

## Detector evaluation → pick one (Chunk 21)

[`anomaly/evaluate.py`](anomaly/evaluate.py) scores **both** detectors offline (no Pi, no broker) on
labelled synthetic data from the same generator, measuring false-positive rate on clean data and
detection + latency on injected faults, then sweeps each one's sensitivity knob.

```bash
python anomaly/evaluate.py          # prints per-channel tables + the decision summary
```

**Decision: the z-score / MAD baseline enters the alerting path** — 0.000 FP vs the Isolation Forest's
5.6% (17% on humidity), which on a 10 Hz stream is the deciding axis; both detect clear faults at
~one-sample latency. Full evidence, limitations (periodic-channel misses, regime-change false positives),
the contamination/threshold tradeoff, and when IF *would* win (multivariate joint anomalies) are written
up in [`docs/detector-evaluation.md`](../docs/detector-evaluation.md). The chosen detector is recorded as
`ALERTING_DETECTOR` in [`anomaly/anomaly_service.py`](anomaly/anomaly_service.py); Chunk 22 wires its flag
to paging.

## SLIs / SLOs (Chunk 18)

[`prometheus/rules/fleet_slos.yml`](prometheus/rules/fleet_slos.yml) defines the SLIs as `fleet:...`
recording rules (freshness, availability, ingest error rate) and the SLO targets as alert thresholds.
Targets and rationale are documented in the [root README](../README.md#slis--slos--error-budgets).

- Recording rules / SLI series: http://localhost:9090/graph (e.g. `fleet:availability:ratio`)
- SLO alerts (pending/firing): http://localhost:9090/alerts

There's no Alertmanager yet — breaches only surface on the Prometheus **/alerts** page. Routing them to
Slack with severity, dedup, and suppression is Chunk 22. After editing the rules, reload Prometheus:
`docker compose kill -s SIGHUP prometheus` (or restart the container).
