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

After editing the rules, reload Prometheus: `docker compose kill -s SIGHUP prometheus` (or restart it).

## Alerting: severity + routing (Chunk 22)

Prometheus decides **when** an alert fires; [Alertmanager](alertmanager/alertmanager.yml) decides **who**
hears it and **how loud**. Prometheus (`alerting:` in [prometheus.yml](prometheus/prometheus.yml)) pushes
firing alerts to Alertmanager, which groups them and walks a routing tree:

- `severity=critical` → **fleet-critical** (fast `group_wait`, the pager channel)
- `severity=warning` + `team=device-reliability` → **device-reliability** (the owning team's channel)
- `severity=warning` (anything else) → **fleet-warning**
- catch-all → **fleet-default**

Every alert rule carries `severity`, `team` (owner), `sli`, and a `runbook_url` — routing and attribution
are labels on the alert, not values baked into notification code. Alerts fire on **symptoms** (SLO breach
or a *sustained* anomaly from the chosen z-score detector, `avg_over_time(fleet_anomaly_flag{detector="zscore"}[1m]) > 0.5`),
never on raw sensor values.

**Why Alertmanager instead of hand-rolled webhook code:** grouping, dedup, silences, inhibition, and
per-route fan-out are all config, not code we maintain — and the same tree serves Slack, PagerDuty, or a
custom webhook by adding a receiver. It's also the natural home for the dedup/suppression tuning below.

**Every receiver notifies three places:** a Slack channel, the local `alert-sink` container, and the
incident store. Slack needs a webhook this project doesn't have yet, so the sink (stdlib,
[alertmanager/sink/](alertmanager/sink/)) makes the whole path demonstrable with **zero external setup**
— it prints each routed alert with severity + attribution to `docker compose logs alert-sink`. To turn
Slack on, drop your webhook URL into `alertmanager/secrets/slack_api_url` — see
[`slack_api_url.example`](alertmanager/slack_api_url.example). Absent → Slack is skipped, sink still fires.

- Alertmanager UI (grouped alerts, silences): http://localhost:9093
- See it fire end to end: feed one device, stop it, watch the sink —
  ```bash
  docker compose up -d --build
  python tools/fake_telemetry.py --devices 1 &   # register a device, then Ctrl-C after ~15s
  docker compose logs -f alert-sink              # FleetDeviceStale (+availability) route in ~1 min
  ```

## Dedup + suppression

One fault must produce **one** notification, not a storm. Four layers, each catching a different kind
of noise — all config, no code:

1. **Dedup by identity + grouping** (Alertmanager): identical label sets are one alert however often
   Prometheus re-evaluates; `group_by: [alertname, device]` collapses e.g. four anomalous channels on
   one device into one message.
2. **Suppression windows** (Alertmanager, [alertmanager.yml](alertmanager/alertmanager.yml)):
   `group_wait: 30s` batches related alerts into the first notification; `group_interval: 5m` is the
   floor between re-notifications of a changed group — the backstop that turns a worst-case flap into
   ≤1 message per 5m; `repeat_interval: 4h` re-pings a still-firing group at most that often. The
   reasoning behind each length is commented on the config.
3. **Flap-damping hysteresis** (Prometheus, [rules](prometheus/rules/fleet_slos.yml)): slow to fire
   (`avg_over_time` dwell + `for:`), slow to clear (`keep_firing_for: 1–2m`) — an intermittent fault
   oscillating around the threshold is fused into one continuous alert instead of a resolve/re-fire
   pair per cycle.
4. **Inhibition** (Alertmanager): suppression along the *causality* chain — bridge down mutes every
   per-device freshness/anomaly symptom it explains; a fleet-availability critical mutes the
   per-device stale warnings it subsumes. One page for the cause, not N for the symptoms.

Try it: run `fake_telemetry.py --anomaly temp` (toggles the fault every 15s) — the sink shows a single
`FleetChannelAnomaly` firing + one resolve after the run, not a message per toggle.

## Incident store + timeline

Alerting is stateless — nothing remembers what happened. The [incidents/](incidents/) service is the
memory: it consumes the **same Alertmanager webhook** as the sink (`firing` opens an incident,
`resolved` closes it and records the duration) and persists to SQLite on a named volume, so incident
history survives `compose down`.

- **Three incident scopes**, derived from alert labels: `channel` (per-channel anomaly), `device`
  (staleness), `fleet` (availability / error rate / pipeline — one row for the systemic event, never
  one per device).
- **Flap-aware**: a re-fire within the 5m reopen window *reopens* the same incident (a `reopened`
  timeline event, `fleet_incidents_reopened_total` ticks) instead of minting a new row — the store
  agrees with Alertmanager's `group_interval` about what "the same episode" means.
- **Expiry backstop**: a resolve notification can be lost (found live in testing: inhibition mutes an
  alert's *resolve* too, if a covering critical is still firing). A janitor force-closes open
  incidents silent for >5h — longer than `repeat_interval`, so a live un-muted alert always
  re-notifies first. Expired closures don't feed MTTR; the real recovery time is unknown.
- **Timeline in Grafana without plugins**: the store exports `fleet_incident_active` (1 while open),
  open counts, opened/reopened totals, and a TTR histogram; Prometheus scrapes them and
  **Fleet → Fleet — Incidents** renders the state timeline, MTTR, and an active-incident table.
- Full rows + per-incident event timelines: http://localhost:9096/incidents (JSON).
