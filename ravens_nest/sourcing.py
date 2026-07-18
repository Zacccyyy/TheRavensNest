"""Suppliers, item sourcing links, the reorder basket, candidate order
baskets, and received-order recording.

Candidate baskets are honest greedy heuristics, not an LP solver:
"cheapest" assigns each item to its cheapest option then adds shipping,
"fewest suppliers" is greedy set cover, "fastest" picks minimum lead
time. Identical outcomes are deduplicated before display.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_CEILING
from typing import Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from . import bom, db, pricing, store, ui_sourcing

log = logging.getLogger(__name__)

router = APIRouter()

# Rough, editable starting points — thresholds/shipping/lead vary; the
# supplier page is where real numbers live. reliability is always None
# here: ratings are set manually by the user after an order arrives.
SEED_SUPPLIERS = [
    {"name": "Core Electronics", "free_shipping_threshold_aud": "99", "typical_shipping_aud": "7.50", "typical_lead_days": 2},
    {"name": "element14", "free_shipping_threshold_aud": "45", "typical_shipping_aud": "12", "typical_lead_days": 2},
    {"name": "RS Components", "free_shipping_threshold_aud": "80", "typical_shipping_aud": "12", "typical_lead_days": 3},
    {"name": "DigiKey", "free_shipping_threshold_aud": "60", "typical_shipping_aud": "8", "typical_lead_days": 5},
    {"name": "Mouser", "free_shipping_threshold_aud": "60", "typical_shipping_aud": "10", "typical_lead_days": 5},
    {"name": "AliExpress", "free_shipping_threshold_aud": "0", "typical_shipping_aud": "0", "typical_lead_days": 21},
    {"name": "Bunnings", "free_shipping_threshold_aud": None, "typical_shipping_aud": "0", "typical_lead_days": 1},
    {"name": "Jaycar", "free_shipping_threshold_aud": "99", "typical_shipping_aud": "9", "typical_lead_days": 2},
]


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


# -------------------------------------------------------- basket assembly


def assemble_basket(conn) -> list[dict[str, Any]]:
    """Auto entries (free_stock below min / reservation shortfalls) merged
    with manual additions. Needed amounts add when both apply."""
    entries: dict[str, dict[str, Any]] = {}
    for auto in bom.reorder_basket(conn):
        entries[auto["id"]] = {**auto, "needed_dec": db.parse_qty(auto["needed"]), "manual": False}
    for row in conn.execute(
        """SELECT b.item_id, b.qty, i.name, i.unit_type, i.qty_on_hand
           FROM basket_items b JOIN items i ON i.id = b.item_id"""
    ):
        qty = db.parse_qty(row["qty"])
        if row["item_id"] in entries:
            entry = entries[row["item_id"]]
            entry["needed_dec"] += qty
            entry["reasons"] = entry["reasons"] + ["added manually"]
            entry["manual"] = True
        else:
            reserved = bom.reserved_by_item(conn).get(row["item_id"], Decimal(0))
            on_hand = db.parse_qty(row["qty_on_hand"])
            entries[row["item_id"]] = {
                "id": row["item_id"],
                "name": row["name"],
                "unit_type": row["unit_type"],
                "on_hand": db.qty_str(on_hand),
                "reserved": db.qty_str(reserved),
                "free": db.qty_str(on_hand - reserved),
                "needed_dec": qty,
                "reasons": ["added manually"],
                "manual": True,
            }
    result = []
    for entry in entries.values():
        entry["needed"] = db.qty_str(entry["needed_dec"])
        entry["options"] = _entry_options(conn, entry["id"], entry["needed_dec"])
        result.append(entry)
    result.sort(key=lambda e: e["name"].lower())
    return result


def _entry_options(conn, item_id: str, needed: Decimal) -> list[dict[str, Any]]:
    """Priced supplier options for an item, pack-aware: if a part comes in
    packs of 10, a need of 7 becomes one pack of 10."""
    options = []
    for row in conn.execute(
        """SELECT l.*, s.name AS supplier_name, s.reliability,
                  s.free_shipping_threshold_aud, s.typical_shipping_aud,
                  s.typical_lead_days
           FROM item_links l JOIN suppliers s ON s.id = l.supplier_id
           WHERE l.item_id = ?""",
        (item_id,),
    ):
        option = dict(row)
        if row["last_price_aud"] is None:
            option["packs"] = None
            options.append(option)
            continue
        pack_qty = db.parse_qty(row["pack_qty"])
        price = db.parse_qty(row["last_price_aud"])
        packs = max(
            (needed / pack_qty).to_integral_value(rounding=ROUND_CEILING), Decimal(1)
        )
        option["packs"] = int(packs)
        option["order_qty"] = db.qty_str(packs * pack_qty)
        option["cost_dec"] = packs * price
        option["cost"] = _money(packs * price)
        options.append(option)
    return options


# ----------------------------------------------------- candidate baskets


def _summarize(label: str, assignment: dict[str, dict[str, Any]], total_items: int) -> dict[str, Any]:
    """assignment: item_id -> chosen option (with entry name attached)."""
    by_supplier: dict[str, dict[str, Any]] = {}
    for option in assignment.values():
        group = by_supplier.setdefault(
            option["supplier_id"],
            {
                "supplier_name": option["supplier_name"],
                "reliability": option["reliability"],
                "threshold": option["free_shipping_threshold_aud"],
                "shipping": option["typical_shipping_aud"],
                "lead": option["typical_lead_days"],
                "lines": [],
                "subtotal": Decimal(0),
            },
        )
        group["lines"].append(option)
        group["subtotal"] += option["cost_dec"]

    total = Decimal(0)
    leads, ratings = [], []
    suppliers_out = []
    for group in by_supplier.values():
        threshold = None if group["threshold"] is None else db.parse_qty(group["threshold"])
        shipping = Decimal(0)
        free_ship = threshold is not None and group["subtotal"] >= threshold
        if not free_ship and group["shipping"] is not None:
            shipping = db.parse_qty(group["shipping"])
        total += group["subtotal"] + shipping
        if group["lead"] is not None:
            leads.append(group["lead"])
        if group["reliability"] is not None:
            ratings.append(group["reliability"])
        suppliers_out.append(
            {
                "supplier_name": group["supplier_name"],
                "line_count": len(group["lines"]),
                "lines": group["lines"],
                "subtotal": _money(group["subtotal"]),
                "shipping": _money(shipping),
                "free_shipping": free_ship,
                "threshold": None if threshold is None else _money(threshold),
                "lead": group["lead"],
                "reliability": group["reliability"],
            }
        )
    suppliers_out.sort(key=lambda s: s["supplier_name"])
    lead_known_for_all = len(leads) == len(by_supplier) and bool(leads)
    return {
        "label": label,
        "suppliers": suppliers_out,
        "total": _money(total),
        "covered": len(assignment),
        "total_items": total_items,
        "supplier_count": len(by_supplier),
        "lead_days": max(leads) if leads else None,
        "lead_certain": lead_known_for_all,
        "mean_reliability": (
            round(sum(ratings) / len(ratings), 1) if ratings else None
        ),
        "rated_suppliers": len(ratings),
        "signature": tuple(
            sorted((item_id, opt["supplier_id"]) for item_id, opt in assignment.items())
        ),
    }


def candidate_baskets(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """2-3 candidates: cheapest total, fewest suppliers, fastest. Items
    without a priced option are uncovered — shown, never silently priced."""
    priceable = {
        e["id"]: [{**o, "item_name": e["name"]} for o in e["options"] if o.get("packs") is not None]
        for e in entries
    }
    priceable = {k: v for k, v in priceable.items() if v}
    total_items = len(entries)
    if not priceable:
        return []

    def cheapest() -> dict[str, dict[str, Any]]:
        return {
            item_id: min(options, key=lambda o: o["cost_dec"])
            for item_id, options in priceable.items()
        }

    def fewest() -> dict[str, dict[str, Any]]:
        remaining = set(priceable)
        assignment: dict[str, dict[str, Any]] = {}
        while remaining:
            coverage: dict[str, list[str]] = {}
            for item_id in remaining:
                for option in priceable[item_id]:
                    coverage.setdefault(option["supplier_id"], []).append(item_id)
            best_supplier = max(
                coverage,
                key=lambda sid: (
                    len(coverage[sid]),
                    -sum(
                        next(
                            o["cost_dec"]
                            for o in priceable[i]
                            if o["supplier_id"] == sid
                        )
                        for i in coverage[sid]
                    ),
                ),
            )
            for item_id in coverage[best_supplier]:
                assignment[item_id] = next(
                    o for o in priceable[item_id] if o["supplier_id"] == best_supplier
                )
                remaining.discard(item_id)
        return assignment

    def fastest() -> dict[str, dict[str, Any]]:
        return {
            item_id: min(
                options,
                key=lambda o: (
                    o["typical_lead_days"] if o["typical_lead_days"] is not None else 9999,
                    o["cost_dec"],
                ),
            )
            for item_id, options in priceable.items()
        }

    candidates = [
        _summarize("Cheapest total", cheapest(), total_items),
        _summarize("Fewest suppliers", fewest(), total_items),
        _summarize("Fastest", fastest(), total_items),
    ]
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        twin = next(
            (c for c in merged if c["signature"] == candidate["signature"]), None
        )
        if twin:
            twin["label"] += " · " + candidate["label"].lower()
        else:
            merged.append(candidate)
    return merged


# ---------------------------------------------------------------- routes


@router.get("/suppliers", response_class=HTMLResponse)
def suppliers_page() -> str:
    conn = db.connect()
    try:
        suppliers = [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name")]
    finally:
        conn.close()
    return ui_sourcing.suppliers_page(suppliers)


@router.post("/suppliers/seed")
def seed_suppliers() -> RedirectResponse:
    conn = db.connect()
    try:
        existing = {r["name"] for r in conn.execute("SELECT name FROM suppliers")}
    finally:
        conn.close()
    for seed in SEED_SUPPLIERS:
        if seed["name"] not in existing:
            store.create_supplier(**seed)
    return RedirectResponse(url="/suppliers", status_code=303)


@router.post("/suppliers/{supplier_id}")
def update_supplier(
    supplier_id: str,
    reliability: str = Form(""),
    free_shipping_threshold_aud: str = Form(""),
    typical_shipping_aud: str = Form(""),
    typical_lead_days: str = Form(""),
) -> RedirectResponse:
    fields: dict[str, Any] = {}
    if reliability.strip():
        rating = int(reliability)
        if not 1 <= rating <= 5:
            raise HTTPException(status_code=400, detail="reliability must be 1-5")
        fields["reliability"] = rating
    fields["free_shipping_threshold_aud"] = (
        db.qty_str(db.parse_qty(free_shipping_threshold_aud))
        if free_shipping_threshold_aud.strip()
        else None
    )
    fields["typical_shipping_aud"] = (
        db.qty_str(db.parse_qty(typical_shipping_aud))
        if typical_shipping_aud.strip()
        else None
    )
    fields["typical_lead_days"] = (
        int(typical_lead_days) if typical_lead_days.strip() else None
    )
    store.update_supplier(supplier_id, **fields)
    return RedirectResponse(url="/suppliers", status_code=303)


@router.get("/items/{item_id}/sourcing", response_class=HTMLResponse)
def item_sourcing(item_id: str) -> str:
    conn = db.connect()
    try:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
        links = [
            dict(r)
            for r in conn.execute(
                """SELECT l.*, s.name AS supplier_name FROM item_links l
                   JOIN suppliers s ON s.id = l.supplier_id
                   WHERE l.item_id = ? ORDER BY s.name""",
                (item_id,),
            )
        ]
        suppliers = [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name")]
    finally:
        conn.close()
    return ui_sourcing.item_sourcing_page(dict(item), links, suppliers)


@router.post("/items/{item_id}/links")
def add_link(
    item_id: str,
    supplier_id: str = Form(...),
    url: str = Form(...),
    sku: str = Form(""),
    pack_qty: str = Form("1"),
    last_price_aud: str = Form(""),
) -> RedirectResponse:
    try:
        pricing.validate_link_url(url.strip(), resolve=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.add_item_link(
        item_id,
        supplier_id,
        url.strip(),
        sku=sku.strip() or None,
        pack_qty=pack_qty.strip() or "1",
        last_price_aud=last_price_aud.strip() or None,
    )
    return RedirectResponse(url=f"/items/{item_id}/sourcing", status_code=303)


# ---------------------------------------------------------------- basket


@router.get("/reorder", response_class=HTMLResponse)
def reorder_page() -> str:
    conn = db.connect()
    try:
        entries = assemble_basket(conn)
        items = [
            dict(r) for r in conn.execute("SELECT id, name FROM items WHERE archived = 0 ORDER BY name")
        ]
    finally:
        conn.close()
    return ui_sourcing.basket_page(entries, candidate_baskets(entries), items)


@router.post("/reorder/add")
def basket_add(item_id: str = Form(...), qty: str = Form("1")) -> RedirectResponse:
    store.add_basket_item(item_id, qty.strip() or "1")
    return RedirectResponse(url="/reorder", status_code=303)


@router.post("/reorder/remove")
def basket_remove(item_id: str = Form(...)) -> RedirectResponse:
    store.remove_basket_item(item_id)
    return RedirectResponse(url="/reorder", status_code=303)


def run_pricing() -> tuple[int, int, list[str]]:
    """Price every basket item's links now. Returns (updated, total,
    stale_notes). Shared by the basket page and the command bar."""
    conn = db.connect()
    try:
        entries = assemble_basket(conn)
        links = []
        for entry in entries:
            for row in conn.execute(
                """SELECT l.item_id, l.supplier_id, l.url, s.name AS supplier_name
                   FROM item_links l JOIN suppliers s ON s.id = l.supplier_id
                   WHERE l.item_id = ?""",
                (entry["id"],),
            ):
                links.append(dict(row))
    finally:
        conn.close()

    results = pricing.price_links(links)
    stale: list[str] = []
    updated = 0
    for result in results:
        if result["outcome"] == "ok":
            store.record_link_price(
                result["item_id"], result["supplier_id"], result["price"]
            )
            updated += 1
        else:
            stale.append(f"{result['supplier_name']}: {result['detail']}")
    return updated, len(results), stale


@router.post("/reorder/price", response_class=HTMLResponse)
def price_basket() -> str:
    """ON DEMAND only. Fetch every basket item's links, update prices on
    success, fall back to the stored price (marked stale) on failure."""
    updated, total, stale = run_pricing()

    conn = db.connect()
    try:
        entries = assemble_basket(conn)
        items = [
            dict(r) for r in conn.execute("SELECT id, name FROM items WHERE archived = 0 ORDER BY name")
        ]
    finally:
        conn.close()
    notice = f"Priced {updated} of {total} link(s)."
    if stale:
        notice += f" {len(stale)} fell back to their stored price (marked stale)."
    return ui_sourcing.basket_page(
        entries, candidate_baskets(entries), items, notice=notice, stale_notes=stale
    )


# ---------------------------------------------------------------- orders


@router.get("/orders/receive", response_class=HTMLResponse)
def receive_order_form() -> str:
    conn = db.connect()
    try:
        suppliers = [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name")]
        items = [dict(r) for r in conn.execute("SELECT id, name, unit_type FROM items WHERE archived = 0 ORDER BY name")]
    finally:
        conn.close()
    return ui_sourcing.receive_order_page(suppliers, items)


@router.post("/orders/receive")
def receive_order(
    supplier_id: str = Form(...),
    reliability: str = Form(""),
    item_id: list[str] = Form([]),
    qty: list[str] = Form([]),
    unit_price: list[str] = Form([]),
) -> RedirectResponse:
    """Record a received order: sets last_paid_aud, applies the manual
    reliability rating, and emits qty_adjusted events for the stock."""
    conn = db.connect()
    try:
        supplier = conn.execute(
            "SELECT name FROM suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()
        if supplier is None:
            raise HTTPException(status_code=404, detail="supplier not found")
        manual_basket = {
            r["item_id"] for r in conn.execute("SELECT item_id FROM basket_items")
        }
    finally:
        conn.close()

    received = 0
    for line_item, line_qty, line_price in zip(item_id, qty, unit_price):
        if not line_item.strip() or not line_qty.strip():
            continue
        quantity = db.parse_qty(line_qty)
        if quantity <= 0:
            continue
        if line_price.strip():
            store.update_item(
                line_item, last_paid_aud=db.qty_str(db.parse_qty(line_price))
            )
        store.adjust_qty(
            line_item, quantity, f"order received: {supplier['name']}"
        )
        if line_item in manual_basket:
            store.remove_basket_item(line_item)
        received += 1
    if received == 0:
        raise HTTPException(status_code=400, detail="no order lines filled in")

    if reliability.strip():
        rating = int(reliability)
        if not 1 <= rating <= 5:
            raise HTTPException(status_code=400, detail="reliability must be 1-5")
        store.update_supplier(supplier_id, reliability=rating)
    return RedirectResponse(url="/reorder", status_code=303)
