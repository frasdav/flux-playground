#!/bin/sh
set -eu

: "${KUBECONFIG:?}"
if [ ! -r "$KUBECONFIG" ]; then
  echo "Kubeconfig is not readable: $KUBECONFIG (run make up first)" >&2
  exit 1
fi

kubectl apply -f flux-operator-helmchart.yaml
kubectl wait --for=create namespace/flux-system --timeout=5m
kubectl wait --for=create deployment/flux-operator -n flux-system --timeout=5m
kubectl rollout status deployment/flux-operator -n flux-system --timeout=5m
kubectl wait --for=condition=Established \
  crd/fluxinstances.fluxcd.controlplane.io --timeout=5m
