#!/bin/sh
set -eu

: "${INSTANCE:?}"
: "${UBUNTU_RELEASE:?}"
: "${CPUS:?}"
: "${MEMORY:?}"
: "${DISK:?}"
: "${RKE2_VERSION:?}"
: "${RKE2_ARCH:?}"
: "${RKE2_CACHE_DIR:?}"

# Check daemon access before treating a failed instance lookup as a missing VM.
multipass list >/dev/null

if multipass info "$INSTANCE" >/dev/null 2>&1; then
  state=$(multipass info --format csv "$INSTANCE" | awk -F, 'NR == 2 { print $2 }')
  case "$state" in
    Stopped) multipass start "$INSTANCE" ;;
    Running) echo "Instance $INSTANCE is already running" ;;
    *) echo "Cannot start $INSTANCE: state is $state" >&2; exit 1 ;;
  esac
else
  sh scripts/cache-rke2.sh
  multipass launch "$UBUNTU_RELEASE" \
    --name "$INSTANCE" \
    --cpus "$CPUS" \
    --memory "$MEMORY" \
    --disk "$DISK" \
    --cloud-init cloud-init.yaml
fi

multipass exec "$INSTANCE" -- sudo cloud-init status --wait
sh scripts/install-rke2.sh
multipass exec "$INSTANCE" -- sudo /var/lib/rancher/rke2/bin/kubectl \
  --kubeconfig /etc/rancher/rke2/rke2.yaml \
  wait --for=condition=Ready nodes --all --timeout=5m
sh scripts/export-kubeconfig.sh "$INSTANCE"
