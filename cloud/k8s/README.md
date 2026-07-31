# k8s/

The same cloud stack as [`../docker-compose.yml`](../docker-compose.yml), expressed as Kubernetes
**Deployments + Services** and run on **k3s**. Compose was the way to get the pipeline working end to
end; this is the platform layer on top of it.

Both paths read the **same config files** — `prometheus.yml`, the SLO rules, the Grafana dashboards.
Compose bind-mounts them; here [`../kustomization.yaml`](../kustomization.yaml) turns them into
ConfigMaps with `configMapGenerator`. Nothing is duplicated, so the two deploy paths can't drift.

## Cluster

k3s needs a Linux kernel, so on Windows it runs via **k3d** (k3s in Docker). The `-p` mappings publish
the same host ports Compose did, so every URL below and in the runbooks stays valid.

```bash
k3d cluster create fleetwright --servers 1 --agents 0 \
  -p "1883:1883@loadbalancer" -p "3000:3000@loadbalancer" \
  -p "9090:9090@loadbalancer" -p "9093:9093@loadbalancer" \
  -p "9096:9096@loadbalancer" -p "9097:9097@loadbalancer" \
  -p "9098:9098@loadbalancer" -p "9099:9099@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0"
```

Traefik is disabled because nothing here uses Ingress — the published ports come from `Service`
objects of `type: LoadBalancer`, which k3s satisfies with its built-in **ServiceLB**. On a managed
cloud cluster the same manifests would provision real cloud load balancers instead.

On Windows the generated kubeconfig points at `host.docker.internal`, which may not resolve to a
reachable address. If `kubectl` hangs:

```bash
kubectl config set-cluster k3d-fleetwright --server=https://127.0.0.1:<api-port>
```

## Deploy

```bash
bash k8s/build-images.sh     # build the 7 local images, import them into the cluster
kubectl apply -k .           # from cloud/ — namespace, ConfigMaps, Deployments, Services, PVCs
kubectl -n fleet rollout status deploy --timeout=180s
```

There is no registry: images are built locally and loaded into the node's image store, which is why
every Deployment pins `imagePullPolicy: IfNotPresent`.

Same endpoints as Compose — Grafana <http://localhost:3000>, Prometheus <http://localhost:9090>,
Alertmanager <http://localhost:9093>, incidents <http://localhost:9096>, remediator
<http://localhost:9098>, control <http://localhost:9099>, broker `localhost:1883`.

The simulated fleet is excluded from the default apply, the same way it sat behind Compose's `sim`
profile, so a default deploy never competes with a real Pi on the broker:

```bash
kubectl apply -n fleet -f k8s/simulator.yaml    # fault-control page on :9097
```

Slack is optional. With no Secret the mount is empty, Slack is skipped, and the local sink plus the
incident store still receive every alert:

```bash
kubectl -n fleet create secret generic alertmanager-slack \
  --from-file=slack_api_url=alertmanager/secrets/slack_api_url
```

## What changed from Compose

| Compose | Kubernetes | Why |
| --- | --- | --- |
| `docker run` / `restart: unless-stopped` | Deployment | A ReplicaSet reconciles the pod count continuously; restart is a property of desired state, not a flag. |
| bind-mounted config files | ConfigMaps via `configMapGenerator` | Config travels with the manifest. Each ConfigMap name carries a content hash, so editing a config rolls the pods that mount it. |
| named volumes | PersistentVolumeClaims | Storage is requested by the workload and bound by the cluster; k3s's `local-path` provisioner satisfies it. |
| `ports:` | `Service` (LoadBalancer / ClusterIP) | Only what an operator or the Pi actually needs is published; the bridge, anomaly detector, and alert sink are cluster-internal. |
| `depends_on` | nothing | No start-order primitive exists. Every service already retries its dependency, and CrashLoopBackOff converges on its own. |
| `/var/run/docker.sock` on the remediator | removed | Restarting a hung container is the kubelet's job now. The loop keeps device remediation and drops root. |
| `restart: unless-stopped` alone | liveness + readiness probes | Restart policies only see a process that exited. Probes also catch one that is alive and not serving, and readiness gates traffic without killing anything. |

Stateful services (`mosquitto`, `prometheus`, `alertmanager`, `incidents`, `grafana`) use
`strategy: Recreate`: their claims are `ReadWriteOnce`, so a rolling surge pod would block forever
waiting for the outgoing pod to release the volume.

## Probes

Two different questions, two different consequences:

- **Readiness — "should traffic reach me?"** Failing removes the pod from its Service's endpoints. It
  is a routing decision, and it is reversible.
- **Liveness — "am I beyond saving?"** Failing kills the container and the kubelet restarts it. It is
  the compose-era remediator's service-healing branch, moved to the platform — which is why
  `FLEET_HEAL_SERVICES` is off in [`remediator.yaml`](remediator.yaml).

| Workload | Readiness | Liveness | Startup |
| --- | --- | --- | --- |
| bridge, anomaly | `/readyz` — broker subscription established | `/healthz` — MQTT network loop still running | anomaly only (~6s of numpy/sklearn import) |
| incidents | `/readyz` — `SELECT 1` on SQLite | `/healthz` — same query | — |
| remediator | `/readyz` — first reconcile finished | `/healthz` — reconcile loop beat within `FLEET_LOOP_STALL` | — |
| control, alert-sink, simulator | HTTP serving | HTTP serving | — |
| prometheus | `/-/ready` (false during WAL replay) | `/-/healthy` | yes — replay can take minutes |
| grafana | `/api/health` (checks its DB) | `/api/health` | yes — migrations + provisioning |
| mosquitto | TCP accept on 1883 | TCP accept on 1883 | — |

Two rules shaped every row:

**Liveness never checks a dependency.** A restart cannot fix someone else's outage, and a probe that
restarts on one turns a single failure into a crash-looping cascade. So `/healthz` on the bridge asks
only whether *this process's* MQTT loop is running; a broker that is down leaves it a healthy 200. The
same reasoning applies to the remediator: a cycle that failed because Prometheus was unreachable still
counts as a heartbeat. Getting there also meant switching every MQTT client to `connect_async` — the
synchronous `connect()` raises when the broker isn't up yet, which is a crash loop wearing a costume.

**Readiness must not delete the signal it reports.** The obvious design ties the bridge's readiness to
its broker connection. It's wrong: an unready bridge leaves the Service, Prometheus stops scraping
`fleet_last_message_timestamp_seconds`, the freshness SLI goes stale, and `FleetDeviceStale` quietly
stops evaluating — the monitoring goes dark exactly when the fleet does. So readiness here is a
*warm-up* gate only: unready until the first subscription, ready across every flap after it. The broker
state is reported the honest way instead, as `fleet_broker_connected` with a `FleetBrokerUnreachable`
alert on it.

### Demo

A hung container — alive, not serving, the case a restart policy alone never catches. `SIGSTOP` has to
come from outside the pod's PID namespace, because the kernel refuses to stop a namespace's PID 1 from
inside it:

```bash
NODE=k3d-fleetwright-server-0
CID=$(docker exec $NODE crictl ps --name bridge -q)
PID=$(docker exec $NODE crictl inspect --output go-template --template '{{.info.pid}}' $CID)
docker exec $NODE kill -STOP $PID          # frozen: socket still listens, nothing answers
kubectl -n fleet get pod -l app=bridge -w
```

Readiness fails first and the pod leaves the Service within ~10s; liveness takes ~30s (3 × 10s) before
it kills the container and the kubelet restarts it — fast and reversible versus slow and destructive,
exactly the split the two probes are for. On a cluster with no node shell,
`kubectl debug node/<node>` gives the same reach via a `hostPID` pod.

An unready pod pulled from service — start the bridge with no broker to talk to:

```bash
kubectl -n fleet scale deploy/mosquitto --replicas=0
kubectl -n fleet rollout restart deploy/bridge
kubectl -n fleet get pod -l app=bridge     # Running, READY 0/1 — 0 restarts, it isn't broken
kubectl -n fleet get endpointslice -l kubernetes.io/service-name=bridge \
  -o jsonpath='{.items[0].endpoints[*].conditions.ready}'    # false
kubectl -n fleet scale deploy/mosquitto --replicas=1         # ready within seconds
```

The rolling update also refuses to finish while the new pod is unready, so the old one keeps serving —
a bad config that can't reach the broker never takes the ingest path down with it.

Scaling the broker to zero *without* restarting the bridge shows the deliberate asymmetry: the pod
stays `1/1 READY`, keeps exporting, `fleet_broker_connected` drops to 0, and the alert fires.
