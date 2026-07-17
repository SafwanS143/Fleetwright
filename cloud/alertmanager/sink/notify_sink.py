#!/usr/bin/env python3
"""Local Alertmanager webhook receiver (Chunk 22).

Alertmanager fans every routed alert out to its receivers. Slack is the real destination, but it needs a
webhook URL this project doesn't have yet — so this tiny always-on sink is wired into *every* receiver as
well. It gives a zero-setup way to SEE alerts fire with their severity + attribution (device/channel/owner)
in `docker compose logs`, which is what makes the Chunk 22 "a real alert fires" checkpoint demonstrable
without any external service.

It's also the seam for Chunk 24: the incident store will consume this same Alertmanager webhook (status
`firing` opens an incident row, `resolved` closes it), so the payload is parsed here the way that service
will need it. Stdlib only — no Flask, no requirements.
"""

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("FLEET_SINK_PORT", "9095"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        receiver = payload.get("receiver", "?")
        # Alertmanager batches a whole group into one POST; iterate the members so each shows its own line.
        for alert in payload.get("alerts", []):
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            status = alert.get("status", "?").upper()
            channel = labels.get("channel")
            # Attribution: device, plus channel when the alert is per-channel (anomaly), else just device.
            attribution = labels.get("device", "-")
            if channel:
                attribution = f"{attribution}/{channel}"
            print(
                f"[sink {now}] {status:8} "
                f"sev={labels.get('severity', '?'):8} "
                f"team={labels.get('team', '-'):18} "
                f"route={receiver:19} "
                f"{labels.get('alertname', '?')} <{attribution}> :: {annotations.get('summary', '')}",
                flush=True,
            )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        # Trivial liveness endpoint so a probe/curl can confirm the sink is up.
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alert-sink ok")

    def log_message(self, *args):
        # Silence the default per-request access log; the alert lines above are the only output we want.
        pass


if __name__ == "__main__":
    print(f"[sink] Alertmanager webhook sink listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
