"""The command bar — the primary interface. `help` lists every verb;
nothing here should require reading source code to discover.

Visibility rules: zero-quantity items are hidden from search by default
(revealed with the `all:` prefix, always with a count of what was
hidden); archived items are excluded everywhere except the `archived:`
prefix. Bin listings always show zero-qty items greyed — the physical
bin still nominally belongs to them.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from rapidfuzz import fuzz

from . import (
    bom,
    db,
    events,
    health,
    history,
    ingest,
    merge,
    sourcing,
    store,
    ui_command,
    undo,
)
from .locations import InvalidLocationId, is_valid_location_id, parse_location_id
from .movement import _ensure_location, _location_description, normalize_scan

router = APIRouter()

SEARCH_CUTOFF = 55
CERTAIN_SCORE = 90

_RECOUNT_RE = re.compile(r"^recount\s+(\S+)$", re.IGNORECASE)
_MOVE_RE = re.compile(r"^move(?:\s+to)?\s+(\S+)$", re.IGNORECASE)
_BUILD_RE = re.compile(r"^build\s+(.+?)(?:\s*[x×]\s*(\d+))?$", re.IGNORECASE)
_NEED_RE = re.compile(r"^need\s+(\d+(?:\.\d+)?)\s+(?:more\s+)?(.+)$", re.IGNORECASE)
_HISTORY_RE = re.compile(r"^history\s+(.+)$", re.IGNORECASE)
_UNDO_N_RE = re.compile(r"^undo\s+(\d+)$", re.IGNORECASE)
_SCOPE_RE = re.compile(r"^(all|archived):\s*(.*)$", re.IGNORECASE)

# Verb reference — this IS the discoverability contract. `help` renders it.
HELP = [
    ("search", "3mm heat shrink", "Fuzzy search: name, part number, description, aliases. Zero-qty items are hidden (a count tells you)."),
    ("all:", "all: heat shrink", "Search including zero-quantity items. `all:` alone lists everything."),
    ("archived:", "archived: servo", "Search retired (archived) items — they appear nowhere else."),
    ("<bin>", "A-2-3b", "Show a bin's contents. Zero-qty items are greyed, not hidden — the bin is still theirs."),
    ("move", "move to A-2-3b", "Arm a move target, then scan/type items; exact matches move instantly."),
    ("build", "build RPSRobot x2", "Build a project ×N — shows needs and exact shortages, then asks to confirm."),
    ("need", "need 20 more m3 screw", "Add to the reorder basket. Ambiguous names get a pick list, never a guess."),
    ("recount", "recount A-2-3b", "Count a bin's contents; unchanged numbers emit nothing."),
    ("low", "low", "Everything with free stock under its minimum."),
    ("price basket", "price basket", "Fetch current supplier prices for the basket, right now."),
    ("history", "history A-2-3b", "Event history for a bin or item (also: history m3 screw)."),
    ("undo", "undo · undo list · undo 3", "Reverse your last action on THIS machine. Undoing an undo redoes."),
    ("health", "health", "Data-quality score with itemised, fixable counts."),
    ("merge", "merge", "Scan for likely duplicate items and merge them safely."),
    ("help", "help build", "This list — or details for one verb."),
]

_SYNTAX = {
    "move": "move to <bin>   e.g. `move to A-2-3b`",
    "build": "build <project> x<count>   e.g. `build RPSRobot x2`",
    "need": "need <qty> more <item>   e.g. `need 20 more m3 screw`",
    "recount": "recount <bin>   e.g. `recount A-2-3b`",
    "history": "history <bin or item>   e.g. `history A-2-3b`",
    "price": "price basket   (fetches supplier prices on demand)",
    "all:": "all: <search>   includes zero-quantity items",
    "archived:": "archived: <search>   searches retired items only",
    "merge": "merge   opens the duplicate scanner",
    "undo": "undo · undo list · undo <n>",
}


def parse(text: str) -> dict[str, Any]:
    q = text.strip()
    if not q:
        return {"kind": "empty"}
    lower = q.lower()

    scan_kind, scanned = normalize_scan(q)
    if scan_kind == "item":
        return {"kind": "item_jump", "item_id": scanned}
    if scan_kind == "location":
        if is_valid_location_id(scanned):
            return {"kind": "bin", "location_id": scanned}
        return {"kind": "invalid", "message": f"{scanned!r} is not a valid location ID — the format is Unit-Shelf-Bin[section], e.g. A-2-3b"}

    if lower in ("help", "?"):
        return {"kind": "help"}
    if lower.startswith("help "):
        return {"kind": "help", "verb": q[5:].strip().lower()}
    if match := _SCOPE_RE.match(q):
        return {
            "kind": "search",
            "query": match.group(2).strip(),
            "scope": match.group(1).lower(),
        }
    if lower in ("price basket", "price the basket"):
        return {"kind": "price_basket"}
    if lower == "low":
        return {"kind": "low"}
    if lower == "health":
        return {"kind": "health"}
    if lower == "merge":
        return {"kind": "merge_tool"}
    if lower == "undo":
        return {"kind": "undo"}
    if lower in ("undo list", "undo ls"):
        return {"kind": "undo_list"}
    if match := _UNDO_N_RE.match(q):
        return {"kind": "undo", "n": int(match.group(1))}
    if lower in _SYNTAX:
        return {"kind": "syntax", "verb": lower}
    if is_valid_location_id(q):
        return {"kind": "bin", "location_id": q}
    if match := _HISTORY_RE.match(q):
        return {"kind": "history", "query": match.group(1).strip()}
    if match := _RECOUNT_RE.match(q):
        loc = match.group(1)
        if is_valid_location_id(loc):
            return {"kind": "recount", "location_id": loc}
        return {"kind": "invalid", "message": f"{loc!r} is not a valid location ID — the format is Unit-Shelf-Bin[section], e.g. `recount A-2-3b`"}
    if match := _MOVE_RE.match(q):
        loc = match.group(1)
        if is_valid_location_id(loc):
            return {"kind": "move", "location_id": loc}
        return {"kind": "invalid", "message": f"{loc!r} is not a valid location ID — try `move to A-2-3b`"}
    if match := _BUILD_RE.match(q):
        return {
            "kind": "build",
            "query": match.group(1).strip(),
            "count": int(match.group(2) or 1),
        }
    if match := _NEED_RE.match(q):
        return {"kind": "need", "qty": match.group(1), "query": match.group(2).strip()}
    return {"kind": "search", "query": q, "scope": "default"}


# ------------------------------------------------------------------ search


def search_items(
    conn,
    query: str,
    limit: int = 15,
    include_zero: bool = False,
    archived_only: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Returns (results, hidden_zero_count). Nothing is silently omitted —
    callers must surface the hidden count."""
    aliases: dict[str, list[str]] = {}
    for row in conn.execute("SELECT alias_text, item_id FROM aliases"):
        aliases.setdefault(row["item_id"], []).append(row["alias_text"])
    reserved = bom.reserved_by_item(conn)

    q = query.strip().lower()
    scored = []
    for item in conn.execute(
        "SELECT * FROM items WHERE archived = ?", (1 if archived_only else 0,)
    ):
        if not q:
            best = 100.0
        else:
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

    hidden_zero = 0
    results = []
    for score, item in scored:
        on_hand = db.parse_qty(item["qty_on_hand"])
        if not include_zero and not archived_only and on_hand == 0:
            hidden_zero += 1
            continue
        if len(results) >= limit:
            continue
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
    return results, hidden_zero


def _stock_summary(item: dict[str, Any], on_hand: Decimal, reserved: Decimal) -> str:
    """The canonical line: "A-2-3b, 12 left, 4 free (8 reserved)"."""
    location = item["location_id"] or "no location"
    text = f"{location}, {db.qty_str(on_hand)} left"
    if reserved > 0:
        text += f", {db.qty_str(on_hand - reserved)} free ({db.qty_str(reserved)} reserved)"
    if item.get("archived"):
        text += " · ARCHIVED"
    return text


def _bin_contents(conn, location_id: str) -> tuple[dict | None, list[dict[str, Any]]]:
    location = conn.execute(
        "SELECT * FROM locations WHERE id = ?", (location_id,)
    ).fetchone()
    reserved = bom.reserved_by_item(conn)
    items = []
    for row in conn.execute(
        "SELECT * FROM items WHERE location_id = ? AND archived = 0 ORDER BY name",
        (location_id,),
    ):
        held = reserved.get(row["id"], Decimal(0))
        on_hand = db.parse_qty(row["qty_on_hand"])
        items.append(
            {
                **dict(row),
                "free": db.qty_str(on_hand - held),
                "reserved": db.qty_str(held),
                "zero": on_hand == 0,
            }
        )
    return (dict(location) if location else None), items


def _unknown_bin_message(conn, location_id: str) -> str:
    """A helpful error: what DOES exist near the id they typed."""
    try:
        loc = parse_location_id(location_id)
    except InvalidLocationId:
        return f"{location_id!r} is not a valid location ID — the format is Unit-Shelf-Bin[section], e.g. A-2-3b."
    siblings = [
        r["bin"]
        for r in conn.execute(
            "SELECT DISTINCT bin FROM locations WHERE unit = ? AND shelf = ? ORDER BY bin",
            (loc.unit, loc.shelf),
        )
    ]
    if siblings:
        bins = f"{siblings[0]}-{siblings[-1]}" if len(siblings) > 1 else str(siblings[0])
        return (
            f"Unknown location '{location_id}' — Unit {loc.unit} shelf {loc.shelf} "
            f"has bins {bins}. Try `{loc.unit}-{loc.shelf}-{siblings[-1]}`."
        )
    shelves = [
        r["shelf"]
        for r in conn.execute(
            "SELECT DISTINCT shelf FROM locations WHERE unit = ? ORDER BY shelf", (loc.unit,)
        )
    ]
    if shelves:
        return (
            f"Unknown location '{location_id}' — Unit {loc.unit} has shelves "
            f"{shelves[0]}-{shelves[-1]}, but no shelf {loc.shelf} bin {loc.bin} is registered."
        )
    return (
        f"Unknown location '{location_id}' — no Unit {loc.unit} is registered yet. "
        f"Generate its labels at /labels or run setup (/setup) to add it."
    )


def _low_items(conn) -> list[dict[str, Any]]:
    reserved = bom.reserved_by_item(conn)
    low = []
    for row in conn.execute(
        "SELECT * FROM items WHERE min_qty IS NOT NULL AND archived = 0 ORDER BY name"
    ):
        on_hand = db.parse_qty(row["qty_on_hand"])
        free = on_hand - reserved.get(row["id"], Decimal(0))
        min_qty = db.parse_qty(row["min_qty"])
        if free < min_qty:
            low.append(
                {**dict(row), "free": db.qty_str(free), "min": db.qty_str(min_qty)}
            )
    return low


def _match_project(conn, query: str) -> tuple[dict | None, list[dict[str, Any]]]:
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
        len(candidates) == 1 and candidates[0]["score"] >= CERTAIN_SCORE
    ) or (
        len(candidates) > 1
        and candidates[0]["score"] >= CERTAIN_SCORE
        and candidates[1]["score"] < CERTAIN_SCORE
    ):
        return candidates[0], candidates
    return None, candidates


# ------------------------------------------------------------------ render


def _render(intent: dict[str, Any], live: bool) -> str:
    kind = intent["kind"]
    if kind == "empty":
        return ""
    if kind == "invalid":
        return ui_command.note(intent["message"], error=True)
    if kind == "help":
        return ui_command.help_panel(HELP, _SYNTAX, intent.get("verb"))
    if kind == "syntax":
        return ui_command.enter_hint("… " + _SYNTAX[intent["verb"]], syntax=True)

    if kind == "item_jump":
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE id = ?", (intent["item_id"],)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ui_command.note(
                "That item label doesn't match anything here — was it printed from a "
                "different inventory?", error=True,
            )
        return ui_command.item_jump(dict(row))

    if kind == "search":
        scope = intent.get("scope", "default")
        conn = db.connect()
        try:
            results, hidden = search_items(
                conn,
                intent["query"],
                include_zero=scope == "all",
                archived_only=scope == "archived",
            )
        finally:
            conn.close()
        return ui_command.search_results(results, intent["query"], hidden, scope)

    if kind == "bin":
        conn = db.connect()
        try:
            location, items = _bin_contents(conn, intent["location_id"])
            if location is None and not items:
                return ui_command.note(_unknown_bin_message(conn, intent["location_id"]), error=True)
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

    if kind == "history":
        return _history_command(intent["query"])

    if kind == "undo_list":
        return _undo_list_panel()

    if kind == "health":
        conn = db.connect()
        try:
            report = health.report(conn)
        finally:
            conn.close()
        return ui_command.health_panel(report, health.sync_summary())

    if kind == "merge_tool":
        conn = db.connect()
        try:
            pairs = merge.likely_duplicate_pairs(conn)
        finally:
            conn.close()
        return ui_command.merge_panel(pairs)

    if live:
        hints = {
            "move": f"arm move mode → {intent.get('location_id', '')}",
            "recount": f"recount bin {intent.get('location_id', '')}",
            "build": f"build “{intent.get('query', '')}” ×{intent.get('count', 1)}",
            "need": f"add {intent.get('qty')} × “{intent.get('query')}” to the reorder basket",
            "price_basket": "price the basket now (fetches supplier pages)",
            "undo": "undo your last action on this machine (try `undo list` first)",
        }
        return ui_command.enter_hint(hints.get(kind, kind))

    if kind == "undo":
        return _undo_command(intent.get("n"))

    if kind == "move":
        _ensure_location(intent["location_id"])
        return ui_command.move_panel(
            intent["location_id"], _location_description(intent["location_id"])
        )

    if kind == "recount":
        conn = db.connect()
        try:
            location, items = _bin_contents(conn, intent["location_id"])
            if location is None and not items:
                return ui_command.note(_unknown_bin_message(conn, intent["location_id"]), error=True)
        finally:
            conn.close()
        if not items:
            return ui_command.note(
                f"Nothing recorded in {intent['location_id']} to recount — it's an empty bin.",
                error=True,
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
            # Zero-qty items are exactly what you'd reorder — include them.
            results, _hidden = search_items(conn, intent["query"], limit=6, include_zero=True)
        finally:
            conn.close()
        certain = (len(results) == 1) or (
            len(results) > 1
            and results[0]["score"] >= CERTAIN_SCORE
            and results[1]["score"] < CERTAIN_SCORE
        )
        if results and certain:
            return _add_to_basket(results[0]["id"], intent["qty"])
        if not results:
            return ui_command.note(
                f"No item matches “{intent['query']}” — nothing added. "
                f"(Zero-qty items were included in this search; `archived:` items are not.)",
                error=True,
            )
        return ui_command.need_ask(results, intent["qty"], intent["query"])

    if kind == "price_basket":
        updated, total, stale = sourcing.run_pricing()
        return ui_command.price_done(updated, total, stale)

    return ui_command.note(f"Unhandled command kind {kind!r}", error=True)


def _history_command(query: str) -> str:
    if is_valid_location_id(query):
        return render_history("bin", query, page=1, type_filter=None)
    conn = db.connect()
    try:
        results, _ = search_items(conn, query, limit=6, include_zero=True)
        if not results:
            archived, _ = search_items(conn, query, limit=6, archived_only=True)
            results = archived
    finally:
        conn.close()
    if not results:
        return ui_command.note(
            f"Nothing matches “{query}” — try `history <bin>` (e.g. `history A-2-3b`) "
            f"or an item name.", error=True,
        )
    certain = (len(results) == 1) or (
        results[0]["score"] >= CERTAIN_SCORE and results[1]["score"] < CERTAIN_SCORE
    )
    if certain:
        return render_history("item", results[0]["id"], page=1, type_filter=None)
    return ui_command.history_ask(results, query)


def render_history(
    target_kind: str, target_id: str, page: int, type_filter: str | None
) -> str:
    log = history.load_log()
    conn = db.connect()
    try:
        if target_kind == "item":
            raw = history.item_events(log, target_id)
            row = conn.execute("SELECT name FROM items WHERE id = ?", (target_id,)).fetchone()
            title = f"History — {row['name'] if row else target_id}"
            focus = target_id
        elif target_kind == "bin":
            raw = history.bin_events(log, target_id)
            title = f"History — bin {target_id}"
            focus = None
        elif target_kind == "project":
            raw = history.project_events(log, target_id)
            row = conn.execute("SELECT name FROM projects WHERE id = ?", (target_id,)).fetchone()
            title = f"History — project {row['name'] if row else target_id}"
            focus = None
        else:
            return ui_command.note("Unknown history target.", error=True)
        data = history.build_entries(
            conn, raw, log, focus_item=focus, type_filter=type_filter, page=page
        )
    finally:
        conn.close()
    return ui_command.history_panel(title, target_kind, target_id, data)


def _undo_command(n: int | None) -> str:
    stack = undo.undo_stack()
    if not stack:
        return ui_command.note(
            "Nothing to undo — no undoable actions by this machine in the log."
        )
    if n is not None:
        if not 1 <= n <= len(stack):
            return ui_command.note(
                f"`undo {n}` is out of range — `undo list` shows entries 1-{len(stack)}.",
                error=True,
            )
        target = stack[n - 1]
    else:
        target = stack[0]
    ok, message = undo.perform_undo(target["id"])
    return ui_command.note(message, error=not ok)


def _undo_list_panel() -> str:
    stack = undo.undo_stack()
    conn = db.connect()
    try:
        log = history.load_log()
        entries = []
        for event in stack:
            data = history.build_entries(conn, [event], log)
            entries.append(
                {
                    "event_id": event["id"],
                    "ts": event["ts"],
                    "text": data["entries"][0]["text"] if data["entries"] else event["type"],
                    "type": event["type"],
                }
            )
    finally:
        conn.close()
    return ui_command.undo_list_panel(entries)


def _add_to_basket(item_id: str, qty: str) -> str:
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
    event = store.add_basket_item(item_id, total)
    return ui_command.need_done(item["name"], qty, db.qty_str(total), event["id"])


# ------------------------------------------------------------------ routes


@router.get("/command", response_class=HTMLResponse)
def command_live(q: str = "") -> str:
    return _render(parse(q), live=True)


@router.post("/command", response_class=HTMLResponse)
def command_execute(q: str = Form("")) -> str:
    return _render(parse(q), live=False)


@router.post("/command/need", response_class=HTMLResponse)
def command_need(item_id: str = Form(...), qty: str = Form(...)) -> str:
    try:
        return _add_to_basket(item_id, qty)
    except (InvalidOperation, TypeError):
        return ui_command.note(
            f"{qty!r} isn't a quantity — quantities are plain numbers like 8 or 12.5.",
            error=True,
        )


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
    ok, message, event_id = bom.attempt_build(project_id, count)
    return ui_command.note(f"{project['name']}: {message}", error=not ok, undo_event=event_id)


@router.post("/command/recount", response_class=HTMLResponse)
def command_recount(
    location_id: str = Form(...),
    item_id: list[str] = Form([]),
    counted: list[str] = Form([]),
) -> str:
    """Validate every count first, apply only if ALL parse — one bad line
    must not leave a bin half-recounted (atomic remediation item 2)."""
    conn = db.connect()
    try:
        current = {
            line_item: conn.execute(
                "SELECT qty_on_hand FROM items WHERE id = ?", (line_item,)
            ).fetchone()
            for line_item in item_id
            if line_item.strip()
        }
    finally:
        conn.close()

    plan: list[tuple[str, Any]] = []
    unchanged = 0
    for row_no, (line_item, line_count) in enumerate(zip(item_id, counted), start=1):
        if not line_item.strip() or not line_count.strip():
            continue
        row = current.get(line_item)
        if row is None:
            continue
        try:
            counted_qty = db.parse_qty(line_count)
        except (InvalidOperation, TypeError):
            return ui_command.note(
                f"Nothing recounted — line {row_no}: {line_count!r} is not a number; "
                f"counts are plain numbers like 8 or 12.5.",
                error=True,
            )
        if counted_qty == db.parse_qty(row["qty_on_hand"]):
            unchanged += 1
        else:
            plan.append((line_item, counted_qty))

    for line_item, counted_qty in plan:
        store.recount_item(line_item, counted_qty)
    return ui_command.note(
        f"Recounted {location_id}: {len(plan)} corrected, {unchanged} already right."
    )


@router.post("/command/undo", response_class=HTMLResponse)
def command_undo(event_id: str = Form(...)) -> str:
    ok, message = undo.perform_undo(event_id)
    return ui_command.note(message, error=not ok)


@router.get("/command/history", response_class=HTMLResponse)
def command_history(
    target: str, page: int = 1, type: str | None = None
) -> str:
    kind, _, target_id = target.partition(":")
    return render_history(kind, target_id, max(1, page), type or None)


@router.get("/history", response_class=HTMLResponse)
def history_page(target: str, page: int = 1, type: str | None = None) -> str:
    from .ui import page as wrap

    kind, _, target_id = target.partition(":")
    body = render_history(kind, target_id, max(1, page), type or None)
    return wrap("The Raven's Nest — History", f'<p><a href="/">← command bar</a></p>{body}')


# ------------------------------------------------------------------- merge


@router.get("/merge", response_class=HTMLResponse)
def merge_page() -> str:
    from .ui import page as wrap

    conn = db.connect()
    try:
        pairs = merge.likely_duplicate_pairs(conn)
    finally:
        conn.close()
    return wrap(
        "The Raven's Nest — Merge duplicates",
        f'<p><a href="/">← command bar</a></p>{ui_command.merge_panel(pairs)}',
    )


@router.post("/merge", response_class=HTMLResponse)
def merge_execute(
    source_id: str = Form(...),
    target_id: str = Form(...),
    location_id: str = Form(""),
    allow_units: str = Form(""),
) -> str:
    ok, message, event_id = merge.perform_merge(
        source_id,
        target_id,
        location_id.strip() or None,
        allow_unit_mismatch=bool(allow_units),
    )
    if not ok:
        conn = db.connect()
        try:
            pairs = merge.likely_duplicate_pairs(conn)
        finally:
            conn.close()
        return ui_command.note(message, error=True) + ui_command.merge_panel(pairs)
    conn = db.connect()
    try:
        pairs = merge.likely_duplicate_pairs(conn)
    finally:
        conn.close()
    return ui_command.note(message, undo_event=event_id) + ui_command.merge_panel(pairs)


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
        log = history.load_log()
        raw = history.item_events(log, item_id)
        entries = history.build_entries(conn, raw, log, focus_item=item_id, page=1)
    finally:
        conn.close()
    on_hand = db.parse_qty(item["qty_on_hand"])
    reserved_total = sum((db.parse_qty(r["qty"]) for r in reserved_rows), Decimal(0))
    return ui_command.item_card(
        item,
        free=db.qty_str(on_hand - reserved_total),
        reserved=db.qty_str(reserved_total),
        reservations=reserved_rows,
        links=links,
        history_data=entries,
    )


@router.post("/items/{item_id}/archive")
def archive_item(item_id: str, reason: str = Form("")) -> RedirectResponse:
    store.archive_item(item_id, reason.strip())
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


@router.post("/items/{item_id}/unarchive")
def unarchive_item(item_id: str) -> RedirectResponse:
    store.unarchive_item(item_id)
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


@router.post("/items/{item_id}/edit")
def quick_edit_item(
    item_id: str,
    min_qty: str = Form(""),
    last_paid_aud: str = Form(""),
    location_id: str = Form(""),
) -> RedirectResponse:
    """The item-card fix flow: set the fields the health report nags about."""
    updates: dict[str, Any] = {}
    try:
        if min_qty.strip():
            updates["min_qty"] = db.qty_str(db.parse_qty(min_qty))
        if last_paid_aud.strip():
            updates["last_paid_aud"] = db.qty_str(db.parse_qty(last_paid_aud))
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="quantities must be plain numbers like 8 or 12.5")
    if updates:
        store.update_item(item_id, **updates)
    if location_id.strip():
        try:
            _ensure_location(location_id.strip())
        except InvalidLocationId as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.move_item(item_id, location_id.strip())
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


# --------------------------------------------------------------- phone UI


@router.get("/m", response_class=HTMLResponse)
def mobile() -> str:
    pending = len(ingest.list_cards())
    return ui_command.mobile_page(pending)
