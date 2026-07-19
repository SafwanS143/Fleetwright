# Runbook — Channel anomaly (`FleetChannelAnomaly`)

**Severity:** warning · **Owner:** device-reliability · **SLI:** anomaly

## What it means
One sensor channel on one device (e.g. `sim-01 / temperature`) has read outside its learned normal band
for the majority of the last minute. This is the z-score/MAD detector's sustained flag — not a single
spike. **An anomaly is not automatically a fault**: it means "this looks unlike the baseline," which can
be a real problem *or* a legitimate change in operating conditions.

## Diagnose — is it a fault or a regime change?
1. Grafana → *Fleet — Anomaly Detection*, select the device. Look at the channel's raw value with the
   shaded normal band and both detector scores.
2. **Interpret by channel:**
   - **accel_magnitude** spikes to well above 1 g → real vibration/shock (loose mounting, impact). Treat
     as a fault; correlate with the timeline.
   - **temperature / humidity / pressure** drifting steadily out of band → most often a **regime change**
     (the environment actually warmed up), not a sensor fault. A cliff-edge jump or a physically
     impossible value (e.g. humidity > 100%, pressure far off ambient) → suspect the sensor.
3. **One channel or several?** A single channel anomalous points at that sensor or its surroundings.
   Several channels at once on one device points at the shared I²C bus or a power issue.
4. Check the incident timeline (http://localhost:9096/incidents) — reopens/flaps suggest an intermittent
   connection rather than a steady shift.

## Remediate
- Confirmed fault (vibration, bad reading) → address the physical cause (reseat sensor, secure mounting,
  check wiring), or take the device out of service.
- Legitimate regime change (a real, sustained new normal) → the frozen baseline will keep flagging it.
  Re-baseline the channel by restarting the anomaly service (`docker compose restart anomaly`); it
  refits on the next warm-up window. Re-baseline deliberately, not reflexively — doing it aggressively
  hides slow real drift.

## Notes
- Only the z-score/MAD detector pages; Isolation Forest is dashboard-only (it had a higher
  false-positive rate on these 1-D channels).
- `keep_firing_for` fuses an intermittent (flapping) anomaly into one alert and one incident.
