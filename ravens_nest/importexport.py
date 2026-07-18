"""Bulk CSV import (dry-run first, always) and full export.

Import reuses the matching machinery: exact part number → exact name →
alias → fuzzy near-match (shown, decided by the user). Nothing is
written until the preview is confirmed; every write is a normal
item.created / item.updated / item.recounted / item.moved event — no
bypass. Resolutions store aliases so re-importing the same sheet
matches clean.

Export is the escape hatch: an items CSV in the same column layout, and
a full zip of the raw event log + photos. Your data is yours to take.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import re
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import config, db, merge, store, ui_command
from .locations import InvalidLocationId, parse_location_id
from .movement import _ensure_location

log = logging.getLogger(__name__)

router = APIRouter()

COLUMNS = (
    "name",
    "part_number",
    "description",
    "unit_type",
    "qty",
    "min_qty",
    "location",
    "last_paid_aud",
    "manufacturer",
    "package_type",
    "supplier_url",
)
UNIT_TYPES = ("each", "g", "mm", "mL")


def parse_items_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], ["CSV is empty"]
    headers = {(h or "").strip().lower(): h for h in reader.fieldnames}
    if "name" not in headers:
        return [], ["missing required column: name (all other columns are optional)"]

    rows, errors = [], []
    for row_no, raw in enumerate(reader, start=1):
        def cell(column: str) -> str:
            header = headers.get(column)
            return (raw.get(header) or "").strip() if header else ""

        row: dict[str, Any] = {"row_no": row_no, "error": None}
        row["name"] = cell("name")
        if not row["name"]:
            row["error"] = "name is required"
        row["part_number"] = cell("part_number") or None
        row["description"] = cell("description")
        row["unit_type"] = cell("unit_type") or "each"
        if row["unit_type"] not in UNIT_TYPES:
            row["error"] = f"unit_type {row['unit_type']!r} must be one of {', '.join(UNIT_TYPES)}"
        for qty_col in ("qty", "min_qty", "last_paid_aud"):
            value = cell(qty_col)
            if value:
                try:
                    row[qty_col] = db.qty_str(Decimal(value))
                except InvalidOperation:
                    row["error"] = f"{qty_col} {value!r} is not a number"
                    row[qty_col] = None
            else:
                row[qty_col] = None
        location = cell("location")
        if location:
            try:
                parse_location_id(location)
                row["location"] = location
            except InvalidLocationId:
                row["error"] = (
                    f"location {location!r} is not valid — the format is Unit-Shelf-Bin"
                    f"[section], e.g. A-2-3b"
                )
                row["location"] = None
        else:
            row["location"] = None
        row["manufacturer"] = cell("manufacturer") or None
        row["package_type"] = cell("package_type") or None
        row["supplier_url"] = cell("supplier_url") or None
        if row["error"]:
            errors.append(f"row {row_no}: {row['error']}")
        rows.append(row)
    if not rows:
        errors.append("CSV has no data rows")
    return rows, errors


def classify_rows(conn, rows: list[dict[str, Any]]) -> None:
    """Annotate each row: status new|matched|ambiguous|error (+candidates)."""
    items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE archived = 0")]
    aliases: dict[str, list[str]] = {}
    for r in conn.execute("SELECT alias_text, item_id FROM aliases"):
        aliases.setdefault(r["item_id"], []).append(r["alias_text"].lower())

    for row in rows:
        if row["error"]:
            row["status"] = "error"
            continue
        part = (row["part_number"] or "").lower()
        name = row["name"].lower()
        exact = [
            i for i in items
            if (part and (i["part_number"] or "").lower() == part)
            or i["name"].lower() == name
            or part in aliases.get(i["id"], [])
            or name in aliases.get(i["id"], [])
        ]
        if len(exact) == 1:
            row["status"] = "matched"
            row["match"] = {"id": exact[0]["id"], "name": exact[0]["name"], "score": 100}
            row["candidates"] = []
            continue
        near = merge.near_matches(conn, row["name"], row["part_number"])
        if near or len(exact) > 1:
            row["status"] = "ambiguous"
            seen = {c["id"] for c in near}
            row["candidates"] = near + [
                {**i, "score": 100} for i in exact if i["id"] not in seen
            ]
        else:
            row["status"] = "new"
            row["candidates"] = []


def _description(row: dict[str, Any]) -> str:
    parts = [row["description"]]
    if row["manufacturer"]:
        parts.append(f"Manufacturer: {row['manufacturer']}")
    if row["package_type"]:
        parts.append(f"Package: {row['package_type']}")
    return "; ".join(p for p in parts if p)


def _supplier_for_url(conn, url: str) -> str | None:
    """Find (or create) a supplier from the link's domain."""
    host = urlparse(url).netloc.lower()
    if not host:
        return None
    label = re.sub(r"^www\.", "", host).split(".")[0].replace("-", " ")
    for row in conn.execute("SELECT id, name FROM suppliers"):
        if label.replace(" ", "") in row["name"].lower().replace(" ", "").replace("-", ""):
            return row["id"]
    event = store.create_supplier(label.title())
    return event["payload"]["id"]


def apply_row(row: dict[str, Any], decision: str) -> str:
    """decision: 'new' | 'skip' | an existing item id. Returns a summary."""
    if decision == "skip":
        return "skipped"
    if row["location"]:
        _ensure_location(row["location"])
    if decision == "new":
        event = store.create_item(
            row["name"],
            row["unit_type"],
            qty_on_hand=row["qty"] or "0",
            description=_description(row),
            part_number=row["part_number"],
            min_qty=row["min_qty"],
            location_id=row["location"],
            last_paid_aud=row["last_paid_aud"],
        )
        item_id = event["payload"]["id"]
        outcome = "created"
    else:
        item_id = decision
        # Existence check BEFORE any write — a tampered/vanished target
        # must not receive an orphan item.updated event (atomic
        # remediation item 2 sweep).
        conn = db.connect()
        try:
            current = conn.execute(
                "SELECT qty_on_hand, location_id FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        finally:
            conn.close()
        if current is None:
            return "target item vanished — skipped"
        updates: dict[str, Any] = {}
        if row["description"]:
            updates["description"] = _description(row)
        if row["min_qty"] is not None:
            updates["min_qty"] = row["min_qty"]
        if row["last_paid_aud"] is not None:
            updates["last_paid_aud"] = row["last_paid_aud"]
        if row["part_number"]:
            updates["part_number"] = row["part_number"]
        if updates:
            store.update_item(item_id, **updates)
        if row["qty"] is not None and db.parse_qty(row["qty"]) != db.parse_qty(current["qty_on_hand"]):
            delta = db.qty_str(db.parse_qty(row["qty"]) - db.parse_qty(current["qty_on_hand"]))
            store.record_event(
                "item.recounted",
                {"item_id": item_id, "qty": row["qty"], "delta": delta, "reason": "CSV import"},
            )
        if row["location"] and row["location"] != current["location_id"]:
            store.move_item(item_id, row["location"])
        # Teach the matcher: the sheet's texts become aliases so the same
        # CSV re-imports clean.
        for alias in (row["part_number"], row["name"]):
            if alias:
                store.add_alias(item_id, alias)
        outcome = "updated"
    if row["supplier_url"]:
        from . import pricing

        try:
            pricing.validate_link_url(row["supplier_url"], resolve=False)
        except ValueError as exc:
            log.warning("skipping supplier_url on row %s: %s", row["row_no"], exc)
            return outcome
        conn = db.connect()
        try:
            supplier_id = _supplier_for_url(conn, row["supplier_url"])
            existing = supplier_id and conn.execute(
                "SELECT 1 FROM item_links WHERE item_id = ? AND supplier_id = ?",
                (item_id, supplier_id),
            ).fetchone()
        finally:
            conn.close()
        if supplier_id and not existing:
            store.add_item_link(item_id, supplier_id, row["supplier_url"])
    return outcome


# ---------------------------------------------------------------- routes


@router.get("/import", response_class=HTMLResponse)
def import_form() -> str:
    return ui_command.import_page()


@router.post("/import/preview", response_class=HTMLResponse)
async def import_preview(items_csv: UploadFile = File(...)) -> str:
    """Dry run: N new / N matched / N ambiguous / N errors, with row
    numbers and reasons. Nothing is written here."""
    text = (await items_csv.read()).decode("utf-8-sig", errors="replace")
    rows, errors = parse_items_csv(text)
    if not rows:
        return ui_command.import_page(error="; ".join(errors))
    conn = db.connect()
    try:
        classify_rows(conn, rows)
    finally:
        conn.close()
    return ui_command.import_preview_page(rows, encode_csv(text))


@router.post("/import/confirm", response_class=HTMLResponse)
async def import_confirm(request: Request) -> str:
    """Apply the previewed import with the user's per-row decisions.
    Emits only normal events — no bypass of the log."""
    form = await request.form()
    blob = form.get("csv_b64")
    if not blob:
        return ui_command.import_page(error="preview expired — upload the CSV again")
    rows, _ = parse_items_csv(decode_csv(str(blob)))
    conn = db.connect()
    try:
        classify_rows(conn, rows)
    finally:
        conn.close()
    outcomes = {"created": 0, "updated": 0, "skipped": 0}
    for row in rows:
        if row["status"] == "error":
            outcomes["skipped"] += 1
            continue
        default = "new" if row["status"] == "new" else (
            row["match"]["id"] if row["status"] == "matched" else "skip"
        )
        decision = str(form.get(f"row_{row['row_no']}", default))
        outcome = apply_row(row, decision)
        outcomes[outcome if outcome in outcomes else "skipped"] += 1
    return ui_command.import_done(outcomes)


def export_items_csv(conn, include_zero: bool, include_archived: bool) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    first_links: dict[str, str] = {}
    for row in conn.execute("SELECT item_id, url FROM item_links ORDER BY supplier_id"):
        first_links.setdefault(row["item_id"], row["url"])
    for item in conn.execute("SELECT * FROM items ORDER BY name"):
        if not include_archived and item["archived"]:
            continue
        if not include_zero and db.parse_qty(item["qty_on_hand"]) == 0 and not item["archived"]:
            continue
        writer.writerow(
            [
                item["name"],
                item["part_number"] or "",
                item["description"] or "",
                item["unit_type"],
                item["qty_on_hand"],
                item["min_qty"] or "",
                item["location_id"] or "",
                item["last_paid_aud"] or "",
                "",  # manufacturer lives inside description
                "",  # package_type lives inside description
                first_links.get(item["id"], ""),
            ]
        )
    return out.getvalue()


@router.get("/export/items.csv")
def export_items(include_zero: int = 1, include_archived: int = 0) -> Response:
    conn = db.connect()
    try:
        body = export_items_csv(conn, bool(include_zero), bool(include_archived))
    finally:
        conn.close()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ravens-nest-items.csv"},
    )


@router.get("/export/full.zip")
def export_full() -> StreamingResponse:
    """Complete data export — event log + photos. Everything needed to
    leave, or to stand the same inventory up elsewhere."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        events_dir = config.events_dir()
        if events_dir.is_dir():
            for path in sorted(events_dir.glob("*.jsonl")):
                archive.write(path, f"events/{path.name}")
        assets_dir = config.assets_dir()
        if assets_dir.is_dir():
            for path in sorted(assets_dir.glob("*.jpg")):
                archive.write(path, f"assets/{path.name}")
        archive.writestr(
            "README.txt",
            "The Raven's Nest full data export.\n\n"
            "events/  append-only event log, one JSON object per line:\n"
            '         {"id", "ts", "actor", "type", "payload"}\n'
            "assets/  item photos, content-addressed as <sha256>.jpg\n\n"
            "To restore: place events/ and assets/ inside a data directory,\n"
            "point RAVENS_NEST_DATA at it, and run:\n"
            "    uv run python -m ravens_nest.replay\n",
        )
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=ravens-nest-export.zip"},
    )


def encode_csv(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_csv(blob: str) -> str:
    return base64.b64decode(blob.encode("ascii")).decode("utf-8")
