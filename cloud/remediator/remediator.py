#!/usr/bin/env python3
"""Self-healing remediation controller: a reconcile loop that drives the fleet toward its desired
state (every device healthy) with no human in the path.

Each cycle it OBSERVES actual state from Prometheus (the same freshness / anomaly series the alerts
fire on), DIFFS it against desired (healthy), and ACTS on the gap — publishing a reboot command on the
device's MQTT downlink so the device recovers, its SLO alert resolves, and the incident auto-closes.
That's the observe -> diff -> act loop a Kubernetes controller runs; the guardrails below (cooldown,
capped attempts, escalation) are what make it heal instead of thrash.

A second, optional reconciler restarts a hung/unscrapeable cloud service via the Docker socket — the
compose-era stand-in for a k8s liveness probe. Docker already restarts a crashed container, so this
only earns its keep on the case Docker misses: a container alive but not serving.
"""

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import paho.mqtt.client as mqtt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

# ── config (env-driven) ──────────────────────────────────────────────────────
BROKER_HOST = os.environ.get("FLEET_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("FLEET_BROKER_PORT", "1883"))
PROM_URL = os.environ.get("FLEET_PROM_URL", "http://prometheus:9090")
METRICS_PORT = int(os.environ.get("FLEET_METRICS_PORT", "9098"))
CMD_TOPIC = os.environ.get("FLEET_CMD_TOPIC", "fleet/{device}/cmd")

RECONCILE_INTERVAL = float(os.environ.get("FLEET_RECONCILE_INTERVAL", "15"))
# Act past a clear SLO breach, not the 10s edge, so we never fight a device that's merely late.
FRESHNESS_THRESHOLD = float(os.environ.get("FLEET_FRESHNESS_THRESHOLD", "20"))
REMEDIATE_ANOMALY = os.environ.get("FLEET_REMEDIATE_ANOMALY", "true").lower() == "true"
# Cooldown exceeds the anomaly's 1m averaging window, so recovery isn't mistaken for a fresh fault.
COOLDOWN = float(os.environ.get("FLEET_REMEDIATION_COOLDOWN", "60"))
MAX_ATTEMPTS = int(os.environ.get("FLEET_REMEDIATION_MAX_ATTEMPTS", "3"))

HEAL_SERVICES = os.environ.get("FLEET_HEAL_SERVICES", "true").lower() == "true"
# Longer than Docker's own crash-restart, so we only step in when Docker couldn't bring the service back.
SERVICE_GRACE = float(os.environ.get("FLEET_SERVICE_GRACE", "45"))
# Cloud services whose hang blinds the fleet; each maps to its Prometheus scrape job.
SERVICE_JOBS = {"bridge": "fleet-bridge", "anomaly": "fleet-anomaly", "incidents": "fleet-incidents"}

# ── metrics (observe the healer itself — automation you can't see is a liability) ──────────────
ATTEMPTS = Counter(
    "fleet_remediation_attempts_total",
    "Automated recovery actions taken.",
    ["target", "reason", "action"],
)
RECOVERED = Counter(
    "fleet_remediation_recovered_total",
    "Targets that returned to healthy after we acted (the loop converged).",
    ["target", "reason"],
)
EXHAUSTED = Gauge(
    "fleet_remediation_exhausted",
    "1 while automation has given up on a target (attempts capped) and a human is needed.",
    ["target", "reason"],
)
RECONCILE_LOOPS = Counter(
    "fleet_reconcile_loops_total",
    "Reconcile cycles completed — the healer's own heartbeat.",
)


def log(msg):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[remediator {now}] {msg}", flush=True)


class Finding:
    """One observed gap between desired and actual state."""

    def __init__(self, kind, name, reason, action, detail=""):
        self.kind = kind          # device | service
        self.name = name          # device id or compose service name
        self.reason = reason      # stale | anomaly | down
        self.action = action      # reboot | restart
        self.detail = detail
        self.grace = SERVICE_GRACE if kind == "service" else 0.0

    @property
    def key(self):
        return (self.kind, self.name, self.reason)


class Target:
    """A finding we're tracking across cycles, with its remediation budget."""

    def __init__(self, finding):
        self.f = finding
        self.attempts = 0
        self.first_bad = time.time()
        self.last_action = 0.0
        self.exhausted = False


class Controller:
    def __init__(self, mqtt_client):
        self.mqtt = mqtt_client
        self.targets: dict[tuple, Target] = {}
        self.docker = None
        if HEAL_SERVICES:
            self._init_docker()

    def _init_docker(self):
        """Lazy so a missing socket disables only service healing, never device remediation."""
        try:
            import docker
            self.docker = docker.from_env()
            self.docker.ping()
        except Exception as exc:
            self.docker = None
            log(f"service healing disabled: cannot reach Docker ({exc})")

    # ── observe ──────────────────────────────────────────────────────────────
    def prom_query(self, expr):
        url = f"{PROM_URL}/api/v1/query?query={quote(expr)}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.load(resp)
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error", "query failed"))
        return payload["data"]["result"]

    def observe(self):
        findings = []

        for s in self.prom_query(f"fleet:device_freshness_seconds > {FRESHNESS_THRESHOLD}"):
            device = s["metric"].get("device")
            if device:
                findings.append(Finding("device", device, "stale", "reboot"))

        if REMEDIATE_ANOMALY:
            # Sustained anomaly (same dwelled signal the page uses), collapsed to one reboot per device.
            expr = 'avg_over_time(fleet_anomaly_flag{detector="zscore"}[1m]) > 0.5'
            channels: dict[str, list[str]] = {}
            for s in self.prom_query(expr):
                device = s["metric"].get("device")
                if device:
                    channels.setdefault(device, []).append(s["metric"].get("channel", "?"))
            for device, chans in channels.items():
                findings.append(Finding("device", device, "anomaly", "reboot", ",".join(sorted(chans))))

        if self.docker:
            jobs = "|".join(SERVICE_JOBS.values())
            job_to_svc = {job: svc for svc, job in SERVICE_JOBS.items()}
            for s in self.prom_query(f'up{{job=~"{jobs}"}} == 0'):
                svc = job_to_svc.get(s["metric"].get("job"))
                if svc:
                    findings.append(Finding("service", svc, "down", "restart"))

        return findings

    # ── reconcile ────────────────────────────────────────────────────────────
    def reconcile(self):
        try:
            findings = self.observe()
        except Exception as exc:
            log(f"observe failed, skipping cycle (acting on stale state is worse than waiting): {exc}")
            return
        finally:
            RECONCILE_LOOPS.inc()

        bad = {f.key: f for f in findings}

        # Anything we tracked that's no longer bad has converged — record it and drop it.
        for key, tgt in list(self.targets.items()):
            if key not in bad:
                self._on_recovered(tgt)
                del self.targets[key]

        for key, finding in bad.items():
            tgt = self.targets.get(key)
            if tgt is None:
                tgt = Target(finding)
                self.targets[key] = tgt
            else:
                tgt.f.detail = finding.detail
            self._act(tgt)

    def _on_recovered(self, tgt):
        f = tgt.f
        EXHAUSTED.labels(target=f.name, reason=f.reason).set(0)
        if tgt.exhausted:
            log(f"{f.name}/{f.reason}: recovered after escalation (resolved out-of-band)")
        elif tgt.attempts > 0:
            RECOVERED.labels(target=f.name, reason=f.reason).inc()
            log(f"{f.name}/{f.reason}: RECOVERED after {tgt.attempts} action(s) — loop converged")

    def _act(self, tgt):
        now = time.time()
        f = tgt.f
        if tgt.exhausted:
            return  # already escalated; leave the open incident for a human
        if now - tgt.first_bad < f.grace:
            return  # let Docker's own restart (or the alert dwell) have first crack
        if now - tgt.last_action < COOLDOWN:
            return  # a prior action is still settling
        if tgt.attempts >= MAX_ATTEMPTS:
            tgt.exhausted = True
            EXHAUSTED.labels(target=f.name, reason=f.reason).set(1)
            log(f"{f.name}/{f.reason}: ESCALATED — {tgt.attempts} actions didn't recover it; paging a human")
            return

        if self._actuate(f):
            tgt.attempts += 1
            tgt.last_action = now
            ATTEMPTS.labels(target=f.name, reason=f.reason, action=f.action).inc()
            detail = f" ({f.detail})" if f.detail else ""
            log(f"{f.name}/{f.reason}{detail}: {f.action} #{tgt.attempts}/{MAX_ATTEMPTS}")

    def _actuate(self, f):
        try:
            if f.kind == "device":
                payload = json.dumps({"cmd": "reboot", "reason": f.reason, "source": "remediator"})
                self.mqtt.publish(CMD_TOPIC.format(device=f.name), payload, qos=1)
                return True
            if f.kind == "service" and self.docker:
                containers = self.docker.containers.list(
                    all=True, filters={"label": f"com.docker.compose.service={f.name}"}
                )
                for c in containers:
                    c.restart(timeout=10)
                return bool(containers)
        except Exception as exc:
            log(f"{f.name}/{f.reason}: action failed: {exc}")
        return False

    def state(self):
        return [
            {
                "kind": t.f.kind, "target": t.f.name, "reason": t.f.reason,
                "detail": t.f.detail, "attempts": t.attempts, "exhausted": t.exhausted,
                "seconds_since_action": round(time.time() - t.last_action, 1) if t.last_action else None,
            }
            for t in self.targets.values()
        ]


def make_handler(controller):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

        def do_GET(self):
            if self.path == "/metrics":
                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif self.path.startswith("/state"):
                self._reply(200, json.dumps(controller.state(), indent=2), "application/json")
            else:
                self._reply(200, "remediator ok")

        def log_message(self, *args):
            pass  # the reconcile lines are the only output we want

    return Handler


def main():
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="fleet-remediator")
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    controller = Controller(client)

    server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), make_handler(controller))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    log(f"reconcile every {int(RECONCILE_INTERVAL)}s; freshness>{int(FRESHNESS_THRESHOLD)}s, "
        f"anomaly={'on' if REMEDIATE_ANOMALY else 'off'}, service-healing="
        f"{'on' if controller.docker else 'off'}; max {MAX_ATTEMPTS} attempts then escalate; :{METRICS_PORT}")

    try:
        while True:
            controller.reconcile()
            time.sleep(RECONCILE_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        server.shutdown()


if __name__ == "__main__":
    main()
