#!/usr/bin/env bash
# Deploy verify.velda.ai on the Traefik box (the machine at 2.24.64.239).
# Run as root:  sudo bash install-verify.sh
#
# What it does, in order:
#   1. creates /var/www/verify.velda.ai and copies the site files there
#   2. installs and starts a systemd service serving it on 127.0.0.1:8091
#   3. installs the Traefik route WITHOUT restarting Traefik
#   4. checks it locally, then over https
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBROOT=/var/www/verify.velda.ai
TRAEFIK_DIR=/docker/traefik/config
DOMAIN=verify.velda.ai
PORT=8091

if [ "$(id -u)" -ne 0 ]; then
  echo "Needs root: sudo bash $0" >&2
  exit 1
fi

echo "==> 1/4 site files"
mkdir -p "$WEBROOT"
cp -a "$HERE/verify.velda.ai/." "$WEBROOT/"
# Serve as an unprivileged user where one exists.
RUNAS=root
if id www-data >/dev/null 2>&1; then RUNAS=www-data; chown -R www-data:www-data "$WEBROOT"; fi
echo "    $WEBROOT ready ($(find "$WEBROOT" -type f | wc -l) files), served as $RUNAS"

echo "==> 2/4 systemd service"
cat > /etc/systemd/system/verify-site.service <<UNIT
[Unit]
Description=Static site for $DOMAIN
After=network.target

[Service]
Type=simple
User=$RUNAS
WorkingDirectory=$WEBROOT
ExecStart=/usr/bin/python3 -m http.server $PORT --bind 127.0.0.1
Restart=always
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=$WEBROOT

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now verify-site
sleep 1
systemctl is-active --quiet verify-site && echo "    verify-site is running" || { journalctl -u verify-site -n 20 --no-pager; exit 1; }

echo "==> 3/4 traefik route (no restart)"
if [ -d "$TRAEFIK_DIR" ]; then
  cp -a "$HERE/verify.yml" "$TRAEFIK_DIR/verify.yml"
  echo "    wrote $TRAEFIK_DIR/verify.yml — Traefik's file provider picks it up on its own"
else
  echo "    !! $TRAEFIK_DIR does not exist on this machine — put verify.yml where this host's"
  echo "       Traefik file provider actually watches, then re-run step 4."
fi

echo "==> 4/4 checks"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$PORT/")
echo "    local  http://127.0.0.1:$PORT/  -> $code"
if [ "$code" != "200" ]; then echo "    local check failed"; exit 1; fi

echo "    waiting for the certificate / route to come up (up to 60s)..."
for i in $(seq 1 12); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://$DOMAIN/" || echo 000)
  echo "      attempt $i: https://$DOMAIN/ -> $code"
  [ "$code" = "200" ] && break
  sleep 5
done

if [ "$code" = "200" ]; then
  echo
  echo "DONE. https://$DOMAIN/ returns 200."
else
  echo
  echo "Not 200 yet. Check, in this order:"
  echo "  - is DNS still DNS-only, pointing at this machine?  dig +short $DOMAIN"
  echo "  - did Traefik load the route?  check its logs for 'verify' or a config error"
  echo "  - does the certificate exist?  check Traefik's acme.json for $DOMAIN"
  exit 1
fi
