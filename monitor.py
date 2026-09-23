"""Generic Yahoo Taiwan auction watcher. Personal settings belong in Secrets."""

import argparse
import hashlib
import hmac
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

STATE = Path(__file__).with_name("seen.json")
ITEM_ID = re.compile(r"/item/(?:[^/?#]*-)?(\d{8,})(?:[/?#]|$)")


class SafeError(Exception):
    """An error whose message contains no private values."""


def settings():
    required = ("BOOTH_URL", "MAIL_TO", "SMTP_USER", "SMTP_PASSWORD")
    if any(not os.getenv(name, "").strip() for name in required):
        raise SafeError("Missing required Repository secrets; see README.")
    config = {name: os.environ[name].strip() for name in required}
    config["SMTP_PASSWORD"] = "".join(config["SMTP_PASSWORD"].split())
    config["SMTP_HOST"] = os.getenv("SMTP_HOST", "").strip() or "smtp.gmail.com"
    config["SMTP_PORT"] = int(os.getenv("SMTP_PORT", "").strip() or "465")
    config["SMTP_FROM"] = os.getenv("SMTP_FROM", "").strip() or config["SMTP_USER"]
    url = urlparse(config["BOOTH_URL"])
    if (url.scheme != "https" or url.hostname != "tw.bid.yahoo.com"
            or not re.fullmatch(r"/booth/[A-Za-z0-9]+/?", url.path)
            or url.query or url.fragment or url.username or url.password):
        raise SafeError("BOOTH_URL must be an HTTPS Yahoo booth URL without filters.")
    config["BOOTH_URL"] = config["BOOTH_URL"].rstrip("/")
    return config


def item_token(config, item_id):
    # Public state stores keyed hashes, never seller URLs or actual product IDs.
    message = config["BOOTH_URL"] + "\n" + str(item_id)
    return hmac.new(config["SMTP_PASSWORD"].encode(), message.encode(), hashlib.sha256).hexdigest()


def collect_items(page):
    links = page.locator('a[href*="/item/"]').evaluate_all(
        "els => els.map(a => ({href:a.href, text:(a.innerText || a.title || '').trim(), "
        "alt:(a.querySelector('img')?.alt || '').trim()}))"
    )
    items = {}
    for link in links:
        url = urlparse(link["href"])
        if url.hostname != "tw.bid.yahoo.com":
            continue
        match = ITEM_ID.search(url.path)
        if match:
            items.setdefault(match.group(1), {
                "name": (link["text"] or link["alt"] or "新商品").splitlines()[0][:160],
                "url": "https://tw.bid.yahoo.com" + url.path,
            })
    return items


def fetch_items(config):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(locale="zh-TW", timezone_id="Asia/Taipei")
        page.goto(config["BOOTH_URL"], wait_until="domcontentloaded", timeout=45000)
        page.get_by_role("heading", level=1).wait_for(timeout=30000)
        page.get_by_role("button", name="最新上架", exact=True).wait_for(timeout=30000)
        # Require actual items: a zero-count loading/error page must not create a baseline.
        page.locator('a[href*="/item/"]').first.wait_for(timeout=30000)
        page.wait_for_timeout(2500)
        body = page.locator("body").inner_text()
        items = collect_items(page)
        browser.close()
    match = re.search(r"([0-9,]+)\s*筆結果", body)
    if not match or not items:
        raise SafeError("Catalog unavailable; previous records kept. No email sent.")
    count = int(match.group(1).replace(",", ""))
    if count == 0 or len(items) > count:
        raise SafeError("Catalog count inconsistent; previous records kept.")
    return items, count


def deliver(config, subject, body):
    message = EmailMessage()
    message["From"] = config["SMTP_FROM"]
    message["To"] = config["MAIL_TO"]
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if config["SMTP_PORT"] == 465:
        smtp = smtplib.SMTP_SSL(config["SMTP_HOST"], 465, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=30)
    with smtp:
        if config["SMTP_PORT"] != 465:
            smtp.starttls(context=context)
        smtp.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        smtp.send_message(message)


def check(config):
    items, count = fetch_items(config)
    old = json.loads(STATE.read_text()) if STATE.exists() else None
    context = item_token(config, "context-v2")
    current = {item_token(config, key): value for key, value in items.items()}
    if old is None or old.get("context") != context:
        # New credentials/booth establish a fresh baseline without mailing old items.
        result = {"schema": 2, "context": context, "tokens": sorted(current)}
        print(f"Initial baseline: {len(items)} products; page reports {count} total.")
        print("First-page monitoring only. No historical items emailed.")
    else:
        if old.get("schema") != 2 or not isinstance(old.get("tokens"), list):
            raise SafeError("State format invalid; no email sent.")
        previous = set(old["tokens"])
        new = {key: value for key, value in current.items() if key not in previous}
        if new:
            body = "發現新上架商品：\n\n" + "\n\n".join(
                f"{item['name']}\n{item['url']}" for item in new.values()
            )
            deliver(config, f"Yahoo 賣場新上架：{len(new)} 件", body)
            print(f"New-item email accepted by mail server: {len(new)} products.")
        else:
            print("No new products detected.")
        result = {"schema": 2, "context": context, "tokens": sorted(previous | set(current))}
    # No state change if collection or mail delivery failed; failed sends retry next run.
    if result != old:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2) + "\n")
        tmp.replace(STATE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-email", action="store_true")
    args = parser.parse_args()
    config = settings()
    if args.test_email:
        deliver(config, "Yahoo 新品通知：寄信測試", "這是測試信，代表寄信設定正常。")
        print("Test email accepted by mail server. Product records unchanged.")
    else:
        check(config)


if __name__ == "__main__":
    try:
        main()
    except SafeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except smtplib.SMTPAuthenticationError:
        print("Email login failed. Check SMTP_USER and SMTP_PASSWORD secrets.", file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Raw browser/SMTP exceptions may contain full URLs or addresses.
        print("Run failed; private error details suppressed. Check page access and email settings.", file=sys.stderr)
        sys.exit(1)
