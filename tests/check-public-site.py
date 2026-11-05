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
    pngs = list(ROOT.rglob("*.png"))
    if pngs:
        sys.exit("public site should not ship screenshots: " + ", ".join(map(str, pngs)))

    text = "\n".join((ROOT / p).read_text(encoding="utf-8") for p in REQUIRED_PAGES if p.endswith((".html", ".js")))
    for needle in FORBIDDEN:
        if needle in text:
            sys.exit(f"public site contains paid or stale text: {needle}")
    if "innerHTML" in text:
        sys.exit("site script uses innerHTML")
    for link in STRIPE:
        if text.count(link) < 2:
            sys.exit(f"stripe link missing on home and intake: {link}")
    if "getappverified@gmail.com" not in text:
        sys.exit("contact email missing")
    if "OAuth Config Editor" not in text:
        sys.exit("least-privilege role missing")
    if "We are not Google" not in text and "not Google" not in text:
        sys.exit("missing not-Google line")

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
    httpd.shutdown()
    if failures:
        sys.exit("link failures:\n" + "\n".join(failures))
    pages = [u for u in seen if u.endswith(".html") or u.endswith("/")]
    print(f"public crawl ok: {len(seen)} urls, {len(pages)} html")


if __name__ == "__main__":
    main()
