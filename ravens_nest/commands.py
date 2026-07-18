"""The command bar — the primary interface. One input, natural syntax:

    3mm heat shrink        search
    move to A-2-3b         move mode for scanned/selected items
    build RPSRobot x2      build confirmation
    need 20 more m3 screw  add to reorder basket
    recount A-2-3b         bin recount flow
    A-2-3b                 what's in this bin
    low                    everything under min_qty
    price basket           basket pricing + candidates

Typing renders read-only results instantly (search, bin, low) and shows a
"press Enter to …" hint for actions; Enter executes. Ambiguous commands
ask rather than guess: a fuzzy project or item match below certainty
renders a pick list, never a silent action.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse
from rapidfuzz import fuzz

from . import bom, db, events, ingest, sourcing, store, ui_command
from .locations import is_valid_location_id

router = APIRouter()

SEARCH_CUTOFF = 55
CERTAIN_SCORE = 90

_RECOUNT_RE = re.compile(r"^recount\s+(\S+)$", re.IGNORECASE)
_MOVE_RE = re.compile(r"^move(?:\s+to)?\s+(\S+)$", re.IGNORECASE)
_BUILD_RE = re.compile(r"^build\s+(.+?)(?:\s*[x×]\s*(\d+))?$", re.IGNORECASE)
_NEED_RE = re.compile(r"^need\s+(\d+(?:\.\d+)?)\s+(?:more\s+)?(.+)$", re.IGNORECASE)


def parse(text: str) -> dict[str, Any]:
    q = text.strip()
    if not q:
        return {"kind": "empty"}
    lower = q.lower()
    if lower in ("price basket", "price the basket"):
        return {"kind": "price_basket"}
    if lower == "low":
        return {"kind": "low"}
    if is_valid_location_id(q):
        return {"kind": "bin", "location_id": q}
    if match := _RECOUNT_RE.match(q):
        loc = match.group(1)
        if is_valid_location_id(loc):
            return {"kind": "recount", "location_id": loc}
        return {"kind": "invalid", "message": f"{loc!r} is not a valid location ID"}
    if match := _MOVE_RE.match(q):
        loc = match.group(1)
        if is_valid_location_id(loc):
            return {"kind": "move", "location_id": loc}
        return {"kind": "invalid", "message": f"{loc!r} is not a valid location ID"}
    if match := _BUILD_RE.match(q):
        return {
            "kind": "build",
            "query": match.group(1).strip(),
            "count": int(match.group(2) or 1),
        }
    if match := _NEED_RE.match(q):
        return {"kind": "need", "qty": match.group(1), "query": match.group(2).strip()}
    return {"kind": "search", "query": q}


# ------------------------------------------------------------------ search


def search_items(conn, query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Fuzzy search across name, part_number, description, and aliases,
    with stock summary attached."""
    aliases: dict[str, list[str]] = {}
    for row in conn.execute("SELECT alias_text, item_id FROM aliases"):
        aliases.setdefault(row["item_id"], []).append(row["alias_text"])
    reserved = bom.reserved_by_item(conn)

    q = query.strip().lower()
    scored = []
    for item in conn.execute("SELECT * FROM items"):
        fields = [item["name"], item["part_number"] or "", item["description"] or ""]
        fields += aliases.get(item["id"], [])
        best = 0.0
        for field in fields:
            text = field.strip().lower()
            if not text:
                continue
            if q == text:
                score = 100.0
            elif q in text:
                score = 92.0
            else:
                score = fuzz.WRatio(q, text)
            best = max(best, score)
        if best >= SEARCH_CUTOFF:
            scored.append((best, dict(item)))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"].lower()))

    results = []
    for score, item in scored[:limit]:
        on_hand = db.parse_qty(item["qty_on_hand"])
        held = reserved.get(item["id"], Decimal(0))
        results.append(
            {
                **item,
                "score": round(score),
                "free": db.qty_str(on_hand - held),
                "reserved": db.qty_str(held),
                "summary": _stock_summary(item, on_hand, held),
            }
        )
    return results


def _stock_summary(item: dict[str, Any], on_hand: Decimal, reserved: Decimal) -> str:
    """The spec's format: "A-2-3b, 12 left, 4 free (8 reserved)"."""
    location = item["location_id"] or "no location"
    text = f"{location}, {db.qty_str(on_hand)} left"
    if reserved > 0:
        text += f", {db.qty_str(on_hand - reserved)} free ({db.qty_str(reserved)} reserved)"
    return text


def _bin_contents(conn, location_id: str) -> tuple[dict | None, list[dict[str, Any]]]:
    location = conn.execute(
        "SELECT * FROM locations WHERE id = ?", (location_id,)
    ).fetchone()
    reserved = bom.reserved_by_item(conn)
    items = []
    for row in conn.execute(
        "SELECT * FROM items WHERE location_id = ? ORDER BY name", (location_id,)
    ):
        held = reserved.get(row["id"], Decimal(0))
        on_hand = db.parse_qty(row["qty_on_hand"])
        items.append(
            {**dict(row), "free": db.qty_str(on_hand - held), "reserved": db.qty_str(held)}
        )
    return (dict(location) if location else None), items


def _low_items(conn) -> list[dict[str, Any]]:
    reserved = bom.reserved_by_item(conn)
    low = []
    for row in conn.execute("SELECT * FROM items WHERE min_qty IS NOT NULL ORDER BY name"):
        on_hand = db.parse_qty(row["qty_on_hand"])
        free = on_hand - reserved.get(row["id"], Decimal(0))
        min_qty = db.parse_qty(row["min_qty"])
        if free < min_qty:
            low.append(
                {**dict(row), "free": db.qty_str(free), "min": db.qty_str(min_qty)}
            )
    return low


def _match_project(conn, query: str) -> tuple[dict | None, list[dict[str, Any]]]:
    """Exact name first; otherwise fuzzy. Returns (certain_match, candidates)."""
    projects = [dict(r) for r in conn.execute("SELECT * FROM projects")]
    q = query.strip().lower()
    exact = [p for p in projects if p["name"].strip().lower() == q]
    if len(exact) == 1:
        return exact[0], []
    scored = sorted(
        ((fuzz.WRatio(q, p["name"].lower()), p) for p in projects),
        key=lambda pair: -pair[0],
    )
    candidates = [{**p, "score": round(s)} for s, p in scored if s >= SEARCH_CUTOFF][:5]
    if (
        len(candidates) == 1
        and candidates[0]["score"] >= CERTAIN_SCORE
    ) or (
        len(candidates) > 1
        and candidates[0]["score"] >= CERTAIN_SCORE
        and candidates[1]["score"] < CERTAIN_SCORE
    ):
        return candidates[0], candidates
    return None, candidates


# ------------------------------------------------------------------ routes


def _render(intent: dict[str, Any], live: bool) -> str:
    """Render one parsed command. live=True is search-as-you-type: read-only
    intents render fully, actions render only a what-Enter-does hint."""
    kind = intent["kind"]
    if kind == "empty":
        return ""
    if kind == "invalid":
        return ui_command.note(intent["message"], error=True)

    if kind == "search":
        conn = db.connect()
        try:
            results = search_items(conn, intent["query"])
        finally:
            conn.close()
        return ui_command.search_results(results, intent["query"])

    if kind == "bin":
        conn = db.connect()
        try:
            location, items = _bin_contents(conn, intent["location_id"])
        finally:
            conn.close()
        return ui_command.bin_view(intent["location_id"], location, items)

    if kind == "low":
        conn = db.connect()
        try:
            rows = _low_items(conn)
        finally:
            conn.close()
        return ui_command.low_view(rows)

    if live:
        hints = {
            "move": f"move mode → {intent.get('location_id', '')}",
            "recount": f"recount bin {intent.get('location_id', '')}",
            "build": f"build “{intent.get('query', '')}” ×{intent.get('count', 1)}",
            "need": f"add {intent.get('qty')} × “{intent.get('query')}” to the reorder basket",
            "price_basket": "price the basket now (fetches supplier pages)",
        }
        return ui_command.enter_hint(hints.get(kind, kind))

    if kind == "move":
        from .movement import _ensure_location, _location_description

        _ensure_location(intent["location_id"])
        return ui_command.move_panel(
            intent["location_id"], _location_description(intent["location_id"])
        )

    if kind == "recount":
        conn = db.connect()
        try:
            location, items = _bin_contents(conn, intent["location_id"])
        finally:
            conn.close()
        if not items:
            return ui_command.note(
                f"Nothing recorded in {intent['location_id']} to recount.", error=True
            )
        return ui_command.recount_form(intent["location_id"], items)

    if kind == "build":
        conn = db.connect()
        try:
            project, candidates = _match_project(conn, intent["query"])
            if project is None:
                return ui_command.build_ask(intent["query"], candidates, intent["count"])
            needs, shortages = bom.build_shortages(conn, project["id"], intent["count"])
            need_rows = []
            for item_id, qty in needs.items():
                row = conn.execute(
                    "SELECT name, qty_on_hand FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                need_rows.append(
                    {
                        "name": row["name"] if row else item_id,
                        "need": db.qty_str(qty),
                        "on_hand": row["qty_on_hand"] if row else "0",
                    }
                )
            unmatched = [
                l for l in bom.matched_lines(conn, project["id"]) if not l["item_id"]
            ]
        finally:
            conn.close()
        return ui_command.build_panel(
            project, intent["count"], need_rows, shortages, len(unmatched)
        )

    if kind == "need":
        conn = db.connect()
        try:
            results = search_items(conn, intent["query"], limit=6)
        finally:
            conn.close()
        certain = (
            len(results) == 1
            and results[0]["score"] >= SEARCH_CUTOFF
        ) or (
            len(results) > 1
            and results[0]["score"] >= CERTAIN_SCORE
            and results[1]["score"] < CERTAIN_SCORE
        )
        if results and certain:
            return _add_to_basket(results[0]["id"], intent["qty"])
        if not results:
            return ui_command.note(
                f"No item matches “{intent['query']}” — nothing added.", error=True
            )
        return ui_command.need_ask(results, intent["qty"], intent["query"])

    if kind == "price_basket":
        updated, total, stale = sourcing.run_pricing()
        return ui_command.price_done(updated, total, stale)

    return ui_command.note(f"Unhandled command kind {kind!r}", error=True)


def _add_to_basket(item_id: str, qty: str) -> str:
    """'need N more' is additive on top of any existing manual entry."""
    conn = db.connect()
    try:
        existing = conn.execute(
            "SELECT qty FROM basket_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        item = conn.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    total = db.parse_qty(qty) + (db.parse_qty(existing["qty"]) if existing else Decimal(0))
    store.add_basket_item(item_id, total)
    return ui_command.need_done(item["name"], qty, db.qty_str(total))


@router.get("/command", response_class=HTMLResponse)
def command_live(q: str = "") -> str:
    return _render(parse(q), live=True)


@router.post("/command", response_class=HTMLResponse)
def command_execute(q: str = Form("")) -> str:
    return _render(parse(q), live=False)


@router.post("/command/need", response_class=HTMLResponse)
def command_need(item_id: str = Form(...), qty: str = Form(...)) -> str:
    return _add_to_basket(item_id, qty)


@router.post("/command/build", response_class=HTMLResponse)
def command_build(project_id: str = Form(...), count: int = Form(1)) -> str:
    conn = db.connect()
    try:
        project = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    finally:
        conn.close()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    ok, message = bom.attempt_build(project_id, count)
    return ui_command.note(
        f"{project['name']}: {message}", error=not ok
    )


@router.post("/command/recount", response_class=HTMLResponse)
def command_recount(
    location_id: str = Form(...),
    item_id: list[str] = Form([]),
    counted: list[str] = Form([]),
) -> str:
    """Apply a bin recount: only changed counts emit item.recounted."""
    changed = 0
    unchanged = 0
    for line_item, line_count in zip(item_id, counted):
        if not line_item.strip() or not line_count.strip():
            continue
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT qty_on_hand FROM items WHERE id = ?", (line_item,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            continue
        if db.parse_qty(line_count) == db.parse_qty(row["qty_on_hand"]):
            unchanged += 1
            continue
        store.recount_item(line_item, db.parse_qty(line_count))
        changed += 1
    return ui_command.note(
        f"Recounted {location_id}: {changed} corrected, {unchanged} already right."
    )


# --------------------------------------------------------------- item card


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_card(item_id: str) -> str:
    conn = db.connect()
    try:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
        item = dict(item)
        reserved_rows = [
            dict(r)
            for r in conn.execute(
                """SELECT r.qty, p.name AS project_name FROM reservations r
                   LEFT JOIN projects p ON p.id = r.project_id
                   WHERE r.item_id = ? AND r.status = 'active'""",
                (item_id,),
            )
        ]
        links = [
            dict(r)
            for r in conn.execute(
                """SELECT l.*, s.name AS supplier_name FROM item_links l
                   JOIN suppliers s ON s.id = l.supplier_id
                   WHERE l.item_id = ? ORDER BY s.name""",
                (item_id,),
            )
        ]
    finally:
        conn.close()
    on_hand = db.parse_qty(item["qty_on_hand"])
    reserved_total = sum(
        (db.parse_qty(r["qty"]) for r in reserved_rows), Decimal(0)
    )
    return ui_command.item_card(
        item,
        free=db.qty_str(on_hand - reserved_total),
        reserved=db.qty_str(reserved_total),
        reservations=reserved_rows,
        links=links,
        history=item_history(item_id),
    )


def item_history(item_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Every event that touched this item, newest first."""
    hits = []
    for event in events.read_all_events():
        payload = event["payload"]
        touched = (
            (event["type"].startswith("item.") and payload.get("id") == item_id)
            or payload.get("item_id") == item_id
            or any(
                entry.get("item_id") == item_id
                for entry in payload.get("consumed", []) + payload.get("returned", [])
            )
        )
        if touched:
            hits.append({"ts": event["ts"], "type": event["type"], "payload": payload})
    return hits[-limit:][::-1]


# --------------------------------------------------------------- phone UI


@router.get("/m", response_class=HTMLResponse)
def mobile() -> str:
    pending = len(ingest.list_cards())
    return ui_command.mobile_page(pending)
