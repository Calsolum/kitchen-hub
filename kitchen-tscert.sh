#!/bin/bash
# Renew the Tailscale HTTPS certificate for the phone app's address and reload nginx.
# Run as root (a systemd timer, weekly). `tailscale cert` only fetches a new certificate when
# the current one is close to expiry, so running it often is cheap.
set -euo pipefail
NAME=kitchen.your-tailnet.ts.net          # the Pi's MagicDNS name
DIR=/home/kitchenpi/grocy/nginx/ts        # mounted into the nginx container as /etc/nginx/ts
tailscale cert --cert-file "$DIR/ts.crt" --key-file "$DIR/ts.key" "$NAME"
docker exec nginx nginx -t
docker exec nginx nginx -s reload
echo "expires: $(openssl x509 -in "$DIR/ts.crt" -noout -enddate | cut -d= -f2)"
