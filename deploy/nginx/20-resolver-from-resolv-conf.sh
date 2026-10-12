#!/bin/sh
# Writes the `resolver` directive nginx.conf includes, taking the address from
# the container's own /etc/resolv.conf.
#
# Why this exists: nginx.conf proxies to `ingest` through a variable so the name
# is re-resolved per request instead of being frozen at config load — see the
# comment there. A variable in proxy_pass requires a `resolver`, and the address
# cannot be written into the config: Podman's aardvark-dns lives on the network
# gateway, which changes whenever the network is recreated. `resolver local=on`
# would read resolv.conf natively but nginx 1.31.6 rejects it, so derive it.
#
# Runs from /docker-entrypoint.d, which the nginx image executes before start.
set -eu

ns=$(awk '/^nameserver/ { print $2; exit }' /etc/resolv.conf)
if [ -z "${ns:-}" ]; then
    echo "resolver-from-resolv-conf: no nameserver in /etc/resolv.conf" >&2
    exit 1
fi

printf 'resolver %s valid=10s ipv6=off;\n' "$ns" > /etc/nginx/resolver.conf
echo "resolver-from-resolv-conf: using $ns"
