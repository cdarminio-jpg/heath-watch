#!/usr/bin/env python3
"""
Watch a Heath Ceramics "Pass the Plate" seller page and alert on new listings.

Setup (once):
    pip install playwright
    playwright install chromium

Run:
    python heath_watch.py              # check once (first run just records what's there)
    python heath_watch.py --every 15   # keep running, check every 15 minutes
    python heath_watch.py --test       # send a test alert to confirm notifications work

Alerts are configured with environment variables (set either or both):
    NTFY_TOPIC   phone push via the free ntfy app, e.g. "heath-watch-x7k2p9"
    SMTP_USER, SMTP_PASS, EMAIL_TO   email (for Gmail, SMTP_PASS is an app password)
    SMTP_HOST    optional, defaults to smtp.gmail.com
"""
import argparse
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

URL = "https://passtheplate.heathceramics.com/u/66e22b58-baa2-4cbe-aa88-9e2dbc983e9e"
STATE_FILE = Path(__file__).with_name("seen_listings.json")

# Listing links look like /l/<name>/<uuid>. "/l/new" (the Sell button) has no uuid, so it's ignored.
LISTING_RE = re.compile(
    r"/l/(?:[^/?#]+/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch_listings(url):
    """Load the page in a headless browser and return {listing_id: {"title", "url"}}."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass  # some pages never go fully idle; carry on with what loaded

        # Scroll until no more listings appear, in case the page lazy-loads them.
        grab = """els => els.map(a => ({
            href: a.href,
            text: (a.innerText || '').trim(),
            alt: (a.querySelector('img') || {}).alt || ''
        }))"""
        links, last_count = [], -1
        for _ in range(20):
            links = page.eval_on_selector_all("a[href]", grab)
            count = sum(1 for l in links if LISTING_RE.search(l["href"]))
            if count == last_count:
                break
            last_count = count
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(1500)
        browser.close()

    listings = {}
    for link in links:
        m = LISTING_RE.search(link["href"])
        if not m:
            continue
        lid = m.group(1).lower()
        title = " ".join((link["text"] or link["alt"]).split())
        entry = listings.setdefault(lid, {"title": "", "url": link["href"].split("?")[0]})
        if len(title) > len(entry["title"]):
            entry["title"] = title
    for entry in listings.values():
        entry["title"] = entry["title"] or "(untitled listing)"
    return listings


def notify(subject, body, click_url=URL):
    """Send the alert on every configured channel. Returns True if all succeeded."""
    ok = True
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": subject, "Click": click_url, "Tags": "bell"},
            )
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            log(f"ntfy alert failed: {e}")
            ok = False
    if SMTP_USER and SMTP_PASS and EMAIL_TO:
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, EMAIL_TO
            msg.set_content(body)
            with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        except Exception as e:
            log(f"email alert failed: {e}")
            ok = False
    if not NTFY_TOPIC and not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        log("No alert channel configured (set NTFY_TOPIC or the SMTP_* variables).")
    return ok


def check_once(url):
    listings = fetch_listings(url)
    if not listings:
        log("No listings found. The page may be down or empty; nothing changed.")
        return

    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(listings, indent=2))
        log(f"First run: recorded {len(listings)} existing listings. Future additions will alert.")
        return

    seen = json.loads(STATE_FILE.read_text())
    new = {lid: item for lid, item in listings.items() if lid not in seen}
    if not new:
        log(f"No new listings ({len(listings)} on page).")
        return

    lines = [f"{item['title']}\n{item['url']}" for item in new.values()]
    subject = f"Heath resale: {len(new)} new listing{'s' if len(new) != 1 else ''}"
    log(subject + " -> " + "; ".join(item["title"] for item in new.values()))
    first_url = next(iter(new.values()))["url"] if len(new) == 1 else url
    if notify(subject, "\n\n".join(lines), first_url):
        # Only remember them once the alert went out, so a failed alert retries next check.
        seen.update(new)
        STATE_FILE.write_text(json.dumps(seen, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Alert on new Heath resale listings.")
    ap.add_argument("--url", default=URL, help="seller page to watch")
    ap.add_argument("--every", type=float, metavar="MIN", help="keep running, check every MIN minutes")
    ap.add_argument("--test", action="store_true", help="send a test alert and exit")
    args = ap.parse_args()

    if args.test:
        ok = notify("Heath resale: test alert", "Notifications are working.", args.url)
        sys.exit(0 if ok else 1)

    if not args.every:
        check_once(args.url)
        return

    while True:
        try:
            check_once(args.url)
        except Exception as e:
            log(f"Check failed, will retry: {e}")
        time.sleep(max(args.every, 1) * 60)


if __name__ == "__main__":
    main()
