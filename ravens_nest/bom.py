"""BOM parsing, the matching ladder, stock math, and the reorder basket.

Matching ladder (per line): exact part_number → exact name → stored alias
→ fuzzy name suggestions (rapidfuzz, scored, never auto-applied). Each
rung must produce exactly one item to auto-match; ambiguity falls through
to manual resolution.

Stock semantics: free_stock = qty_on_hand − Σ(active reservations).
Quantities are Decimal throughout — never SQL SUM, which would go
through floats.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from rapidfuzz import fuzz, process

from . import db

UNIT_TYPES = ("each", "g", "mm", "mL")
REQUIRED_COLUMNS = ("part_number", "description", "quantity", "unit")
OPTIONAL_COLUMNS = ("reference_designators", "notes")

FUZZY_CUTOFF = 55
FUZZY_LIMIT = 5


# ------------------------------------------------------------------ parsing


def parse_bom_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate BOM CSV. Returns (lines, errors); lines are only
    usable when errors is empty."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], ["CSV is empty"]
    headers = {(h or "").strip().lower(): h for h in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return [], [f"missing required column(s): {', '.join(missing)}"]

    lines: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, row in enumerate(reader, start=1):
        def cell(column: str) -> str:
            header = headers.get(column)
            return (row.get(header) or "").strip() if header else ""

        part_number = cell("part_number")
        if not part_number:
            errors.append(f"line {line_no}: part_number is required")
        unit = cell("unit")
        if unit not in UNIT_TYPES:
            errors.append(
                f"line {line_no}: unit {unit!r} must be one of {', '.join(UNIT_TYPES)}"
            )
        quantity = None
        try:
            quantity = Decimal(cell("quantity"))
            if quantity <= 0:
                errors.append(f"line {line_no}: quantity must be positive")
        except InvalidOperation:
            errors.append(f"line {line_no}: quantity {cell('quantity')!r} is not a number")

        lines.append(
            {
                "line_no": line_no,
                "part_number": part_number,
                "description": cell("description"),
                "quantity": db.qty_str(quantity) if quantity is not None else cell("quantity"),
                "unit": unit,
                "reference_designators": cell("reference_designators") or None,
                "notes": cell("notes") or None,
            }
        )
    if not lines and not errors:
        errors.append("CSV has no data rows")
    return lines, errors


# ----------------------------------------------------------------- matching


def load_match_context(conn: sqlite3.Connection) -> dict[str, Any]:
    items = [
        dict(row)
        for row in conn.execute("SELECT id, name, part_number, unit_type FROM items")
    ]
    aliases = [dict(row) for row in conn.execute("SELECT alias_text, item_id FROM aliases")]
    return {"items": items, "aliases": aliases}


def auto_match(line: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    """Try the automatic rungs of the ladder. Returns {item_id, method,
    alias_text} or None. Each rung must be unambiguous (exactly one hit)."""
    items = context["items"]
    part_number = line["part_number"].strip().lower()
    description = line["description"].strip().lower()

    if part_number:
        hits = [
            i for i in items if (i["part_number"] or "").strip().lower() == part_number
        ]
        if len(hits) == 1:
            return {"item_id": hits[0]["id"], "method": "part_number", "alias_text": None}

    if description:
        hits = [i for i in items if i["name"].strip().lower() == description]
        if len(hits) == 1:
            # Remember the BOM's part number so the next revision matches by alias.
            return {
                "item_id": hits[0]["id"],
                "method": "name",
                "alias_text": line["part_number"] or None,
            }

    known_ids = {i["id"] for i in items}
    texts = {t for t in (part_number, description) if t}
    alias_hits = {
        a["item_id"]
        for a in context["aliases"]
        if a["alias_text"].strip().lower() in texts and a["item_id"] in known_ids
    }
    if len(alias_hits) == 1:
        return {"item_id": alias_hits.pop(), "method": "alias", "alias_text": None}

    return None


def fuzzy_candidates(line: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Scored fuzzy name suggestions — shown to the user, never auto-applied."""
    query = line["description"].strip() or line["part_number"].strip()
    if not query:
        return []
    choices = {item["id"]: item["name"] for item in context["items"]}
    results = process.extract(
        query, choices, scorer=fuzz.WRatio, limit=FUZZY_LIMIT, score_cutoff=FUZZY_CUTOFF
    )
    return [
        {"item_id": key, "name": name, "score": round(score)}
        for name, score, key in results
    ]


# -------------------------------------------------------------------- stock


def reserved_by_item(
    conn: sqlite3.Connection, exclude_project: str | None = None
) -> dict[str, Decimal]:
    """Total active reservations per item, optionally excluding one project
    (a build should not be blocked by its own reservations)."""
    sql = "SELECT item_id, qty FROM reservations WHERE status = 'active'"
    params: tuple = ()
    if exclude_project:
        sql += " AND project_id != ?"
        params = (exclude_project,)
    totals: dict[str, Decimal] = {}
    for row in conn.execute(sql, params):
        totals[row["item_id"]] = totals.get(row["item_id"], Decimal(0)) + db.parse_qty(row["qty"])
    return totals


def active_reservations(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM reservations WHERE project_id = ? AND status = 'active'",
            (project_id,),
        )
    ]


def _needed_for(unit_type: str, amount: Decimal) -> Decimal:
    """Unit-type-aware reorder rounding: 'each' items reorder in whole
    units; g/mm/mL consumables reorder in their native decimal amounts."""
    if unit_type == "each":
        return amount.to_integral_value(rounding=ROUND_CEILING)
    return amount


def reorder_basket(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Items that need reordering: reservation shortfalls (reserved beyond
    on-hand stock) and items whose free stock has fallen below min_qty.
    The needed amount restores free stock to at least max(0, min_qty)."""
    reserved = reserved_by_item(conn)
    basket = []
    for item in conn.execute("SELECT * FROM items ORDER BY name"):
        on_hand = db.parse_qty(item["qty_on_hand"])
        item_reserved = reserved.get(item["id"], Decimal(0))
        free = on_hand - item_reserved
        reasons = []
        if item_reserved > on_hand:
            reasons.append("reserved beyond stock")
        deficit = -free if free < 0 else Decimal(0)
        if item["min_qty"] is not None:
            min_qty = db.parse_qty(item["min_qty"])
            if free < min_qty:
                deficit = max(deficit, min_qty - free)
                reasons.append(f"free stock below min ({db.qty_str(min_qty)})")
        if deficit > 0:
            basket.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "unit_type": item["unit_type"],
                    "on_hand": db.qty_str(on_hand),
                    "reserved": db.qty_str(item_reserved),
                    "free": db.qty_str(free),
                    "needed": db.qty_str(_needed_for(item["unit_type"], deficit)),
                    "reasons": reasons,
                }
            )
    return basket


# ------------------------------------------------------------------- build


def matched_lines(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM bom_lines WHERE project_id = ? ORDER BY line_no", (project_id,)
        )
    ]


def per_build_needs(lines: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Quantity of each item one build consumes (lines grouped by item)."""
    needs: dict[str, Decimal] = {}
    for line in lines:
        if line["item_id"]:
            needs[line["item_id"]] = needs.get(line["item_id"], Decimal(0)) + db.parse_qty(
                line["quantity"]
            )
    return needs


def build_shortages(
    conn: sqlite3.Connection, project_id: str, count: int
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    """Compute total needs for N builds and any shortages against free
    stock. A project's own reservations don't count against it."""
    lines = matched_lines(conn, project_id)
    needs = {
        item_id: per_build * count
        for item_id, per_build in per_build_needs(lines).items()
    }
    reserved_other = reserved_by_item(conn, exclude_project=project_id)
    shortages = []
    for item_id, need in needs.items():
        row = conn.execute(
            "SELECT name, qty_on_hand, unit_type FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            shortages.append(
                {"name": f"unknown item {item_id}", "needed": db.qty_str(need),
                 "available": "0", "short": db.qty_str(need), "unit_type": "each"}
            )
            continue
        available = db.parse_qty(row["qty_on_hand"]) - reserved_other.get(item_id, Decimal(0))
        if need > available:
            short = _needed_for(row["unit_type"], need - available)
            shortages.append(
                {
                    "name": row["name"],
                    "unit_type": row["unit_type"],
                    "needed": db.qty_str(need),
                    "available": db.qty_str(max(available, Decimal(0))),
                    "short": db.qty_str(short),
                }
            )
    return needs, shortages


def attempt_build(project_id: str, count: int) -> tuple[bool, str]:
    """Validate and execute a build. Returns (ok, message) — on failure the
    message lists exactly what blocks it (unresolved lines or shortages)."""
    from . import store  # local import: store depends on replay, not on bom

    if count < 1:
        return False, "Build count must be at least 1."
    conn = db.connect()
    try:
        lines = matched_lines(conn, project_id)
        if not lines:
            return False, "No BOM imported yet."
        unmatched = [l for l in lines if not l["item_id"]]
        if unmatched:
            nos = ", ".join(str(l["line_no"]) for l in unmatched)
            return False, f"Cannot build: unresolved BOM line(s) {nos}."
        needs, shortages = build_shortages(conn, project_id, count)
    finally:
        conn.close()
    if shortages:
        detail = "; ".join(
            f"{s['name']}: need {s['needed']}, have {s['available']} free — short {s['short']} {s['unit_type']}"
            for s in shortages
        )
        return False, f"Insufficient free stock: {detail}"
    consumed = [
        {"item_id": item_id, "qty": db.qty_str(qty)} for item_id, qty in needs.items()
    ]
    store.execute_build(project_id, count, consumed)
    return True, f"Built ×{count}"


def net_builds(conn: sqlite3.Connection, project_id: str) -> int:
    total = 0
    for row in conn.execute(
        "SELECT kind, count FROM builds WHERE project_id = ?", (project_id,)
    ):
        total += row["count"] if row["kind"] == "build" else -row["count"]
    return total


# -------------------------------------------------------------------- cost


def bom_cost_rows(
    conn: sqlite3.Connection, project_id: str
) -> tuple[list[dict[str, Any]], str, int]:
    """BOM lines with unit and extended cost from each item's last_paid_aud.
    Returns (rows, total, unpriced_count) — unpriced lines are flagged, not
    silently priced at zero (though they contribute zero to the total)."""
    rows = []
    total = Decimal(0)
    unpriced = 0
    cents = Decimal("0.01")
    for line in matched_lines(conn, project_id):
        entry = dict(line)
        entry["item_name"] = None
        entry["unit_cost"] = None
        entry["ext_cost"] = None
        if line["item_id"]:
            item = conn.execute(
                "SELECT name, last_paid_aud FROM items WHERE id = ?", (line["item_id"],)
            ).fetchone()
            if item is not None:
                entry["item_name"] = item["name"]
                if item["last_paid_aud"] is not None:
                    unit_cost = db.parse_qty(item["last_paid_aud"])
                    ext = unit_cost * db.parse_qty(line["quantity"])
                    entry["unit_cost"] = db.qty_str(unit_cost)
                    entry["ext_cost"] = str(ext.quantize(cents))
                    total += ext
        if entry["ext_cost"] is None:
            unpriced += 1
        rows.append(entry)
    return rows, str(total.quantize(cents)), unpriced
