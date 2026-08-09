#!/bin/bash
# OFFLINE_LOCAL sandbox network namespace (Phase 0B hardening).
# Private netns whose ONLY permitted egress is the inference-only Ollama proxy on
# the netns gateway IP. No DNS, no internet, no LAN, no IPv6. Independent of any
# Prime-era bridge (the proxy forwards straight to local Ollama).
set -e
NS=rdsbx-offline
VM=veth-rdo-m ; VJ=veth-rdo-j
NET=10.201.0 ; PROXY_PORT=11435

ip netns del $NS 2>/dev/null || true
ip link del $VM 2>/dev/null || true
ip netns add $NS
ip link add $VM type veth peer name $VJ
ip link set $VJ netns $NS
ip addr add ${NET}.1/24 dev $VM
ip link set $VM up
ip netns exec $NS ip addr add ${NET}.2/24 dev $VJ
ip netns exec $NS ip link set $VJ up
ip netns exec $NS ip link set lo up
ip netns exec $NS ip route add default via ${NET}.1

# IPv6 fully disabled inside the namespace (belt: inet default-drop also blocks v6).
ip netns exec $NS sysctl -q -w net.ipv6.conf.all.disable_ipv6=1 || true
ip netns exec $NS sysctl -q -w net.ipv6.conf.default.disable_ipv6=1 || true
ip netns exec $NS sysctl -q -w net.ipv6.conf.lo.disable_ipv6=1 || true

# Jail firewall (inet family => governs BOTH ipv4 and ipv6). Default drop; only
# the inference proxy on the gateway IP is reachable.
ip netns exec $NS nft -f - <<JNFT
table inet sbxfw {
	chain output {
		type filter hook output priority 0; policy drop;
		oifname "lo" accept
		ct state established,related accept
		ip daddr ${NET}.1 tcp dport ${PROXY_PORT} accept
	}
}
JNFT

# No resolver at all inside the namespace.
mkdir -p /etc/netns/$NS
: > /etc/netns/$NS/resolv.conf
echo "rdsbx-offline up: proxy gw ${NET}.1:${PROXY_PORT}, default-drop, no DNS, IPv6 off"
