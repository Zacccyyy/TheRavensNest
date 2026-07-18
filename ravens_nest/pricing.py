"""On-demand price extraction — no background scraper, ever.

"Price the basket" fetches each stored product URL once and runs the
supplier's extraction chain over the HTML. Every strategy is isolated and
independently failable: an exception or a miss falls through to the next
strategy, and a link that yields nothing falls back to last_price_aud
marked stale with its date. A price is only ever taken from the page —
never invented, never estimated.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "RavensNest/0.1 (local-first inventory; on-demand price check)"
FETCH_TIMEOUT = 10.0
MAX_PARALLEL_FETCHES = 6


def fetch_url(url: str) -> str:
    """Fetch one product page. Patched in tests."""
    response = httpx.get(
        url,
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.text


def _to_price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return price if price > 0 else None


def _from_json_ld(html: str) -> Decimal | None:
    """schema.org Product markup — the most reliable source when present."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in list(nodes):
            if isinstance(node, dict) and "@graph" in node:
                nodes.extend(g for g in node["@graph"] if isinstance(g, dict))
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type", "")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "Product" not in types:
                continue
            offers = node.get("offers") or {}
            offer_list = offers if isinstance(offers, list) else [offers]
            for offer in offer_list:
                if isinstance(offer, dict):
                    price = _to_price(offer.get("price") or offer.get("lowPrice"))
                    if price is not None:
                        return price
    return None


def _from_meta_tags(html: str) -> Decimal | None:
    """OpenGraph / microdata price attributes."""
    patterns = (
        r'<meta[^>]+(?:property|name)=["\'](?:product|og):price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:product|og):price:amount["\']',
        r'itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]+itemprop=["\']price["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            price = _to_price(match.group(1))
            if price is not None:
                return price
    return None


def _from_price_json_key(html: str) -> Decimal | None:
    """Inline JS state: "price": "12.95" / salePrice / formattedPrice etc."""
    for key in ("price", "salePrice", "productPrice", "actSkuCalPrice"):
        match = re.search(
            rf'"{key}"\s*:\s*"?(\d+(?:\.\d+)?)"?', html, re.IGNORECASE
        )
        if match:
            price = _to_price(match.group(1))
            if price is not None:
                return price
    return None


Strategy = Callable[[str], "Decimal | None"]

GENERIC_CHAIN: tuple[Strategy, ...] = (_from_json_ld, _from_meta_tags, _from_price_json_key)

# Per-supplier chains: same isolated strategies, ordered for the site.
# Structure over per-site perfection — sites drift, adapters are swappable.
ADAPTERS: dict[str, tuple[Strategy, ...]] = {
    "AliExpress": (_from_price_json_key, _from_json_ld, _from_meta_tags),
}


def extract_price(supplier_name: str, html: str) -> Decimal | None:
    """Run the supplier's strategy chain. Each strategy is independently
    failable; a miss or crash just moves to the next. None means the page
    yielded no price — the caller falls back to the stored one."""
    for strategy in ADAPTERS.get(supplier_name, GENERIC_CHAIN):
        try:
            price = strategy(html)
        except Exception:
            log.exception("price strategy %s crashed", strategy.__name__)
            continue
        if price is not None:
            return price
    return None


def price_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch + extract for each link concurrently. Returns one outcome per
    link: {"outcome": "ok", "price": ...} or {"outcome": "stale", "detail"}.
    Never raises — one bad page never blocks the rest of the basket."""

    def check(link: dict[str, Any]) -> dict[str, Any]:
        try:
            html = fetch_url(link["url"])
        except Exception as exc:
            return {**link, "outcome": "stale", "detail": f"fetch failed: {exc}"}
        price = extract_price(link["supplier_name"], html)
        if price is None:
            return {**link, "outcome": "stale", "detail": "no price found on page"}
        return {**link, "outcome": "ok", "price": price}

    if not links:
        return []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCHES) as pool:
        return list(pool.map(check, links))
