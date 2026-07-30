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

Stateful services (`mosquitto`, `prometheus`, `alertmanager`, `incidents`, `grafana`) use
`strategy: Recreate`: their claims are `ReadWriteOnce`, so a rolling surge pod would block forever
waiting for the outgoing pod to release the volume.

Liveness and readiness probes are added next — the remediator's service-healing branch is switched
off here precisely because that responsibility moves to the kubelet.
