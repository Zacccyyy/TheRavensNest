"""Rendering for project pages, BOM tables, and the reorder basket."""

from __future__ import annotations

from typing import Any

from .ui import _e, page

_NAV = """<nav><a href="/">Home</a> <a href="/queue">Queue</a> <a href="/move">Move</a>
<a href="/locations">Locations</a> <a href="/projects">Projects</a>
<a href="/reorder">Reorder</a> <a href="/suppliers">Suppliers</a></nav>"""

_TABLE_STYLE = """<style>
table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid #eee; font-size: .9rem; }
th { color: #555; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.unpriced td, td.flag { color: #b45309; }
tr.shortfall td { color: #991b1b; }
.unresolved { border: 1px solid #fbbf24; background: #fffbeb; border-radius: 6px;
              padding: .6rem; margin: .6rem 0; }
.unresolved .cand { margin: .2rem 0; }
form.inline { display: inline; }
input.narrow { width: 4rem; }
</style>"""


def projects_page(projects: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td><a href="/projects/{_e(p["id"])}">{_e(p["name"])}</a></td>'
        f'<td>{_e(p["description"])}</td>'
        f'<td class="num">{p["matched_count"]}/{p["line_count"]}</td>'
        f'<td class="num">{p["reservation_count"]}</td>'
        f'<td class="num">{p["built"]}</td></tr>'
        for p in projects
    )
    table = (
        f"<table><tr><th>Project</th><th>Description</th><th class='num'>Matched lines</th>"
        f"<th class='num'>Active reservations</th><th class='num'>Built</th></tr>{rows}</table>"
        if projects
        else "<p>No projects yet.</p>"
    )
    body = f"""{_NAV}{_TABLE_STYLE}
<h1>Projects</h1>
{table}
<h2>New project</h2>
<form method="post" action="/projects">
  <div class="row"><label for="name">Name</label><input id="name" name="name" required></div>
  <div class="row"><label for="description">Description</label>
    <input id="description" name="description"></div>
  <div class="actions"><button type="submit" class="primary">Create</button></div>
</form>"""
    return page("The Raven's Nest — Projects", body)


def _bom_table(cost_rows: list[dict[str, Any]], total_cost: str, unpriced: int) -> str:
    if not cost_rows:
        return "<p>No BOM imported yet.</p>"
    rows = []
    for line in cost_rows:
        matched = (
            _e(line["item_name"])
            if line["item_name"]
            else '<span class="flag">unresolved</span>'
        )
        unit_cost = line["unit_cost"] if line["unit_cost"] is not None else "—"
        ext_cost = line["ext_cost"] if line["ext_cost"] is not None else "—"
        cls = ' class="unpriced"' if line["ext_cost"] is None else ""
        refdes = _e(line["reference_designators"]) if line["reference_designators"] else ""
        rows.append(
            f"<tr{cls}><td class='num'>{line['line_no']}</td>"
            f"<td>{_e(line['part_number'])}</td><td>{_e(line['description'])}</td>"
            f"<td class='num'>{_e(line['quantity'])} {_e(line['unit'])}</td>"
            f"<td>{refdes}</td><td>{matched}</td>"
            f"<td class='num'>{_e(unit_cost)}</td><td class='num'>{_e(ext_cost)}</td></tr>"
        )
    flag = (
        f' <span class="flag">({unpriced} line(s) without price data)</span>'
        if unpriced
        else ""
    )
    return (
        "<table><tr><th class='num'>#</th><th>Part number</th><th>Description</th>"
        "<th class='num'>Qty</th><th>Ref des</th><th>Item</th>"
        "<th class='num'>Unit AUD</th><th class='num'>Ext AUD</th></tr>"
        + "".join(rows)
        + f"</table><p><strong>Build cost: {_e(total_cost)} AUD</strong>{flag}</p>"
    )


def _unresolved_section(
    project_id: str, unresolved: list[dict[str, Any]], all_items: list[dict[str, Any]]
) -> str:
    if not unresolved:
        return ""
    options = "".join(
        f'<option value="{_e(i["id"])}">{_e(i["name"])}'
        f'{" · " + _e(i["part_number"]) if i.get("part_number") else ""}</option>'
        for i in all_items
    )
    blocks = []
    for line in unresolved:
        candidates = "".join(
            f'<form class="inline" method="post" action="/projects/{_e(project_id)}/match">'
            f'<input type="hidden" name="line_no" value="{line["line_no"]}">'
            f'<input type="hidden" name="item_id" value="{_e(c["item_id"])}">'
            f'<input type="hidden" name="method" value="fuzzy">'
            f'<input type="hidden" name="score" value="{c["score"]}">'
            f'<button type="submit">{_e(c["name"])} (score {c["score"]})</button></form> '
            for c in line["candidates"]
        )
        candidate_html = (
            f'<div class="cand">Fuzzy suggestions: {candidates}</div>'
            if candidates
            else '<div class="cand">No fuzzy suggestions.</div>'
        )
        picker = ""
        if all_items:
            picker = f"""<form class="inline" method="post" action="/projects/{_e(project_id)}/match">
  <input type="hidden" name="line_no" value="{line["line_no"]}">
  <select name="item_id">{options}</select>
  <button type="submit">Use this item</button>
</form>"""
        blocks.append(f"""<div class="unresolved">
<strong>Line {line["line_no"]}:</strong> {_e(line["part_number"])} — {_e(line["description"])}
({_e(line["quantity"])} {_e(line["unit"])})
{candidate_html}
<div class="cand">{picker}
<form class="inline" method="post" action="/projects/{_e(project_id)}/match-new">
  <input type="hidden" name="line_no" value="{line["line_no"]}">
  <input name="name" value="{_e(line["description"] or line["part_number"])}" required>
  <button type="submit">Create new item</button>
</form></div>
<p class="count">Resolving stores “{_e(line["part_number"])}” as an alias, so the next
BOM revision matches automatically.</p>
</div>""")
    return "<h2>Unresolved lines</h2>" + "".join(blocks)


def _reservations_section(project_id: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='count'>No active reservations.</p>"
    body = "".join(
        f"<tr{' class=\"shortfall\"' if r['shortfall'] else ''}>"
        f"<td>{_e(r['item_name'])}</td><td class='num'>{_e(r['qty'])} {_e(r['unit_type'])}</td>"
        f"<td class='num'>{_e(r['on_hand'])}</td><td class='num'>{_e(r['free'])}</td>"
        f"<td>{'shortfall — in reorder basket' if r['shortfall'] else ''}</td></tr>"
        for r in rows
    )
    return (
        "<table><tr><th>Item</th><th class='num'>Reserved</th><th class='num'>On hand</th>"
        f"<th class='num'>Free</th><th></th></tr>{body}</table>"
        f'<form method="post" action="/projects/{_e(project_id)}/release">'
        '<button type="submit">Release all reservations</button></form>'
    )


def _history_section(history: list[dict[str, Any]]) -> str:
    if not history:
        return "<p class='count'>No builds yet.</p>"
    rows = "".join(
        f"<tr><td>{_e(b['ts'][:19])}</td>"
        f"<td>{'Built' if b['kind'] == 'build' else 'Un-built'} ×{b['count']}</td></tr>"
        for b in history
    )
    return f"<table><tr><th>When</th><th>What</th></tr>{rows}</table>"


def project_page(
    project: dict[str, Any],
    cost_rows: list[dict[str, Any]],
    total_cost: str,
    unpriced: int,
    unresolved: list[dict[str, Any]],
    all_items: list[dict[str, Any]],
    reservation_rows: list[dict[str, Any]],
    history: list[dict[str, Any]],
    built: int,
    error: str | None = None,
    notice: str | None = None,
) -> str:
    pid = project["id"]
    banner = ""
    if error:
        banner += f'<div class="note error">{_e(error)}</div>'
    if notice:
        banner += f'<div class="note">{_e(notice)}</div>'
    body = f"""{_NAV}{_TABLE_STYLE}
<h1>{_e(project["name"])}</h1>
<p class="count">{_e(project["description"])}</p>
{banner}
<h2>BOM</h2>
{_bom_table(cost_rows, total_cost, unpriced)}
<form method="post" action="/projects/{_e(pid)}/bom" enctype="multipart/form-data">
  <input type="file" name="bom_csv" accept=".csv,text/csv" required>
  <button type="submit">Import BOM CSV</button>
  <span class="count">Columns: part_number, description, quantity, unit
  [, reference_designators, notes]. Import reserves stock — it does not consume.</span>
</form>
{_unresolved_section(pid, unresolved, all_items)}
<h2>Reservations</h2>
{_reservations_section(pid, reservation_rows)}
<h2>Build ({built} net build(s) so far)</h2>
<form class="inline" method="post" action="/projects/{_e(pid)}/build">
  <input class="narrow" type="number" name="count" value="1" min="1">
  <button type="submit" class="primary">Build</button>
</form>
<form class="inline" method="post" action="/projects/{_e(pid)}/unbuild">
  <input class="narrow" type="number" name="count" value="1" min="1">
  <button type="submit">Un-build</button>
</form>
<h2>Build history</h2>
{_history_section(history)}"""
    return page(f"The Raven's Nest — {project['name']}", body)
