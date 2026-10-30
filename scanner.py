#!/usr/bin/env python3
"""Watch deal scanner: searches eBay UK and Vinted UK for watches on your
list at or under your max price, and sends new matches to Telegram.

Standard library only. Configure with environment variables:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   eBay developer app keys (Production)
  TELEGRAM_BOT_TOKEN                   Telegram bot token
  (your Telegram chat id goes in watches.json as telegram_chat_id)

Usage:
  python3 scanner.py              scan and send alerts
  python3 scanner.py --dry-run    scan and print matches, no Telegram
  python3 scanner.py --test-alert send one test Telegram message
"""

import argparse
import base64
import html
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "watches.json"
SEEN_PATH = HERE / "seen.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
EBAY_WRISTWATCHES_CATEGORY = "31387"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr)


def http_json(url, *, headers=None, data=None, opener=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- matching

def title_ok(title, watch, global_exclude):
    t = title.lower()
    # A list inside must_include means "any one of these words".
    for word in watch.get("must_include", []):
        options = word if isinstance(word, list) else [word]
        if not any(w.lower() in t for w in options):
            return False
    banned = global_exclude + watch.get("exclude", [])
    return not any(word.lower() in t for word in banned)


def search_cap(watch):
    return max(watch["max_price"], watch.get("max_price_new_boxed", 0))


def price_cap(watch, item):
    # Optional higher limit for brand new watches sold with box and papers.
    if "max_price_new_boxed" in watch and item["condition"].lower().startswith("new with box"):
        return watch["max_price_new_boxed"]
    return watch["max_price"]


# ---------------------------------------------------------------- eBay

class Ebay:
    """eBay Browse API (official). Needs a free developer account."""

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None

    def _get_token(self):
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }).encode()
        # eBay's token endpoint occasionally returns a spurious 401, so retry.
        for attempt in range(3):
            try:
                resp = http_json(
                    "https://api.ebay.com/identity/v1/oauth2/token",
                    headers={"Authorization": f"Basic {creds}",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data=body,
                )
                break
            except urllib.error.HTTPError:
                if attempt == 2:
                    raise
                time.sleep(3)
        self.token = resp["access_token"]

    def search(self, watch, since):
        if not self.token:
            self._get_token()
        filters = [
            f"price:[..{search_cap(watch)}]",
            "priceCurrency:GBP",
            "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
            "itemLocationCountry:GB",
            f"itemStartDate:[{since.strftime('%Y-%m-%dT%H:%M:%S.000Z')}..]",
        ]
        params = urllib.parse.urlencode({
            "q": watch["query"],
            "category_ids": EBAY_WRISTWATCHES_CATEGORY,
            "filter": ",".join(filters),
            "sort": "newlyListed",
            "limit": "100",
        })
        resp = http_json(
            f"https://api.ebay.com/buy/browse/v1/item_summary/search?{params}",
            headers={"Authorization": f"Bearer {self.token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        )
        for item in resp.get("itemSummaries", []):
            # Multi-variation listings can come back without a single price.
            if "value" not in item.get("price", {}):
                continue
            price = float(item["price"]["value"])
            shipping = 0.0
            for opt in item.get("shippingOptions", []):
                cost = opt.get("shippingCost", {}).get("value")
                if cost is not None:
                    shipping = float(cost)
                    break
            yield {
                "id": f"ebay:{item['itemId']}",
                "site": "eBay",
                "title": item.get("title", ""),
                "price": price,
                "total": price + shipping,
                "condition": item.get("condition", ""),
                "url": item.get("itemWebUrl", ""),
                "image": item.get("image", {}).get("imageUrl", ""),
            }


# ---------------------------------------------------------------- Vinted

class Vinted:
    """Vinted UK via the JSON endpoint its own website uses (unofficial)."""

    BASE = "https://www.vinted.co.uk"

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.ready = False

    def _init_session(self):
        # Visiting the homepage sets the anonymous session cookie the API needs.
        req = urllib.request.Request(self.BASE + "/", headers={"User-Agent": USER_AGENT})
        self.opener.open(req, timeout=20).read()
        self.ready = True

    def search(self, watch, since):
        if not self.ready:
            self._init_session()
        params = urllib.parse.urlencode({
            "search_text": watch["query"],
            "price_to": search_cap(watch),
            "currency": "GBP",
            "order": "newest_first",
            "per_page": "96",
            "page": "1",
        })
        resp = http_json(
            f"{self.BASE}/api/v2/catalog/items?{params}",
            headers={"Accept": "application/json"},
            opener=self.opener,
        )
        for item in resp.get("items", []):
            created = (item.get("photo") or {}).get("high_resolution", {}).get("timestamp")
            if created and datetime.fromtimestamp(created, timezone.utc) < since:
                continue
            price = _vinted_amount(item.get("price"))
            total = _vinted_amount(item.get("total_item_price")) or price
            yield {
                "id": f"vinted:{item['id']}",
                "site": "Vinted",
                "title": item.get("title", ""),
                "price": price,
                "total": total,
                "condition": item.get("status", ""),
                "url": item.get("url") or f"{self.BASE}/items/{item['id']}",
                "image": (item.get("photo") or {}).get("url", ""),
            }


def _vinted_amount(value):
    # Vinted has returned prices both as {"amount": "12.0"} and as plain strings.
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- Telegram

def telegram_chat_id():
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    return str(json.loads(CONFIG_PATH.read_text()).get("telegram_chat_id", ""))


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    body = urllib.parse.urlencode({
        "chat_id": telegram_chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    http_json(f"https://api.telegram.org/bot{token}/sendMessage", data=body)


def format_alert(watch, item):
    lines = [
        f"⌚ <b>{html.escape(watch['name'])}</b> on {item['site']}",
        html.escape(item["title"]),
        f"💷 £{item['price']:.2f} (≈ £{item['total']:.2f} incl. fees/postage) · max £{price_cap(watch, item)}",
    ]
    if watch.get("resale_price"):
        margin = watch["resale_price"] - item["total"]
        lines.append(f"📈 Est. resale £{watch['resale_price']} → ~£{margin:.0f} before selling fees")
    if item["condition"]:
        lines.append(f"Condition: {html.escape(str(item['condition']))}")
    lines.append(item["url"])
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def load_seen():
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    try:
        SEEN_PATH.write_text(json.dumps(sorted(seen)[-5000:]))
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print matches instead of sending")
    ap.add_argument("--test-alert", action="store_true", help="send a test Telegram message")
    args = ap.parse_args()

    if args.test_alert:
        send_telegram("✅ Watch scanner is connected to Telegram.")
        log("Test alert sent.")
        return 0

    config = json.loads(CONFIG_PATH.read_text())
    since = datetime.now(timezone.utc) - timedelta(minutes=config.get("lookback_minutes", 75))
    global_exclude = config.get("global_exclude", [])

    enabled = config.get("sites", ["ebay"])
    sources = []
    if "ebay" in enabled:
        if os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"):
            sources.append(Ebay(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]))
        else:
            log("eBay skipped: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set.")
    if "vinted" in enabled:
        sources.append(Vinted())
    if not sources:
        log("No sites to search.")
        return 2

    if not args.dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN") and telegram_chat_id()):
        log("TELEGRAM_BOT_TOKEN or telegram_chat_id not set; use --dry-run or set them.")
        return 2

    seen = load_seen()
    found = errors = 0
    for watch in config["watches"]:
        for source in sources:
            name = type(source).__name__
            try:
                items = list(source.search(watch, since))
            except Exception as exc:  # one site failing shouldn't stop the rest
                errors += 1
                log(f"{name} search failed for {watch['name']}: {exc}")
                continue
            for item in items:
                if item["id"] in seen or not item["price"]:
                    continue
                if item["price"] > price_cap(watch, item) or not title_ok(item["title"], watch, global_exclude):
                    continue
                seen.add(item["id"])
                found += 1
                msg = format_alert(watch, item)
                if args.dry_run:
                    print(msg, "\n")
                else:
                    try:
                        send_telegram(msg)
                    except Exception as exc:
                        log(f"Telegram send failed: {exc}")
            time.sleep(1)  # be gentle with the sites

    save_seen(seen)
    log(f"Done: {found} new match(es), {errors} search error(s).")
    return 1 if errors and not found else 0


if __name__ == "__main__":
    sys.exit(main())
