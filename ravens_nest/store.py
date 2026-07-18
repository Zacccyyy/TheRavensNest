"""Write path: every mutation builds an event, appends it to the current
month's log file, and applies it to the cache in one call."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Callable

from . import db, events, replay
from .locations import parse_location_id

log = logging.getLogger(__name__)

_write_listeners: list[Callable[[dict[str, Any]], None]] = []


def add_write_listener(listener: Callable[[dict[str, Any]], None]) -> None:
    """Register a callback invoked after every event is appended and applied
    (used by sync to schedule the debounced commit+push)."""
    if listener not in _write_listeners:
        _write_listeners.append(listener)


def remove_write_listener(listener: Callable[[dict[str, Any]], None]) -> None:
    try:
        _write_listeners.remove(listener)
    except ValueError:
        pass


@contextmanager
def _connection(conn: sqlite3.Connection | None):
    if conn is not None:
        yield conn
        return
    owned = db.connect()
    try:
        with owned:
            yield owned
    finally:
        owned.close()


def record_event(
    type: str, payload: dict[str, Any], conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Append a new event to the log and apply it to the cache."""
    event = events.new_event(type, payload)
    events.append_to_log(event)
    with _connection(conn) as c:
        replay.apply_event(c, event)
    for listener in list(_write_listeners):
        try:
            listener(event)
        except Exception:
            log.exception("event write listener failed")
    return event


def create_item(
    name: str,
    unit_type: str,
    *,
    qty_on_hand: Decimal | int | str = 0,
    description: str = "",
    part_number: str | None = None,
    min_qty: Decimal | int | str | None = None,
    location_id: str | None = None,
    last_paid_aud: Decimal | int | str | None = None,
    photo_hash: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if location_id is not None:
        parse_location_id(location_id)
    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "unit_type": unit_type,
        "qty_on_hand": db.qty_str(db.parse_qty(qty_on_hand)),
        "description": description,
        "part_number": part_number,
        "min_qty": None if min_qty is None else db.qty_str(db.parse_qty(min_qty)),
        "location_id": location_id,
        "last_paid_aud": None if last_paid_aud is None else db.qty_str(db.parse_qty(last_paid_aud)),
        "photo_hash": photo_hash,
    }
    return record_event("item.created", payload, conn)


def update_item(
    item_id: str, conn: sqlite3.Connection | None = None, **fields: Any
) -> dict[str, Any]:
    if "location_id" in fields and fields["location_id"] is not None:
        parse_location_id(fields["location_id"])
    return record_event("item.updated", {"id": item_id, **fields}, conn)


def adjust_qty(
    item_id: str,
    delta: Decimal | int | str,
    reason: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    payload = {
        "item_id": item_id,
        "delta": db.qty_str(db.parse_qty(delta)),
        "reason": reason,
    }
    return record_event("item.qty_adjusted", payload, conn)


def move_item(
    item_id: str, location_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    parse_location_id(location_id)
    return record_event("item.moved", {"item_id": item_id, "location_id": location_id}, conn)


def recount_item(
    item_id: str,
    counted_qty: Decimal | int | str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record a physical recount. The event carries the absolute count and,
    for the audit trail, the correction delta against the cache at write time."""
    counted = db.parse_qty(counted_qty)
    with _connection(conn) as c:
        row = c.execute("SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown item {item_id!r}")
        delta = counted - db.parse_qty(row["qty_on_hand"])
        payload = {
            "item_id": item_id,
            "qty": db.qty_str(counted),
            "delta": db.qty_str(delta),
        }
        return record_event("item.recounted", payload, c)


def create_location(
    location_id: str, description: str = "", conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    parse_location_id(location_id)
    return record_event(
        "location.created", {"id": location_id, "description": description}, conn
    )


def update_location(
    location_id: str, description: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    return record_event(
        "location.updated", {"id": location_id, "description": description}, conn
    )


def create_project(
    name: str, description: str = "", conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    payload = {"id": str(uuid.uuid4()), "name": name, "description": description}
    return record_event("project.created", payload, conn)


def import_bom(
    project_id: str, lines: list[dict[str, Any]], conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Record a BOM revision: the full parsed line set replaces any prior one."""
    return record_event("bom.imported", {"project_id": project_id, "lines": lines}, conn)


def match_bom_line(
    project_id: str,
    line_no: int,
    item_id: str,
    method: str,
    score: float | None = None,
    alias_text: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Attach an item to a BOM line. alias_text (the BOM's own text) is
    stored as an alias so the next revision matches automatically."""
    payload: dict[str, Any] = {
        "project_id": project_id,
        "line_no": line_no,
        "item_id": item_id,
        "method": method,
    }
    if score is not None:
        payload["score"] = score
    if alias_text:
        payload["alias_text"] = alias_text
    return record_event("bom.line_matched", payload, conn)


def create_reservation(
    project_id: str,
    item_id: str,
    qty: Decimal | int | str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    payload = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "item_id": item_id,
        "qty": db.qty_str(db.parse_qty(qty)),
    }
    return record_event("reservation.created", payload, conn)


def release_reservation(
    reservation_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    return record_event("reservation.released", {"id": reservation_id}, conn)


def execute_build(
    project_id: str,
    count: int,
    consumed: list[dict[str, str]],
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Consume stock for N builds. `consumed` carries the exact quantities
    (computed at write time) so replay stays deterministic even if the BOM
    changes later."""
    payload = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "count": count,
        "consumed": consumed,
    }
    return record_event("build.executed", payload, conn)


def reverse_build(
    project_id: str,
    count: int,
    returned: list[dict[str, str]],
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    payload = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "count": count,
        "returned": returned,
    }
    return record_event("build.reversed", payload, conn)


def create_supplier(
    name: str,
    free_shipping_threshold_aud: Decimal | int | str | None = None,
    typical_shipping_aud: Decimal | int | str | None = None,
    typical_lead_days: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """New suppliers start unrated — reliability is set manually by the
    user after an order arrives, never inferred."""
    payload = {
        "id": str(uuid.uuid4()),
        "name": name,
        "reliability": None,
        "free_shipping_threshold_aud": None
        if free_shipping_threshold_aud is None
        else db.qty_str(db.parse_qty(free_shipping_threshold_aud)),
        "typical_shipping_aud": None
        if typical_shipping_aud is None
        else db.qty_str(db.parse_qty(typical_shipping_aud)),
        "typical_lead_days": typical_lead_days,
    }
    return record_event("supplier.created", payload, conn)


def update_supplier(
    supplier_id: str, conn: sqlite3.Connection | None = None, **fields: Any
) -> dict[str, Any]:
    return record_event("supplier.updated", {"id": supplier_id, **fields}, conn)


def add_item_link(
    item_id: str,
    supplier_id: str,
    url: str,
    sku: str | None = None,
    pack_qty: Decimal | int | str = 1,
    last_price_aud: Decimal | int | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    payload = {
        "item_id": item_id,
        "supplier_id": supplier_id,
        "url": url,
        "sku": sku,
        "pack_qty": db.qty_str(db.parse_qty(pack_qty)),
        "last_price_aud": None
        if last_price_aud is None
        else db.qty_str(db.parse_qty(last_price_aud)),
    }
    return record_event("item.link_added", payload, conn)


def record_link_price(
    item_id: str,
    supplier_id: str,
    price_aud: Decimal | int | str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """A successful on-demand price check; the event's ts is the check time."""
    payload = {
        "item_id": item_id,
        "supplier_id": supplier_id,
        "price_aud": db.qty_str(db.parse_qty(price_aud)),
    }
    return record_event("item.link_price_checked", payload, conn)


def add_basket_item(
    item_id: str, qty: Decimal | int | str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    payload = {"item_id": item_id, "qty": db.qty_str(db.parse_qty(qty))}
    return record_event("basket.item_added", payload, conn)


def remove_basket_item(
    item_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    return record_event("basket.item_removed", {"item_id": item_id}, conn)


def archive_item(
    item_id: str, reason: str = "", conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Genuine retirement — the item, its photo, and its history all stay;
    it just leaves search, reorder, and BOM matching."""
    return record_event("item.archived", {"id": item_id, "reason": reason}, conn)


def unarchive_item(item_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    return record_event("item.unarchived", {"id": item_id}, conn)


def add_alias(
    item_id: str, alias_text: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    return record_event(
        "item.alias_added", {"item_id": item_id, "alias_text": alias_text}, conn
    )


def merge_items(payload: dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Payload is fully precomputed by merge.build_merge_payload so the
    applier (and any replay) never has to guess."""
    return record_event("item.merged", payload, conn)


def unmerge_items(payload: dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    return record_event("item.unmerged", payload, conn)
