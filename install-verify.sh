#!/usr/bin/env bash
# Deploy or update verify.velda.ai on the Traefik box (2.24.64.239).
# Run as root:
#   sudo bash install-verify.sh
#   sudo bash install-verify.sh --rollback
#
# What a normal run does, in order:
#   1. If 127.0.0.1:8094 is already verify-site, update that service in place.
#      If anything else owns the port, stop and change nothing.
#   2. Backs up the current webroot, unit, and Traefik verify.yml (if present).
#   3. Copies verify.velda.ai/ to /var/www/verify.velda.ai
#   4. Writes and restarts ONLY systemd unit verify-site (127.0.0.1:8094).
#   5. Copies verify.yml into /docker/traefik/config/ and does NOT restart Traefik.
#   6. Checks http://127.0.0.1:8094/ . On failure, restores the backup.
#   7. Polls https://verify.velda.ai/ . A failed poll does not pretend the public site is confirmed.
#
# Test runs set VERIFY_TEST=1 and VERIFY_PREFIX to a scratch directory.
# Those variables are ignored unless VERIFY_TEST=1. Do not set VERIFY_TEST on the server.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN=verify.velda.ai
PORT=8094
SITE_SRC="$HERE/verify.velda.ai"

ACTION=install
if [ "${1:-}" = "--rollback" ]; then
  ACTION=rollback
elif [ -n "${1:-}" ]; then
  echo "Unknown argument: $1" >&2
  echo "Use: sudo bash install-verify.sh [--rollback]" >&2
  exit 1
fi

if [ "${VERIFY_TEST:-}" = "1" ]; then
  PREFIX="${VERIFY_PREFIX:?VERIFY_PREFIX is required when VERIFY_TEST=1}"
  PORT="${VERIFY_PORT:-8094}"
  if [ -n "${VERIFY_SITE_SRC:-}" ]; then
    SITE_SRC="$VERIFY_SITE_SRC"
  fi
else
  PREFIX=""
  if [ "$(id -u)" -ne 0 ]; then
    echo "Needs root: sudo bash $0" >&2
    exit 1
  fi
fi

WEBROOT="$PREFIX/var/www/verify.velda.ai"
UNIT_DIR="$PREFIX/etc/systemd/system"
UNIT="$UNIT_DIR/verify-site.service"
TRAEFIK_DIR="$PREFIX/docker/traefik/config"
BACKUP_ROOT="$PREFIX/var/backups/verify-site"

MUTATED=0
BACKUP_DEST=""
ROLLING_BACK=0

restore_backup() {
  local dest="$1"
  if [ -d "$dest/webroot" ]; then
    rm -rf "$WEBROOT"
    mkdir -p "$(dirname "$WEBROOT")"
    cp -a "$dest/webroot" "$WEBROOT"
  elif [ -f "$dest/no-webroot" ]; then
    rm -rf "$WEBROOT"
  fi
  if [ -f "$dest/verify-site.service" ]; then
    mkdir -p "$UNIT_DIR"
    cp -a "$dest/verify-site.service" "$UNIT"
  elif [ -f "$dest/no-unit" ]; then
    rm -f "$UNIT"
  fi
  if [ -d "$TRAEFIK_DIR" ]; then
    if [ -f "$dest/traefik-verify.yml" ]; then
      cp -a "$dest/traefik-verify.yml" "$TRAEFIK_DIR/verify.yml"
    elif [ -f "$dest/no-traefik-yml" ]; then
      rm -f "$TRAEFIK_DIR/verify.yml"
    fi
  fi
}

rollback_mutated() {
  if [ "$MUTATED" = "1" ] && [ -n "$BACKUP_DEST" ] && [ "$ROLLING_BACK" != "1" ]; then
    echo "Restoring $BACKUP_DEST" >&2
    ROLLING_BACK=1
    restore_backup "$BACKUP_DEST" || true
    systemctl daemon-reload || true
    systemctl restart verify-site || true
  fi
}

die() {
  echo "$*" >&2
  rollback_mutated
  exit 1
}

on_err() {
  local code=$?
  echo "Command failed (exit $code). Restoring backup if this run already changed files." >&2
  rollback_mutated
  exit "$code"
}
trap on_err ERR

port_is_listening() {
  ss -ltn 2>/dev/null | grep -E -q "[:.]${PORT}([^0-9]|$)"
}

listening_pids() {
  { ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}([^0-9]|$)" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u; } || true
}

our_main_pid() {
  if [ ! -f "$UNIT" ]; then
    return 1
  fi
  if ! grep -q "http.server ${PORT}" "$UNIT"; then
    return 1
  fi
  if ! grep -q "127.0.0.1" "$UNIT"; then
    return 1
  fi
  if ! systemctl is-active --quiet verify-site; then
    return 1
  fi
  local pid
  pid="$(systemctl show -p MainPID --value verify-site 2>/dev/null || true)"
  case "$pid" in
    ""|0|*[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$pid"
}

listener_is_ours() {
  local pid ours=""
  if ! pid="$(our_main_pid)"; then
    return 1
  fi
  while IFS= read -r ours; do
    [ -z "$ours" ] && continue
    if [ "$ours" = "$pid" ]; then
      return 0
    fi
  done < <(listening_pids)
  return 1
}

make_backup() {
  local stamp dest
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dest="$BACKUP_ROOT/$stamp"
  mkdir -p "$dest"
  if [ -d "$WEBROOT" ]; then
    cp -a "$WEBROOT" "$dest/webroot"
  else
    : > "$dest/no-webroot"
  fi
  if [ -f "$UNIT" ]; then
    cp -a "$UNIT" "$dest/verify-site.service"
  else
    : > "$dest/no-unit"
  fi
  if [ -f "$TRAEFIK_DIR/verify.yml" ]; then
    cp -a "$TRAEFIK_DIR/verify.yml" "$dest/traefik-verify.yml"
  else
    : > "$dest/no-traefik-yml"
  fi
  cat > "$dest/README.txt" <<NOTE
Backup of verify-site taken before an update.
Restore the newest backup with:
  sudo bash install-verify.sh --rollback
This restores only /var/www/verify.velda.ai, verify-site.service, and
/docker/traefik/config/verify.yml. It does not restart Traefik and it does
not stop or edit any other service.
NOTE
  ln -sfn "$dest" "$BACKUP_ROOT/latest"
  printf '%s\n' "$dest"
}

write_unit() {
  local runas="root"
  if [ "${VERIFY_TEST:-}" = "1" ]; then
    runas="$(id -un)"
  elif id www-data >/dev/null 2>&1; then
    runas="www-data"
  fi
  mkdir -p "$UNIT_DIR" "$WEBROOT"
  if [ "$runas" = "www-data" ]; then
    chown -R www-data:www-data "$WEBROOT"
  fi
  cat > "$UNIT" <<UNIT
[Unit]
Description=Static site for $DOMAIN
After=network.target

[Service]
Type=simple
User=$runas
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
}

install_site_files() {
  if [ ! -f "$SITE_SRC/index.html" ]; then
    echo "Missing $SITE_SRC/index.html" >&2
    return 1
  fi
  rm -rf "$WEBROOT"
  mkdir -p "$WEBROOT"
  cp -a "$SITE_SRC/." "$WEBROOT/"
}

copy_traefik() {
  if [ -d "$TRAEFIK_DIR" ]; then
    cp -a "$HERE/verify.yml" "$TRAEFIK_DIR/verify.yml"
    echo "    wrote $TRAEFIK_DIR/verify.yml — Traefik's file provider picks it up on its own. Traefik was not restarted."
  else
    echo "    !! $TRAEFIK_DIR does not exist on this machine — put verify.yml where this host's"
    echo "       Traefik file provider actually watches, then check https yourself."
  fi
}

local_health() {
  local i code="000"
  if [ "${VERIFY_TEST:-}" = "1" ] && [ "${VERIFY_FORCE_HEALTH_FAIL:-}" = "1" ]; then
    echo "500"
    return 0
  fi
  for i in 1 2 3 4 5 6 7 8 9 10; do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/" || true)"
    if [ "$code" = "200" ]; then
      echo "200"
      return 0
    fi
    sleep 0.3
  done
  echo "$code"
}

restart_ours() {
  systemctl daemon-reload
  if [ "$1" = "update" ]; then
    systemctl restart verify-site
  else
    systemctl enable --now verify-site
  fi
  sleep 0.4
  systemctl is-active --quiet verify-site
}

do_rollback() {
  local dest=""
  if [ -L "$BACKUP_ROOT/latest" ]; then
    dest="$(readlink -f "$BACKUP_ROOT/latest")"
  fi
  if [ -z "$dest" ] || [ ! -d "$dest" ]; then
    echo "No backup found in $BACKUP_ROOT. Nothing was changed." >&2
    exit 1
  fi
  echo "==> restoring $dest"
  ROLLING_BACK=1
  restore_backup "$dest"
  systemctl daemon-reload
  systemctl restart verify-site
  sleep 0.4
  local code
  code="$(local_health)"
  echo "    local http://127.0.0.1:$PORT/ -> $code"
  if [ "$code" != "200" ]; then
    echo "Rollback restored files, but the local check is not 200." >&2
    exit 1
  fi
  echo "Rollback finished. Only verify-site was restarted. Traefik was not restarted."
}

if [ "$ACTION" = "rollback" ]; then
  do_rollback
  exit 0
fi

if ! command -v ss >/dev/null 2>&1; then
  echo "FATAL: ss is required to see who owns port $PORT. Nothing was changed." >&2
  exit 1
fi

echo "==> 0/4 port $PORT"
MODE=install
if port_is_listening; then
  if listener_is_ours; then
    MODE=update
    echo "    port $PORT is verify-site (pid $(our_main_pid)). Updating that service in place."
  else
    echo "FATAL: port $PORT is already in use, and it is not verify-site." >&2
    ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}([^0-9]|$)" >&2 || true
    echo "Nothing was changed. This script will not stop another program, and it will not" >&2
    echo "switch ports. Leave 8091 and 8092 alone. Fix the listener, or set a new port in" >&2
    echo "BOTH this script and verify.yml only if you are sure the new port is free." >&2
    exit 1
  fi
else
  echo "    port $PORT is free"
fi

echo "==> 1/4 backup"
BACKUP_DEST="$(make_backup)"
echo "    $BACKUP_DEST"

echo "==> 2/4 site files ($MODE)"
MUTATED=1
install_site_files
RUNAS_NOTE="root"
if [ "${VERIFY_TEST:-}" != "1" ] && id www-data >/dev/null 2>&1; then
  RUNAS_NOTE="www-data"
fi
echo "    $WEBROOT ready ($(find "$WEBROOT" -type f | wc -l) files), service user $RUNAS_NOTE"

echo "==> 3/4 systemd verify-site (no other services)"
write_unit
if ! restart_ours "$MODE"; then
  journalctl -u verify-site -n 20 --no-pager >&2 || true
  die "verify-site did not stay active."
fi
echo "    verify-site is running"

copy_traefik

echo "==> 4/4 checks"
code="$(local_health)"
echo "    local  http://127.0.0.1:$PORT/  -> $code"
if [ "$code" != "200" ]; then
  die "Local check failed (HTTP $code)."
fi

if [ "${VERIFY_TEST:-}" = "1" ]; then
  echo
  echo "DONE (test mode). Local verify-site on port $PORT returned 200. Public HTTPS was not checked."
  exit 0
fi

echo "    waiting for the certificate / route to come up (up to 60s)..."
echo "    This does not restart Traefik. A non-200 here means the public check is unconfirmed."
for i in $(seq 1 12); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://$DOMAIN/" || echo 000)"
  echo "      attempt $i: https://$DOMAIN/ -> $code"
  [ "$code" = "200" ] && break
  sleep 5
done

if [ "$code" = "200" ]; then
  echo
  echo "DONE. https://$DOMAIN/ returned 200 from this machine."
else
  echo
  echo "Local site is up on 127.0.0.1:$PORT, but https://$DOMAIN/ did not return 200."
  echo "Files were updated. Traefik was not restarted. Backup: $BACKUP_DEST"
  echo "This script is not calling the public site confirmed. Check, in this order:"
  echo "  - is DNS still pointing at this machine?  dig +short $DOMAIN"
  echo "  - did Traefik load the route?  check its logs for 'verify' or a config error"
  echo "  - does the certificate exist?  check Traefik's acme.json for $DOMAIN"
  echo "  - roll back with: sudo bash install-verify.sh --rollback"
  exit 1
fi
