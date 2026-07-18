"""Apply events to the SQLite cache, and rebuild the cache from scratch.

`python -m ravens_nest.replay` deletes cache.db and replays every event
in (ts, id) order. apply_event() is idempotent: each event ID is applied
at most once, guarded by the events_applied table.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from . import config, db, events
from .locations import parse_location_id

log = logging.getLogger(__name__)

# item.created / item.updated payload fields that map straight onto columns
_ITEM_FIELDS = (
    "name",
    "description",
    "part_number",
    "unit_type",
    "min_qty",
    "location_id",
    "last_paid_aud",
    "photo_hash",
)


def apply_event(conn: sqlite3.Connection, event: dict[str, Any]) -> bool:
    """Apply one event to the cache. Returns False if it was already applied."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO events_applied (event_id) VALUES (?)", (event["id"],)
    )
    if cur.rowcount == 0:
        return False

    handler = _HANDLERS.get(event["type"])
    if handler is None:
        log.warning("skipping event %s: unknown type %r", event["id"], event["type"])
        return True
    handler(conn, event["ts"], event["payload"])
    return True


def _item_exists(conn: sqlite3.Connection, item_id: str) -> bool:
    if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        return True
    log.warning("skipping event for unknown item %s", item_id)
    return False


def _apply_item_created(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    qty = db.qty_str(db.parse_qty(p.get("qty_on_hand", "0")))
    conn.execute(
        """
        INSERT OR REPLACE INTO items
            (id, name, description, part_number, unit_type, qty_on_hand,
             min_qty, location_id, last_paid_aud, photo_hash, created_ts, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            p["id"],
            p["name"],
            p.get("description", ""),
            p.get("part_number"),
            p["unit_type"],
            qty,
            p.get("min_qty"),
            p.get("location_id"),
            p.get("last_paid_aud"),
            p.get("photo_hash"),
            ts,
            ts,
        ),
    )


def _apply_item_updated(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    if not _item_exists(conn, p["id"]):
        return
    fields = [f for f in _ITEM_FIELDS if f in p]
    if not fields:
        return
    assignments = ", ".join(f"{f} = ?" for f in fields)
    values = [p[f] for f in fields]
    conn.execute(
        f"UPDATE items SET {assignments}, updated_ts = ? WHERE id = ?",
        (*values, ts, p["id"]),
    )


def _apply_item_qty_adjusted(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    if not _item_exists(conn, p["item_id"]):
        return
    row = conn.execute(
        "SELECT qty_on_hand FROM items WHERE id = ?", (p["item_id"],)
    ).fetchone()
    new_qty = db.parse_qty(row["qty_on_hand"]) + db.parse_qty(p["delta"])
    conn.execute(
        "UPDATE items SET qty_on_hand = ?, updated_ts = ? WHERE id = ?",
        (db.qty_str(new_qty), ts, p["item_id"]),
    )


def _apply_item_moved(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    if not _item_exists(conn, p["item_id"]):
        return
    conn.execute(
        "UPDATE items SET location_id = ?, updated_ts = ? WHERE id = ?",
        (p["location_id"], ts, p["item_id"]),
    )


def _apply_item_recounted(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    # The recount's absolute value wins; the delta in the payload is an
    # audit record computed at write time, not something we re-apply.
    if not _item_exists(conn, p["item_id"]):
        return
    conn.execute(
        "UPDATE items SET qty_on_hand = ?, updated_ts = ? WHERE id = ?",
        (db.qty_str(db.parse_qty(p["qty"])), ts, p["item_id"]),
    )


def _apply_location_created(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    loc = parse_location_id(p["id"])
    conn.execute(
        """
        INSERT OR REPLACE INTO locations (id, unit, shelf, bin, section, description)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (p["id"], loc.unit, loc.shelf, loc.bin, loc.section, p.get("description", "")),
    )


def _apply_location_updated(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    if not conn.execute("SELECT 1 FROM locations WHERE id = ?", (p["id"],)).fetchone():
        log.warning("skipping event for unknown location %s", p["id"])
        return
    conn.execute(
        "UPDATE locations SET description = ? WHERE id = ?",
        (p.get("description", ""), p["id"]),
    )


def _apply_project_created(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO projects (id, name, description, created_ts) VALUES (?, ?, ?, ?)",
        (p["id"], p["name"], p.get("description", ""), ts),
    )


def _apply_bom_imported(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    # A new revision replaces the project's whole line set.
    conn.execute("DELETE FROM bom_lines WHERE project_id = ?", (p["project_id"],))
    for line in p["lines"]:
        conn.execute(
            """
            INSERT OR REPLACE INTO bom_lines
                (project_id, line_no, part_number, description, quantity, unit,
                 reference_designators, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p["project_id"],
                line["line_no"],
                line["part_number"],
                line.get("description", ""),
                line["quantity"],
                line["unit"],
                line.get("reference_designators"),
                line.get("notes"),
            ),
        )


def _apply_bom_line_matched(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        """UPDATE bom_lines SET item_id = ?, match_method = ?, match_score = ?
           WHERE project_id = ? AND line_no = ?""",
        (p["item_id"], p.get("method"), p.get("score"), p["project_id"], p["line_no"]),
    )
    if p.get("alias_text"):
        conn.execute(
            "INSERT OR IGNORE INTO aliases (alias_text, item_id) VALUES (?, ?)",
            (p["alias_text"], p["item_id"]),
        )


def _apply_reservation_created(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO reservations
               (id, project_id, item_id, qty, status, created_ts, released_ts)
           VALUES (?, ?, ?, ?, 'active', ?, NULL)""",
        (p["id"], p["project_id"], p["item_id"], db.qty_str(db.parse_qty(p["qty"])), ts),
    )


def _apply_reservation_released(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    cur = conn.execute(
        "UPDATE reservations SET status = 'released', released_ts = ? WHERE id = ?",
        (ts, p["id"]),
    )
    if cur.rowcount == 0:
        log.warning("release for unknown reservation %s", p["id"])


def _shift_item_qty(conn: sqlite3.Connection, ts: str, item_id: str, delta: Any) -> None:
    row = conn.execute(
        "SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        log.warning("skipping build stock shift for unknown item %s", item_id)
        return
    new_qty = db.parse_qty(row["qty_on_hand"]) + db.parse_qty(delta)
    conn.execute(
        "UPDATE items SET qty_on_hand = ?, updated_ts = ? WHERE id = ?",
        (db.qty_str(new_qty), ts, item_id),
    )


def _apply_build_executed(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO builds (id, project_id, kind, count, ts) VALUES (?, ?, 'build', ?, ?)",
        (p["id"], p["project_id"], p["count"], ts),
    )
    for entry in p.get("consumed", []):
        _shift_item_qty(conn, ts, entry["item_id"], -db.parse_qty(entry["qty"]))


def _apply_build_reversed(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO builds (id, project_id, kind, count, ts) VALUES (?, ?, 'reversal', ?, ?)",
        (p["id"], p["project_id"], p["count"], ts),
    )
    for entry in p.get("returned", []):
        _shift_item_qty(conn, ts, entry["item_id"], db.parse_qty(entry["qty"]))


_SUPPLIER_FIELDS = (
    "name",
    "reliability",
    "free_shipping_threshold_aud",
    "typical_shipping_aud",
    "typical_lead_days",
)


def _apply_supplier_created(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO suppliers
               (id, name, reliability, free_shipping_threshold_aud,
                typical_shipping_aud, typical_lead_days)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            p["id"],
            p["name"],
            p.get("reliability"),
            p.get("free_shipping_threshold_aud"),
            p.get("typical_shipping_aud"),
            p.get("typical_lead_days"),
        ),
    )


def _apply_supplier_updated(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    fields = [f for f in _SUPPLIER_FIELDS if f in p]
    if not fields:
        return
    assignments = ", ".join(f"{f} = ?" for f in fields)
    cur = conn.execute(
        f"UPDATE suppliers SET {assignments} WHERE id = ?",
        (*[p[f] for f in fields], p["id"]),
    )
    if cur.rowcount == 0:
        log.warning("update for unknown supplier %s", p["id"])


def _apply_item_link_added(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO item_links
               (item_id, supplier_id, url, sku, pack_qty, last_price_aud, last_checked_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            p["item_id"],
            p["supplier_id"],
            p["url"],
            p.get("sku"),
            p.get("pack_qty", "1"),
            p.get("last_price_aud"),
            ts if p.get("last_price_aud") is not None else None,
        ),
    )


def _apply_item_link_price_checked(
    conn: sqlite3.Connection, ts: str, p: dict[str, Any]
) -> None:
    cur = conn.execute(
        """UPDATE item_links SET last_price_aud = ?, last_checked_ts = ?
           WHERE item_id = ? AND supplier_id = ?""",
        (db.qty_str(db.parse_qty(p["price_aud"])), ts, p["item_id"], p["supplier_id"]),
    )
    if cur.rowcount == 0:
        log.warning(
            "price check for unknown link %s/%s", p["item_id"], p["supplier_id"]
        )


def _apply_basket_item_added(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO basket_items (item_id, qty, added_ts) VALUES (?, ?, ?)",
        (p["item_id"], db.qty_str(db.parse_qty(p["qty"])), ts),
    )


def _apply_basket_item_removed(conn: sqlite3.Connection, ts: str, p: dict[str, Any]) -> None:
    conn.execute("DELETE FROM basket_items WHERE item_id = ?", (p["item_id"],))


_HANDLERS = {
    "item.created": _apply_item_created,
    "item.updated": _apply_item_updated,
    "item.qty_adjusted": _apply_item_qty_adjusted,
    "item.moved": _apply_item_moved,
    "item.recounted": _apply_item_recounted,
    "location.created": _apply_location_created,
    "location.updated": _apply_location_updated,
    "project.created": _apply_project_created,
    "bom.imported": _apply_bom_imported,
    "bom.line_matched": _apply_bom_line_matched,
    "reservation.created": _apply_reservation_created,
    "reservation.released": _apply_reservation_released,
    "build.executed": _apply_build_executed,
    "build.reversed": _apply_build_reversed,
    "supplier.created": _apply_supplier_created,
    "supplier.updated": _apply_supplier_updated,
    "item.link_added": _apply_item_link_added,
    "item.link_price_checked": _apply_item_link_price_checked,
    "basket.item_added": _apply_basket_item_added,
    "basket.item_removed": _apply_basket_item_removed,
}


def rebuild() -> int:
    """Delete cache.db and rebuild it from the event log. Returns event count."""
    cache = config.cache_path()
    cache.unlink(missing_ok=True)
    all_events = events.read_all_events()
    conn = db.connect(cache)
    try:
        with conn:
            for event in all_events:
                apply_event(conn, event)
    finally:
        conn.close()
    return len(all_events)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    count = rebuild()
    print(f"Rebuilt {config.cache_path()} from {count} events.")


if __name__ == "__main__":
    main()
