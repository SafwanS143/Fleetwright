# Runbook — Automated remediation exhausted (`FleetRemediationExhausted`)

**Severity:** critical · **Owner:** platform · **SLI:** remediation

## What it means
The self-healing loop tried its capped attempts (default 3) on a target and it's *still* unhealthy, so
the loop gave up and paged. This is the design working as intended: automation absorbs the transient
faults silently, and only the ones it can't fix reach a human. A page here means "the easy fix didn't
work" — treat it as a real fault, not a flapping alert.

`$labels.target` is the device or service; `$labels.reason` is `stale`, `anomaly`, or `down`.

## Diagnose
1. **What did the loop try?** `curl localhost:9098/state` (what it's still working) and
   `docker compose logs remediator` (the attempt/escalation history for this target).
2. **Device target (`stale` / `anomaly`)** — the reboot commands didn't bring it back. Follow the
   underlying runbook: [device-offline.md](device-offline.md) for `stale`,
   [channel-anomaly.md](channel-anomaly.md) for `anomaly`. A persistent anomaly the loop can't clear is
   often a *real regime change* (the device genuinely runs hot), not a fault — confirm before acting.
3. **Service target (`down`)** — a restart didn't make the container scrapeable again. It's likely
   crash-looping on a bad config or a dependency; `docker compose logs <service>` and check the last
   change to its config.

## Remediate
- Fix the root cause per the underlying runbook, then the target returns to healthy on its own.
- The loop clears `fleet_remediation_exhausted` the moment the target is healthy again, this alert
  resolves, and its incident auto-closes — the same close-on-recovery path as every other incident.
- To retry automation after a fix, nothing is needed: once healthy, the target's attempt budget resets,
  so a later fault gets a fresh set of automated attempts.

## Escalate
If the target is a service and repeated manual restarts also fail, the container image or its config is
broken — roll back the last change to it. If a device is powered and publishing but the loop still can't
clear it, the fault is in the ingest path, not the device → [ingest-pipeline-down.md](ingest-pipeline-down.md).
