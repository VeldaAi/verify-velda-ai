# verify.velda.ai

Public sales site for Get App Verified. The paid guides are **not** in this repository. After Stripe confirms a payment, the buyer gets a personal email from `verify@velda.ai` with a private password-protected link. The link stays active for 4 hours and works on up to 2 IP addresses.

Prices on the site:

- $100 DIY guides. Final sale, except a refund right the law already requires.
- $249 done for you. Refunded if Google rejects the app, unless the customer does not reply for 30 days or removes access before the work is finished.
- $149 upgrade from DIY. The original $100 stays final sale. The $149 follows the done-for-you rule.

Stripe and the intake form are linked from the pages. Card numbers stay on Stripe. The form goes to Formspree. Do not add passwords, tokens, or client secrets to either.

## What is public

`verify.velda.ai/` is the whole public site: home, start form, privacy, terms, refunds, contact, Guide 01 (add `verify@velda.ai` as OAuth Config Editor), and the done-for-you onboarding page (including how to remove access).

## Update an existing server

The live host is the machine at 2.24.64.239, where `verify-site` already listens on `127.0.0.1:8094`. This checkout cannot log into that machine. After this change is merged, on that machine only:

```bash
cd /path/to/this/repo
sudo git pull --ff-only
sudo bash install-verify.sh
```

That script:

1. If port 8094 is held by anything other than `verify-site`, it exits and changes nothing.
2. If `verify-site` already owns 8094, it updates that service in place.
3. It saves a backup under `/var/backups/verify-site/<timestamp>/`.
4. It copies the site to `/var/www/verify.velda.ai` and restarts **only** `verify-site`.
5. It copies `verify.yml` to `/docker/traefik/config/verify.yml`. Traefik is not restarted. Its file provider reloads the file. `/api/stripe/` and `/access/` go to `127.0.0.1:8095`. Everything else on `verify.velda.ai` stays on `127.0.0.1:8094`.
6. If user `quizmynotes` or `/home/quizmynotes/verify-fulfill` exists, it copies `fulfill/verify_fulfill.py` and `fulfill/pdf_stamp.py` into that directory. It does not replace `fulfill.env`, `packs/`, `grants.db`, or `sent.db`.
7. It does not edit other people's ports. Leave `8090`–`8093` alone.

Check, on the server:

```bash
systemctl status verify-site --no-pager
curl -I http://127.0.0.1:8094/
curl -I https://verify.velda.ai/
```

`install-verify.sh` prints DONE only when `https://verify.velda.ai/` returns 200 from that machine. If the public check fails, the local files may already be updated. The script says the public check is unconfirmed. It does not restart Traefik.

Roll back the newest backup (site, unit, and `verify.yml` only):

```bash
sudo bash install-verify.sh --rollback
```

## Tests in this repo

```bash
bash tests/test-install.sh
python3 tests/check-public-site.py
python3 -m unittest discover -s tests
```

The installer test uses a scratch directory and a fake `systemctl`. It does not bind port 8094 and does not change the real host.

## Paid pack

Do not copy the paid guides into `verify.velda.ai/` or into this git repo. On the server they live only under `/home/quizmynotes/verify-fulfill/packs/` (`diy/`, `dfy/`, and the two PDFs). The public site must keep returning 404 for `/diy/`, `/dfy/`, and the pack filenames.

`fulfill/verify_fulfill.py` listens on `127.0.0.1:8095`. Stripe should send `checkout.session.completed` to `https://verify.velda.ai/api/stripe/webhook`. Only the three Get App Verified Payment Links are fulfilled. A paid session gets one email, with a link `https://verify.velda.ai/access/<token>` and a generated password. The password is stored only as a salted PBKDF2 hash. The link lasts 4 hours and accepts at most 2 IP addresses. The PDF download is watermarked with the buyer's email and Stripe session id.

Secrets go in `/home/quizmynotes/verify-fulfill/fulfill.env` (see `fulfill/fulfill.env.example`). To mint a pass by hand:

```bash
python3 /home/quizmynotes/verify-fulfill/verify_fulfill.py mint --plan diy_100 --email buyer@example.com --name "Ada"
```

Add `--send-email` to deliver that pass through SMTP. The service unit is `fulfill/verify-fulfill.service`.
