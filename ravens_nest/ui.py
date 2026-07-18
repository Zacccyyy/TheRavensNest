"""Server-rendered HTMX UI for the capture review queue.

Cards swap in place: confirm/merge/skip posts return the next card
partial, which htmx swaps into the page. Low-confidence and unknown
fields are flagged and the first one receives focus; Ctrl+Enter
confirms, so a queue can be cleared without touching the mouse.
"""

from __future__ import annotations

import html
from typing import Any

UNIT_TYPES = ("each", "g", "mm", "mL")

_STYLE = """
body { font-family: system-ui, sans-serif; margin: 0 auto; max-width: 720px; padding: 1rem; }
h1 { font-size: 1.3rem; }
img.photo { max-width: 100%; max-height: 320px; border-radius: 6px; }
.row { display: flex; align-items: baseline; gap: .5rem; margin: .5rem 0; }
.row label { flex: 0 0 8.5rem; font-size: .85rem; color: #555; }
.row input, .row select { flex: 1; padding: .35rem; border: 1px solid #ccc; border-radius: 4px; }
.row.flag input, .row.flag select { border-color: #d97706; background: #fffbeb; }
.conf { font-size: .7rem; padding: .1rem .4rem; border-radius: 8px; flex: 0 0 auto; }
.conf-high { background: #dcfce7; color: #166534; }
.conf-medium { background: #fef9c3; color: #854d0e; }
.conf-low { background: #fee2e2; color: #991b1b; }
.question { flex-basis: 100%; font-size: .85rem; color: #b45309; margin: -0.3rem 0 .3rem 8.5rem; }
.error { background: #fee2e2; color: #991b1b; padding: .5rem; border-radius: 4px; margin: .5rem 0; }
.actions { display: flex; gap: .5rem; margin-top: 1rem; }
button { padding: .5rem 1rem; border-radius: 4px; border: 1px solid #ccc; background: #f9fafb; cursor: pointer; }
button.primary { background: #1d4ed8; color: white; border-color: #1d4ed8; }
.merge { margin-top: 1.5rem; padding-top: 1rem; border-top: 1px dashed #ccc; }
.count { color: #666; font-size: .85rem; }
.empty { text-align: center; color: #666; padding: 3rem 0; }
kbd { background: #eee; border-radius: 3px; padding: 0 .3rem; font-size: .8rem; }
nav a { margin-right: 1rem; }
.locbox { padding: .6rem; border: 2px solid #1d4ed8; border-radius: 6px; margin: .5rem 0; background: #eff6ff; }
.locbox.unset { border-color: #d97706; background: #fffbeb; }
.note { padding: .4rem .6rem; border-radius: 4px; background: #f0fdf4; color: #166534; margin: .4rem 0; }
.note.error { background: #fee2e2; color: #991b1b; }
label.match { display: block; padding: .3rem 0; border-bottom: 1px solid #eee; }
#log { list-style: none; padding: 0; }
#log li { padding: .2rem 0; color: #444; font-size: .9rem; border-bottom: 1px dotted #ddd; }
#scan-video { width: 100%; max-height: 320px; background: #000; border-radius: 6px; }
details.tree-unit { margin: .6rem 0; }
details.tree-shelf { margin-left: 1rem; }
.tree-bin { margin-left: 2rem; padding: .15rem 0; }
.tree-bin.is-empty { color: #16a34a; }
.tree-bin ul { margin: .2rem 0 .2rem 1.2rem; }
.freespace { background: #f0fdf4; padding: .5rem .7rem; border-radius: 6px; }
"""

_HOTKEYS = """
document.addEventListener('keydown', function (e) {
  if (e.ctrlKey && e.key === 'Enter') {
    var btn = document.getElementById('confirm-btn');
    if (btn) { e.preventDefault(); btn.click(); }
  }
});
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True) if value is not None else ""


def page(title: str, body: str, scripts: tuple[str, ...] = ()) -> str:
    script_tags = "".join(f'<script src="{src}"></script>' for src in scripts)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <script src="/static/htmx.min.js"></script>
  <style>{_STYLE}</style>
</head>
<body>
{body}
<script>{_HOTKEYS}</script>
{script_tags}
</body>
</html>"""


def queue_page(cards: list[dict], items: list[dict]) -> str:
    body = f"""<h1>Review queue</h1>
<p class="count">Confirm creates the item; merge adds quantity to an existing item.
<kbd>Tab</kbd> between fields, <kbd>Ctrl</kbd>+<kbd>Enter</kbd> to confirm.</p>
{card_partial(cards[0], len(cards), items) if cards else empty_partial()}"""
    return page("The Raven's Nest — Review queue", body)


def empty_partial() -> str:
    return """<div id="card"><p class="empty">Queue is clear. 🎉<br>
<a href="/">Back to home</a></p></div>"""


def card_partial(
    card: dict, total: int, items: list[dict], error: str | None = None
) -> str:
    photo_hash = card["photo_hash"]
    fields = card.get("fields", {})
    questions = {q["field"]: q["question"] for q in card.get("questions", []) if q.get("field")}
    general_questions = [q["question"] for q in card.get("questions", []) if not q.get("field")]

    def field(name: str) -> tuple[Any, str]:
        entry = fields.get(name) or {}
        return entry.get("value"), entry.get("confidence", "low")

    # Focus the first unknown or low-confidence field; fall back to name.
    focus = next(
        (
            f
            for f in ("name", "description", "part_number", "unit_type", "qty_visible")
            if field(f)[0] is None or field(f)[1] == "low"
        ),
        "name",
    )

    def text_row(label: str, input_name: str, source_field: str) -> str:
        value, confidence = field(source_field)
        flagged = value is None or confidence == "low"
        question = questions.get(source_field)
        autofocus = " autofocus" if source_field == focus else ""
        row = (
            f'<div class="row{" flag" if flagged else ""}">'
            f'<label for="{input_name}">{_e(label)}</label>'
            f'<input id="{input_name}" name="{input_name}" value="{_e(value)}"{autofocus}>'
            f'<span class="conf conf-{confidence}">{confidence}</span>'
            f"</div>"
        )
        if question:
            row += f'<div class="question">❓ {_e(question)}</div>'
        return row

    value, confidence = field("unit_type")
    unit_options = "".join(
        f'<option value="{u}"{" selected" if u == value else ""}>{u}</option>'
        for u in UNIT_TYPES
    )
    unit_row = (
        f'<div class="row{" flag" if value is None else ""}">'
        f'<label for="unit_type">Unit type</label>'
        f'<select id="unit_type" name="unit_type">{unit_options}</select>'
        f'<span class="conf conf-{confidence}">{confidence}</span>'
        f"</div>"
    )
    if questions.get("unit_type"):
        unit_row += f'<div class="question">❓ {_e(questions["unit_type"])}</div>'

    qty_value, qty_confidence = field("qty_visible")
    qty_row = (
        f'<div class="row{" flag" if qty_value is None else ""}">'
        f'<label for="qty">Quantity</label>'
        f'<input id="qty" name="qty" inputmode="decimal" value="{_e(qty_value) or "0"}">'
        f'<span class="conf conf-{qty_confidence}">{qty_confidence}</span>'
        f"</div>"
    )
    if questions.get("qty_visible"):
        qty_row += f'<div class="question">❓ {_e(questions["qty_visible"])}</div>'

    error_html = f'<div class="error">{_e(error)}</div>' if error else ""
    if card.get("error"):
        error_html += (
            f'<div class="error">Automatic identification failed '
            f"({_e(card['error'])}) — fill the fields in manually.</div>"
        )
    general_html = "".join(f'<div class="question">❓ {_e(q)}</div>' for q in general_questions)

    item_options = "".join(
        f'<option value="{_e(i["id"])}">'
        f'{_e(i["name"])} — {_e(i["qty_on_hand"])} {_e(i["unit_type"])}</option>'
        for i in items
    )
    merge_html = ""
    if items:
        merge_html = f"""<div class="merge">
  <form hx-post="/queue/{photo_hash}/merge" hx-target="#card" hx-swap="outerHTML">
    <div class="row">
      <label for="item_id">Merge into</label>
      <select id="item_id" name="item_id">{item_options}</select>
    </div>
    <div class="row">
      <label for="merge_qty">Add quantity</label>
      <input id="merge_qty" name="qty" inputmode="decimal" value="{_e(qty_value) or "1"}">
    </div>
    <div class="actions"><button type="submit">Merge into existing item</button></div>
  </form>
</div>"""

    return f"""<div id="card">
<p class="count">{total} card(s) in queue</p>
<img class="photo" src="/assets/{photo_hash}.jpg" alt="captured item photo">
{error_html}{general_html}
<form hx-post="/queue/{photo_hash}/confirm" hx-target="#card" hx-swap="outerHTML">
  {text_row("Name", "name", "name")}
  {text_row("Description", "description", "description")}
  {text_row("Part number", "part_number", "part_number")}
  {unit_row}
  {qty_row}
  {text_row("Manufacturer", "manufacturer", "manufacturer")}
  {text_row("Package type", "package_type", "package_type")}
  <div class="row">
    <label for="location_id">Location</label>
    <input id="location_id" name="location_id" placeholder="A-2-3b (optional)">
  </div>
  <div class="actions">
    <button id="confirm-btn" type="submit" class="primary">Confirm — create item</button>
    <button type="button" hx-post="/queue/{photo_hash}/skip" hx-target="#card"
            hx-swap="outerHTML" hx-confirm="Discard this card? The photo stays in assets.">
      Skip
    </button>
  </div>
</form>
{merge_html}
</div>"""


# ------------------------------------------------------------- move console


def move_page() -> str:
    body = f"""<nav><a href="/">Home</a> <a href="/queue">Queue</a>
<a href="/locations">Locations</a> <a href="/labels">Labels</a></nav>
<h1>Move items</h1>
<p class="count">Scan a bin label to set the target, then scan items — exact scans
(item ID, part number, alias) move immediately. USB scanners just type + Enter
into the box below. <kbd>Enter</kbd> submits.</p>
{move_loc_fragment(None, "")}
<form id="scan-form" hx-post="/move/scan" hx-target="#result" hx-include="#loc"
      hx-on::after-request="this.reset(); document.getElementById('scan-input').focus()">
  <div class="row">
    <input id="scan-input" name="code" autofocus autocomplete="off"
           placeholder="Scan or type a location / item code">
    <button type="submit" class="primary">Go</button>
    <button type="button" id="camera-btn">&#128247; Camera</button>
  </div>
</form>
<video id="scan-video" playsinline muted hidden></video>
<canvas id="scan-canvas" hidden></canvas>
<div id="result"></div>
<h2>Recent moves</h2>
<ul id="log"></ul>"""
    return page(
        "The Raven's Nest — Move items",
        body,
        scripts=("/static/jsQR.js", "/static/scan.js"),
    )


def move_loc_fragment(location_id: str | None, description: str, oob: bool = False) -> str:
    oob_attr = ' hx-swap-oob="true"' if oob else ""
    if location_id:
        desc = f" — {_e(description)}" if description else ""
        return (
            f'<div id="loc" class="locbox"{oob_attr}>Target: <strong>{_e(location_id)}</strong>{desc}'
            f'<input type="hidden" name="location_id" value="{_e(location_id)}"></div>'
        )
    return (
        f'<div id="loc" class="locbox unset"{oob_attr}>No target location — scan a bin label first.'
        f'<input type="hidden" name="location_id" value=""></div>'
    )


def move_note(text: str, error: bool = False) -> str:
    return f'<div class="note{" error" if error else ""}">{_e(text)}</div>'


def move_log_entry(item_name: str, location_id: str) -> str:
    return (
        f'<li hx-swap-oob="afterbegin:#log">Moved <strong>{_e(item_name)}</strong>'
        f" &rarr; {_e(location_id)}</li>"
    )


def move_matches(items: list[dict]) -> str:
    rows = []
    for item in items:
        part = f" &middot; {_e(item['part_number'])}" if item.get("part_number") else ""
        where = (
            f" &middot; now in {_e(item['location_id'])}"
            if item.get("location_id")
            else " &middot; unassigned"
        )
        rows.append(
            f'<label class="match"><input type="checkbox" name="item_ids" value="{_e(item["id"])}">'
            f" {_e(item['name'])}{part} &mdash; {_e(item['qty_on_hand'])} {_e(item['unit_type'])}{where}</label>"
        )
    return f"""<form hx-post="/move" hx-target="#result" hx-include="#loc">
{"".join(rows)}
<div class="actions"><button type="submit" class="primary">Move selected here</button></div>
</form>"""


# ------------------------------------------------------------ location tree


def locations_page(
    tree: dict,
    unassigned: list[dict],
    unregistered: dict[str, list],
    empty_bins: list[str],
) -> str:
    def item_lines(contents: list[dict]) -> str:
        return "".join(
            f"<li>{_e(i['name'])} &mdash; {_e(i['qty_on_hand'])} {_e(i['unit_type'])}</li>"
            for i in contents
        )

    parts = []
    if empty_bins:
        parts.append(
            f'<p class="freespace">Free space: {len(empty_bins)} empty bin(s) &mdash; '
            f"{_e(', '.join(empty_bins))}</p>"
        )
    else:
        parts.append('<p class="freespace">No empty bins.</p>')

    for unit in sorted(tree):
        shelves = tree[unit]
        unit_count = sum(len(b["items"]) for bins in shelves.values() for b in bins)
        parts.append(
            f'<details class="tree-unit" open><summary><strong>Unit {_e(unit)}</strong>'
            f" &mdash; {unit_count} item(s)</summary>"
        )
        for shelf in sorted(shelves):
            parts.append(
                f'<details class="tree-shelf" open><summary>Shelf {shelf}</summary>'
            )
            for bin_entry in shelves[shelf]:
                desc = (
                    f" &mdash; {_e(bin_entry['description'])}"
                    if bin_entry["description"]
                    else ""
                )
                contents = bin_entry["items"]
                if contents:
                    parts.append(
                        f'<details class="tree-bin"><summary>{_e(bin_entry["id"])}{desc}'
                        f" &mdash; {len(contents)} item(s)</summary>"
                        f"<ul>{item_lines(contents)}</ul></details>"
                    )
                else:
                    parts.append(
                        f'<div class="tree-bin is-empty">{_e(bin_entry["id"])}{desc} &mdash; empty</div>'
                    )
            parts.append("</details>")
        parts.append("</details>")

    if unregistered:
        parts.append(
            '<h2>Unregistered locations</h2><p class="count">Items point here '
            "but no location record exists.</p>"
        )
        for loc_id in sorted(unregistered):
            parts.append(
                f'<details class="tree-bin"><summary>{_e(loc_id)} &mdash; '
                f"{len(unregistered[loc_id])} item(s)</summary>"
                f"<ul>{item_lines(unregistered[loc_id])}</ul></details>"
            )

    if unassigned:
        parts.append(
            f'<h2>Unassigned</h2><details class="tree-bin" open><summary>'
            f"{len(unassigned)} item(s) with no location</summary>"
            f"<ul>{item_lines(unassigned)}</ul></details>"
        )

    body = f"""<nav><a href="/">Home</a> <a href="/queue">Queue</a>
<a href="/move">Move</a> <a href="/labels">Labels</a></nav>
<h1>Locations</h1>
{"".join(parts)}
<h2>Add location</h2>
<form method="post" action="/locations">
  <div class="row"><label for="location_id">Location ID</label>
    <input id="location_id" name="location_id" placeholder="A-2-3b" required></div>
  <div class="row"><label for="description">Description</label>
    <input id="description" name="description" placeholder="small fasteners (optional)"></div>
  <div class="actions"><button type="submit" class="primary">Add</button></div>
</form>"""
    return page("The Raven's Nest — Locations", body)


# ------------------------------------------------------------- label sheet


_LABEL_STYLE = """
body { font-family: system-ui, sans-serif; margin: 1rem; }
.controls { margin-bottom: 1rem; }
.controls a { margin-right: .8rem; }
.controls input { width: 5rem; }
/* 3 x 2in grid — Avery 22806-style square labels on US Letter */
.sheet { display: grid; grid-template-columns: repeat(3, 2in); gap: 0.17in 0.55in; }
.qrlabel { width: 2in; height: 2in; display: flex; flex-direction: column;
           align-items: center; justify-content: center;
           break-inside: avoid; page-break-inside: avoid; border: 1px dashed #ddd; }
.qrlabel svg { width: 1.45in; height: 1.45in; }
.qrlabel .lid { font-family: ui-monospace, monospace; font-size: 14pt; font-weight: 600; }
@media print {
  .controls, nav { display: none; }
  .qrlabel { border: none; }
  @page { margin: 0.4in; }
}
"""


def labels_page(
    labels: list[tuple[dict, str]], unit: str | None, units: list[str]
) -> str:
    unit_links = " ".join(f'<a href="/labels?unit={_e(u)}">{_e(u)}</a>' for u in units)
    cells = "".join(
        f'<div class="qrlabel">{svg}<div class="lid">{_e(loc["id"])}</div></div>'
        for loc, svg in labels
    )
    empty_note = "" if labels else "<p>No locations yet — generate some below.</p>"
    title = f"Labels — unit {unit}" if unit else "Labels"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <style>{_LABEL_STYLE}</style>
</head>
<body>
<nav><a href="/">Home</a> <a href="/move">Move</a> <a href="/locations">Locations</a></nav>
<div class="controls">
  <p>Print on 2&times;2in labels (Avery 22806 or similar), 3 per row.
     Filter by unit: <a href="/labels">all</a> {unit_links}</p>
  <form method="post" action="/labels/generate">
    Generate: unit <input name="unit" placeholder="A" maxlength="1" required>
    shelves <input name="shelves" type="number" value="4" min="1" max="30">
    bins <input name="bins" type="number" value="6" min="1" max="30">
    sections <input name="sections" placeholder="f,b (optional)">
    <button type="submit">Create &amp; show labels</button>
  </form>
  <p><button onclick="window.print()">Print sheet</button></p>
</div>
{empty_note}
<div class="sheet">{cells}</div>
</body>
</html>"""
