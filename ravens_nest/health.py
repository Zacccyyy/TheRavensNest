"""Data-quality health report: a score plus itemised, clickable counts,
each pointing at a fix flow — not just a report."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from . import bom, config, db, events, merge

PRICE_STALE_DAYS_DEFAULT = 90
UNTOUCHED_MONTHS = 12


def price_stale_days() -> int:
    try:
        return int(os.environ.get("RAVENS_NEST_PRICE_STALE_DAYS", PRICE_STALE_DAYS_DEFAULT))
    except ValueError:
        return PRICE_STALE_DAYS_DEFAULT


def report(conn) -> dict[str, Any]:
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE archived = 0")]
    linked = {r["item_id"] for r in conn.execute("SELECT DISTINCT item_id FROM item_links")}
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(days=price_stale_days())).isoformat()
    untouched_cutoff = (now - timedelta(days=UNTOUCHED_MONTHS * 30)).isoformat()

    def entry(key, label, rows, fix_hint, fix_href):
        return {
            "key": key,
            "label": label,
            "count": len(rows),
            "rows": rows,
            "fix_hint": fix_hint,
            "fix_href": fix_href,
        }

    no_location = [i for i in items if not i["location_id"]]
    no_price = [i for i in items if i["last_paid_aud"] is None]
    no_link = [i for i in items if i["id"] not in linked]
    no_photo = [i for i in items if not i["photo_hash"]]
    no_min = [i for i in items if i["min_qty"] is None]
    untouched = [i for i in items if i["updated_ts"] < untouched_cutoff]

    stale_prices = [
        dict(r)
        for r in conn.execute(
            """SELECT l.*, i.name AS item_name, s.name AS supplier_name
               FROM item_links l
               JOIN items i ON i.id = l.item_id AND i.archived = 0
               JOIN suppliers s ON s.id = l.supplier_id
               WHERE l.last_price_aud IS NOT NULL
                 AND (l.last_checked_ts IS NULL OR l.last_checked_ts < ?)""",
            (stale_cutoff,),
        )
    ]
    unresolved_bom = [
        dict(r)
        for r in conn.execute(
            """SELECT b.*, p.name AS project_name FROM bom_lines b
               LEFT JOIN projects p ON p.id = b.project_id
               WHERE b.item_id IS NULL"""
        )
    ]
    reserved = bom.reserved_by_item(conn)
    empty_bins = [
        r["id"]
        for r in conn.execute("SELECT id FROM locations ORDER BY unit, shelf, bin, section")
        if not conn.execute(
            "SELECT 1 FROM items WHERE location_id = ? AND archived = 0 LIMIT 1", (r["id"],)
        ).fetchone()
    ]
    duplicates = merge.likely_duplicate_pairs(conn, limit=15)

    checks = [
        entry("no_location", "Items with no location", no_location,
              "move it to a bin", "/items/{id}"),
        entry("no_price", "Items with no last-paid price", no_price,
              "record an order or set it on the item card", "/items/{id}"),
        entry("no_link", "Items with no supplier link", no_link,
              "add a link on the sourcing page", "/items/{id}/sourcing"),
        entry("no_photo", "Items with no photo", no_photo,
              "capture one from the phone UI", "/items/{id}"),
        entry("no_min", "Items with no min qty (can't reorder-alert)", no_min,
              "set a minimum on the item card", "/items/{id}"),
        entry("untouched", f"Items untouched for >{UNTOUCHED_MONTHS} months", untouched,
              "recount their bin to confirm they still exist", "/items/{id}"),
    ]
    # Score: share of items passing each per-item check, averaged.
    total = len(items)
    if total:
        rates = [1 - (c["count"] / total) for c in checks]
        score = round(100 * sum(rates) / len(rates))
    else:
        score = 100

    return {
        "score": score,
        "total_items": total,
        "quarantined": events.quarantined_count(),
        "assets_with_gps": assets_with_gps(),
        "checks": checks,
        "stale_prices": stale_prices,
        "stale_days": price_stale_days(),
        "unresolved_bom": unresolved_bom,
        "empty_bins": empty_bins,
        "duplicates": duplicates,
        "reserved_shortfalls": [
            i for i in items
            if reserved.get(i["id"]) and reserved[i["id"]] > db.parse_qty(i["qty_on_hand"])
        ],
    }


def assets_with_gps() -> list[str]:
    """Assets from before EXIF stripping (audit C4) that still carry GPS
    tags. New ingests are always clean; re-uploading an old photo clears
    it (the sanitized bytes get a new hash)."""
    directory = config.assets_dir()
    if not directory.is_dir():
        return []
    flagged = []
    try:
        from PIL import Image
    except ImportError:
        return []
    for path in sorted(directory.glob("*.jpg")):
        try:
            with Image.open(path) as image:
                if image.getexif().get_ifd(0x8825):  # GPSInfo IFD
                    flagged.append(path.name)
        except Exception:
            continue  # unreadable asset ≠ GPS leak
    return flagged


def sync_summary() -> dict[str, Any]:
    """Unpushed events / sync reachability — best effort, never raises."""
    try:
        from .sync import SyncManager

        manager = SyncManager()
        return {
            "has_remote": manager.has_remote(),
            "unpushed_events": manager.unpushed_event_count(),
        }
    except Exception as exc:
        return {"has_remote": False, "unpushed_events": None, "error": str(exc)}
