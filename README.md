# FOR GROK BOT — deploy verify.velda.ai

This package does the whole job on the Traefik machine. Nothing here belongs on the
Hermes server (82.25.91.25) — **verify.velda.ai points at 2.24.64.239**, and only that
machine can serve it.

## Run it

Copy the folder to the Traefik host, then:

```bash
sudo bash install-verify.sh
```

That is the entire deployment. It does steps 1–4 of the brief:

1. creates `/var/www/verify.velda.ai/` and copies `verify.velda.ai/` (index.html + guides/) into it
2. writes and starts `/etc/systemd/system/verify-site.service` — `python3 -m http.server 8092 --bind 127.0.0.1`
3. copies `verify.yml` into `/docker/traefik/config/` — **Traefik is not restarted**; its file provider hot-reloads
4. checks `http://127.0.0.1:8092/` then polls `https://verify.velda.ai/` until it returns 200

## Checks it must satisfy

```bash
systemctl status verify-site --no-pager
curl -I http://127.0.0.1:8092/            # expect 200
curl -I https://verify.velda.ai/          # expect 200  ← the acceptance check
dig +short verify.velda.ai                # expect 2.24.64.239
```

## Where Jason's tarball goes

Replace the contents of `/var/www/verify.velda.ai/` with it. **No restart** — the static
server reads from disk per request:

```bash
sudo tar -xzf /path/to/site.tar.gz -C /var/www/verify.velda.ai/
```

## If something fails

| Symptom | Cause |
|---|---|
| local 8092 → 000 | service not running: `journalctl -u verify-site -n 50` |
| https → 404 | Traefik has no router for the host: is `verify.yml` in the directory its file provider watches? |
| https → 000 / TLS error | no certificate: check Traefik's `acme.json`, and that DNS is still DNS-only pointing here |
| https works but wrong site | another router matches `verify.velda.ai` first — check the other files in that config dir |

## Notes

- The config dir may not be `/docker/traefik/config` on this host. The script says so
  plainly if it isn't, rather than pretending it succeeded.
- The unit runs as `www-data` where that user exists, with the webroot read-only and
  `NoNewPrivileges` — a static file server has no business holding more than that.
- Rollback: `sudo systemctl disable --now verify-site && sudo rm /docker/traefik/config/verify.yml && sudo rm -rf /var/www/verify.velda.ai`
