#!/bin/sh
set -eu

: "${KUBECONFIG:?}"
: "${CLUSTER_NAME:?}"
: "${LETSENCRYPT_EMAIL:?}"
: "${FLEET_BRANCH:?}"

case "$CLUSTER_NAME" in
  *[!a-z0-9-]*|'') echo "Invalid cluster name: $CLUSTER_NAME" >&2; exit 1 ;;
esac
case "$FLEET_BRANCH" in
  *[!a-zA-Z0-9._/-]*|'') echo "Invalid fleet branch: $FLEET_BRANCH" >&2; exit 1 ;;
esac
case "$LETSENCRYPT_EMAIL" in
  *@*.*) ;;
  *) echo "Invalid Let's Encrypt email: $LETSENCRYPT_EMAIL" >&2; exit 1 ;;
esac
case "$LETSENCRYPT_EMAIL" in
  *[!a-zA-Z0-9._%+@-]*|*@*@*) echo "Invalid Let's Encrypt email: $LETSENCRYPT_EMAIL" >&2; exit 1 ;;
esac
if [ ! -r "$KUBECONFIG" ]; then
  echo "Kubeconfig is not readable: $KUBECONFIG (run make up first)" >&2
  exit 1
fi
kubectl get secret flux-github-app -n flux-system >/dev/null

temporary_dir=$(mktemp -d)
trap 'rm -r "$temporary_dir"' 0
for file in flux-bootstrap/*.yaml; do
  sed -e "s#__CLUSTER_NAME__#$CLUSTER_NAME#g" \
    -e "s#__LETSENCRYPT_EMAIL__#$LETSENCRYPT_EMAIL#g" \
    -e "s#__FLEET_BRANCH__#$FLEET_BRANCH#g" \
    "$file" > "$temporary_dir/${file##*/}"
done
kubectl apply -k "$temporary_dir"
