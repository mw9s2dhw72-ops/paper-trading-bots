#!/usr/bin/env bash
# Public HTTPS tunnel via tunn3l.sh (outbound WSS on 443; no account). Fixed subdomain => stable URL across restarts
# (as long as nobody else takes the name while the tunnel is down). The secret token path is the access control.
# (Cloudflare quick tunnels were tried first but this box's egress blocks Cloudflare's tunnel port 7844.)
cd "$(dirname "$0")"
TOKEN=$(cat token.txt); SUB=$(cat subdomain.txt)
./bin/tunn3l http 8787 --subdomain "$SUB" --json 2>&1 | while IFS= read -r line; do
  echo "[$(date -u +%FT%TZ)] $line"
  url=$(echo "$line" | grep -oE 'https://[a-z0-9-]+\.tunn3l\.sh' | head -1)
  if [ -n "$url" ]; then echo "$url/$TOKEN/" > tunnel_url.txt; fi
done
