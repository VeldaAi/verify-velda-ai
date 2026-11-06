#!/usr/bin/env python3
"""Paid-order fulfillment for Get App Verified.

After Stripe confirms one of the three Get App Verified Payment Links, this
service emails a private password and a link that works for 4 hours on at most
2 IP addresses. Guide files and per-buyer watermarked PDFs stay behind that
check. Secrets and the paid packs are read from the server, never from git.

Bind address defaults to 127.0.0.1:8095.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import smtplib
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdf_stamp import stamp_pdf

DEFAULT_ENV_FILE = "/home/quizmynotes/verify-fulfill/fulfill.env"
DEFAULT_PACKS_DIR = "/home/quizmynotes/verify-fulfill/packs"
DEFAULT_STATE_DB = "/home/quizmynotes/verify-fulfill/grants.db"
REPLY_TO = "verify@velda.ai"
PUBLIC_ORIGIN = "https://verify.velda.ai"
INTAKE_URL = "https://verify.velda.ai/intake.html"
ADD_US_URL = "https://verify.velda.ai/guides/01-add-us.html"
REPLAY_WINDOW_SECONDS = 300
ACCESS_TTL_SECONDS = 14400
MAX_IPS = 2
MAX_PASSWORD_FAILURES = 8
LOCK_SECONDS = 15 * 60
PENDING_STALE_SECONDS = 120
DEFAULT_PBKDF2_ITERS = 200_000
MAX_BODY_BYTES = 1_000_000
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }
)

PLANS = {
    "plink_1UYUex4Q8AoUpkwuilTPpwSm": "diy_100",
    "plink_1UYUez4Q8AoUpkwuazXeH9LK": "dfy_249",
    "plink_1UYUf14Q8AoUpkwuzdDbxudW": "upgrade_149",
}
PDF_FOR_PLAN = {
    "diy_100": "get-app-verified-diy-pack.pdf",
    "dfy_249": "get-app-verified-dfy-pack.pdf",
    "upgrade_149": "get-app-verified-dfy-pack.pdf",
}
GUIDE_DIR_FOR_PLAN = {
    "diy_100": "diy",
    "dfy_249": "dfy",
    "upgrade_149": "dfy",
}
GUIDE_EXTENSIONS = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".css": "text/css; charset=utf-8",
}
TOKEN_RE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True)
class Config:
    bind_host: str
    bind_port: int
    stripe_webhook_secret: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_name: str
    bcc_email: str
    packs_dir: str
    state_db: str
    cookie_secret: str
    pbkdf2_iters: int = DEFAULT_PBKDF2_ITERS


def load_env_file(path: str) -> None:
    """Fill unset variables from a KEY=value file. Existing environment wins."""
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key.isupper() or not key.replace("_", "").isalnum():
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if key not in os.environ:
                os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_config() -> Config:
    load_env_file(os.environ.get("FULFILL_ENV_FILE", DEFAULT_ENV_FILE))
    try:
        bind_port = int(_env("BIND_PORT", "8095"))
        smtp_port = int(_env("SMTP_PORT", "465"))
    except ValueError as exc:
        raise SystemExit("BIND_PORT and SMTP_PORT must be integers") from exc
    return Config(
        bind_host=_env("BIND_HOST", "127.0.0.1") or "127.0.0.1",
        bind_port=bind_port,
        stripe_webhook_secret=_env("STRIPE_WEBHOOK_SECRET"),
        smtp_host=_env("SMTP_HOST", "smtp.hostinger.com") or "smtp.hostinger.com",
        smtp_port=smtp_port,
        smtp_user=_env("SMTP_USER", "verify@velda.ai") or "verify@velda.ai",
        smtp_password=_env("SMTP_PASSWORD"),
        from_name=_env("FROM_NAME", "Get App Verified") or "Get App Verified",
        bcc_email=_env("BCC_EMAIL", "verify@velda.ai") or "verify@velda.ai",
        packs_dir=_env("PACKS_DIR", DEFAULT_PACKS_DIR) or DEFAULT_PACKS_DIR,
        state_db=_env("STATE_DB", DEFAULT_STATE_DB) or DEFAULT_STATE_DB,
        cookie_secret=_env("COOKIE_SECRET"),
    )


def signatures_ok(secret: str, payload: bytes, header: str, now: float | None = None) -> bool:
    """Verify Stripe-Signature over ``{timestamp}.{raw_body}`` inside 300 seconds."""
    if not secret or not header:
        return False
    timestamp_raw = ""
    candidates: list[str] = []
    for item in header.split(","):
        item = item.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "t" and not timestamp_raw:
            timestamp_raw = value
        elif key == "v1" and value:
            candidates.append(value)
    if not timestamp_raw.isdigit() or not candidates:
        return False
    timestamp = int(timestamp_raw)
    current = time.time() if now is None else now
    if abs(current - timestamp) > REPLAY_WINDOW_SECONDS:
        return False
    signed = timestamp_raw.encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    matched = False
    for candidate in candidates:
        if len(candidate) == len(expected) and hmac.compare_digest(expected, candidate):
            matched = True
    return matched


def cookie_key(cfg: Config) -> bytes:
    if cfg.cookie_secret:
        return cfg.cookie_secret.encode("utf-8")
    if cfg.stripe_webhook_secret:
        return hmac.new(b"verify-fulfill-cookie", cfg.stripe_webhook_secret.encode("utf-8"), hashlib.sha256).digest()
    return b""


def normalize_ip(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().strip("[]")
    if not value or value.lower() == "unknown":
        return None
    if value.count(":") == 1 and "." in value:
        value = value.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def client_ip(peer: str, headers) -> str:
    """Trust forwarding headers only when the TCP peer is local Traefik."""
    peer_ip = normalize_ip(peer) or "0.0.0.0"
    if not is_loopback(peer_ip):
        return peer_ip
    real = headers.get("X-Real-IP") if headers is not None else None
    if isinstance(real, str):
        parsed = normalize_ip(real.split(",")[0])
        if parsed:
            return parsed
    forwarded = headers.get("X-Forwarded-For") if headers is not None else None
    if isinstance(forwarded, str):
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        for part in reversed(parts):
            parsed = normalize_ip(part)
            if parsed:
                return parsed
    return peer_ip


def generate_password() -> str:
    groups = [
        "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(4))
        for _ in range(4)
    ]
    return "-".join(groups)


def hash_password(password: str, iters: int) -> tuple[bytes, bytes]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return salt, digest


def password_matches(password: str, salt: bytes, digest: bytes, iters: int) -> bool:
    if not isinstance(password, str) or len(password) > 200 or iters < 1:
        password = ""
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(check, digest)


def valid_email(addr: str) -> bool:
    if not isinstance(addr, str) or not addr or len(addr) > 254:
        return False
    if any(ch in addr for ch in "\r\n\t "):
        return False
    if addr.count("@") != 1:
        return False
    local, domain = addr.split("@")
    return bool(local) and "." in domain and ".." not in addr


def clean_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    return " ".join(name.replace("\r", " ").replace("\n", " ").split())[:200]


def first_name(full: str) -> str | None:
    parts = clean_name(full).split()
    if not parts:
        return None
    first = parts[0]
    if len(first) > 60 or any(ch in first for ch in "<>@\r\n\t"):
        return None
    return first


def _header_safe(value: str) -> str:
    return "".join(ch for ch in value if ch not in "\r\n")


def payment_link_id(session: dict) -> str | None:
    link = session.get("payment_link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    if isinstance(link, dict):
        ident = link.get("id")
        if isinstance(ident, str) and ident.strip():
            return ident.strip()
    return None


def customer_email(session: dict) -> str:
    details = session.get("customer_details")
    if isinstance(details, dict) and isinstance(details.get("email"), str):
        found = details["email"].strip()
        if found:
            return found
    raw = session.get("customer_email")
    if isinstance(raw, str):
        return raw.strip()
    return ""


def customer_name(session: dict) -> str:
    details = session.get("customer_details")
    if isinstance(details, dict) and isinstance(details.get("name"), str):
        return clean_name(details["name"])
    return ""


def license_line(email: str, session_id: str) -> str:
    return f"Licensed exclusively to {email} · Order {session_id} · Do not share"


def _connect(cfg: Config) -> sqlite3.Connection:
    parent = os.path.dirname(cfg.state_db)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(cfg.state_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grants (
            token TEXT PRIMARY KEY,
            password_salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            password_iters INTEGER NOT NULL,
            buyer_email TEXT NOT NULL,
            buyer_name TEXT NOT NULL DEFAULT '',
            stripe_session_id TEXT NOT NULL UNIQUE,
            plan TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            max_ips INTEGER NOT NULL,
            fulfilled INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grant_ips (
            token TEXT NOT NULL,
            ip TEXT NOT NULL,
            first_seen_at INTEGER NOT NULL,
            PRIMARY KEY (token, ip)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failed_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            ip TEXT NOT NULL,
            failed_at INTEGER NOT NULL
        )
        """
    )
    return conn


def _delete_token(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM failed_attempts WHERE token = ?", (token,))
    conn.execute("DELETE FROM grant_ips WHERE token = ?", (token,))
    conn.execute("DELETE FROM grants WHERE token = ?", (token,))


def load_grant(cfg: Config, token: str) -> sqlite3.Row | None:
    conn = _connect(cfg)
    try:
        return conn.execute("SELECT * FROM grants WHERE token = ?", (token,)).fetchone()
    finally:
        conn.close()


def reserve_grant(
    cfg: Config,
    session_id: str,
    plan: str,
    email: str,
    name: str,
    fulfill_now: bool,
) -> tuple[str, sqlite3.Row | None, str | None]:
    """Mint one grant for a Stripe session. Returns status, row, plaintext password."""
    now = int(time.time())
    password = generate_password()
    salt, digest = hash_password(password, cfg.pbkdf2_iters)
    token = secrets.token_urlsafe(32)
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT token, fulfilled, created_at FROM grants WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
        if row is not None:
            if row["fulfilled"]:
                conn.rollback()
                return "duplicate", None, None
            if now - int(row["created_at"]) < PENDING_STALE_SECONDS:
                conn.rollback()
                return "busy", None, None
            _delete_token(conn, row["token"])
        conn.execute(
            """
            INSERT INTO grants (
                token, password_salt, password_hash, password_iters, buyer_email, buyer_name,
                stripe_session_id, plan, created_at, expires_at, max_ips, fulfilled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                salt,
                digest,
                cfg.pbkdf2_iters,
                email,
                name,
                session_id,
                plan,
                now,
                now + ACCESS_TTL_SECONDS,
                MAX_IPS,
                1 if fulfill_now else 0,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return "busy", None, None
    finally:
        conn.close()
    grant = load_grant(cfg, token)
    return "go", grant, password


def mark_fulfilled(cfg: Config, token: str) -> None:
    conn = _connect(cfg)
    try:
        conn.execute("UPDATE grants SET fulfilled = 1 WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def delete_grant(cfg: Config, token: str) -> None:
    conn = _connect(cfg)
    try:
        _delete_token(conn, token)
        conn.commit()
    finally:
        conn.close()


def failure_count(cfg: Config, token: str, now: int) -> int:
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM failed_attempts WHERE token = ? AND failed_at > ?",
            (token, now - LOCK_SECONDS),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def record_failure(cfg: Config, token: str, ip: str, now: int) -> None:
    conn = _connect(cfg)
    try:
        conn.execute(
            "INSERT INTO failed_attempts (token, ip, failed_at) VALUES (?, ?, ?)",
            (token, ip, now),
        )
        conn.commit()
    finally:
        conn.close()


def claim_ip(cfg: Config, token: str, ip: str, max_ips: int, now: int) -> str:
    """Return ok, full, or expired. A wrong password must never call this."""
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        grant = conn.execute(
            "SELECT expires_at, fulfilled FROM grants WHERE token = ?",
            (token,),
        ).fetchone()
        if grant is None or not grant["fulfilled"]:
            conn.rollback()
            return "missing"
        if now >= int(grant["expires_at"]):
            conn.rollback()
            return "expired"
        existing = conn.execute(
            "SELECT 1 FROM grant_ips WHERE token = ? AND ip = ?",
            (token, ip),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return "ok"
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM grant_ips WHERE token = ?",
            (token,),
        ).fetchone()
        if int(count["n"]) >= max_ips:
            conn.rollback()
            return "full"
        conn.execute(
            "INSERT INTO grant_ips (token, ip, first_seen_at) VALUES (?, ?, ?)",
            (token, ip, now),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def ip_is_recorded(cfg: Config, token: str, ip: str) -> bool:
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT 1 FROM grant_ips WHERE token = ? AND ip = ?",
            (token, ip),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def build_message(cfg: Config, plan: str, email_addr: str, name: str, token: str, password: str) -> EmailMessage:
    greeted = first_name(name)
    hello = f"Hi {greeted}," if greeted else "Hi there,"
    url = f"{PUBLIC_ORIGIN}/access/{token}"
    if plan == "diy_100":
        confirm = "Your $100 DIY payment is received."
        extra_plain = ""
        extra_html = ""
    else:
        confirm = "Your Done-for-you payment is received."
        extra_plain = (
            "\nTwo quick starting links:\n"
            f"1. {INTAKE_URL} — send your app details if you have not yet.\n"
            f"2. {ADD_US_URL} — grant verify@velda.ai the OAuth Config Editor role.\n"
        )
        extra_html = (
            "<p>Two quick starting links:</p>"
            f'<p>1. <a href="{INTAKE_URL}">{html.escape(INTAKE_URL)}</a> '
            "— send your app details if you have not yet.<br>"
            f'2. <a href="{ADD_US_URL}">{html.escape(ADD_US_URL)}</a> '
            "— grant verify@velda.ai the OAuth Config Editor role.</p>"
        )
    plain = (
        f"{hello}\n\n"
        f"Thank you for trusting Get App Verified. {confirm}\n\n"
        "Your private guide link:\n"
        f"{url}\n\n"
        "Your password:\n"
        f"{password}\n\n"
        "This link stays active for 4 hours and works on up to 2 IP addresses with this password "
        "(for example your computer and your phone). Please don't forward the link or the password.\n"
        f"{extra_plain}\n"
        "If you need help, reply to verify@velda.ai. We reply within 1 business day.\n\n"
        "Warmly,\n"
        "Get App Verified\n"
        "verify@velda.ai\n"
    )
    safe_hello = html.escape(hello)
    safe_password = html.escape(password)
    safe_url = html.escape(url, quote=True)
    html_body = (
        "<!DOCTYPE html><html><body style=\"font-family:-apple-system,BlinkMacSystemFont,Segoe UI,"
        "Helvetica,Arial,sans-serif;font-size:16px;line-height:1.5;color:#1f2430\">"
        f"<p>{safe_hello}</p>"
        f"<p>Thank you for trusting Get App Verified. {html.escape(confirm)}</p>"
        f'<p>Your private guide link:<br><a href="{safe_url}">{html.escape(url)}</a></p>'
        f"<p>Your password:<br><strong>{safe_password}</strong></p>"
        "<p>This link stays active for 4 hours and works on up to 2 IP addresses with this password "
        "(for example your computer and your phone). Please don't forward the link or the password.</p>"
        f"{extra_html}"
        "<p>If you need help, reply to "
        '<a href="mailto:verify@velda.ai">verify@velda.ai</a>. We reply within 1 business day.</p>'
        "<p>Warmly,<br>Get App Verified<br>verify@velda.ai</p>"
        "</body></html>"
    )
    message = EmailMessage()
    message["From"] = email.utils.formataddr((_header_safe(cfg.from_name), _header_safe(cfg.smtp_user)))
    message["To"] = email_addr
    message["Reply-To"] = REPLY_TO
    message["Bcc"] = _header_safe(cfg.bcc_email)
    message["Subject"] = "Your private Get App Verified link"
    message["Date"] = email.utils.formatdate(localtime=False)
    if token.replace("_", "").replace("-", "").isalnum():
        message["Message-ID"] = f"<{token}.verify-fulfill@velda.ai>"
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")
    return message


def send_email(cfg: Config, message: EmailMessage) -> None:
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
        smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(message)


def deliver(cfg: Config, plan: str, email_addr: str, name: str, session_id: str, fulfill_now: bool) -> tuple[int, dict, str | None]:
    """Mint a grant and, unless fulfill_now, email it. Password is returned only to the caller."""
    status, grant, password = reserve_grant(cfg, session_id, plan, email_addr, name, fulfill_now)
    if status == "duplicate":
        return 200, {"ok": True, "duplicate": True}, None
    if status != "go" or grant is None or password is None:
        return 500, {"ok": False, "error": "send_failed"}, None
    if fulfill_now:
        return 200, {"ok": True, "sent": True}, password
    try:
        send_email(cfg, build_message(cfg, plan, email_addr, name, grant["token"], password))
        mark_fulfilled(cfg, grant["token"])
    except Exception as exc:
        delete_grant(cfg, grant["token"])
        sys.stderr.write(f"verify-fulfill send failed {session_id} {type(exc).__name__}\n")
        return 500, {"ok": False, "error": "send_failed"}, None
    sys.stderr.write(f"verify-fulfill sent {plan} {session_id}\n")
    return 200, {"ok": True, "sent": True}, password


def process_webhook(cfg: Config, payload: bytes, signature: str, now: float | None = None) -> tuple[int, dict]:
    if not signatures_ok(cfg.stripe_webhook_secret, payload, signature, now=now):
        return 400, {"ok": False, "error": "bad_signature"}
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "bad_payload"}
    if not isinstance(event, dict):
        return 400, {"ok": False, "error": "bad_payload"}
    if event.get("type") not in HANDLED_EVENTS:
        return 200, {"ok": True, "ignored": "event_type"}
    data = event.get("data")
    session = data.get("object") if isinstance(data, dict) else None
    if not isinstance(session, dict):
        return 400, {"ok": False, "error": "bad_payload"}
    if session.get("livemode") is not True or session.get("payment_status") != "paid":
        return 200, {"ok": True, "ignored": "not_paid"}
    plan = PLANS.get(payment_link_id(session) or "")
    if plan is None:
        return 200, {"ok": True, "ignored": "other_product"}
    session_id = session.get("id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 255:
        return 400, {"ok": False, "error": "bad_payload"}
    if not session_id.replace("_", "").isalnum():
        return 400, {"ok": False, "error": "bad_payload"}
    email_addr = customer_email(session)
    if not valid_email(email_addr):
        return 500, {"ok": False, "error": "send_failed"}
    status, body, _password = deliver(
        cfg, plan, email_addr, customer_name(session), session_id, fulfill_now=False
    )
    return status, body


def packs_ready(cfg: Config) -> bool:
    for name in ("get-app-verified-diy-pack.pdf", "get-app-verified-dfy-pack.pdf"):
        if safe_child(cfg.packs_dir, name) is None:
            return False
    return True


def is_configured(cfg: Config) -> bool:
    return bool(cfg.stripe_webhook_secret and cfg.smtp_password and packs_ready(cfg))


def safe_child(directory: str, filename: str) -> str | None:
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        return None
    root = os.path.realpath(directory)
    if not os.path.isdir(root):
        return None
    full = os.path.realpath(os.path.join(root, filename))
    try:
        if os.path.commonpath([root, full]) != root:
            return None
    except ValueError:
        return None
    if os.path.dirname(full) != root or not os.path.isfile(full):
        return None
    return full


def resolve_guide(cfg: Config, plan: str, relpath: str) -> str | None:
    folder = GUIDE_DIR_FOR_PLAN.get(plan)
    if not folder:
        return None
    root = os.path.realpath(os.path.join(cfg.packs_dir, folder))
    if not os.path.isdir(root):
        return None
    if not relpath or relpath.startswith("/") or "\\" in relpath or "\x00" in relpath:
        return None
    parts = [part for part in relpath.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    full = os.path.realpath(os.path.join(root, *parts))
    try:
        if os.path.commonpath([root, full]) != root:
            return None
    except ValueError:
        return None
    if not os.path.isfile(full):
        return None
    ext = os.path.splitext(full)[1].lower()
    if ext not in GUIDE_EXTENSIONS:
        return None
    return full


def sign_cookie(key: bytes, token: str, ip: str, expires_at: int) -> str:
    message = f"{token}|{ip}|{expires_at}"
    signature = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{message}|{signature}"


def cookie_valid(key: bytes, token: str, ip: str, expires_at: int, raw: str, now: int) -> bool:
    if not key or not raw:
        return False
    parts = raw.split("|")
    if len(parts) != 4:
        return False
    c_token, c_ip, c_exp, signature = parts
    if len(c_token) != len(token) or not hmac.compare_digest(c_token, token):
        return False
    if len(c_ip) != len(ip) or not hmac.compare_digest(c_ip, ip):
        return False
    if c_exp != str(int(expires_at)) or now >= int(expires_at):
        return False
    expected = hmac.new(key, f"{token}|{ip}|{expires_at}".encode("utf-8"), hashlib.sha256).hexdigest()
    return len(signature) == len(expected) and hmac.compare_digest(signature, expected)


def read_cookie(header: str, name: str = "gav") -> str:
    if not header:
        return ""
    prefix = name + "="
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :]
    return ""


def access_view(cfg: Config, token: str, ip: str, cookie_header: str, now: int) -> tuple[str, sqlite3.Row | None]:
    grant = load_grant(cfg, token)
    if grant is None or not grant["fulfilled"]:
        return "missing", None
    if now >= int(grant["expires_at"]):
        return "expired", grant
    raw = read_cookie(cookie_header)
    if cookie_valid(cookie_key(cfg), token, ip, int(grant["expires_at"]), raw, now) and ip_is_recorded(cfg, token, ip):
        return "ok", grant
    return "login", grant


def inject_bar(page: bytes, email_addr: str, minutes: int, token: str) -> bytes:
    text = page.decode("utf-8", "replace")
    bar = (
        '<div style="position:sticky;top:0;z-index:2147483647;background:#111827;color:#fff;'
        "font:15px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;"
        'padding:10px 16px">'
        f"Licensed to {html.escape(email_addr)} · Expires in {minutes}m · "
        f'<a href="/access/{token}/download.pdf" style="color:#fff">Download PDF</a>'
        "</div>"
    )
    lower = text.lower()
    start = lower.find("<body")
    if start >= 0:
        end = text.find(">", start)
        if end >= 0:
            return (text[: end + 1] + bar + text[end + 1 :]).encode("utf-8")
    return (bar + text).encode("utf-8")


def minutes_left(expires_at: int, now: int) -> int:
    remaining = expires_at - now
    if remaining <= 0:
        return 0
    return max(1, (remaining + 59) // 60)


class FulfillServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(cfg: Config) -> FulfillServer:
    return FulfillServer((cfg.bind_host, cfg.bind_port), build_handler(cfg))


def build_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def version_string(self) -> str:
            return "verify-fulfill"

        def do_GET(self) -> None:
            self._route()

        def do_POST(self) -> None:
            self._route()

        def log_message(self, fmt: str, *args) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/access/"):
                path = "/access/<redacted>"
            sys.stderr.write(f"{self.log_date_time_string()} {self.command} {path}\n")

        def _route(self) -> None:
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if self.command == "GET" and path == "/api/stripe/health":
                self._json(200, {"ok": True, "service": "verify-fulfill", "configured": is_configured(cfg)})
                return
            if self.command == "POST" and path == "/api/stripe/webhook":
                self._webhook()
                return
            if path.startswith("/access/"):
                self._access(path)
                return
            self._text(404, "Not found\n")

        def _webhook(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(400, {"ok": False, "error": "bad_signature"})
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad_signature"})
                return
            payload = self.rfile.read(length)
            signature = self.headers.get("Stripe-Signature", "")
            status, body = process_webhook(cfg, payload, signature)
            self._json(status, body)

        def _access(self, path: str) -> None:
            rest = path[len("/access/") :]
            parts = rest.split("/")
            token = parts[0]
            extra = parts[1:]
            if extra == [""]:
                extra = []
            if not _token_ok(token):
                self._text(404, "Not found\n", private=True)
                return
            ip = client_ip(self.client_address[0], self.headers)
            now = int(time.time())
            if extra == ["unlock"] and self.command == "POST":
                self._unlock(token, ip, now)
                return
            if self.command != "GET":
                self._text(404, "Not found\n", private=True)
                return
            if extra == ["download.pdf"]:
                self._download(token, ip, now)
                return
            if extra and extra[0] == "guide":
                rel = "/".join(extra[1:])
                self._guide(token, ip, now, rel)
                return
            if extra:
                self._text(404, "Not found\n", private=True)
                return
            self._landing(token, ip, now)

        def _landing(self, token: str, ip: str, now: int) -> None:
            state, grant = access_view(cfg, token, ip, self.headers.get("Cookie", ""), now)
            if state == "missing":
                self._text(404, "Not found\n", private=True)
                return
            if state == "expired":
                self._html(410, _expired_page())
                return
            if state == "ok":
                self._redirect(f"/access/{token}/guide/index.html")
                return
            self._html(200, _password_form(token, ""))

        def _unlock(self, token: str, ip: str, now: int) -> None:
            state, grant = access_view(cfg, token, ip, "", now)
            if state == "missing":
                self._text(404, "Not found\n", private=True)
                return
            if state == "expired" or (grant is not None and now >= int(grant["expires_at"])):
                self._html(410, _expired_page())
                return
            if failure_count(cfg, token, now) >= MAX_PASSWORD_FAILURES:
                self._html(429, _message_page("Too many tries", _lock_copy()))
                return
            password = _read_password(self)
            if grant is None or not password_matches(
                password,
                grant["password_salt"],
                grant["password_hash"],
                int(grant["password_iters"]),
            ):
                record_failure(cfg, token, ip, now)
                self._html(401, _password_form(token, "That password does not match. Please try again."))
                return
            claimed = claim_ip(cfg, token, ip, int(grant["max_ips"]), now)
            if claimed == "expired":
                self._html(410, _expired_page())
                return
            if claimed != "ok":
                self._html(403, _message_page("Two addresses already", _ip_limit_copy()))
                return
            remaining = max(1, int(grant["expires_at"]) - now)
            raw = sign_cookie(cookie_key(cfg), token, ip, int(grant["expires_at"]))
            cookie = (
                f"gav={raw}; HttpOnly; Secure; SameSite=Strict; "
                f"Path=/access/{token}; Max-Age={remaining}"
            )
            self._redirect(f"/access/{token}/guide/index.html", cookie=cookie)

        def _guide(self, token: str, ip: str, now: int, relpath: str) -> None:
            grant = self._authorized(token, ip, now)
            if grant is None:
                return
            path = resolve_guide(cfg, grant["plan"], relpath)
            if path is None:
                self._text(404, "Not found\n", private=True)
                return
            ext = os.path.splitext(path)[1].lower()
            with open(path, "rb") as handle:
                data = handle.read()
            if ext in {".html", ".htm"}:
                data = inject_bar(data, grant["buyer_email"], minutes_left(int(grant["expires_at"]), now), token)
            self._bytes(200, GUIDE_EXTENSIONS[ext], data, private=True)

        def _download(self, token: str, ip: str, now: int) -> None:
            grant = self._authorized(token, ip, now)
            if grant is None:
                return
            filename = PDF_FOR_PLAN[grant["plan"]]
            path = safe_child(cfg.packs_dir, filename)
            if path is None:
                self._text(404, "Not found\n", private=True)
                return
            with open(path, "rb") as handle:
                original = handle.read()
            stamped = stamp_pdf(original, license_line(grant["buyer_email"], grant["stripe_session_id"]))
            self._bytes(
                200,
                "application/pdf",
                stamped,
                private=True,
                disposition=f'attachment; filename="{filename}"',
            )

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _text(self, status: int, text: str, private: bool = False) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store" if private else "no-store")
            if private:
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, page: str) -> None:
            body = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, status: int, content_type: str, data: bytes, private: bool, disposition: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if disposition:
                self.send_header("Content-Disposition", disposition)
            if private:
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, token: str, ip: str, now: int):
            state, grant = access_view(cfg, token, ip, self.headers.get("Cookie", ""), now)
            if state == "ok":
                return grant
            if state == "expired":
                self._html(410, _expired_page())
            elif state == "login":
                self._redirect(f"/access/{token}")
            else:
                self._text(404, "Not found\n", private=True)
            return None

        def _redirect(self, location: str, cookie: str | None = None) -> None:
            body = b"Redirecting\n"
            self.send_response(303)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _token_ok(token: str) -> bool:
    return bool(token) and 20 <= len(token) <= 200 and set(token) <= TOKEN_RE_CHARS


def _read_password(handler) -> str:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return ""
    if length < 0 or length > 8192:
        return ""
    raw = handler.rfile.read(length).decode("utf-8", "replace")
    values = urllib.parse.parse_qs(raw, keep_blank_values=True)
    found = values.get("password", [""])
    return found[0] if found else ""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<title>{html.escape(title)}</title></head>"
        "<body style=\"font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;"
        "max-width:36rem;margin:48px auto;padding:0 16px;line-height:1.5;color:#1f2430\">"
        f"<h1 style=\"font-size:1.4rem\">{html.escape(title)}</h1>{body}</body></html>"
    )


def _password_form(token: str, error: str) -> str:
    notice = f"<p>{html.escape(error)}</p>" if error else ""
    return _page(
        "Open your guide",
        notice
        + "<p>This private link stays active for 4 hours and works on up to 2 IP addresses.</p>"
        + f'<form method="post" action="/access/{token}/unlock">'
        + '<label>Password<br><input type="password" name="password" autocomplete="current-password" required></label>'
        + '<p><button type="submit">Open the guide</button></p></form>',
    )


def _expired_page() -> str:
    return _page(
        "This link has expired",
        "<p>The private link stays active for 4 hours, and that window has ended.</p>"
        '<p>Email <a href="mailto:verify@velda.ai">verify@velda.ai</a> and we will help. '
        "We reply within 1 business day.</p>",
    )


def _ip_limit_copy() -> str:
    return "Maximum 2 IP addresses reached for this access link."


def _lock_copy() -> str:
    return (
        "Too many tries. This link is locked for 15 minutes. "
        'You can email <a href="mailto:verify@velda.ai">verify@velda.ai</a>.'
    )


def _message_page(title: str, paragraph: str) -> str:
    return _page(title, f"<p>{paragraph}</p>")


def mint_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="verify_fulfill.py mint")
    parser.add_argument("--plan", required=True, choices=sorted(PDF_FOR_PLAN))
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--send-email", action="store_true")
    args = parser.parse_args(argv)
    if not valid_email(args.email):
        sys.stderr.write("mint: email address is not usable\n")
        return 2
    cfg = load_config()
    session_id = "manual_" + secrets.token_hex(16)
    status, body, password = deliver(
        cfg,
        args.plan,
        args.email,
        clean_name(args.name),
        session_id,
        fulfill_now=not args.send_email,
    )
    if status != 200 or not password or body.get("duplicate"):
        sys.stderr.write("mint: could not issue the access pass\n")
        return 1
    conn = _connect(cfg)
    try:
        grant = conn.execute(
            "SELECT * FROM grants WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if grant is None or not grant["fulfilled"]:
        sys.stderr.write("mint: access pass was not activated\n")
        return 1
    json.dump(
        {
            "url": f"{PUBLIC_ORIGIN}/access/{grant['token']}",
            "password": password,
            "expires_at": int(grant["expires_at"]),
            "max_ips": int(grant["max_ips"]),
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["mint"]:
        return mint_main(argv[1:])
    cfg = load_config()
    try:
        server = make_server(cfg)
    except OSError as exc:
        sys.stderr.write(f"verify-fulfill could not bind {cfg.bind_host}:{cfg.bind_port} ({exc.errno})\n")
        return 1
    sys.stderr.write(f"verify-fulfill listening on {cfg.bind_host}:{server.server_address[1]}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
