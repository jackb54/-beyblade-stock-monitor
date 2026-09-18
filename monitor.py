import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

WATCHLIST_FILE = Path("watchlist.json")
STATE_FILE = Path("state.json")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

OUT_WORDS = (
    "out of stock",
    "sold out",
    "currently unavailable",
    "temporarily unavailable",
    "not available",
)

IN_WORDS = (
    "add to cart",
    "add to bag",
    "buy now",
    "in stock",
    "available for shipping",
    "ship it",
)


def load_json(path, default):
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def clean_price(value):
    if value is None:
        return None

    text = str(value).replace(",", "")
    match = re.search(r"(\d+(?:\.\d{1,2})?)", text)

    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None


def extract_jsonld(soup):
    prices = []
    availability = []

    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]

        while stack:
            obj = stack.pop()

            if isinstance(obj, dict):
                offers = obj.get("offers")

                if isinstance(offers, dict):
                    if offers.get("price") is not None:
                        price = clean_price(offers.get("price"))
                        if price is not None:
                            prices.append(price)

                    if offers.get("lowPrice") is not None:
                        price = clean_price(offers.get("lowPrice"))
                        if price is not None:
                            prices.append(price)

                    if offers.get("availability"):
                        availability.append(
                            str(offers["availability"]).lower()
                        )

                elif isinstance(offers, list):
                    stack.extend(offers)

                for value in obj.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)

            elif isinstance(obj, list):
                stack.extend(obj)

    return prices, availability


def extract_visible_prices(text):
    matches = re.findall(
        r"\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)",
        text,
    )

    prices = []

    for match in matches:
        price = clean_price(match)
        if price is not None and 0.50 <= price <= 1000:
            prices.append(price)

    return prices


def inspect_listing(listing):
    url = listing["url"]

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20,
            allow_redirects=True,
        )

        if response.status_code != 200:
            return {
                "qualifies": False,
                "reason": f"HTTP {response.status_code}",
            }

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = " ".join(soup.stripped_strings).lower()

        jsonld_prices, jsonld_availability = extract_jsonld(
            BeautifulSoup(response.text, "html.parser")
        )

        visible_prices = extract_visible_prices(text)

        prices = jsonld_prices or visible_prices

        max_price = float(listing["max_price"])

        qualifying_prices = [
            p for p in prices
            if p <= max_price
        ]

        has_out = any(word in text for word in OUT_WORDS)

        has_in = any(word in text for word in IN_WORDS)

        schema_in = any(
            "instock" in item or "limitedavailability" in item
            for item in jsonld_availability
        )

        schema_out = any(
            "outofstock" in item
            or "soldout" in item
            or "discontinued" in item
            for item in jsonld_availability
        )

        # Conservative stock rule:
        # Don't alert merely because a search/indexed page has a price.
        stock_signal = (has_in or schema_in) and not schema_out

        # If the page explicitly says sold/out-of-stock, require
        # structured in-stock data before trusting it.
        if has_out and not schema_in:
            stock_signal = False

        if not qualifying_prices:
            return {
                "qualifies": False,
                "reason": "No qualifying price detected",
            }

        if not stock_signal:
            return {
                "qualifies": False,
                "reason": "No reliable in-stock signal",
            }

        price = min(qualifying_prices)

        return {
            "qualifies": True,
            "price": price,
            "final_url": response.url,
            "seller": listing.get(
                "seller",
                urlparse(url).netloc,
            ),
            "fulfillment": listing.get(
                "fulfillment",
                "Shipping / availability shown on listing",
            ),
        }

    except Exception as exc:
        return {
            "qualifies": False,
            "reason": str(exc)[:200],
        }


def send_discord(bey, listing, result):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL GitHub secret is missing."
        )

    title = f"🚨 {bey['name']} IN STOCK"

    description = (
        f"**Price:** ${result['price']:.2f}\n"
        f"**Your limit:** ${float(listing['max_price']):.2f}\n"
        f"**Seller:** {result['seller']}\n"
        f"**Availability:** {result['fulfillment']}\n\n"
        f"**[🛒 BUY / OPEN LISTING]({result['final_url']})**"
    )

    payload = {
        "username": "Beyblade Stock Bot",
        "content": "@everyone",
        "embeds": [
            {
                "title": title,
                "description": description,
                "url": result["final_url"],
            }
        ],
        "allowed_mentions": {
            "parse": ["everyone"]
        },
    }

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=15,
    )

    response.raise_for_status()


def main():
    watchlist = load_json(WATCHLIST_FILE, {"beys": []})
    state = load_json(STATE_FILE, {})

    changed = False

    for bey in watchlist.get("beys", []):
        if not bey.get("enabled", True):
            continue

        for listing in bey.get("listings", []):
            if not listing.get("enabled", True):
                continue

            key = listing["url"]
            previous = state.get(key, {})
            result = inspect_listing(listing)

            currently_qualifies = result.get("qualifies", False)
            previously_qualified = previous.get(
                "qualifies",
                False,
            )

            print(
                bey["name"],
                listing.get("seller", ""),
                result,
            )

            # Alert only on unavailable -> qualifying transition.
            if currently_qualifies and not previously_qualified:
                send_discord(bey, listing, result)

            new_state = {
                "qualifies": currently_qualifies,
            }

            if currently_qualifies:
                new_state["price"] = result["price"]

            if previous != new_state:
                state[key] = new_state
                changed = True

    if changed or not STATE_FILE.exists():
        save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
