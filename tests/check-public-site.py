#!/usr/bin/env python3
"""Crawl the public static site and reject paid-pack leaks."""
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import threading
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / "verify.velda.ai"
FORBIDDEN = [
    "Confirm Downscoping",
    "Manually paste scopes",
    "Owner (Legacy)",
    "{{DOMAIN}}",
    "{{TXT_VALUE}}",
    "{{GUIDE",
    "onetimesecret",
]
STRIPE = [
    "https://buy.stripe.com/3cIaEQ4R3cuw2S204XfjG00",
    "https://buy.stripe.com/aFa8wIcjvgKM2S2191fjG01",
    "https://buy.stripe.com/4gM00c1ERamo3W6g3VfjG02",
]
REQUIRED_PAGES = [
    "index.html",
    "intake.html",
    "privacy.html",
    "terms.html",
    "refunds.html",
    "contact.html",
    "guides/index.html",
    "guides/01-add-us.html",
    "guides/done-for-you.html",
    "site.css",
    "site.js",
]
GUIDE_PNGS = [
    "guides/img/01-project-picker.png",
    "guides/img/02-project-id.png",
    "guides/img/03-grant-access.png",
    "guides/img/04-oauth-config-editor.png",
]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ACCESS_PHRASE = (
    "Once Stripe confirms your payment, we email a personal welcome from verify@velda.ai "
    "with a private password-protected link. The link stays active for 4 hours and works on up to 2 IP addresses."
)
ACCESS_PAGES = [
    "index.html",
    "intake.html",
    "terms.html",
    "refunds.html",
    "privacy.html",
    "contact.html",
    "guides/index.html",
    "guides/01-add-us.html",
    "guides/done-for-you.html",
]
STALE = [
    "by hand",
    "have not confirmed",
    "does not mean the message is already in our inbox",
    "does not send the guide pack by itself",
    "Nothing on this site sends",
    "after a person confirms",
    "after a person sees",
    "There is no automatic download",
    "This page does not email the guide pack",
]
HIDDEN_PACKS = [
    "/diy/",
    "/dfy/",
    "/get-app-verified-diy-pack.pdf",
    "/get-app-verified-diy-pack.zip",
    "/get-app-verified-dfy-pack.pdf",
    "/get-app-verified-dfy-pack.zip",
]


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "a" and "href" in attr:
            self.links.append(attr["href"])
        if tag in {"img", "script", "link"} and (attr.get("src") or attr.get("href")):
            self.links.append(attr.get("src") or attr.get("href"))
        if tag == "input" and attr.get("type") == "password":
            self.errors.append("password input")
        if tag == "form":
            action = attr.get("action", "")
            if action and "formspree.io/f/xdeejbnl" not in action and not action.startswith("#"):
                self.errors.append(f"unexpected form action {action}")


def main():
    missing = [p for p in REQUIRED_PAGES if not (ROOT / p).is_file()]
    if missing:
        sys.exit("missing files: " + ", ".join(missing))
    pngs = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.png"))
    if pngs != sorted(GUIDE_PNGS):
        sys.exit("public png set must be the four guide images, found: " + ", ".join(pngs))
    for name in GUIDE_PNGS:
        raw = (ROOT / name).read_bytes()
        if not raw.startswith(PNG_MAGIC) or len(raw) < 24 or raw[12:16] != b"IHDR":
            sys.exit(f"bad png header: {name}")

    parts = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path.suffix.lower() != ".png":
            parts.append(path.read_text(encoding="utf-8"))
    text = "\n".join(parts)
    for needle in FORBIDDEN:
        if needle in text:
            sys.exit(f"public site contains paid or stale text: {needle}")
    if "innerHTML" in text:
        sys.exit("site script uses innerHTML")
    for link in STRIPE:
        if text.count(link) < 2:
            sys.exit(f"stripe link missing on home and intake: {link}")
    lowered = text.lower()
    if "getappverified@gmail.com" in lowered:
        sys.exit("old support email still present")
    if "xgaaqljw" in lowered:
        sys.exit("old form id still present")
    if "verify@velda.ai" not in text:
        sys.exit("verify@velda.ai missing")
    if "formspree.io/f/xdeejbnl" not in text:
        sys.exit("formspree endpoint missing")
    if "OAuth Config Editor" not in text:
        sys.exit("least-privilege role missing")
    if "We are not Google" not in text and "not Google" not in text:
        sys.exit("missing not-Google line")

    guide = (ROOT / "guides/01-add-us.html").read_text(encoding="utf-8")
    img_tags = re.findall(r"<img\b[^>]*>", guide, flags=re.IGNORECASE)
    if len(img_tags) != 4:
        sys.exit(f"01-add-us.html should contain 4 img tags, found {len(img_tags)}")
    srcs = []
    for tag in img_tags:
        match = re.search(r"""src\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
        if not match:
            sys.exit(f"img tag missing src: {tag}")
        srcs.append(match.group(1))
    for name in GUIDE_PNGS:
        if "/" + name not in srcs:
            sys.exit(f"guide is missing img src /{name}")
    if "New principals" not in guide:
        sys.exit("New principals step missing")
    if "active Google Account" not in guide or "Do not switch to Owner" not in guide:
        sys.exit("troubleshooting line missing")
    for name in ACCESS_PAGES:
        page = (ROOT / name).read_text(encoding="utf-8")
        if ACCESS_PHRASE not in page:
            sys.exit(f"{name} does not explain the private 4-hour link")
    for needle in STALE:
        if needle in text:
            sys.exit(f"public site still has a stale fulfillment line: {needle}")
    for name in ("intake.html", "privacy.html", "contact.html"):
        page = (ROOT / name).read_text(encoding="utf-8")
        if "delivers a copy to verify@velda.ai" not in page:
            sys.exit(f"{name} does not say Formspree delivers to verify@velda.ai")
    leaked = [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*")
        if p.is_file() and p.suffix.lower() in {".pdf", ".zip"}
    ]
    if leaked:
        sys.exit("paid pack file in the public site: " + ", ".join(leaked))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}/"
    seen = set()
    queue = [base]
    for path in REQUIRED_PAGES:
        if path.endswith(".html"):
            queue.append(urljoin(base, path))
    for name in GUIDE_PNGS:
        queue.append(urljoin(base, name))
    failures = []
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "")
        except Exception as exc:
            failures.append(f"{url} {exc}")
            continue
        if status != 200:
            failures.append(f"{url} status {status}")
            continue
        if url.endswith(".png"):
            if not body.startswith(PNG_MAGIC) or len(body) < 24 or body[12:16] != b"IHDR":
                failures.append(f"{url} is not a valid PNG")
            continue
        if "text/html" not in ctype and not url.endswith((".css", ".js", ".html", "/")):
            continue
        if "text/html" not in ctype and not url.endswith(".html") and url != base:
            if url.endswith(".js") or url.endswith(".css"):
                if not body:
                    failures.append(f"empty {url}")
            continue
        parser = LinkParser()
        parser.feed(body.decode("utf-8", "replace"))
        failures.extend(f"{url}: {err}" for err in parser.errors)
        for href in parser.links:
            if href.startswith(("mailto:", "tel:", "#")):
                continue
            joined = urljoin(url, href)
            parsed = urlparse(joined)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.netloc and parsed.netloc != f"127.0.0.1:{port}":
                continue
            clean = joined.split("#")[0]
            queue.append(clean)
    for rel in HIDDEN_PACKS:
        url = urljoin(base, rel.lstrip("/"))
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                failures.append(f"{rel} status {resp.status}, want 404")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                failures.append(f"{rel} status {exc.code}, want 404")
        except Exception as exc:
            failures.append(f"{rel} {exc}")
    httpd.shutdown()
    if failures:
        sys.exit("link failures:\n" + "\n".join(failures))
    pages = [u for u in seen if u.endswith(".html") or u.endswith("/")]
    print(f"public crawl ok: {len(seen)} urls, {len(pages)} html")


if __name__ == "__main__":
    main()
