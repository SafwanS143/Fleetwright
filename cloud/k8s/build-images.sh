#!/usr/bin/env bash
# Builds the locally-authored service images and loads them straight into the cluster's image store.
# There is no registry in this setup, which is why every Deployment pins imagePullPolicy: IfNotPresent.
set -euo pipefail

CLUSTER="${FLEET_K3D_CLUSTER:-fleetwright}"
TAG="${FLEET_IMAGE_TAG:-dev}"
CLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# service_name:build_context
SERVICES=(
  "bridge:bridge"
  "anomaly:anomaly"
  "incidents:incidents"
  "remediator:remediator"
  "control:control"
  "simulator:simulator"
  "alert-sink:alertmanager/sink"
)

images=()
for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  context="${entry#*:}"
  image="fleetwright/${name}:${TAG}"
  echo "==> building ${image}"
  docker build -t "${image}" "${CLOUD_DIR}/${context}"
  images+=("${image}")
done

echo "==> importing ${#images[@]} images into k3d cluster '${CLUSTER}'"
k3d image import "${images[@]}" --cluster "${CLUSTER}"

echo "done. next: kubectl apply -k ${CLOUD_DIR}"
