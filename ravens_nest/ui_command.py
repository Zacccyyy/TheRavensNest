"""Rendering for the command bar, its result partials, the item card,
and the phone UI."""

from __future__ import annotations

from typing import Any

from .ui import _e, move_loc_fragment, page
from .ui_projects import _TABLE_STYLE

_COMMAND_STYLE = """<style>
#cmd { width: 100%; font-size: 1.15rem; padding: .6rem .8rem; border: 2px solid #1d4ed8;
       border-radius: 8px; box-sizing: border-box; }
#result { margin-top: .8rem; }
.result-item { display: block; padding: .45rem .6rem; border-radius: 6px; color: inherit;
               text-decoration: none; border-bottom: 1px solid #f1f5f9; }
.result-item .sub { color: #555; font-size: .85rem; }
.result-item.sel, .result-item:hover { background: #eff6ff; outline: 1px solid #bfdbfe; }
.hintbar { color: #666; font-size: .82rem; margin-top: .5rem; }
.hintbar code { background: #f1f5f9; padding: 0 .3rem; border-radius: 3px; }
.enterhint { color: #1d4ed8; padding: .4rem .6rem; background: #eff6ff; border-radius: 6px; }
.panel { border: 1px solid #cbd5e1; border-radius: 8px; padding: .7rem; margin-top: .5rem; }
</style>"""


def command_page(pending_captures: int) -> str:
    queue_note = (
        f' · <a href="/queue">{pending_captures} capture(s) to review</a>'
        if pending_captures
        else ""
    )
    body = f"""{_TABLE_STYLE}{_COMMAND_STYLE}
<h1>The Raven's Nest</h1>
<form id="cmd-form" hx-post="/command" hx-target="#result">
  <input id="cmd" name="q" autofocus autocomplete="off" spellcheck="false"
         placeholder="Search, or: move to A-2-3b · build X x2 · need 20 more … · recount A-2-3b · low · price basket"
         hx-get="/command" hx-trigger="input changed delay:250ms" hx-target="#result">
</form>
<div id="result"></div>
<p class="hintbar">
<code>3mm heat shrink</code> search ·
<code>A-2-3b</code> bin contents ·
<code>move to A-2-3b</code> ·
<code>build RPSRobot x2</code> ·
<code>need 20 more m3</code> ·
<code>recount A-2-3b</code> ·
<code>low</code> under-min ·
<code>price basket</code><br>
↑↓ select · Enter act · Esc clear ·
<a href="/m">phone UI</a>{queue_note}
</p>"""
    return page(
        "The Raven's Nest", body, scripts=("/static/command.js",)
    )


def note(text: str, error: bool = False) -> str:
    return f'<div class="note{" error" if error else ""}">{_e(text)}</div>'


def enter_hint(action: str) -> str:
    return f'<div class="enterhint">⏎ Press Enter to {_e(action)}</div>'


def search_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return note(f"No items match “{query}”.")
    rows = "".join(
        f'<a class="result-item" href="/items/{_e(r["id"])}">'
        f'<strong>{_e(r["name"])}</strong>'
        f'{" · " + _e(r["part_number"]) if r["part_number"] else ""}'
        f'<div class="sub">{_e(r["summary"])}</div></a>'
        for r in results
    )
    return f"<div>{rows}</div>"


def bin_view(location_id: str, location: dict | None, items: list[dict[str, Any]]) -> str:
    desc = f" — {_e(location['description'])}" if location and location["description"] else ""
    known = "" if location else ' <span class="flag">(no location record)</span>'
    if items:
        rows = "".join(
            f'<a class="result-item" href="/items/{_e(i["id"])}">'
            f'<strong>{_e(i["name"])}</strong>'
            f'<div class="sub">{_e(i["qty_on_hand"])} {_e(i["unit_type"])} on hand, '
            f'{_e(i["free"])} free</div></a>'
            for i in items
        )
        contents = f"<div>{rows}</div>"
    else:
        contents = '<p class="count">Empty bin — free space.</p>'
    return f"<h2>{_e(location_id)}{desc}{known}</h2>{contents}"


def low_view(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return note("Nothing is under its minimum. 🎉")
    body = "".join(
        f'<a class="result-item" href="/items/{_e(r["id"])}">'
        f'<strong>{_e(r["name"])}</strong>'
        f'<div class="sub">{_e(r["free"])} free of min {_e(r["min"])} {_e(r["unit_type"])}'
        f'{" · " + _e(r["location_id"]) if r["location_id"] else ""}</div></a>'
        for r in rows
    )
    return f"<h2>Under minimum ({len(rows)})</h2><div>{body}</div>" + (
        '<p class="count"><a href="/reorder">Open the reorder basket</a></p>'
    )


def need_done(name: str, added: str, total: str) -> str:
    return note(
        f"Added {added} × {name} to the reorder basket (manual total now {total})."
    ) + '<p class="count"><a href="/reorder">Open the reorder basket</a></p>'


def need_ask(candidates: list[dict[str, Any]], qty: str, query: str) -> str:
    buttons = "".join(
        f'<form class="inline" hx-post="/command/need" hx-target="#result">'
        f'<input type="hidden" name="item_id" value="{_e(c["id"])}">'
        f'<input type="hidden" name="qty" value="{_e(qty)}">'
        f'<button type="submit">{_e(c["name"])} (score {c["score"]})</button></form> '
        for c in candidates
    )
    return f"""<div class="panel">Several items match “{_e(query)}” — which one needs {_e(qty)} more?
<div>{buttons}</div></div>"""


def build_ask(query: str, candidates: list[dict[str, Any]], count: int) -> str:
    if not candidates:
        return note(f"No project matches “{query}”.", error=True)
    buttons = "".join(
        f'<form class="inline" hx-post="/command" hx-target="#result">'
        f'<input type="hidden" name="q" value="build {_e(c["name"])} x{count}">'
        f'<button type="submit">{_e(c["name"])} (score {c["score"]})</button></form> '
        for c in candidates
    )
    return f"""<div class="panel">Which project did you mean by “{_e(query)}”?
<div>{buttons}</div></div>"""


def build_panel(
    project: dict[str, Any],
    count: int,
    need_rows: list[dict[str, Any]],
    shortages: list[dict[str, Any]],
    unmatched: int,
) -> str:
    needs = "".join(
        f"<tr><td>{_e(r['name'])}</td><td class='num'>{_e(r['need'])}</td>"
        f"<td class='num'>{_e(r['on_hand'])}</td></tr>"
        for r in need_rows
    )
    table = (
        f"<table><tr><th>Item</th><th class='num'>Needed</th><th class='num'>On hand</th></tr>{needs}</table>"
        if need_rows
        else "<p class='count'>No matched BOM lines.</p>"
    )
    blockers = ""
    if unmatched:
        blockers += note(f"{unmatched} unresolved BOM line(s) — resolve on the project page first.", error=True)
    if shortages:
        detail = "; ".join(
            f"{s['name']}: short {s['short']} {s['unit_type']}" for s in shortages
        )
        blockers += note(f"Short on: {detail}", error=True)
    confirm = (
        f"""<form hx-post="/command/build" hx-target="#result">
  <input type="hidden" name="project_id" value="{_e(project["id"])}">
  <input type="hidden" name="count" value="{count}">
  <button type="submit" class="primary">Confirm build ×{count}</button>
</form>"""
        if not shortages and not unmatched and need_rows
        else ""
    )
    return f"""<div class="panel"><h2>Build {_e(project["name"])} ×{count}</h2>
{table}{blockers}{confirm}
<p class="count"><a href="/projects/{_e(project["id"])}">Open project page</a></p></div>"""


def recount_form(location_id: str, items: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""<div class="row"><label>{_e(i["name"])}</label>
<input type="hidden" name="item_id" value="{_e(i["id"])}">
<input name="counted" inputmode="decimal" value="{_e(i["qty_on_hand"])}">
<span class="count">{_e(i["unit_type"])} (recorded {_e(i["qty_on_hand"])})</span></div>"""
        for i in items
    )
    return f"""<div class="panel"><h2>Recount {_e(location_id)}</h2>
<p class="count">Enter what's physically there — unchanged counts emit nothing.</p>
<form hx-post="/command/recount" hx-target="#result">
  <input type="hidden" name="location_id" value="{_e(location_id)}">
  {rows}
  <div class="actions"><button type="submit" class="primary">Apply recount</button></div>
</form></div>"""


def move_panel(location_id: str, description: str) -> str:
    return f"""<div class="panel"><h2>Move items → {_e(location_id)}</h2>
{move_loc_fragment(location_id, description)}
<form hx-post="/move/scan" hx-target="#move-scan-result" hx-include="#loc"
      hx-on::after-request="this.reset()">
  <div class="row">
    <input name="code" autofocus autocomplete="off"
           placeholder="Scan or type an item code — exact matches move immediately">
    <button type="submit" class="primary">Go</button>
  </div>
</form>
<div id="move-scan-result"></div>
<ul id="log"></ul>
<p class="count"><a href="/move">Full move console (with camera)</a></p></div>"""


def price_done(updated: int, total: int, stale: list[str]) -> str:
    body = note(f"Priced {updated} of {total} link(s).")
    if stale:
        body += "".join(f'<div class="stale">stale: {_e(s)}</div>' for s in stale)
    body += '<p class="count"><a href="/reorder">Open the basket for candidate orders</a></p>'
    return body


# --------------------------------------------------------------- item card


def item_card(
    item: dict[str, Any],
    free: str,
    reserved: str,
    reservations: list[dict[str, Any]],
    links: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> str:
    photo = (
        f'<img class="photo" src="/assets/{_e(item["photo_hash"])}.jpg" alt="item photo">'
        if item["photo_hash"]
        else ""
    )
    fields = "".join(
        f"<tr><th>{label}</th><td>{_e(value) if value is not None else '—'}</td></tr>"
        for label, value in (
            ("Part number", item["part_number"]),
            ("Description", item["description"] or None),
            ("Unit type", item["unit_type"]),
            ("Location", item["location_id"]),
            ("On hand", item["qty_on_hand"]),
            ("Free", free),
            ("Reserved", reserved if reserved != "0" else None),
            ("Min qty", item["min_qty"]),
            ("Last paid AUD", item["last_paid_aud"]),
            ("Created", item["created_ts"][:10]),
            ("Updated", item["updated_ts"][:10]),
        )
    )
    reservation_rows = (
        "<h2>Reservations</h2><table>"
        + "".join(
            f"<tr><td>{_e(r['project_name'] or 'unknown project')}</td>"
            f"<td class='num'>{_e(r['qty'])}</td></tr>"
            for r in reservations
        )
        + "</table>"
        if reservations
        else ""
    )
    link_rows = (
        "<h2>Supplier links</h2><table><tr><th>Supplier</th><th>Product</th>"
        "<th class='num'>Pack</th><th class='num'>Price AUD</th><th>Checked</th></tr>"
        + "".join(
            f"<tr><td>{_e(l['supplier_name'])}</td>"
            f"<td><a href=\"{_e(l['url'])}\" rel=\"noopener\">{_e(l['sku'] or 'link')}</a></td>"
            f"<td class='num'>{_e(l['pack_qty'])}</td>"
            f"<td class='num'>{_e(l['last_price_aud']) if l['last_price_aud'] else '—'}</td>"
            f"<td>{_e(l['last_checked_ts'][:10]) if l['last_checked_ts'] else 'never'}</td></tr>"
            for l in links
        )
        + "</table>"
        if links
        else '<p class="count">No supplier links — <a href="/items/'
        + _e(item["id"])
        + '/sourcing">add one</a>.</p>'
    )
    history_rows = "".join(
        f"<tr><td>{_e(h['ts'][:16].replace('T', ' '))}</td>"
        f"<td>{_e(h['type'])}</td><td>{_e(_history_detail(h))}</td></tr>"
        for h in history
    )
    body = f"""{_TABLE_STYLE}
<p><a href="/">← command bar</a></p>
<h1>{_e(item["name"])}</h1>
{photo}
<table>{fields}</table>
{reservation_rows}
{link_rows}
<p><a href="/items/{_e(item["id"])}/sourcing">Manage sourcing links</a></p>
<h2>History</h2>
<table><tr><th>When</th><th>Event</th><th>Detail</th></tr>{history_rows}</table>"""
    return page(f"The Raven's Nest — {item['name']}", body)


def _history_detail(entry: dict[str, Any]) -> str:
    payload = entry["payload"]
    kind = entry["type"]
    if kind == "item.qty_adjusted":
        delta = payload.get("delta", "?")
        sign = "" if str(delta).startswith("-") else "+"
        return f"{sign}{delta} ({payload.get('reason', '')})"
    if kind == "item.recounted":
        return f"recounted to {payload.get('qty')} (correction {payload.get('delta')})"
    if kind == "item.moved":
        return f"moved to {payload.get('location_id')}"
    if kind == "item.created":
        return f"created with {payload.get('qty_on_hand', '0')}"
    if kind == "item.updated":
        changed = [k for k in payload if k != "id"]
        return "updated " + ", ".join(changed)
    if kind == "reservation.created":
        return f"reserved {payload.get('qty')}"
    if kind == "build.executed":
        qty = next(
            (c["qty"] for c in payload.get("consumed", []) if c.get("item_id")),
            "?",
        )
        return f"build consumed {qty}"
    if kind == "build.reversed":
        qty = next(
            (c["qty"] for c in payload.get("returned", []) if c.get("item_id")),
            "?",
        )
        return f"un-build returned {qty}"
    if kind == "item.link_added":
        return "supplier link added"
    if kind == "item.link_price_checked":
        return f"price check: {payload.get('price_aud')} AUD"
    if kind == "bom.line_matched":
        return f"matched to BOM line {payload.get('line_no')} ({payload.get('method')})"
    return ""


# --------------------------------------------------------------- phone UI


_MOBILE_STYLE = """<style>
body { font-family: system-ui, sans-serif; margin: 0; padding: .8rem;
       display: flex; flex-direction: column; min-height: 96vh; }
.m-top { flex: 1; }
.m-search { width: 100%; font-size: 1.1rem; padding: .7rem; box-sizing: border-box;
            border: 2px solid #ccc; border-radius: 10px; }
.m-result .result-item { display: block; padding: .6rem .4rem; border-bottom: 1px solid #eee;
                         color: inherit; text-decoration: none; }
.m-result .sub { color: #555; font-size: .9rem; }
.m-status { color: #b45309; font-size: .9rem; min-height: 1.2rem; margin: .4rem 0; }
.m-actions { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; padding-bottom: .6rem; }
.m-btn { display: flex; align-items: center; justify-content: center; font-size: 1.25rem;
         padding: 1.1rem 0; border-radius: 14px; border: none; color: white; }
.m-capture { background: #1d4ed8; grid-column: span 2; font-size: 1.5rem; padding: 1.4rem 0; }
.m-scan { background: #047857; grid-column: span 2; }
h2 { font-size: 1rem; }
</style>"""


def mobile_page(pending_captures: int) -> str:
    queue_line = (
        f'<a href="/queue">{pending_captures} capture(s) waiting for review</a>'
        if pending_captures
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>Raven's Nest</title>
  <script src="/static/htmx.min.js"></script>
  {_MOBILE_STYLE}
</head>
<body>
<div class="m-top">
  <input class="m-search" name="q" placeholder="Search…" autocomplete="off"
         hx-get="/command" hx-trigger="input changed delay:300ms" hx-target="#m-result">
  <div class="m-status" id="m-status"></div>
  <div class="m-result" id="m-result"></div>
  <p class="sub">{queue_line}</p>
</div>
<div class="m-actions">
  <button class="m-btn m-capture" id="m-capture-btn">&#128247; Capture item</button>
  <button class="m-btn m-scan" id="m-scan-btn">&#9635; Scan location label</button>
</div>
<input id="m-capture-file" type="file" accept="image/jpeg" capture="environment" hidden>
<input id="m-scan-file" type="file" accept="image/*" capture="environment" hidden>
<canvas id="m-canvas" hidden></canvas>
<script src="/static/jsQR.js"></script>
<script src="/static/mobile.js"></script>
</body>
</html>"""
