# Runbook — Ingest error rate above SLO (`FleetHighErrorRate`)

**Severity:** warning · **Owner:** platform · **SLI:** error rate (< 0.1% malformed frames over 5m)

## What it means
More than 0.1% of frames arriving at the bridge failed to decode/parse over the last 5 minutes.
Machine-generated NDJSON should essentially always parse, so a rising error rate points at **framing
corruption on the wire** (the UART/serial hop), not normal load. Note this counts *malformed frames* —
dropped messages (sequence gaps) are tracked separately and are not errors, because QoS 0 trades
completeness for liveness.

## Diagnose
1. **Which device?** `fleet_message_errors_total` is labelled by device and `reason`. In Prometheus:
   `sum by (device, reason) (rate(fleet_message_errors_total[5m]))`. A single device dominating →
   that device's serial link; many devices → something changed in the bridge or a shared component.
2. **Look at the raw frames.** `mosquitto_sub -t 'fleet/<id>/telemetry' -v` — are payloads truncated,
   concatenated, or garbled? That's the classic UART symptom (baud mismatch, noise, half-lines).
3. **Bridge logs** show the decode failures with context.

## Remediate
- Single device, garbled frames → suspect the serial link: baud-rate mismatch, a flaky USB cable, or
  electrical noise on the UART. Reseat/replace the cable; confirm the firmware and reader agree on baud.
- Recent firmware change to the telemetry schema → a field type or framing change can break decoding;
  roll it back or fix the schema.
- The alert clears when the malformed ratio drops back under 0.1% for the window.

## Note
This SLI only covers failures **after** the gateway. It does not see faults on the firmware or the
device→gateway UART hop that never produced a frame at all — those show up as staleness, not errors.
