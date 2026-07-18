"""Duplicate detection and item merging.

Merge semantics: target keeps identity; quantities sum; the source's
aliases, supplier links, active reservations, and photo transfer; the
source's name becomes an alias on the target; the source archives at
qty 0 — its history stays and is readable from the target. Everything is
recorded in one item.merged event whose payload is fully precomputed
here, so replay never guesses and undo can reverse it exactly.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from . import db, store

DUPLICATE_SCORE = 87


def near_matches(
    conn, name: str, part_number: str | None = None, exclude_id: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Fuzzy near-duplicates across name, part_number, and aliases —
    used proactively at capture-confirm and import time."""
    aliases: dict[str, list[str]] = {}
    for row in conn.execute("SELECT alias_text, item_id FROM aliases"):
        aliases.setdefault(row["item_id"], []).append(row["alias_text"])
    query_name = (name or "").strip().lower()
    query_part = (part_number or "").strip().lower()
    scored = []
    for item in conn.execute("SELECT * FROM items WHERE archived = 0"):
        if item["id"] == exclude_id:
            continue
        texts = [item["name"], item["part_number"] or ""] + aliases.get(item["id"], [])
        best = 0.0
        for text in texts:
            text = text.strip().lower()
            if not text:
                continue
            if query_part and text == query_part:
                best = 100.0
                break
            if query_name:
                best = max(best, fuzz.WRatio(query_name, text))
        if best >= DUPLICATE_SCORE:
            scored.append((best, dict(item)))
    scored.sort(key=lambda pair: -pair[0])
    return [{**item, "score": round(score)} for score, item in scored[:limit]]


def likely_duplicate_pairs(conn, limit: int = 30) -> list[dict[str, Any]]:
    """Standalone scan: same part number or very similar names."""
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE archived = 0 ORDER BY name")]
    pairs = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            part_match = (
                a["part_number"]
                and b["part_number"]
                and a["part_number"].strip().lower() == b["part_number"].strip().lower()
            )
            score = fuzz.WRatio(a["name"].lower(), b["name"].lower())
            if part_match or score >= DUPLICATE_SCORE:
                pairs.append(
                    {
                        "a": a,
                        "b": b,
                        "score": 100 if part_match else round(score),
                        "same_part": bool(part_match),
                        "unit_mismatch": a["unit_type"] != b["unit_type"],
                    }
                )
    pairs.sort(key=lambda p: -p["score"])
    return pairs[:limit]


def build_merge_payload(
    conn, source_id: str, target_id: str, location_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Precompute everything item.merged's applier will do. Returns
    (payload, error). location_id resolves a location conflict — required
    when source and target disagree (we never guess)."""
    source = conn.execute("SELECT * FROM items WHERE id = ?", (source_id,)).fetchone()
    target = conn.execute("SELECT * FROM items WHERE id = ?", (target_id,)).fetchone()
    if source is None or target is None:
        return None, "one of the items no longer exists"
    if source_id == target_id:
        return None, "cannot merge an item into itself"
    if source["archived"]:
        return None, "the source item is archived — unarchive it first if this is a real merge"

    source_loc, target_loc = source["location_id"], target["location_id"]
    if location_id is None:
        if source_loc and target_loc and source_loc != target_loc:
            return None, (
                f"the items live in different bins ({source_loc} vs {target_loc}) — "
                f"choose which location the merged item keeps"
            )
        location_id = target_loc or source_loc

    aliases = [
        r["alias_text"]
        for r in conn.execute("SELECT alias_text FROM aliases WHERE item_id = ?", (source_id,))
    ]
    target_suppliers = {
        r["supplier_id"]
        for r in conn.execute(
            "SELECT supplier_id FROM item_links WHERE item_id = ?", (target_id,)
        )
    }
    link_suppliers = [
        r["supplier_id"]
        for r in conn.execute(
            "SELECT supplier_id FROM item_links WHERE item_id = ?", (source_id,)
        )
        if r["supplier_id"] not in target_suppliers  # target's own links win
    ]
    reservation_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM reservations WHERE item_id = ? AND status = 'active'",
            (source_id,),
        )
    ]
    photo_transferred = bool(source["photo_hash"]) and not target["photo_hash"]
    payload = {
        "source_id": source_id,
        "target_id": target_id,
        "qty": db.qty_str(db.parse_qty(source["qty_on_hand"])),
        "source_name": source["name"],
        "source_photo_hash": source["photo_hash"],
        "photo_transferred": photo_transferred,
        "target_prev_photo": target["photo_hash"],
        "aliases": aliases,
        "link_suppliers": link_suppliers,
        "reservation_ids": reservation_ids,
        "location_id": location_id,
        "target_prev_location": target_loc,
        "source_prev_location": source_loc,
        "unit_mismatch": source["unit_type"] != target["unit_type"],
    }
    return payload, None


def perform_merge(
    source_id: str,
    target_id: str,
    location_id: str | None = None,
    allow_unit_mismatch: bool = False,
) -> tuple[bool, str, str | None]:
    """Returns (ok, message, merge_event_id)."""
    conn = db.connect()
    try:
        payload, error = build_merge_payload(conn, source_id, target_id, location_id)
        if error:
            return False, error, None
        source = conn.execute("SELECT name, unit_type FROM items WHERE id = ?", (source_id,)).fetchone()
        target = conn.execute("SELECT name, unit_type FROM items WHERE id = ?", (target_id,)).fetchone()
    finally:
        conn.close()
    if payload["unit_mismatch"] and not allow_unit_mismatch:
        return False, (
            f"unit types differ ({source['unit_type']} vs {target['unit_type']}) — "
            f"summing them would corrupt quantities. Tick the confirmation if you're "
            f"sure they're really the same thing."
        ), None
    event = store.merge_items(payload)
    return True, (
        f"Merged '{source['name']}' into '{target['name']}' "
        f"(+{payload['qty']}, {len(payload['aliases'])} alias(es) and "
        f"{len(payload['link_suppliers'])} link(s) transferred; source archived)"
    ), event["id"]
