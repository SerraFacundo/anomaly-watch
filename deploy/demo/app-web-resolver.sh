#!/bin/sh
# Points the app's web nginx at the container's real DNS server.
#
# The app's nginx.conf.template hardcodes `resolver 127.0.0.11`, Docker's
# embedded DNS. Under Podman that address answers nothing (aardvark-dns lives
# on the network gateway), so every /api request failed to resolve `api` and the
# login page said "Unable to sign in". Same fix as deploy/nginx's resolver
# script, applied after the image's envsubst step (20-*) has written the config.
set -eu

ns=$(awk '/^nameserver/ { print $2; exit }' /etc/resolv.conf)
[ -n "${ns:-}" ] || { echo "app-web-resolver: no nameserver in /etc/resolv.conf" >&2; exit 1; }
sed -i "s/resolver 127\.0\.0\.11/resolver $ns/" /etc/nginx/conf.d/*.conf
echo "app-web-resolver: using $ns"
