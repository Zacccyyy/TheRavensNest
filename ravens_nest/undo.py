"""Universal undo: every inverse is a compensating event, never a
deletion. The undo stack is derived from the event log, filtered to THIS
actor (hostname) — one machine never blind-undoes another's work.
Compensating events are ordinary events, so undoing an undo redoes, and
everything stays replay-deterministic.

Where an inverse is genuinely unsafe (the state moved on since), we
refuse with an explanation and point at the manual path.
"""

from __future__ import annotations

import socket
from decimal import Decimal
from typing import Any

from . import db, history, store

STACK_SIZE = 20

UNDOABLE = {
    "item.created",
    "item.updated",
    "item.qty_adjusted",
    "item.moved",
    "item.recounted",
    "item.archived",
    "item.unarchived",
    "item.merged",
    "item.unmerged",
    "build.executed",
    "build.reversed",
    "reservation.created",
    "reservation.released",
    "basket.item_added",
    "basket.item_removed",
}

NOT_UNDOABLE_REASONS = {
    "bom.imported": "re-import the previous CSV revision instead — the log keeps every revision",
    "bom.line_matched": "aliases are additive; match the line to a different item on the project page",
    "item.alias_added": "aliases are additive and harmless; a wrong one is out-scored by better matches",
    "item.link_added": "edit the link on the item's sourcing page instead",
    "item.link_price_checked": "run 'price basket' again for a fresh price",
    "location.created": "an unused location record is harmless; it just sits empty",
    "supplier.created": "unused suppliers are harmless; edit them on /suppliers",
    "supplier.updated": "set the field back on /suppliers",
    "project.created": "an empty project is harmless; it holds no stock",
}


def actor() -> str:
    return socket.gethostname()


def undo_stack(limit: int = STACK_SIZE) -> list[dict[str, Any]]:
    """The last N undoable events by THIS actor, newest first."""
    me = actor()
    log = history.load_log()
    mine = [e for e in log if e.get("actor") == me and e["type"] in UNDOABLE]
    return mine[::-1][:limit]


def find_event(event_id: str) -> dict[str, Any] | None:
    for event in history.load_log():
        if event["id"] == event_id:
            return event
    return None


def _item_row(conn, item_id: str):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def _refuse(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "emits": []}


def _plan(description: str, emits: list[tuple[str, dict]]) -> dict[str, Any]:
    return {"ok": True, "description": description, "emits": emits}


def _field_before(log: list[dict], item_id: str, field: str, event: dict) -> Any:
    """The value a field held immediately before `event` (from the log)."""
    key = (event["ts"], event["id"])
    value = None
    for candidate in log:
        if (candidate["ts"], candidate["id"]) >= key:
            break
        p = candidate["payload"]
        if candidate["type"] == "item.created" and p.get("id") == item_id:
            value = p.get(field)
        elif candidate["type"] == "item.updated" and p.get("id") == item_id and field in p:
            value = p[field]
    return value


def _basket_qty_before(log: list[dict], item_id: str, event: dict) -> str | None:
    """Manual basket qty immediately before `event` (None = not in basket)."""
    key = (event["ts"], event["id"])
    qty = None
    for candidate in log:
        if (candidate["ts"], candidate["id"]) >= key:
            break
        p = candidate["payload"]
        if candidate["type"] == "basket.item_added" and p.get("item_id") == item_id:
            qty = p["qty"]
        elif candidate["type"] == "basket.item_removed" and p.get("item_id") == item_id:
            qty = None
    return qty


def compute_inverse(event: dict[str, Any], conn) -> dict[str, Any]:
    """Build the compensating event(s) for one event, or a refusal that
    explains the conflict and the manual path."""
    kind = event["type"]
    p = event["payload"]
    if kind in NOT_UNDOABLE_REASONS:
        return _refuse(f"'{kind}' has no automatic inverse — {NOT_UNDOABLE_REASONS[kind]}.")
    if kind not in UNDOABLE:
        return _refuse(f"'{kind}' has no automatic inverse.")

    log = history.load_log()

    if kind == "item.created":
        return _plan(
            "archive the item (creation is never deleted — the photo and history stay)",
            [("item.archived", {"id": p["id"], "reason": "undo of creation"})],
        )

    if kind == "item.archived":
        return _plan("unarchive the item", [("item.unarchived", {"id": p["id"]})])

    if kind == "item.unarchived":
        return _plan(
            "re-archive the item",
            [("item.archived", {"id": p["id"], "reason": "undo of unarchive"})],
        )

    if kind == "item.qty_adjusted":
        inverse = db.qty_str(-db.parse_qty(p["delta"]))
        return _plan(
            f"adjust quantity by {inverse}",
            [
                (
                    "item.qty_adjusted",
                    {
                        "item_id": p["item_id"],
                        "delta": inverse,
                        "reason": f"undo: {p.get('reason', '')}",
                    },
                )
            ],
        )

    if kind == "item.recounted":
        row = _item_row(conn, p["item_id"])
        if row is None:
            return _refuse("the item no longer exists in the cache")
        if db.parse_qty(row["qty_on_hand"]) != db.parse_qty(p["qty"]):
            return _refuse(
                f"quantity has changed since this recount (now {row['qty_on_hand']}, "
                f"the recount set {p['qty']}) — undoing would clobber later changes. "
                f"Run 'recount' again with the right number instead."
            )
        prior = db.qty_str(db.parse_qty(p["qty"]) - db.parse_qty(p.get("delta", "0")))
        return _plan(
            f"recount back to {prior}",
            [
                (
                    "item.recounted",
                    {
                        "item_id": p["item_id"],
                        "qty": prior,
                        "delta": db.qty_str(-db.parse_qty(p.get("delta", "0"))),
                        "reason": "undo of recount",
                    },
                )
            ],
        )

    if kind == "item.moved":
        row = _item_row(conn, p["item_id"])
        if row is None:
            return _refuse("the item no longer exists in the cache")
        if row["location_id"] != p["location_id"]:
            return _refuse(
                f"the item has been moved again since (now in "
                f"{row['location_id'] or 'no location'}) — undoing would clobber that move. "
                f"Use 'move to <bin>' to put it where it belongs."
            )
        before = history.location_before(log, p["item_id"], event)
        if before is None:
            return _refuse(
                "the item had no location before this move — set one with 'move to <bin>'"
            )
        return _plan(
            f"move back to {before}",
            [("item.moved", {"item_id": p["item_id"], "location_id": before})],
        )

    if kind == "item.updated":
        row = _item_row(conn, p["id"])
        if row is None:
            return _refuse("the item no longer exists in the cache")
        fields = [k for k in p if k != "id"]
        restore = {}
        for field in fields:
            if field in row.keys() and row[field] != p[field]:
                return _refuse(
                    f"'{field}' has changed again since this update — edit the item directly instead"
                )
            restore[field] = _field_before(log, p["id"], field, event)
        return _plan(
            "restore previous field values: " + ", ".join(fields),
            [("item.updated", {"id": p["id"], **restore})],
        )

    if kind == "build.executed":
        from . import bom

        if bom.net_builds(conn, p["project_id"]) < p["count"]:
            return _refuse(
                "this build has already been reversed (net builds are fewer than its count)"
            )
        return _plan(
            f"un-build ×{p['count']}, returning the consumed stock",
            [
                (
                    "build.reversed",
                    {
                        "id": _new_id(),
                        "project_id": p["project_id"],
                        "count": p["count"],
                        "returned": p.get("consumed", []),
                    },
                )
            ],
        )

    if kind == "build.reversed":
        short = []
        for entry in p.get("returned", []):
            row = _item_row(conn, entry["item_id"])
            if row is None or db.parse_qty(row["qty_on_hand"]) < db.parse_qty(entry["qty"]):
                short.append(row["name"] if row else entry["item_id"])
        if short:
            return _refuse(
                "re-running this build would need stock that is no longer there: "
                + ", ".join(short)
                + ". Adjust quantities first if you really want to redo it."
            )
        return _plan(
            f"re-run the build ×{p['count']}",
            [
                (
                    "build.executed",
                    {
                        "id": _new_id(),
                        "project_id": p["project_id"],
                        "count": p["count"],
                        "consumed": p.get("returned", []),
                    },
                )
            ],
        )

    if kind == "reservation.created":
        row = conn.execute(
            "SELECT status FROM reservations WHERE id = ?", (p["id"],)
        ).fetchone()
        if row is None or row["status"] != "active":
            return _refuse("this reservation has already been released")
        return _plan("release the reservation", [("reservation.released", {"id": p["id"]})])

    if kind == "reservation.released":
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (p["id"],)
        ).fetchone()
        if row is None:
            return _refuse("the reservation is unknown to the cache")
        if row["status"] == "active":
            return _refuse("the reservation is already active again")
        return _plan(
            f"re-create the reservation ({row['qty']})",
            [
                (
                    "reservation.created",
                    {
                        "id": _new_id(),
                        "project_id": row["project_id"],
                        "item_id": row["item_id"],
                        "qty": row["qty"],
                    },
                )
            ],
        )

    if kind == "basket.item_added":
        row = conn.execute(
            "SELECT qty FROM basket_items WHERE item_id = ?", (p["item_id"],)
        ).fetchone()
        if row is None or db.parse_qty(row["qty"]) != db.parse_qty(p["qty"]):
            return _refuse(
                "the basket entry has changed since — edit it on /reorder instead"
            )
        prior = _basket_qty_before(log, p["item_id"], event)
        if prior is None:
            return _plan(
                "remove the basket entry",
                [("basket.item_removed", {"item_id": p["item_id"]})],
            )
        return _plan(
            f"restore the previous basket quantity ({prior})",
            [("basket.item_added", {"item_id": p["item_id"], "qty": prior})],
        )

    if kind == "basket.item_removed":
        row = conn.execute(
            "SELECT 1 FROM basket_items WHERE item_id = ?", (p["item_id"],)
        ).fetchone()
        if row is not None:
            return _refuse("the item is back in the basket already")
        prior = _basket_qty_before(log, p["item_id"], event)
        if prior is None:
            return _refuse("no earlier basket quantity is recorded for this item")
        return _plan(
            f"re-add to the basket (qty {prior})",
            [("basket.item_added", {"item_id": p["item_id"], "qty": prior})],
        )

    if kind == "item.merged":
        source = _item_row(conn, p["source_id"])
        target = _item_row(conn, p["target_id"])
        if source is None or target is None:
            return _refuse("one of the merged items is unknown to the cache")
        if not source["archived"]:
            return _refuse("the merge source has been unarchived since — un-merge by hand")
        if db.parse_qty(target["qty_on_hand"]) < db.parse_qty(p["qty"]):
            return _refuse(
                f"the target now holds less ({target['qty_on_hand']}) than the merge "
                f"transferred ({p['qty']}) — stock has been consumed since. Adjust "
                f"quantities manually if you need to split them."
            )
        return _plan("un-merge (restore both items as they were)", [("item.unmerged", p)])

    if kind == "item.unmerged":
        return _plan("re-apply the merge", [("item.merged", p)])

    return _refuse(f"'{kind}' has no automatic inverse.")


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def perform_undo(event_id: str) -> tuple[bool, str]:
    """Undo one event by id. Returns (ok, message)."""
    event = find_event(event_id)
    if event is None:
        return False, "That event is not in the log."
    if event.get("actor") != actor():
        return False, (
            f"That was done by '{event.get('actor')}', not this machine — undo it "
            f"there, or apply the inverse manually so it's a deliberate choice."
        )
    conn = db.connect()
    try:
        plan = compute_inverse(event, conn)
    finally:
        conn.close()
    if not plan["ok"]:
        return False, plan["reason"]
    for event_type, payload in plan["emits"]:
        store.record_event(event_type, payload)
    return True, f"Undone — {plan['description']}."
