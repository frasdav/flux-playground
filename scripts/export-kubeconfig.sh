#!/bin/sh
set -eu

cluster_name=${1:?Cluster name is required}
cluster_ip=$(multipass info --format csv "$cluster_name" | awk -F, 'NR == 2 { print $3 }')
if [ -z "$cluster_ip" ]; then
  echo "Could not determine the IPv4 address of $cluster_name" >&2
  exit 1
fi

umask 077
destination_dir="$HOME/.kube"
destination="$destination_dir/$cluster_name.yaml"
mkdir -p "$destination_dir"
temporary_dir=$(mktemp -d "$destination_dir/.$cluster_name.XXXXXX")
trap 'rm -r "$temporary_dir"' 0

# Let the host shell write hidden paths that the Multipass snap cannot access.
multipass exec "$cluster_name" -- cat /etc/rancher/rke2/rke2.yaml > "$temporary_dir/rke2.yaml"
if ! grep -Fq 'server: https://127.0.0.1:6443' "$temporary_dir/rke2.yaml"; then
  echo "Expected the RKE2 kubeconfig to use https://127.0.0.1:6443" >&2
  exit 1
fi

sed "s#server: https://127[.]0[.]0[.]1:6443#server: https://$cluster_ip:6443#" \
  "$temporary_dir/rke2.yaml" > "$temporary_dir/updated.yaml"
current_context=$(kubectl config --kubeconfig "$temporary_dir/updated.yaml" current-context)
if [ "$current_context" != "$cluster_name" ]; then
  kubectl config --kubeconfig "$temporary_dir/updated.yaml" \
    rename-context "$current_context" "$cluster_name" >/dev/null
fi
kubectl config --kubeconfig "$temporary_dir/updated.yaml" use-context "$cluster_name" >/dev/null
chmod 600 "$temporary_dir/updated.yaml"
mv "$temporary_dir/updated.yaml" "$destination"
echo "Wrote $destination (server: https://$cluster_ip:6443)"
