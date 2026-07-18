"""Narrated history from the event log.

The log already holds everything — this module turns it into
human-readable streams: per item (following merge chains), per bin
(what entered and left), and per project (builds and reservations).
It also derives prior values (location before a move, quantity before
a recount) that the undo engine needs to build compensating events.
"""

from __future__ import annotations

from typing import Any

from . import db, events

PAGE_SIZE = 20


def _project_names(conn) -> dict[str, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects")}


def _item_names(conn) -> dict[str, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM items")}


def merged_sources(all_events: list[dict], item_id: str) -> list[str]:
    """Items that were merged INTO item_id (so their history is readable
    from the target), transitively."""
    sources, frontier = [], [item_id]
    while frontier:
        current = frontier.pop()
        for event in all_events:
            if event["type"] == "item.merged" and event["payload"].get("target_id") == current:
                source = event["payload"]["source_id"]
                if source not in sources:
                    sources.append(source)
                    frontier.append(source)
    return sources


def _touches_item(event: dict, ids: set[str]) -> bool:
    p = event["payload"]
    if event["type"].startswith("item.") and p.get("id") in ids:
        return True
    if p.get("item_id") in ids:
        return True
    if p.get("source_id") in ids or p.get("target_id") in ids:
        return True
    return any(
        entry.get("item_id") in ids
        for entry in p.get("consumed", []) + p.get("returned", [])
    )


def narrate(event: dict, context: dict[str, Any]) -> str:
    """One human-readable line for an event. Never raises: a payload
    shape this narrator doesn't understand (new event type, evolved
    payload) falls back to a generic line rather than breaking every
    history view (audit item 10b)."""
    try:
        return _narrate(event, context)
    except Exception:
        return f"{event.get('type', 'unknown')} event (no narration available)"


def _narrate(event: dict, context: dict[str, Any]) -> str:
    """context: item_names, project_names, location_before (may be None)."""
    p = event["payload"]
    kind = event["type"]
    item_names = context.get("item_names", {})
    project_names = context.get("project_names", {})

    def item_name(item_id):
        return item_names.get(item_id, "unknown item")

    def project_name(project_id):
        return project_names.get(project_id, "unknown project")

    if kind == "item.created":
        where = f" in {p['location_id']}" if p.get("location_id") else ""
        return f"Created with {p.get('qty_on_hand', '0')} {p.get('unit_type', '')}{where}"
    if kind == "item.moved":
        before = context.get("location_before")
        arrow = f"{before} → {p['location_id']}" if before else f"→ {p['location_id']}"
        return f"Moved {arrow}"
    if kind == "item.qty_adjusted":
        delta = str(p.get("delta", "?"))
        sign = "" if delta.startswith("-") else "+"
        reason = f", reason: '{p['reason']}'" if p.get("reason") else ""
        return f"Adjusted {sign}{delta}{reason}"
    if kind == "item.recounted":
        qty, delta = str(p.get("qty", "?")), str(p.get("delta", "0"))
        prior = db.qty_str(db.parse_qty(qty) - db.parse_qty(delta)) if qty != "?" else "?"
        sign = "" if delta.startswith("-") else "+"
        return f"Recounted {prior} → {qty} (correction {sign}{delta})"
    if kind == "item.updated":
        changed = ", ".join(k for k in p if k != "id")
        return f"Updated {changed}"
    if kind == "item.archived":
        reason = f" — {p['reason']}" if p.get("reason") else ""
        return f"Archived{reason}"
    if kind == "item.unarchived":
        return "Unarchived"
    if kind == "item.alias_added":
        return f"Alias added: '{p['alias_text']}'"
    if kind == "item.merged":
        return (
            f"Merged '{item_name(p['source_id'])}' into '{item_name(p['target_id'])}'"
            f" (+{p.get('qty', '0')})"
        )
    if kind == "item.unmerged":
        return f"Un-merged '{item_name(p['source_id'])}' back out of '{item_name(p['target_id'])}'"
    if kind == "item.link_added":
        return "Supplier link added"
    if kind == "item.link_price_checked":
        return f"Price check: {p.get('price_aud')} AUD"
    if kind == "reservation.created":
        return f"Reserved {p.get('qty')} for {project_name(p.get('project_id'))}"
    if kind == "reservation.released":
        return "Reservation released"
    if kind == "build.executed":
        per_item = context.get("focus_item")
        if per_item:
            qty = next(
                (c["qty"] for c in p.get("consumed", []) if c.get("item_id") == per_item),
                None,
            )
            if qty is not None:
                return f"Consumed {qty} by build {project_name(p['project_id'])} ×{p['count']}"
        return f"Built ×{p['count']}"
    if kind == "build.reversed":
        per_item = context.get("focus_item")
        if per_item:
            qty = next(
                (c["qty"] for c in p.get("returned", []) if c.get("item_id") == per_item),
                None,
            )
            if qty is not None:
                return f"Returned {qty} by un-build {project_name(p['project_id'])} ×{p['count']}"
        return f"Un-built ×{p['count']}"
    if kind == "bom.imported":
        return f"BOM imported ({len(p.get('lines', []))} lines)"
    if kind == "bom.line_matched":
        return f"Matched to BOM line {p.get('line_no')} ({p.get('method')})"
    if kind == "basket.item_added":
        return f"Added to reorder basket (qty {p.get('qty')})"
    if kind == "basket.item_removed":
        return "Removed from reorder basket"
    if kind == "project.created":
        return f"Project created: {p.get('name')}"
    if kind == "location.created":
        return f"Location registered: {p.get('id')}"
    return kind


def _location_track(all_events: list[dict]) -> dict[str, list[tuple[str, str, str | None]]]:
    """Per item: chronological [(ts, event_id, location)] transitions."""
    track: dict[str, list[tuple[str, str, str | None]]] = {}
    for event in all_events:
        p = event["payload"]
        kind = event["type"]
        if kind == "item.created":
            track.setdefault(p["id"], []).append((event["ts"], event["id"], p.get("location_id")))
        elif kind == "item.moved":
            track.setdefault(p["item_id"], []).append((event["ts"], event["id"], p["location_id"]))
        elif kind == "item.merged":
            track.setdefault(p["target_id"], []).append((event["ts"], event["id"], p.get("location_id")))
        elif kind == "item.unmerged":
            track.setdefault(p["target_id"], []).append(
                (event["ts"], event["id"], p.get("target_prev_location"))
            )
            track.setdefault(p["source_id"], []).append(
                (event["ts"], event["id"], p.get("source_prev_location"))
            )
    return track


def location_before(all_events: list[dict], item_id: str, event: dict) -> str | None:
    """The item's location immediately before the given event."""
    key = (event["ts"], event["id"])
    last = None
    for ts, event_id, location in _location_track(all_events).get(item_id, []):
        if (ts, event_id) >= key:
            break
        last = location
    return last


def item_events(
    all_events: list[dict], item_id: str, include_merged_sources: bool = True
) -> list[dict]:
    ids = {item_id}
    if include_merged_sources:
        ids.update(merged_sources(all_events, item_id))
    return [e for e in all_events if _touches_item(e, ids)]


def bin_events(all_events: list[dict], location_id: str) -> list[dict]:
    """Everything that entered or left the location: creations here,
    moves in, moves out (derived by tracking each item's location)."""
    hits = []
    current: dict[str, str | None] = {}
    for event in all_events:
        p = event["payload"]
        kind = event["type"]
        if kind == "item.created":
            if p.get("location_id") == location_id:
                hits.append({**event, "_bin_note": "arrived (created here)"})
            current[p["id"]] = p.get("location_id")
        elif kind == "item.moved":
            item_id = p["item_id"]
            before = current.get(item_id)
            if p["location_id"] == location_id:
                hits.append({**event, "_bin_note": f"arrived from {before or 'unassigned'}"})
            elif before == location_id:
                hits.append({**event, "_bin_note": f"left for {p['location_id']}"})
            current[item_id] = p["location_id"]
        elif kind == "item.merged":
            current[p["target_id"]] = p.get("location_id")
        elif kind == "location.created" and p.get("id") == location_id:
            hits.append({**event, "_bin_note": "location registered"})
    return hits


def project_events(all_events: list[dict], project_id: str) -> list[dict]:
    return [
        e
        for e in all_events
        if e["payload"].get("project_id") == project_id
        or (e["type"] == "project.created" and e["payload"].get("id") == project_id)
    ]


def build_entries(
    conn,
    raw_events: list[dict],
    all_events: list[dict],
    focus_item: str | None = None,
    type_filter: str | None = None,
    page: int = 1,
) -> dict[str, Any]:
    """Newest-first narrated page of events with actor + timestamp."""
    context = {
        "item_names": _item_names(conn),
        "project_names": _project_names(conn),
        "focus_item": focus_item,
    }
    selected = [e for e in raw_events if not type_filter or e["type"] == type_filter]
    selected = selected[::-1]  # newest first
    total = len(selected)
    start = (page - 1) * PAGE_SIZE
    entries = []
    for event in selected[start : start + PAGE_SIZE]:
        ctx = dict(context)
        if event["type"] == "item.moved":
            ctx["location_before"] = location_before(
                all_events, event["payload"]["item_id"], event
            )
        if focus_item is None and event["type"].startswith("item."):
            subject = event["payload"].get("id") or event["payload"].get("item_id")
            ctx["focus_item"] = subject
        entries.append(
            {
                "ts": event["ts"],
                "actor": event.get("actor", "?"),
                "type": event["type"],
                "text": narrate(event, ctx),
                "note": event.get("_bin_note"),
                "item_id": event["payload"].get("item_id") or event["payload"].get("id"),
            }
        )
    types = sorted({e["type"] for e in raw_events})
    return {
        "entries": entries,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "types": types,
        "type_filter": type_filter,
    }


def load_log() -> list[dict]:
    return events.read_all_events()
