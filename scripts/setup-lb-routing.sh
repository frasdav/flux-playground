#!/bin/sh
set -eu

: "${INSTANCE:?}"
: "${LB_ROUTE_CIDR:?}"
: "${LB_CHECK_IP:?}"

host_os=$(uname -s)
case "$host_os" in
  Darwin|Linux) ;;
  *) echo "Unsupported host OS for LoadBalancer routing: $host_os" >&2; exit 1 ;;
esac

instance_info=$(multipass info --format csv "$INSTANCE" | awk -F, 'NR == 2 { print $2 " " $3 }')
instance_state=${instance_info%% *}
instance_ip=${instance_info#* }
if [ "$instance_state" != Running ] || [ -z "$instance_ip" ] || [ "$instance_ip" = -- ]; then
  echo "Could not determine the running IPv4 address of $INSTANCE" >&2
  exit 1
fi

case "$host_os" in
  Darwin)
    if output=$(sudo route -n add -net "$LB_ROUTE_CIDR" "$instance_ip" 2>&1); then
      printf '%s\n' "$output"
    else
      case "$output" in
        *"File exists"*) sudo route -n change -net "$LB_ROUTE_CIDR" "$instance_ip" ;;
        *) printf '%s\n' "$output" >&2; exit 1 ;;
      esac
    fi
    route_info=$(route -n get "$LB_CHECK_IP")
    configured_gateway=$(printf '%s\n' "$route_info" | awk '$1 == "gateway:" { print $2; exit }')
    ;;
  Linux)
    sudo ip -4 route replace "$LB_ROUTE_CIDR" via "$instance_ip"
    route_info=$(ip -4 route get "$LB_CHECK_IP")
    configured_gateway=$(printf '%s\n' "$route_info" | awk '{ for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }')
    ;;
esac
printf '%s\n' "$route_info"
if [ "$configured_gateway" != "$instance_ip" ]; then
  echo "LoadBalancer route does not point to $instance_ip" >&2
  exit 1
fi
