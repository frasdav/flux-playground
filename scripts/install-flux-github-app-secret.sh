#!/bin/sh
set -eu

: "${KUBECONFIG:?}"
: "${FLUX_GITHUB_APP_ID:?}"
: "${FLUX_GITHUB_APP_INSTALLATION_OWNER:?}"

if [ -z "${FLUX_GITHUB_APP_PRIVATE_KEY:-}" ]; then
  : "${FLUX_GITHUB_APP_PRIVATE_KEY_OP_REF:?Set a 1Password secret reference or FLUX_GITHUB_APP_PRIVATE_KEY}"
  FLUX_GITHUB_APP_PRIVATE_KEY=$(op read --account frontierhq "$FLUX_GITHUB_APP_PRIVATE_KEY_OP_REF")
fi
: "${FLUX_GITHUB_APP_PRIVATE_KEY:?}"

if [ ! -r "$KUBECONFIG" ]; then
  echo "Kubeconfig is not readable: $KUBECONFIG (run make up first)" >&2
  exit 1
fi

kubectl get namespace flux-system >/dev/null
{
  printf '%s\n' '---' 'apiVersion: v1' 'kind: Secret' 'metadata:' \
    '  name: flux-github-app' '  namespace: flux-system' 'type: Opaque' 'data:'
  printf '  githubAppID: %s\n' "$(printf '%s' "$FLUX_GITHUB_APP_ID" | base64 | tr -d '\n')"
  printf '  githubAppInstallationOwner: %s\n' "$(printf '%s' "$FLUX_GITHUB_APP_INSTALLATION_OWNER" | base64 | tr -d '\n')"
  printf '  githubAppPrivateKey: %s\n' "$(printf '%s' "$FLUX_GITHUB_APP_PRIVATE_KEY" | base64 | tr -d '\n')"
} | kubectl apply -f -
