#!/bin/sh
set -eu

instance=${1:?Instance name is required}
instance_ip=$(multipass info --format csv "$instance" | awk -F, 'NR == 2 { print $3 }')
if [ -z "$instance_ip" ]; then
  echo "Could not determine the IPv4 address of $instance" >&2
  exit 1
fi

umask 077
destination_dir="$HOME/.kube"
destination="$destination_dir/$instance.yaml"
mkdir -p "$destination_dir"
temporary_dir=$(mktemp -d "$destination_dir/.$instance.XXXXXX")
trap 'rm -r "$temporary_dir"' 0

multipass transfer "$instance:/etc/rancher/rke2/rke2.yaml" "$temporary_dir/rke2.yaml"
if ! grep -Fq 'server: https://127.0.0.1:6443' "$temporary_dir/rke2.yaml"; then
  echo "Expected the RKE2 kubeconfig to use https://127.0.0.1:6443" >&2
  exit 1
fi

sed "s#server: https://127[.]0[.]0[.]1:6443#server: https://$instance_ip:6443#" \
  "$temporary_dir/rke2.yaml" > "$temporary_dir/updated.yaml"
current_context=$(kubectl config --kubeconfig "$temporary_dir/updated.yaml" current-context)
if [ "$current_context" != "$instance" ]; then
  kubectl config --kubeconfig "$temporary_dir/updated.yaml" \
    rename-context "$current_context" "$instance" >/dev/null
fi
kubectl config --kubeconfig "$temporary_dir/updated.yaml" use-context "$instance" >/dev/null
chmod 600 "$temporary_dir/updated.yaml"
mv "$temporary_dir/updated.yaml" "$destination"
echo "Wrote $destination (server: https://$instance_ip:6443)"
