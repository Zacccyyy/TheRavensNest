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
.undo-btn { padding: .1rem .5rem; font-size: .8rem; }
.zeroed { opacity: .55; }
.histrow td { font-size: .85rem; }
.chips a { display: inline-block; background: #f1f5f9; border-radius: 10px;
           padding: .1rem .5rem; margin: .1rem; font-size: .78rem; text-decoration: none; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; border: 1px solid #cbd5e1;
        border-radius: 8px; padding: .6rem; margin: .5rem 0; }
.scorebig { font-size: 2.2rem; font-weight: 700; }
</style>"""


PLACEHOLDER_EXAMPLES = [
    "Try: 3mm heat shrink — search your inventory",
    "Try: move to A-2-3b — then scan items into that bin",
    "Try: build RPSRobot x2 — checks stock, then confirms",
    "Try: need 20 more m3 screws — adds to the reorder basket",
    "Try: recount A-2-3b — count a bin, corrections only",
    "Try: help — every command, with examples",
    "Try: health — how trustworthy is my data?",
    "Try: undo — reverse your last action",
]


def command_page(pending_captures: int, show_setup: bool = False) -> str:
    queue_note = (
        f' · <a href="/queue">{pending_captures} capture(s) to review</a>'
        if pending_captures
        else ""
    )
    setup_banner = ""
    if show_setup:
        setup_banner = """<div class="note">👋 Looks like a fresh install.
<a href="/setup"><strong>Run the guided setup</strong></a> — describe your shelving,
print bin labels, pick your suppliers, and import a CSV if you have one.
Or just type <code>help</code> below and dive in.</div>"""
    import json as _json

    examples_attr = _e(_json.dumps(PLACEHOLDER_EXAMPLES))
    body = f"""{_TABLE_STYLE}{_COMMAND_STYLE}
<h1>The Raven's Nest</h1>
{setup_banner}
<form id="cmd-form" hx-post="/command" hx-target="#result">
  <input id="cmd" name="q" autofocus autocomplete="off" spellcheck="false"
         placeholder="{_e(PLACEHOLDER_EXAMPLES[0])}" data-examples="{examples_attr}"
         hx-get="/command" hx-trigger="input changed delay:250ms" hx-target="#result">
</form>
<div id="result"></div>
<p class="hintbar">
Type <code>help</code> for every command ·
<code>3mm heat shrink</code> search ·
<code>A-2-3b</code> bin ·
<code>move to A-2-3b</code> ·
<code>build X x2</code> ·
<code>need 20 more m3</code> ·
<code>recount A-2-3b</code> ·
<code>low</code> ·
<code>history A-2-3b</code> ·
<code>undo</code> ·
<code>health</code> ·
<code>merge</code> ·
<code>price basket</code><br>
↑↓ select · Enter act · Esc clear ·
<a href="/m">phone UI</a> · <a href="/setup">setup</a>{queue_note}
</p>"""
    return page(
        "The Raven's Nest", body, scripts=("/static/command.js",)
    )


def note(text: str, error: bool = False, undo_event: str | None = None) -> str:
    undo_html = ""
    if undo_event and not error:
        undo_html = (
            f' <form class="inline" hx-post="/command/undo" hx-target="#result">'
            f'<input type="hidden" name="event_id" value="{_e(undo_event)}">'
            f'<button type="submit" class="undo-btn">undo</button></form>'
        )
    return f'<div class="note{" error" if error else ""}">{_e(text)}{undo_html}</div>'


def enter_hint(action: str, syntax: bool = False) -> str:
    if syntax:
        return f'<div class="enterhint">{_e(action)}</div>'
    return f'<div class="enterhint">⏎ Press Enter to {_e(action)}</div>'


def search_results(
    results: list[dict[str, Any]], query: str, hidden_zero: int = 0, scope: str = "default"
) -> str:
    hidden_note = ""
    if hidden_zero:
        hidden_note = (
            f'<p class="count">{len(results)} result(s) ({hidden_zero} zero-qty hidden '
            f"— type <code>all: {_e(query)}</code> to show)</p>"
        )
    elif results:
        label = {"all": " (including zero-qty)", "archived": " (archived only)"}.get(scope, "")
        hidden_note = f'<p class="count">{len(results)} result(s){label}</p>'
    if not results:
        if hidden_zero:
            return note(
                f"0 in-stock results ({hidden_zero} zero-qty hidden — type "
                f"`all: {query}` to show them)."
            )
        extra = " among archived items" if scope == "archived" else ""
        return note(f"No items match “{query}”{extra}.")
    rows = "".join(
        f'<a class="result-item{" zeroed" if r["qty_on_hand"] == "0" else ""}" href="/items/{_e(r["id"])}">'
        f'<strong>{_e(r["name"])}</strong>'
        f'{" · " + _e(r["part_number"]) if r["part_number"] else ""}'
        f'<div class="sub">{_e(r["summary"])}</div></a>'
        for r in results
    )
    return f"{hidden_note}<div>{rows}</div>"


def bin_view(location_id: str, location: dict | None, items: list[dict[str, Any]]) -> str:
    desc = f" — {_e(location['description'])}" if location and location["description"] else ""
    known = "" if location else ' <span class="flag">(no location record)</span>'
    if items:
        rows = "".join(
            f'<a class="result-item{" zeroed" if i.get("zero") else ""}" href="/items/{_e(i["id"])}">'
            f'<strong>{_e(i["name"])}</strong>'
            f'<div class="sub">{_e(i["qty_on_hand"])} {_e(i["unit_type"])} on hand, '
            f'{_e(i["free"])} free'
            f'{" · zero-qty — the bin is still nominally theirs" if i.get("zero") else ""}</div></a>'
            for i in items
        )
        contents = f"<div>{rows}</div>"
    else:
        contents = '<p class="count">Empty bin — free space.</p>'
    return (
        f"<h2>{_e(location_id)}{desc}{known}</h2>{contents}"
        f'<p class="count"><a href="/history?target=bin:{_e(location_id)}">Bin history</a></p>'
    )


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


def need_done(name: str, added: str, total: str, event_id: str | None = None) -> str:
    return note(
        f"Added {added} × {name} to the reorder basket (manual total now {total}).",
        undo_event=event_id,
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
    history_data: dict[str, Any],
) -> str:
    photo = (
        f'<img class="photo" src="/assets/{_e(item["photo_hash"])}.jpg" alt="item photo">'
        if item["photo_hash"]
        else ""
    )
    archived_html = ""
    if item.get("archived"):
        archived_html = f"""<div class="note error">This item is ARCHIVED — retired from
search, reorder, and BOM matching. Its history, photo, and links remain.
<form class="inline" method="post" action="/items/{_e(item["id"])}/unarchive">
<button type="submit">Unarchive</button></form></div>"""
    else:
        archived_html = f"""<form class="inline" method="post"
action="/items/{_e(item["id"])}/archive"
onsubmit="return confirm('Archive this item? It leaves search/reorder/BOM matching but keeps its history and photo.')">
<input type="hidden" name="reason" value="archived from item card">
<button type="submit">Archive (retire)</button></form>"""

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
        f"<tr class='histrow'><td>{_e(h['ts'][:16].replace('T', ' '))}</td>"
        f"<td>{_e(h['actor'])}</td><td>{_e(h['text'])}</td></tr>"
        for h in history_data["entries"]
    )
    more = ""
    if history_data["pages"] > 1:
        more = (
            f'<p class="count"><a href="/history?target=item:{_e(item["id"])}&page=2">'
            f"Older history ({history_data['total']} events total)</a></p>"
        )
    edit_form = f"""<h2>Quick edit</h2>
<form method="post" action="/items/{_e(item["id"])}/edit">
  <div class="row"><label>Min qty</label>
    <input name="min_qty" value="{_e(item["min_qty"]) if item["min_qty"] is not None else ""}"
           placeholder="reorder-alert threshold"></div>
  <div class="row"><label>Last paid AUD</label>
    <input name="last_paid_aud" value="{_e(item["last_paid_aud"]) if item["last_paid_aud"] is not None else ""}"
           placeholder="per {_e(item["unit_type"])}"></div>
  <div class="row"><label>Move to</label>
    <input name="location_id" placeholder="A-2-3b (leave blank to keep {_e(item["location_id"]) if item["location_id"] else "none"})"></div>
  <div class="actions"><button type="submit" class="primary">Save</button></div>
</form>"""
    zero_note = ""
    if item["qty_on_hand"] == "0" and not item.get("archived"):
        zero_note = (
            '<p class="count">Quantity is zero — this item is hidden from plain search '
            "(<code>all:</code> shows it) but keeps its bin, history, links, and reorder logic. "
            "Archive it only if you'll never buy it again.</p>"
        )
    body = f"""{_TABLE_STYLE}{_COMMAND_STYLE}
<p><a href="/">← command bar</a></p>
<h1>{_e(item["name"])}</h1>
{archived_html}
{zero_note}
{photo}
<table>{fields}</table>
{reservation_rows}
{link_rows}
<p><a href="/items/{_e(item["id"])}/sourcing">Manage sourcing links</a> ·
<a href="/labels/items?item_id={_e(item["id"])}">Print label</a></p>
{edit_form}
<h2>History</h2>
<table><tr><th>When</th><th>By</th><th>What</th></tr>{history_rows}</table>
{more}
"""
    return page(f"The Raven's Nest — {item['name']}", body)


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


# ------------------------------------------------------------- new panels


def help_panel(help_entries, syntax, verb: str | None = None) -> str:
    if verb:
        matches = [h for h in help_entries if h[0].rstrip(":") == verb.rstrip(":")]
        if not matches:
            known = ", ".join(h[0] for h in help_entries)
            return note(f"No help for {verb!r}. Known commands: {known}", error=True)
        name, example, blurb = matches[0]
        usage = syntax.get(name, example)
        return f"""<div class="panel"><h2>help — {_e(name)}</h2>
<p>{_e(blurb)}</p>
<p>Usage: <code>{_e(usage)}</code></p>
<p class="count">Example: <code>{_e(example)}</code></p></div>"""
    rows = "".join(
        f"<tr><td><code>{_e(example)}</code></td><td>{_e(blurb)}</td></tr>"
        for _name, example, blurb in help_entries
    )
    return f"""<div class="panel"><h2>Every command</h2>
<table><tr><th>Type this</th><th>What happens</th></tr>{rows}</table>
<p class="count">`help &lt;verb&gt;` for detail (e.g. <code>help build</code>).
Pages: <a href="/queue">review queue</a> · <a href="/reorder">reorder basket</a> ·
<a href="/projects">projects</a> · <a href="/suppliers">suppliers</a> ·
<a href="/locations">locations</a> · <a href="/labels">labels</a> ·
<a href="/import">CSV import</a> · <a href="/export/items.csv">CSV export</a> ·
<a href="/export/full.zip">full data export</a> · <a href="/setup">setup</a> ·
<a href="/m">phone UI</a></p></div>"""


def item_jump(item: dict[str, Any]) -> str:
    location = " · " + _e(item["location_id"]) if item["location_id"] else ""
    return f"""<div class="panel">Scanned item label:
<a class="result-item" href="/items/{_e(item["id"])}"><strong>{_e(item["name"])}</strong>
<div class="sub">{_e(item["qty_on_hand"])} {_e(item["unit_type"])} on hand{location}</div></a>
<p class="count"><a href="/items/{_e(item["id"])}">Open item card →</a></p></div>"""


def history_ask(candidates: list[dict[str, Any]], query: str) -> str:
    buttons = "".join(
        f'<a class="result-item" href="#" hx-get="/command/history?target=item:{_e(c["id"])}"'
        f' hx-target="#result">{_e(c["name"])} (score {c["score"]})</a>'
        for c in candidates
    )
    return f'<div class="panel">Whose history — which “{_e(query)}”?<div>{buttons}</div></div>'


def history_panel(title: str, target_kind: str, target_id: str, data: dict[str, Any]) -> str:
    base = f"/command/history?target={target_kind}:{target_id}"
    chips = f'<span class="chips"><a hx-get="{base}" hx-target="#result" href="#">all types</a>'
    for event_type in data["types"]:
        marker = " ✓" if data["type_filter"] == event_type else ""
        chips += (
            f'<a hx-get="{base}&type={event_type}" hx-target="#result" href="#">'
            f"{_e(event_type)}{marker}</a>"
        )
    chips += "</span>"
    rows = "".join(
        f"<tr class='histrow'><td>{_e(h['ts'][:16].replace('T', ' '))}</td>"
        f"<td>{_e(h['actor'])}</td>"
        f"<td>{_e(h['text'])}{' — ' + _e(h['note']) if h.get('note') else ''}</td></tr>"
        for h in data["entries"]
    )
    if not rows:
        rows = "<tr><td colspan='3'>No events on this page.</td></tr>"
    nav = ""
    type_arg = f"&type={data['type_filter']}" if data["type_filter"] else ""
    if data["page"] > 1:
        nav += (
            f'<a hx-get="{base}&page={data["page"] - 1}{type_arg}" hx-target="#result"'
            f' href="#">← newer</a> '
        )
    if data["page"] < data["pages"]:
        nav += (
            f'<a hx-get="{base}&page={data["page"] + 1}{type_arg}" hx-target="#result"'
            f' href="#">older →</a>'
        )
    return f"""<div class="panel"><h2>{_e(title)}</h2>
<p class="count">{data["total"]} event(s) · page {data["page"]}/{data["pages"]} · filter: {chips}</p>
<table><tr><th>When</th><th>By</th><th>What</th></tr>{rows}</table>
<p class="count">{nav}</p></div>"""


def undo_list_panel(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return note("Nothing to undo — no undoable actions by this machine in the log.")
    rows = "".join(
        f"<tr class='histrow'><td class='num'>{i + 1}</td>"
        f"<td>{_e(e['ts'][:16].replace('T', ' '))}</td><td>{_e(e['text'])}</td>"
        f"<td><form class='inline' hx-post='/command/undo' hx-target='#result'>"
        f"<input type='hidden' name='event_id' value='{_e(e['event_id'])}'>"
        f"<button type='submit' class='undo-btn'>undo</button></form></td></tr>"
        for i, e in enumerate(entries)
    )
    return f"""<div class="panel"><h2>Undo stack — this machine's last {len(entries)} action(s)</h2>
<p class="count">`undo` reverses #1; `undo &lt;n&gt;` targets an older one (it will refuse
with an explanation if the state has moved on). Undoing an undo redoes.</p>
<table><tr><th>#</th><th>When</th><th>Action</th><th></th></tr>{rows}</table></div>"""


def health_panel(report: dict[str, Any], sync: dict[str, Any]) -> str:
    def item_list(rows, href_template):
        lis = "".join(
            f'<li><a href="{href_template.format(id=_e(r["id"]))}">{_e(r["name"])}</a></li>'
            for r in rows[:30]
        )
        extra = f"<li>… and {len(rows) - 30} more</li>" if len(rows) > 30 else ""
        return f"<ul>{lis}{extra}</ul>"

    sections = ""
    for check in report["checks"]:
        if check["count"] == 0:
            continue
        href = check["fix_href"].replace("{id}", "{id}")
        sections += (
            f'<details><summary><strong>{check["count"]}</strong> {_e(check["label"])}'
            f' — fix: {_e(check["fix_hint"])}</summary>{item_list(check["rows"], href)}</details>'
        )

    if report["stale_prices"]:
        lis = "".join(
            f'<li><a href="/items/{_e(l["item_id"])}/sourcing">{_e(l["item_name"])}</a>'
            f' @ {_e(l["supplier_name"])} (checked '
            f'{_e(l["last_checked_ts"][:10]) if l["last_checked_ts"] else "never"})</li>'
            for l in report["stale_prices"][:30]
        )
        sections += (
            f'<details><summary><strong>{len(report["stale_prices"])}</strong> '
            f'price(s) older than {report["stale_days"]} days — fix: run `price basket`'
            f"</summary><ul>{lis}</ul></details>"
        )

    if report["unresolved_bom"]:
        lis = "".join(
            f'<li><a href="/projects/{_e(b["project_id"])}">{_e(b["project_name"] or "project")}</a>'
            f' line {b["line_no"]}: {_e(b["part_number"])}</li>'
            for b in report["unresolved_bom"][:30]
        )
        sections += (
            f'<details><summary><strong>{len(report["unresolved_bom"])}</strong> '
            f"unresolved BOM line(s) — fix: resolve on the project page</summary>"
            f"<ul>{lis}</ul></details>"
        )

    if report["empty_bins"]:
        bins = ", ".join(report["empty_bins"][:40])
        sections += (
            f'<details><summary><strong>{len(report["empty_bins"])}</strong> '
            f"empty bin(s) — free space, not a problem</summary><p>{_e(bins)}</p></details>"
        )

    if report["duplicates"]:
        sections += (
            f'<details><summary><strong>{len(report["duplicates"])}</strong> '
            f'likely duplicate pair(s) — fix: <a href="/merge">open the merge tool</a>'
            f'</summary><p class="count">Duplicates corrupt free stock and misfire reorders.'
            f"</p></details>"
        )

    unpushed = sync.get("unpushed_events")
    if sync.get("has_remote") and unpushed:
        sync_line = f"{unpushed} unpushed event(s) — <a href='/sync/status'>sync status</a>"
    elif sync.get("has_remote"):
        sync_line = "sync: everything pushed"
    else:
        sync_line = "sync: no remote configured (local-only data)"
    return f"""<div class="panel"><h2>Data health</h2>
<p><span class="scorebig">{report["score"]}%</span> · {report["total_items"]} active item(s)</p>
<p class="count">Score = average share of items passing each per-item check below.</p>
{sections if sections else "<p>All checks clean. 🎉</p>"}
<p class="count">{sync_line}</p></div>"""


def merge_panel(pairs: list[dict[str, Any]]) -> str:
    if not pairs:
        return note("No likely duplicates found. 🎉") + (
            '<p class="count">The scanner compares names (fuzzy), part numbers, and aliases '
            "across all non-archived items.</p>"
        )

    def side(item):
        part = " · " + _e(item["part_number"]) if item["part_number"] else ""
        where = " · " + _e(item["location_id"]) if item["location_id"] else " · no location"
        return (
            f'<div><strong><a href="/items/{_e(item["id"])}">{_e(item["name"])}</a></strong>'
            f'<div class="sub">{_e(item["qty_on_hand"])} {_e(item["unit_type"])}{part}{where}</div></div>'
        )

    blocks = []
    for pair in pairs:
        a, b = pair["a"], pair["b"]
        why = "same part number" if pair["same_part"] else f"name similarity {pair['score']}"
        location_choice = ""
        if a["location_id"] and b["location_id"] and a["location_id"] != b["location_id"]:
            location_choice = f"""<p>They live in different bins — which is correct?
<label><input type="radio" name="location_id" value="{_e(a["location_id"])}"> {_e(a["location_id"])}</label>
<label><input type="radio" name="location_id" value="{_e(b["location_id"])}"> {_e(b["location_id"])}</label></p>"""
        unit_guard = ""
        if pair["unit_mismatch"]:
            unit_guard = f"""<p class="note error">⚠️ Unit types differ ({_e(a["unit_type"])} vs
{_e(b["unit_type"])}) — summing them would corrupt quantities.
<label><input type="checkbox" name="allow_units" value="1"> I understand — they really are the same thing</label></p>"""
        keep_a = _e(a["name"][:24])
        keep_b = _e(b["name"][:24])
        blocks.append(f"""<form class="pair" hx-post="/merge" hx-target="#result">
{side(a)}{side(b)}
<div style="grid-column: span 2;">
<p class="count">Why flagged: {why}.</p>
{location_choice}{unit_guard}
<button type="submit" name="target_id" value="{_e(a["id"])}"
        onclick="this.form.querySelector('[name=source_id]').value='{_e(b["id"])}'">
  Keep “{keep_a}” (merge the other into it)</button>
<button type="submit" name="target_id" value="{_e(b["id"])}"
        onclick="this.form.querySelector('[name=source_id]').value='{_e(a["id"])}'">
  Keep “{keep_b}” (merge the other into it)</button>
<input type="hidden" name="source_id" value="{_e(b["id"])}">
</div>
</form>""")
    return f"""<div class="panel"><h2>Likely duplicates ({len(pairs)})</h2>
<p class="count">Merging sums quantities, transfers aliases/links/photo, keeps the source's
history readable from the target, and archives the source (never deletes). Every merge is undoable.</p>
{"".join(blocks)}</div>"""


# ----------------------------------------------------------- import pages


def import_page(error: str | None = None) -> str:
    banner = note(error, error=True) if error else ""
    body = f"""{_TABLE_STYLE}{_COMMAND_STYLE}
<p><a href="/">← command bar</a></p>
<h1>CSV import</h1>
{banner}
<p>Columns (only <code>name</code> is required):
<code>name, part_number, description, unit_type, qty, min_qty, location,
last_paid_aud, manufacturer, package_type, supplier_url</code></p>
<form method="post" action="/import/preview" enctype="multipart/form-data">
  <input type="file" name="items_csv" accept=".csv,text/csv" required>
  <button type="submit" class="primary">Preview (dry run — nothing is written)</button>
</form>
<p class="count">Export the same format any time: <a href="/export/items.csv">items CSV</a>
(add <code>?include_archived=1</code> for retired items) ·
<a href="/export/full.zip">full data export</a> (event log + photos — everything).</p>"""
    return page("The Raven's Nest — CSV import", body)


def import_preview_page(rows: list[dict[str, Any]], csv_b64: str) -> str:
    counts = {"new": 0, "matched": 0, "ambiguous": 0, "error": 0}
    for row in rows:
        counts[row["status"]] += 1
    body_rows = []
    for row in rows:
        if row["status"] == "error":
            decision = f'<span class="flag">{_e(row["error"])}</span>'
        elif row["status"] == "new":
            decision = (
                f'<select name="row_{row["row_no"]}"><option value="new">create new item'
                f'</option><option value="skip">skip</option></select>'
            )
        elif row["status"] == "matched":
            m = row["match"]
            decision = (
                f'<select name="row_{row["row_no"]}">'
                f'<option value="{_e(m["id"])}">update “{_e(m["name"])}” (matched)</option>'
                f'<option value="new">create new anyway</option>'
                f'<option value="skip">skip</option></select>'
            )
        else:  # ambiguous — never guessed; the user decides
            options = "".join(
                f'<option value="{_e(c["id"])}">use “{_e(c["name"])}” (score {c.get("score", "?")})</option>'
                for c in row["candidates"]
            )
            decision = (
                f'<select name="row_{row["row_no"]}">'
                f'<option value="skip">ambiguous — choose…</option>{options}'
                f'<option value="new">create new item</option></select>'
            )
        body_rows.append(
            f"<tr><td class='num'>{row['row_no']}</td><td>{_e(row['name'])}</td>"
            f"<td>{_e(row['part_number'] or '')}</td><td>{_e(row['qty'] or '')}</td>"
            f"<td>{_e(row['location'] or '')}</td><td>{row['status']}</td><td>{decision}</td></tr>"
        )
    body = f"""{_TABLE_STYLE}{_COMMAND_STYLE}
<p><a href="/import">← back</a></p>
<h1>Import preview — nothing written yet</h1>
<p><strong>{counts["new"]} new · {counts["matched"]} matched to existing ·
{counts["ambiguous"]} need a decision · {counts["error"]} error(s)</strong></p>
<p class="count">Resolving an ambiguous row stores the sheet's name/part number as an alias,
so re-importing the same sheet matches clean.</p>
<form method="post" action="/import/confirm">
  <input type="hidden" name="csv_b64" value="{csv_b64}">
  <table><tr><th>#</th><th>Name</th><th>Part</th><th>Qty</th><th>Location</th>
  <th>Status</th><th>Decision</th></tr>
  {"".join(body_rows)}</table>
  <div class="actions"><button type="submit" class="primary">Apply import</button></div>
</form>"""
    return page("The Raven's Nest — Import preview", body)


def import_done(outcomes: dict[str, int]) -> str:
    body = f"""{_TABLE_STYLE}{_COMMAND_STYLE}
<p><a href="/">← command bar</a></p>
<h1>Import applied</h1>
<div class="note">{outcomes["created"]} created · {outcomes["updated"]} updated ·
{outcomes["skipped"]} skipped</div>
<p><a href="/import">Import another</a> · <a href="/">back to the command bar</a></p>"""
    return page("The Raven's Nest — Import applied", body)
