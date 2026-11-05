#!/usr/bin/env bash
# Sandbox tests for install-verify.sh. Does not touch /var/www, systemd, or Traefik.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL="$ROOT/install-verify.sh"
TMP="$(mktemp -d)"
cleanup() {
  if [ -n "${PIDS:-}" ]; then
    for p in $PIDS; do kill "$p" 2>/dev/null || true; done
  fi
  if [ -f "$TMP/state/pid" ]; then kill "$(cat "$TMP/state/pid")" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT
PIDS=""

free_port() {
  python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
}

mkdir -p "$TMP/bin" "$TMP/state"
cat > "$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cmd="$1"
shift
start() {
  if [ -f "$VERIFY_FAKE_STATE/pid" ]; then
    old="$(cat "$VERIFY_FAKE_STATE/pid" || true)"
    if [ -n "${old:-}" ] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
      sleep 0.4
    fi
  fi
  wd="$(sed -n 's/^WorkingDirectory=//p' "$VERIFY_FAKE_UNIT")"
  execstart="$(sed -n 's/^ExecStart=//p' "$VERIFY_FAKE_UNIT")"
  try=0
  while [ "$try" -lt 8 ]; do
    bash -c "cd $(printf '%q' "$wd") && exec $execstart" >"$VERIFY_FAKE_STATE/http.log" 2>&1 &
    echo $! >"$VERIFY_FAKE_STATE/pid"
    sleep 0.25
    if kill -0 "$(cat "$VERIFY_FAKE_STATE/pid")" 2>/dev/null; then
      return 0
    fi
    try=$((try + 1))
    sleep 0.3
  done
  echo "fake systemctl could not bind" >&2
  cat "$VERIFY_FAKE_STATE/http.log" >&2 || true
  return 1
}
case "$cmd" in
  is-active)
    pid="$(cat "$VERIFY_FAKE_STATE/pid" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then exit 0; fi
    exit 3
    ;;
  show)
    cat "$VERIFY_FAKE_STATE/pid"
    ;;
  daemon-reload) exit 0 ;;
  enable|restart) start ;;
  *) echo "unhandled systemctl $cmd $*" >&2; exit 1 ;;
esac
EOF
cat > "$TMP/bin/journalctl" <<'EOF'
#!/usr/bin/env bash
echo "fake journalctl"
EOF
chmod +x "$TMP/bin/systemctl" "$TMP/bin/journalctl"

export PATH="$TMP/bin:$PATH"
export VERIFY_TEST=1
export VERIFY_FAKE_STATE="$TMP/state"

assert() {
  if ! eval "$2"; then
    echo "FAIL: $1" >&2
    exit 1
  fi
  echo "ok: $1"
}

# --- unrelated listener is refused and left running ---
PORT1="$(free_port)"
UNREL="$TMP/unrelated"
mkdir -p "$UNREL"
printf 'UNRELATED-SENTINEL' >"$UNREL/index.html"
(
  cd "$UNREL"
  exec python3 -m http.server "$PORT1" --bind 127.0.0.1
) >"$TMP/unrelated.log" 2>&1 &
PIDS="$PIDS $!"
sleep 0.4
PREFIX1="$TMP/prefix-conflict"
mkdir -p "$PREFIX1"
export VERIFY_PREFIX="$PREFIX1"
export VERIFY_PORT="$PORT1"
export VERIFY_FAKE_UNIT="$PREFIX1/etc/systemd/system/verify-site.service"
set +e
bash "$INSTALL" >"$TMP/conflict.out" 2>"$TMP/conflict.err"
conflict_code=$?
set -e
assert "conflict exits non-zero" "[ $conflict_code -ne 0 ]"
assert "conflict says nothing changed" "grep -q 'Nothing was changed' $TMP/conflict.err"
assert "conflict did not create webroot" "[ ! -d $PREFIX1/var/www/verify.velda.ai ]"
body="$(curl -fsS "http://127.0.0.1:$PORT1/")"
printf '%s' "$body" | grep -q 'UNRELATED-SENTINEL'
echo "ok: unrelated listener still serving"
kill $PIDS 2>/dev/null || true
PIDS=""
sleep 0.3

# --- update an existing verify-site, then roll it back ---
PORT2="$(free_port)"
PREFIX2="$TMP/prefix-update"
mkdir -p "$PREFIX2/var/www/verify.velda.ai" "$PREFIX2/etc/systemd/system" "$PREFIX2/docker/traefik/config"
echo "OLD-SITE-MARKER" >"$PREFIX2/var/www/verify.velda.ai/index.html"
echo "LEAVE-ME" >"$PREFIX2/docker/traefik/config/other.yml"
UNIT="$PREFIX2/etc/systemd/system/verify-site.service"
cat >"$UNIT" <<UNIT
[Service]
User=$(id -un)
WorkingDirectory=$PREFIX2/var/www/verify.velda.ai
ExecStart=/usr/bin/python3 -m http.server $PORT2 --bind 127.0.0.1
UNIT
export VERIFY_PREFIX="$PREFIX2"
export VERIFY_PORT="$PORT2"
export VERIFY_FAKE_UNIT="$UNIT"
unset VERIFY_FORCE_HEALTH_FAIL || true
bash "$TMP/bin/systemctl" restart verify-site
sleep 0.3
bash "$INSTALL"
curl -fsS "http://127.0.0.1:$PORT2/" | grep -q 'Get your app ready'
echo "ok: update serves the new home page"
grep -q OLD-SITE-MARKER "$PREFIX2"/var/backups/verify-site/*/webroot/index.html
echo "ok: backup kept the old page"
test "$(cat "$PREFIX2/docker/traefik/config/other.yml")" = "LEAVE-ME"
echo "ok: other traefik file untouched"
grep -q 'Host(`verify.velda.ai`)' "$PREFIX2/docker/traefik/config/verify.yml"
grep -q '127.0.0.1:8094' "$PREFIX2/docker/traefik/config/verify.yml"
echo "ok: route file keeps backticks and 8094"
grep -q "http.server $PORT2" "$UNIT"
echo "ok: unit listens on the sandbox port"

bash "$INSTALL" --rollback
curl -fsS "http://127.0.0.1:$PORT2/" | grep -q 'OLD-SITE-MARKER'
echo "ok: rollback restored the previous page"

bash "$INSTALL"
curl -fsS "http://127.0.0.1:$PORT2/" | grep -q 'Get your app ready'
bash "$INSTALL"
curl -fsS "http://127.0.0.1:$PORT2/" | grep -q 'Get your app ready'
echo "ok: second update still serves the home page"

BAD="$TMP/bad-site"
mkdir -p "$BAD"
printf 'SHOULD-NOT-STICK' >"$BAD/index.html"
export VERIFY_SITE_SRC="$BAD"
export VERIFY_FORCE_HEALTH_FAIL=1
set +e
bash "$INSTALL" >"$TMP/fail.out" 2>"$TMP/fail.err"
fail_code=$?
set -e
unset VERIFY_FORCE_HEALTH_FAIL VERIFY_SITE_SRC
test "$fail_code" -ne 0
echo "ok: failed health check exits non-zero"
curl -fsS "http://127.0.0.1:$PORT2/" | grep -q 'Get your app ready'
if curl -fsS "http://127.0.0.1:$PORT2/" | grep -q 'SHOULD-NOT-STICK'; then
  echo "failed health check left the bad page in place" >&2
  exit 1
fi
echo "ok: failed health check restored the previous page"

echo
echo "installer sandbox tests passed"
