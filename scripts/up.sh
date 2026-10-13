#!/bin/sh
set -eu

: "${CLUSTER_NAME:?}"
: "${UBUNTU_RELEASE:?}"
: "${CPUS:?}"
: "${MEMORY:?}"
: "${DISK:?}"
: "${RKE2_VERSION:?}"
: "${RKE2_ARCH:?}"
: "${RKE2_CACHE_DIR:?}"

# Check daemon access before treating a failed instance lookup as a missing VM.
multipass list >/dev/null

if multipass info "$CLUSTER_NAME" >/dev/null 2>&1; then
  state=$(multipass info --format csv "$CLUSTER_NAME" | awk -F, 'NR == 2 { print $2 }')
  case "$state" in
    Stopped) multipass start "$CLUSTER_NAME" ;;
    Running) echo "Instance $CLUSTER_NAME is already running" ;;
    *) echo "Cannot start $CLUSTER_NAME: state is $state" >&2; exit 1 ;;
  esac
else
  sh scripts/cache-rke2.sh
  multipass launch "$UBUNTU_RELEASE" \
    --name "$CLUSTER_NAME" \
    --cpus "$CPUS" \
    --memory "$MEMORY" \
    --disk "$DISK" \
    --cloud-init cloud-init.yaml
fi

multipass exec "$CLUSTER_NAME" -- sudo cloud-init status --wait
sh scripts/install-rke2.sh
multipass exec "$CLUSTER_NAME" -- sudo /var/lib/rancher/rke2/bin/kubectl \
  --kubeconfig /etc/rancher/rke2/rke2.yaml \
  wait --for=condition=Ready nodes --all --timeout=5m
sh scripts/export-kubeconfig.sh "$CLUSTER_NAME"
