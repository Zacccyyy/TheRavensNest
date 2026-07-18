"""Location barcodes and movement: printable QR label sheets, the
units → shelves → bins tree view, and the scan-driven move console.

The move console is built for bulk work: scan a bin label once to set the
target, then rattle through items — each exact scan (item ID, part number,
or alias) emits item.moved immediately. Name searches show a pick list
instead of auto-moving, so a loose text match never moves the wrong thing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, store, ui
from .locations import InvalidLocationId, is_valid_location_id, parse_location_id

router = APIRouter()

_ITEM_COLUMNS = "items.id, items.name, items.qty_on_hand, items.unit_type, items.location_id, items.part_number"


def _ensure_location(location_id: str) -> bool:
    """Validate the ID and create a location record for it if missing —
    scanning a freshly printed label should never dead-end. Returns True
    if a new record was created."""
    parse_location_id(location_id)
    conn = db.connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM locations WHERE id = ?", (location_id,)
        ).fetchone()
    finally:
        conn.close()
    if exists:
        return False
    store.create_location(location_id)
    return True


def _location_description(location_id: str) -> str:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT description FROM locations WHERE id = ?", (location_id,)
        ).fetchone()
        return row["description"] if row else ""
    finally:
        conn.close()


def _exact_item_matches(code: str) -> list[dict[str, Any]]:
    """Matches by item ID, part number, or alias — the identifiers a
    barcode scan produces. These are safe to auto-move on."""
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT {_ITEM_COLUMNS}
                FROM items LEFT JOIN aliases ON aliases.item_id = items.id
                WHERE items.id = :q
                   OR lower(coalesce(items.part_number, '')) = lower(:q)
                   OR lower(coalesce(aliases.alias_text, '')) = lower(:q)""",
            {"q": code},
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _search_items(code: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""SELECT DISTINCT {_ITEM_COLUMNS}
                FROM items LEFT JOIN aliases ON aliases.item_id = items.id
                WHERE items.name LIKE '%' || :q || '%'
                   OR items.id = :q
                   OR lower(coalesce(items.part_number, '')) = lower(:q)
                   OR lower(coalesce(aliases.alias_text, '')) = lower(:q)
                ORDER BY items.name LIMIT 20""",
            {"q": code},
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------ labels


def _qr_svg(data: str) -> str:
    image = qrcode.make(
        data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2
    )
    return image.to_string(encoding="unicode")


@router.get("/labels", response_class=HTMLResponse)
def labels_sheet(unit: str | None = None) -> str:
    conn = db.connect()
    try:
        if unit:
            rows = conn.execute(
                "SELECT * FROM locations WHERE unit = ? ORDER BY unit, shelf, bin, section",
                (unit.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM locations ORDER BY unit, shelf, bin, section"
            ).fetchall()
        units = [r["unit"] for r in conn.execute("SELECT DISTINCT unit FROM locations ORDER BY unit")]
    finally:
        conn.close()
    labels = [(dict(row), _qr_svg(row["id"])) for row in rows]
    return ui.labels_page(labels, unit.upper() if unit else None, units)


@router.post("/labels/generate")
def generate_labels(
    unit: str = Form(...),
    shelves: int = Form(...),
    bins: int = Form(...),
    sections: str = Form(""),
) -> RedirectResponse:
    """Batch-create a unit's worth of locations (skipping ones that exist),
    then land on the printable sheet for that unit."""
    unit = unit.strip().upper()
    if not re.fullmatch(r"[A-Z]", unit):
        raise HTTPException(status_code=400, detail="unit must be a single letter A-Z")
    if not (1 <= shelves <= 30 and 1 <= bins <= 30):
        raise HTTPException(status_code=400, detail="shelves and bins must be 1-30")
    section_list = [s for s in re.split(r"[,\s]+", sections.strip().lower()) if s]
    for section in section_list:
        if not re.fullmatch(r"[a-z]", section):
            raise HTTPException(
                status_code=400, detail=f"section {section!r} must be a single lowercase letter"
            )
    for shelf in range(1, shelves + 1):
        for bin_no in range(1, bins + 1):
            for section in section_list or [""]:
                _ensure_location(f"{unit}-{shelf}-{bin_no}{section}")
    return RedirectResponse(url=f"/labels?unit={unit}", status_code=303)


# -------------------------------------------------------------------- tree


@router.get("/locations", response_class=HTMLResponse)
def locations_view() -> str:
    conn = db.connect()
    try:
        locations = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM locations ORDER BY unit, shelf, bin, section"
            )
        ]
        items = [
            dict(row)
            for row in conn.execute(
                "SELECT id, name, qty_on_hand, unit_type, location_id FROM items ORDER BY name"
            )
        ]
    finally:
        conn.close()

    by_location: dict[str, list] = defaultdict(list)
    unassigned = []
    for item in items:
        if item["location_id"]:
            by_location[item["location_id"]].append(item)
        else:
            unassigned.append(item)

    tree: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for loc in locations:
        tree[loc["unit"]][loc["shelf"]].append(
            {**loc, "items": by_location.get(loc["id"], [])}
        )

    known = {loc["id"] for loc in locations}
    unregistered = {
        loc_id: contents
        for loc_id, contents in by_location.items()
        if loc_id not in known
    }
    empty_bins = [loc["id"] for loc in locations if not by_location.get(loc["id"])]
    return ui.locations_page(tree, unassigned, unregistered, empty_bins)


@router.post("/locations")
def add_location(location_id: str = Form(...), description: str = Form("")) -> RedirectResponse:
    location_id = location_id.strip()
    try:
        parse_location_id(location_id)
    except InvalidLocationId as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if _ensure_location(location_id):
        if description.strip():
            store.update_location(location_id, description.strip())
    elif description.strip():
        store.update_location(location_id, description.strip())
    return RedirectResponse(url="/locations", status_code=303)


# -------------------------------------------------------------------- move


@router.get("/move", response_class=HTMLResponse)
def move_console() -> str:
    return ui.move_page()


@router.post("/move/scan", response_class=HTMLResponse)
def move_scan(code: str = Form(""), location_id: str = Form("")) -> str:
    """One scan (camera, USB scanner, or typed + Enter). Location codes set
    the target bin; exact item codes move immediately when a target is set;
    anything else becomes a search."""
    code = code.strip()
    location_id = location_id.strip()
    if not code:
        return ""

    if is_valid_location_id(code):
        created = _ensure_location(code)
        note = "Target location set to " + code
        if created:
            note += " (new location record created)"
        return ui.move_loc_fragment(code, _location_description(code), oob=True) + ui.move_note(note)

    exact = _exact_item_matches(code)
    if location_id and len(exact) == 1:
        item = exact[0]
        try:
            _ensure_location(location_id)
        except InvalidLocationId as exc:
            return ui.move_note(str(exc), error=True)
        store.move_item(item["id"], location_id)
        return ui.move_log_entry(item["name"], location_id) + ui.move_note(
            f"Moved {item['name']} → {location_id}"
        )

    matches = _search_items(code)
    if not matches:
        return ui.move_note(f"No item matches “{code}”.", error=True)
    if not location_id:
        return ui.move_note(
            "Scan a bin label first to set the target location.", error=True
        ) + ui.move_matches(matches)
    return ui.move_matches(matches)


@router.post("/move", response_class=HTMLResponse)
def move_selected(
    location_id: str = Form(""), item_ids: list[str] | None = Form(None)
) -> str:
    """Bulk move: the selected items all get the one target location."""
    location_id = location_id.strip()
    item_ids = item_ids or []
    if not location_id:
        return ui.move_note("Scan a bin label first to set the target location.", error=True)
    if not item_ids:
        return ui.move_note("No items selected.", error=True)
    try:
        _ensure_location(location_id)
    except InvalidLocationId as exc:
        return ui.move_note(str(exc), error=True)

    conn = db.connect()
    try:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"SELECT id, name FROM items WHERE id IN ({placeholders})", item_ids
        ).fetchall()
    finally:
        conn.close()

    logs = []
    for row in rows:
        store.move_item(row["id"], location_id)
        logs.append(ui.move_log_entry(row["name"], location_id))
    return "".join(logs) + ui.move_note(f"Moved {len(rows)} item(s) → {location_id}")
