#!/bin/sh
set -eu

: "${INSTANCE:?}"
: "${RKE2_VERSION:?}"
: "${RKE2_ARCH:?}"
: "${RKE2_CACHE_DIR:?}"

guest_cache="/home/ubuntu/.cache/rke2/$RKE2_VERSION/$RKE2_ARCH"

if ! multipass exec "$INSTANCE" -- sh -c 'systemctl is-enabled --quiet rke2-server.service || exit 1'; then
  RKE2_VERSION="$RKE2_VERSION" RKE2_ARCH="$RKE2_ARCH" \
    RKE2_CACHE_DIR="$RKE2_CACHE_DIR" sh scripts/cache-rke2.sh

  multipass exec "$INSTANCE" -- mkdir -p "$guest_cache"
  for asset in install.sh "sha256sum-$RKE2_ARCH.txt" \
    "rke2.linux-$RKE2_ARCH.tar.gz" "rke2-images.linux-$RKE2_ARCH.tar.zst"; do
    multipass transfer "$RKE2_CACHE_DIR/$asset" "$INSTANCE:$guest_cache/$asset"
  done

  multipass exec "$INSTANCE" -- sudo mkdir -p /var/lib/rancher/rke2/agent/images
  multipass exec "$INSTANCE" -- sudo mv \
    "$guest_cache/rke2-images.linux-$RKE2_ARCH.tar.zst" \
    /var/lib/rancher/rke2/agent/images/
  multipass exec "$INSTANCE" -- sudo env \
    INSTALL_RKE2_VERSION="$RKE2_VERSION" \
    INSTALL_RKE2_METHOD=tar \
    INSTALL_RKE2_TYPE=server \
    INSTALL_RKE2_ARTIFACT_PATH="$guest_cache" \
    sh "$guest_cache/install.sh"
  multipass exec "$INSTANCE" -- sudo systemctl enable --now rke2-server.service
fi

multipass exec "$INSTANCE" -- sudo sh -c '
  set -eu
  attempts=0
  until [ -s /etc/rancher/rke2/rke2.yaml ]; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 60 ]; then
      echo "Timed out waiting for the RKE2 kubeconfig" >&2
      exit 1
    fi
    sleep 5
  done

  install -d -m 0755 -o ubuntu -g ubuntu /home/ubuntu/.kube
  ln -sfn /etc/rancher/rke2/rke2.yaml /home/ubuntu/.kube/config
  chown -h ubuntu:ubuntu /home/ubuntu/.kube/config

  profile_line="export PATH=\$PATH:/var/lib/rancher/rke2/bin"
  if ! grep -Fxq "$profile_line" /home/ubuntu/.profile; then
    printf "%s\n" "$profile_line" >> /home/ubuntu/.profile
  fi
'
