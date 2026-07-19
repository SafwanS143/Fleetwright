# Runbook — Device offline (`FleetDeviceStale`)

**Severity:** warning · **Owner:** device-reliability · **SLI:** freshness (< 10s)

## What it means
No telemetry from one device for more than 10 seconds. One device is quiet — the rest of the fleet is
fine. If *many* devices trip at once you'll instead see `FleetAvailabilityBelowSLO` (critical), which
points at the shared ingest path, not one device.

## Diagnose (walk the hops device → cloud)
1. **Is it really one device?** Grafana → *Fleet — Per-Device*, freshness panel. One red row = device;
   many = jump to [fleet-availability.md](fleet-availability.md).
2. **Is the broker still getting it?** `mosquitto_sub -t 'fleet/<id>/telemetry' -v` (on the broker host).
   - Messages arriving → the gap is downstream (bridge/scrape); check the bridge logs.
   - Silence → the device or its gateway stopped publishing. Continue.
3. **Gateway alive?** On the Pi: `docker compose ps` (gateway container up?), `docker compose logs gateway`.
   Look for serial reconnect loops or a broker connection error.
4. **Device → gateway link.** `ls /dev/ttyACM*` — is the Nucleo still enumerated? A yanked/again USB
   cable is the most common real cause. `dmesg | tail` shows unplug/replug events.
5. **Device itself.** If the port is present but no bytes arrive, the firmware has hung — power-cycle
   the Nucleo.

## Remediate
- USB dropped → reseat the cable; the gateway's serial reader reconnects on its own.
- Gateway container down → `docker compose up -d gateway`; buffered telemetry flushes from the ring buffer.
- Firmware hung → power-cycle the Nucleo; `seq` restarts at 0 (expected, the bridge reseeds).

The alert auto-resolves once telemetry is fresh again; `keep_firing_for` holds it ~1m so a
reconnect-flap doesn't page twice, and the incident closes on the resolve.

## Escalate
If the device is powered, enumerated, and publishing to the broker but Grafana still shows it stale,
the fault is in the ingest path, not the device → [ingest-pipeline-down.md](ingest-pipeline-down.md).
