#!/bin/sh
set -eu

: "${INSTANCE:?}"
: "${LB_ROUTE_CIDR:?}"
: "${LB_CHECK_IP:?}"

if [ "$(uname -s)" != Darwin ]; then
  echo "LoadBalancer routing setup requires macOS" >&2
  exit 1
fi

instance_info=$(multipass info --format csv "$INSTANCE" | awk -F, 'NR == 2 { print $2 " " $3 }')
instance_state=${instance_info%% *}
instance_ip=${instance_info#* }
if [ "$instance_state" != Running ] || [ -z "$instance_ip" ] || [ "$instance_ip" = -- ]; then
  echo "Could not determine the running IPv4 address of $INSTANCE" >&2
  exit 1
fi

if output=$(sudo route -n add -net "$LB_ROUTE_CIDR" "$instance_ip" 2>&1); then
  printf '%s\n' "$output"
else
  case "$output" in
    *"File exists"*) sudo route -n change -net "$LB_ROUTE_CIDR" "$instance_ip" ;;
    *) printf '%s\n' "$output" >&2; exit 1 ;;
  esac
fi

route_info=$(route -n get "$LB_CHECK_IP")
printf '%s\n' "$route_info"
configured_gateway=$(printf '%s\n' "$route_info" | awk '$1 == "gateway:" { print $2; exit }')
if [ "$configured_gateway" != "$instance_ip" ]; then
  echo "LoadBalancer route does not point to $instance_ip" >&2
  exit 1
fi
