#!/usr/bin/env python3
"""Fulfillment tests: Stripe allow-list, 4-hour / 2-IP passes, and PDF stamps."""
import contextlib
import hashlib
import hmac
import http.client
import io
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fulfill"))

import pdf_stamp  # noqa: E402
import verify_fulfill  # noqa: E402

DIY = "plink_1UYUex4Q8AoUpkwuilTPpwSm"
DFY = "plink_1UYUez4Q8AoUpkwuazXeH9LK"
UPGRADE = "plink_1UYUf14Q8AoUpkwuzdDbxudW"
SECRET = "test_webhook_secret"
SMTP_PASSWORD = "smtp-test-password"


def sign(secret, payload, timestamp):
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def sample_pdf(pages):
    """Classic two-or-more page PDF with an uncompressed xref."""
    count = len(pages)
    font = 3 + count * 2
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = []
    for index, text in enumerate(pages):
        page_num = 3 + index * 2
        content_num = page_num + 1
        kids.append(f"{page_num} 0 R")
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET\n".encode()
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_num} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        ).encode()
        objects[content_num] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream"
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {count} >>".encode()
    objects[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    parts = [b"%PDF-1.4\n"]
    offsets = {}
    for num in range(1, font + 1):
        offsets[num] = sum(len(part) for part in parts)
        parts.append(f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n")
    xref_at = sum(len(part) for part in parts)
    size = font + 1
    xref = [f"xref\n0 {size}\n".encode(), b"0000000000 65535 f \n"]
    for num in range(1, size):
        xref.append(f"{offsets[num]:010d} 00000 n \n".encode())
    trailer = f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return b"".join(parts + xref + [trailer])


def plain_text(message: EmailMessage) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            return part.get_content()
    raise AssertionError("welcome email has no plain text part")


class SmtpBox:
    def __init__(self):
        self.messages = []
        self.fail = False
        self.host = None
        self.port = None

    def factory(self):
        box = self

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                box.host = host
                box.port = port

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def login(self, user, password):
                box.user = user
                box.password = password

            def send_message(self, message):
                if box.fail:
                    raise smtplib.SMTPException("down")
                box.messages.append(message)

        return FakeSMTP


class Server:
    def __init__(self, cfg):
        self.httpd = verify_fulfill.make_server(cfg)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        headers_out = {}
        cookies = []
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie":
                cookies.append(value)
            else:
                headers_out[key.lower()] = value
        if cookies:
            headers_out["set-cookie"] = cookies
        conn.close()
        return resp.status, headers_out, payload


class FulfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packs = self.root / "packs"
        (self.packs / "diy" / "img").mkdir(parents=True)
        (self.packs / "dfy" / "img").mkdir(parents=True)
        (self.packs / "diy" / "index.html").write_text(
            "<html><body><p>DIY-GUIDE-MARKER</p></body></html>", encoding="utf-8"
        )
        (self.packs / "dfy" / "index.html").write_text(
            "<html><body><p>DFY-GUIDE-MARKER</p></body></html>", encoding="utf-8"
        )
        (self.packs / "diy" / "img" / "pic.jpg").write_bytes(b"JPEGDATA")
        (self.packs / "get-app-verified-diy-pack.pdf").write_bytes(sample_pdf(["DIY-PDF-PAGE-A", "DIY-PDF-PAGE-B"]))
        (self.packs / "get-app-verified-dfy-pack.pdf").write_bytes(sample_pdf(["DFY-PDF-PAGE"]))
        outside = self.root / "outside-secret.txt"
        outside.write_text("TOP-SECRET-GUIDE", encoding="utf-8")
        (self.packs / "diy" / "escape.html").symlink_to(outside)
        self.box = SmtpBox()
        self.cfg = verify_fulfill.Config(
            bind_host="127.0.0.1",
            bind_port=0,
            stripe_webhook_secret=SECRET,
            smtp_host="smtp.hostinger.com",
            smtp_port=465,
            smtp_user="verify@velda.ai",
            smtp_password=SMTP_PASSWORD,
            from_name="Get App Verified",
            bcc_email="verify@velda.ai",
            packs_dir=str(self.packs),
            state_db=str(self.root / "grants.db"),
            cookie_secret="cookie-test-secret",
            pbkdf2_iters=1500,
        )
        self.server = Server(self.cfg)
        self.smtp = patch("verify_fulfill.smtplib.SMTP_SSL", self.box.factory())
        self.smtp.start()

    def tearDown(self):
        self.smtp.stop()
        self.server.close()
        self.tmp.cleanup()

    def webhook(self, session, event_type="checkout.session.completed", timestamp=None, header=None, raw=None):
        payload = raw if raw is not None else json.dumps(
            {"id": "evt_test", "type": event_type, "data": {"object": session}}
        ).encode()
        if header is None:
            when = int(time.time()) if timestamp is None else timestamp
            header = sign(SECRET, payload, when)
        return self.server.request(
            "POST",
            "/api/stripe/webhook",
            body=payload,
            headers={"Stripe-Signature": header, "Content-Type": "application/json"},
        )

    def paid_session(self, **overrides):
        session = {
            "id": "cs_test_1",
            "object": "checkout.session",
            "livemode": True,
            "payment_status": "paid",
            "payment_link": DIY,
            "customer_details": {"email": "ada@example.com", "name": "Ada Lovelace"},
        }
        session.update(overrides)
        return session

    def grant_ips(self, token):
        conn = sqlite3.connect(self.cfg.state_db)
        try:
            return [row[0] for row in conn.execute("SELECT ip FROM grant_ips WHERE token = ?", (token,))]
        finally:
            conn.close()

    def test_signature_window_and_mismatch(self):
        payload = b'{"ok":true}'
        now = 1_700_000_000
        header = sign(SECRET, payload, now)
        self.assertTrue(verify_fulfill.signatures_ok(SECRET, payload, header, now=now + 300))
        self.assertTrue(verify_fulfill.signatures_ok(SECRET, payload, header, now=now - 300))
        self.assertFalse(verify_fulfill.signatures_ok(SECRET, payload, header, now=now + 301))
        self.assertFalse(verify_fulfill.signatures_ok(SECRET, payload, header, now=now - 301))
        self.assertFalse(verify_fulfill.signatures_ok(SECRET, payload, "t=%d,v1=%s" % (now, "ab" * 32), now=now))
        self.assertFalse(verify_fulfill.signatures_ok("", payload, header, now=now))
        rotated = f"t={now},v1={'cd' * 32},v1={sign(SECRET, payload, now).split('v1=')[1]}"
        self.assertTrue(verify_fulfill.signatures_ok(SECRET, payload, rotated, now=now))

    def test_webhook_rejects_bad_signatures(self):
        payload = json.dumps(
            {"type": "checkout.session.completed", "data": {"object": self.paid_session()}}
        ).encode()
        status, _, body = self.server.request("POST", "/api/stripe/webhook", body=payload, headers={})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "bad_signature")
        stale = sign(SECRET, payload, int(time.time()) - 301)
        status, _, _ = self.webhook(self.paid_session(), header=stale, raw=payload)
        self.assertEqual(status, 400)
        status, _, _ = self.webhook(self.paid_session(), raw=payload, header=sign("other-secret", payload, int(time.time())))
        self.assertEqual(status, 400)
        self.assertEqual(self.box.messages, [])

    def test_ignores_other_products_and_unpaid_sessions(self):
        cases = [
            self.paid_session(payment_link="plink_someone_elses_product"),
            self.paid_session(payment_link=DIY + "EXTRA"),
            self.paid_session(payment_link=None),
            self.paid_session(livemode=False),
            self.paid_session(payment_status="unpaid"),
        ]
        expected = ["other_product", "other_product", "other_product", "not_paid", "not_paid"]
        for session, reason in zip(cases, expected):
            status, _, body = self.webhook(session)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body), {"ok": True, "ignored": reason})
        status, _, body = self.webhook(self.paid_session(), event_type="charge.refunded")
        self.assertEqual(json.loads(body), {"ok": True, "ignored": "event_type"})
        self.assertEqual(self.box.messages, [])
        self.assertFalse((self.root / "grants.db").exists())

    def test_welcome_email_has_no_attachment_and_is_idempotent(self):
        status, _, body = self.webhook(self.paid_session())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "sent": True})
        self.assertEqual(len(self.box.messages), 1)
        message = self.box.messages[0]
        text = plain_text(message)
        self.assertIn("Hi Ada,", text)
        self.assertIn("Your $100 DIY payment is received.", text)
        self.assertIn("4 hours", text)
        self.assertIn("2 IP addresses", text)
        self.assertIn("https://verify.velda.ai/access/", text)
        self.assertNotIn(verify_fulfill.ADD_US_URL, text)
        self.assertEqual([part.get_filename() for part in message.walk() if part.get_filename()], [])
        self.assertNotIn("application/pdf", [part.get_content_type() for part in message.walk()])
        self.assertIn("Get App Verified", message["From"])
        self.assertIn("verify@velda.ai", message["From"])
        self.assertEqual(message["To"], "ada@example.com")
        self.assertEqual(message["Reply-To"], "verify@velda.ai")
        self.assertEqual(message["Bcc"], "verify@velda.ai")
        self.assertEqual(self.box.host, "smtp.hostinger.com")
        self.assertEqual(self.box.port, 465)
        self.assertEqual(self.box.user, "verify@velda.ai")
        self.assertEqual(self.box.password, SMTP_PASSWORD)
        password = text.split("Your password:\n", 1)[1].splitlines()[0]
        self.assertNotIn(password.encode(), (self.root / "grants.db").read_bytes())
        conn = sqlite3.connect(self.cfg.state_db)
        created, expires = conn.execute("SELECT created_at, expires_at FROM grants").fetchone()
        conn.close()
        self.assertEqual(expires - created, 14400)
        status, _, body = self.webhook(self.paid_session())
        self.assertEqual(json.loads(body), {"ok": True, "duplicate": True})
        self.assertEqual(len(self.box.messages), 1)

    def test_smtp_failure_is_not_marked_sent_and_can_retry(self):
        self.box.fail = True
        status, _, body = self.webhook(self.paid_session(id="cs_retry_1"))
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "send_failed")
        conn = sqlite3.connect(self.cfg.state_db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0], 0)
        conn.close()
        self.box.fail = False
        status, _, body = self.webhook(self.paid_session(id="cs_retry_1"))
        self.assertEqual(json.loads(body), {"ok": True, "sent": True})
        self.assertEqual(len(self.box.messages), 1)
        conn = sqlite3.connect(self.cfg.state_db)
        self.assertEqual(conn.execute("SELECT fulfilled FROM grants").fetchone()[0], 1)
        conn.close()

    def test_done_for_you_email_includes_starting_links(self):
        status, _, _ = self.webhook(
            self.paid_session(
                id="cs_dfy_1",
                payment_link=DFY,
                customer_details={"email": "buyer@example.com", "name": ""},
            ),
            event_type="checkout.session.async_payment_succeeded",
        )
        self.assertEqual(status, 200)
        text = plain_text(self.box.messages[0])
        self.assertIn("Hi there,", text)
        self.assertIn("Your Done-for-you payment is received.", text)
        self.assertIn(verify_fulfill.INTAKE_URL, text)
        self.assertIn(verify_fulfill.ADD_US_URL, text)
        self.assertIn("OAuth Config Editor", text)
        status, _, _ = self.webhook(self.paid_session(id="cs_up_1", payment_link=UPGRADE))
        self.assertEqual(status, 200)
        self.assertIn("Done-for-you payment is received.", plain_text(self.box.messages[1]))

    def test_access_pass_ips_expiry_and_watermark(self):
        status, _, _ = self.webhook(self.paid_session(id="cs_access_1"))
        self.assertEqual(status, 200)
        text = plain_text(self.box.messages[0])
        token = text.split("/access/", 1)[1].split()[0]
        password = text.split("Your password:\n", 1)[1].splitlines()[0]
        status, headers, body = self.server.request("GET", f"/access/{token}")
        self.assertEqual(status, 200)
        self.assertIn(b"Password", body)
        self.assertIn("private, no-store", headers["cache-control"])
        status, headers, body = self._unlock(token, "WRONG-PASS-WORD-XXXX", "203.0.113.9")
        self.assertEqual(status, 401)
        self.assertNotIn("set-cookie", headers)
        self.assertEqual(self.grant_ips(token), [])
        status, headers, _ = self._unlock(token, password, "203.0.113.1")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/access/{token}/guide/index.html")
        cookie = headers["set-cookie"][0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn(f"Path=/access/{token}", cookie)
        self.assertIn("Max-Age=", cookie)
        pair = cookie.split(";", 1)[0]
        status, headers, body = self._guide(token, "203.0.113.1", pair)
        self.assertEqual(status, 200)
        self.assertIn(b"DIY-GUIDE-MARKER", body)
        self.assertIn(b"Licensed to ada@example.com", body)
        self.assertIn(b"Download PDF", body)
        self.assertIn(b"Expires in", body)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(headers["x-robots-tag"], "noindex, nofollow")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        status, _, image = self._guide(token, "203.0.113.1", pair, "img/pic.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(image, b"JPEGDATA")
        status, _, escaped = self.server.request(
            "GET",
            f"/access/{token}/guide/..%2f..%2foutside-secret.txt",
            headers={"X-Real-IP": "203.0.113.1", "Cookie": pair},
        )
        self.assertNotEqual(status, 200)
        self.assertNotIn(b"TOP-SECRET-GUIDE", escaped)
        status, _, escaped = self._guide(token, "203.0.113.1", pair, "escape.html")
        self.assertNotEqual(status, 200)
        self.assertNotIn(b"TOP-SECRET-GUIDE", escaped)
        status, _, denied = self._guide(token, "203.0.113.1", "")
        self.assertEqual(status, 303)
        self.assertNotIn(b"DIY-GUIDE-MARKER", denied)
        status, pdf_headers, pdf = self.server.request(
            "GET",
            f"/access/{token}/download.pdf",
            headers={"X-Real-IP": "203.0.113.1", "Cookie": pair},
        )
        self.assertEqual(status, 200)
        self.assertEqual(pdf_headers["content-type"], "application/pdf")
        self.assertIn('filename="get-app-verified-diy-pack.pdf"', pdf_headers["content-disposition"])
        self.assertGreaterEqual(pdf.count(b"ada@example.com"), 2)
        self.assertIn(b"cs_access_1", pdf)
        self.assertNotIn(b"TOP-SECRET", pdf)
        texts = pdf_stamp.page_content_texts(pdf)
        self.assertEqual(len(texts), 2)
        for page in texts:
            self.assertIn(b"ada@example.com", page)
            self.assertIn(b"Do not share", page)
        self.assertTrue(any(b"DIY-PDF-PAGE-A" in page or b"DIY-PDF-PAGE-B" in page for page in texts))
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(pdf)
            handle.flush()
            extracted = subprocess.check_output(["pdftotext", "-layout", handle.name, "-"], text=True)
        pages = extracted.split("\f")
        self.assertGreaterEqual(sum(1 for page in pages if "ada@example.com" in page), 2)
        self.assertIn("DIY-PDF-PAGE-A", extracted)
        self.assertIn("DIY-PDF-PAGE-B", extracted)
        status, _, _ = self._unlock(token, password, "203.0.113.2")
        self.assertEqual(status, 303)
        self.assertCountEqual(self.grant_ips(token), ["203.0.113.1", "203.0.113.2"])
        status, _, again = self._unlock(token, password, "203.0.113.1")
        self.assertEqual(status, 303)
        self.assertEqual(len(self.grant_ips(token)), 2)
        status, _, limited = self._unlock(token, password, "203.0.113.3")
        self.assertEqual(status, 403)
        self.assertIn(b"Maximum 2 IP addresses reached for this access link.", limited)
        self.assertNotIn("203.0.113.3", self.grant_ips(token))
        conn = sqlite3.connect(self.cfg.state_db)
        conn.execute("UPDATE grants SET expires_at = ? WHERE token = ?", (int(time.time()) - 5, token))
        conn.commit()
        conn.close()
        status, _, expired = self.server.request("GET", f"/access/{token}", headers={"X-Real-IP": "203.0.113.1"})
        self.assertEqual(status, 410)
        self.assertIn(b"4 hours", expired)
        self.assertIn(b"verify@velda.ai", expired)
        status, _, expired_pdf = self.server.request(
            "GET",
            f"/access/{token}/download.pdf",
            headers={"X-Real-IP": "203.0.113.1", "Cookie": pair},
        )
        self.assertEqual(status, 410)
        self.assertNotIn(b"DIY-PDF-PAGE-A", expired_pdf)

    def test_wrong_password_lock_does_not_consume_an_ip(self):
        self.webhook(self.paid_session(id="cs_lock_1"))
        text = plain_text(self.box.messages[0])
        token = text.split("/access/", 1)[1].split()[0]
        password = text.split("Your password:\n", 1)[1].splitlines()[0]
        for _ in range(8):
            status, _, _ = self._unlock(token, "NOPE-NOPE-NOPE-NOPE", "198.51.100.8")
            self.assertEqual(status, 401)
        self.assertEqual(self.grant_ips(token), [])
        status, _, locked = self._unlock(token, password, "198.51.100.8")
        self.assertEqual(status, 429)
        self.assertIn(b"15 minutes", locked)
        self.assertEqual(self.grant_ips(token), [])
        conn = sqlite3.connect(self.cfg.state_db)
        conn.execute(
            "UPDATE failed_attempts SET failed_at = ? WHERE token = ?",
            (int(time.time()) - verify_fulfill.LOCK_SECONDS - 5, token),
        )
        conn.commit()
        conn.close()
        status, _, _ = self._unlock(token, password, "198.51.100.8")
        self.assertEqual(status, 303)
        self.assertEqual(self.grant_ips(token), ["198.51.100.8"])

    def test_dfy_guide_is_not_the_diy_pack(self):
        self.webhook(self.paid_session(id="cs_dfy_guide", payment_link=DFY))
        text = plain_text(self.box.messages[-1])
        token = text.split("/access/", 1)[1].split()[0]
        password = text.split("Your password:\n", 1)[1].splitlines()[0]
        status, headers, _ = self._unlock(token, password, "203.0.113.20")
        self.assertEqual(status, 303)
        pair = headers["set-cookie"][0].split(";", 1)[0]
        status, _, body = self._guide(token, "203.0.113.20", pair)
        self.assertEqual(status, 200)
        self.assertIn(b"DFY-GUIDE-MARKER", body)
        self.assertNotIn(b"DIY-GUIDE-MARKER", body)
        status, pdf_headers, pdf = self.server.request(
            "GET",
            f"/access/{token}/download.pdf",
            headers={"X-Real-IP": "203.0.113.20", "Cookie": pair},
        )
        self.assertEqual(status, 200)
        self.assertIn("get-app-verified-dfy-pack.pdf", pdf_headers["content-disposition"])
        self.assertIn(b"DFY-PDF-PAGE", b"\n".join(pdf_stamp.page_content_texts(pdf)))
        self.assertNotIn(b"DIY-PDF-PAGE-A", pdf)

    def test_health_and_unknown_routes_hide_secrets(self):
        status, _, body = self.server.request("GET", "/api/stripe/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["service"], "verify-fulfill")
        self.assertIs(payload["configured"], True)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(SMTP_PASSWORD.encode(), body)
        status, _, _ = self.server.request("GET", "/dl/anything/get-app-verified-diy-pack.pdf")
        self.assertEqual(status, 404)
        status, _, _ = self.server.request("GET", "/access/not-a-real-token")
        self.assertEqual(status, 404)

    def _unlock(self, token, password, ip):
        body = urllib.parse.urlencode({"password": password}).encode()
        return self.server.request(
            "POST",
            f"/access/{token}/unlock",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "X-Real-IP": ip},
        )

    def _guide(self, token, ip, cookie, rel="index.html"):
        headers = {"X-Real-IP": ip}
        if cookie:
            headers["Cookie"] = cookie
        return self.server.request("GET", f"/access/{token}/guide/{rel}", headers=headers)


class PdfStampTest(unittest.TestCase):
    def test_every_page_is_watermarked_and_malformed_input_is_not_returned(self):
        original = sample_pdf(["PageOne", "PageTwo"])
        line = "Licensed exclusively to ada@example.com · Order cs_test_1 · Do not share"
        stamped = pdf_stamp.stamp_pdf(original, line)
        self.assertTrue(stamped.startswith(b"%PDF-"))
        self.assertGreaterEqual(stamped.count(b"ada@example.com"), 2)
        texts = pdf_stamp.page_content_texts(stamped)
        self.assertEqual(len(texts), 2)
        self.assertIn(b"PageOne", texts[0])
        self.assertIn(b"PageTwo", texts[1])
        for page in texts:
            self.assertIn(b"ada@example.com", page)
            self.assertIn(b"Do not share", page)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(stamped)
            handle.flush()
            extracted = subprocess.check_output(["pdftotext", handle.name, "-"], text=True)
        self.assertEqual(extracted.count("ada@example.com"), 2)
        fallback = pdf_stamp.stamp_pdf(b"SECRET-PACK-BYTES this is not a pdf", line)
        self.assertTrue(fallback.startswith(b"%PDF-"))
        self.assertNotIn(b"SECRET-PACK-BYTES", fallback)
        self.assertIn(b"ada@example.com", fallback)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(fallback)
            handle.flush()
            extracted = subprocess.check_output(["pdftotext", handle.name, "-"], text=True)
        self.assertIn("ada@example.com", extracted)
        self.assertNotIn("SECRET-PACK-BYTES", extracted)


class PolicyTest(unittest.TestCase):
    def test_client_ip_trusts_forwarded_headers_only_from_loopback(self):
        self.assertEqual(verify_fulfill.client_ip("8.8.8.8", {"X-Real-IP": "1.2.3.4"}), "8.8.8.8")
        self.assertEqual(verify_fulfill.client_ip("127.0.0.1", {"X-Real-IP": "203.0.113.9"}), "203.0.113.9")
        self.assertEqual(
            verify_fulfill.client_ip("127.0.0.1", {"X-Forwarded-For": "9.9.9.9, 198.51.100.4"}),
            "198.51.100.4",
        )
        self.assertEqual(verify_fulfill.client_ip("::1", {"X-Real-IP": "203.0.113.8"}), "203.0.113.8")
        self.assertEqual(verify_fulfill.PLANS, {DIY: "diy_100", DFY: "dfy_249", UPGRADE: "upgrade_149"})
        self.assertGreaterEqual(verify_fulfill.DEFAULT_PBKDF2_ITERS, 100_000)
        self.assertTrue(verify_fulfill.DEFAULT_STATE_DB.endswith("grants.db"))

    def test_env_file_does_not_override_the_environment(self):
        old = os.environ.copy()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / "fulfill.env"
                env_file.write_text(
                    "SMTP_PASSWORD=from-file\nFROM_NAME=From File\nSTATE_DB=/tmp/from-file.db\n",
                    encoding="utf-8",
                )
                os.environ["FULFILL_ENV_FILE"] = str(env_file)
                os.environ["SMTP_PASSWORD"] = "from-env"
                os.environ.pop("FROM_NAME", None)
                os.environ.pop("STATE_DB", None)
                cfg = verify_fulfill.load_config()
                self.assertEqual(cfg.smtp_password, "from-env")
                self.assertEqual(cfg.from_name, "From File")
                self.assertEqual(cfg.state_db, "/tmp/from-file.db")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_mint_prints_a_pass_without_storing_the_password(self):
        old = os.environ.copy()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                os.environ["FULFILL_ENV_FILE"] = str(root / "missing.env")
                os.environ["STATE_DB"] = str(root / "grants.db")
                os.environ["PACKS_DIR"] = str(root / "packs")
                os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET
                os.environ["SMTP_PASSWORD"] = SMTP_PASSWORD
                os.environ["SMTP_HOST"] = "smtp.hostinger.com"
                os.environ["SMTP_USER"] = "verify@velda.ai"
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = verify_fulfill.mint_main(
                        ["--plan", "diy_100", "--email", "verify@velda.ai", "--name", "Jason"]
                    )
                self.assertEqual(code, 0)
                minted = json.loads(buf.getvalue())
                self.assertEqual(minted["max_ips"], 2)
                self.assertTrue(minted["url"].startswith("https://verify.velda.ai/access/"))
                self.assertRegex(minted["password"], r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}(?:-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}){3}$")
                blob = (root / "grants.db").read_bytes()
                self.assertNotIn(minted["password"].encode(), blob)
                conn = sqlite3.connect(root / "grants.db")
                created, expires, name = conn.execute(
                    "SELECT created_at, expires_at, buyer_name FROM grants"
                ).fetchone()
                conn.close()
                self.assertEqual(expires - created, 14400)
                self.assertEqual(name, "Jason")
                box = SmtpBox()
                with patch("verify_fulfill.smtplib.SMTP_SSL", box.factory()):
                    sent = io.StringIO()
                    with contextlib.redirect_stdout(sent):
                        code = verify_fulfill.mint_main(
                            ["--plan", "dfy_249", "--email", "verify@velda.ai", "--name", "Jason", "--send-email"]
                        )
                self.assertEqual(code, 0)
                self.assertEqual(len(box.messages), 1)
                body = plain_text(box.messages[0])
                self.assertIn("Hi Jason,", body)
                self.assertIn(json.loads(sent.getvalue())["password"], body)
                self.assertIn(verify_fulfill.ADD_US_URL, body)
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
