"""First-run guided setup: storage → labels → suppliers → API key →
first data. Skippable, resumable, and re-runnable — "add another
shelving unit" is the same flow with a different unit letter.

Everything it writes goes through the event log like any other change.
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, db
from .movement import _ensure_location
from .sourcing import SEED_SUPPLIERS
from .ui import _e, page
from .ui_projects import _TABLE_STYLE

router = APIRouter()


def is_fresh_install() -> bool:
    conn = db.connect()
    try:
        for table in ("items", "locations", "suppliers"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return False
        return True
    finally:
        conn.close()


def parse_storage_text(text: str) -> dict[str, Any]:
    """Free-text storage description → structure. "I've got 3 shelving
    units, 5 shelves each, 6 bins per shelf, and the deep bins have a
    front and back" → units=3, shelves=5, bins=6, sections=[f, b]."""
    lower = text.lower()
    result: dict[str, Any] = {"units": None, "shelves": None, "bins": None, "sections": []}
    if match := re.search(r"(\d+)\s*(?:shelving\s+)?units?", lower):
        result["units"] = int(match.group(1))
    if match := re.search(r"(\d+)\s*shelves", lower):
        result["shelves"] = int(match.group(1))
    if match := re.search(r"(\d+)\s*bins", lower):
        result["bins"] = int(match.group(1))
    if re.search(r"front\s*(?:and|/|&)\s*back", lower):
        result["sections"] = ["f", "b"]
    elif match := re.search(r"sections?\s*[:\-]?\s*([a-z](?:\s*[/,]\s*[a-z])+)", lower):
        result["sections"] = [
            s.strip() for s in re.split(r"[/,]", match.group(1)) if s.strip()
        ]
    return result


def generate_location_ids(
    units: int, shelves: int, bins: int, sections: list[str]
) -> list[str]:
    ids = []
    for unit_index in range(units):
        unit = chr(ord("A") + unit_index)
        for shelf in range(1, shelves + 1):
            for bin_no in range(1, bins + 1):
                for section in sections or [""]:
                    ids.append(f"{unit}-{shelf}-{bin_no}{section}")
    return ids


def _shell(step: int, body: str) -> str:
    steps = ["Storage", "Labels", "Suppliers", "API key", "First data"]
    crumbs = " → ".join(
        f"<strong>{i + 1}. {name}</strong>" if i + 1 == step else f"{i + 1}. {name}"
        for i, name in enumerate(steps)
    )
    return page(
        "The Raven's Nest — Setup",
        f"""{_TABLE_STYLE}
<p><a href="/">← command bar (setup is skippable — come back any time via /setup)</a></p>
<h1>Setup</h1>
<p class="count">{crumbs}</p>
{body}""",
    )


@router.get("/setup", response_class=HTMLResponse)
def setup_home(step: int = 1) -> str:
    if step == 2:
        return _shell(2, """<h2>Print your bin labels</h2>
<p>Every location you just generated gets a QR label — stick them on the bins.</p>
<p><a href="/labels"><strong>Open the label sheets</strong></a> (print from there), then
continue.</p>
<p><a href="/setup?step=3">Next: suppliers →</a></p>""")
    if step == 3:
        rows = "".join(
            f"""<tr>
<td><label><input type="checkbox" name="use_{i}" value="1" checked> {_e(s["name"])}</label></td>
<td><input class="narrow" name="threshold_{i}"
     value="{_e(s["free_shipping_threshold_aud"]) if s["free_shipping_threshold_aud"] else ""}"></td>
<td><input class="narrow" name="shipping_{i}"
     value="{_e(s["typical_shipping_aud"]) if s["typical_shipping_aud"] else ""}"></td>
<td><input class="narrow" name="lead_{i}"
     value="{s["typical_lead_days"] if s["typical_lead_days"] is not None else ""}"></td>
</tr>"""
            for i, s in enumerate(SEED_SUPPLIERS)
        )
        return _shell(3, f"""<h2>Which suppliers do you actually use?</h2>
<p class="count">Untick the ones you don't. Shipping numbers are editable now and later
(/suppliers). Reliability ratings are yours to set after orders arrive — never guessed.</p>
<form method="post" action="/setup/suppliers">
<table><tr><th>Supplier</th><th>Free ship ≥ AUD</th><th>Shipping AUD</th><th>Lead days</th></tr>
{rows}</table>
<div class="actions"><button type="submit" class="primary">Create selected suppliers</button>
<a href="/setup?step=4">skip</a></div>
</form>""")
    if step == 4:
        config.load_env_file()
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if has_key:
            key_html = """<div class="note">✅ ANTHROPIC_API_KEY found — photo capture will
identify items automatically (name, part number, quantity, with confidence ratings and
questions for anything unreadable).</div>"""
        else:
            key_html = """<div class="note error">No ANTHROPIC_API_KEY found.</div>
<p><strong>What still works:</strong> everything. Captured photos queue as blank cards you
fill in by hand — nothing breaks, nothing is lost.</p>
<p><strong>What the key adds:</strong> each photo gets one AI vision pass that pre-fills
the fields and asks targeted questions about what it can't read. It never guesses.</p>
<p><strong>To add it:</strong> copy <code>.env.example</code> to <code>.env</code> next to
the app and put your key in it (get one at platform.claude.com), then restart the server.</p>"""
        return _shell(4, f"""<h2>Photo identification</h2>{key_html}
<p><a href="/setup?step=5">Next: first data →</a></p>""")
    if step == 5:
        return _shell(5, """<h2>Get your first items in</h2>
<p>Three ways, pick any:</p>
<ul>
<li><a href="/import"><strong>CSV import</strong></a> — bulk entry with a dry-run preview
(columns: name, part_number, qty, location, …).</li>
<li><a href="/m"><strong>Phone capture</strong></a> — photograph parts; each becomes a
review card.</li>
<li>Just type in the <a href="/">command bar</a> — <code>help</code> shows everything.</li>
</ul>
<div class="note">Setup done — re-run any step from /setup whenever (adding another
shelving unit later is the same storage step with the next unit letter).</div>""")

    # Step 1 — storage description (structured AND free text).
    return _shell(1, """<h2>Describe your storage</h2>
<p class="count">Either fill the boxes or just write it out — both work.</p>
<form method="post" action="/setup/storage">
  <div class="row"><label>Shelving units</label>
    <input class="narrow" name="units" type="number" value="1" min="1" max="26"></div>
  <div class="row"><label>Shelves per unit</label>
    <input class="narrow" name="shelves" type="number" value="4" min="1" max="30"></div>
  <div class="row"><label>Bins per shelf</label>
    <input class="narrow" name="bins" type="number" value="6" min="1" max="30"></div>
  <div class="row"><label>Sections</label>
    <input name="sections" placeholder="f,b for front/back — blank for none"></div>
  <p class="count">…or in plain language (this overrides the boxes if it parses):</p>
  <div class="row"><label>Free text</label>
    <input name="free_text" style="flex:2"
     placeholder="I've got 3 shelving units, 5 shelves each, 6 bins per shelf, deep bins have a front and back"></div>
  <div class="actions"><button type="submit" class="primary">Preview locations</button>
  <a href="/setup?step=3">skip (no shelving yet)</a></div>
</form>""")


@router.post("/setup/storage", response_class=HTMLResponse)
def setup_storage_preview(
    units: int = Form(1),
    shelves: int = Form(4),
    bins: int = Form(6),
    sections: str = Form(""),
    free_text: str = Form(""),
) -> str:
    section_list = [s for s in re.split(r"[\s,/]+", sections.strip().lower()) if s]
    if free_text.strip():
        parsed = parse_storage_text(free_text)
        units = parsed["units"] or units
        shelves = parsed["shelves"] or shelves
        bins = parsed["bins"] or bins
        section_list = parsed["sections"] or section_list
    units = max(1, min(units, 26))
    shelves = max(1, min(shelves, 30))
    bins = max(1, min(bins, 30))
    for section in section_list:
        if not re.fullmatch(r"[a-z]", section):
            return _shell(1, f"""<div class="note error">Section {_e(section)!s} must be a
single lowercase letter (like f or b). Go back and adjust.</div>
<p><a href="/setup">← back</a></p>""")
    ids = generate_location_ids(units, shelves, bins, section_list)
    # Preview tree before committing — grouped per unit.
    tree = ""
    for unit_index in range(units):
        unit = chr(ord("A") + unit_index)
        unit_ids = [i for i in ids if i.startswith(unit + "-")]
        tree += f"<details open><summary>Unit {unit} — {len(unit_ids)} bins ({_e(unit_ids[0])} … {_e(unit_ids[-1])})</summary><p class='count'>{_e(', '.join(unit_ids))}</p></details>"
    return _shell(1, f"""<h2>Preview — {len(ids)} locations</h2>
{tree}
<form method="post" action="/setup/storage/commit">
  <input type="hidden" name="units" value="{units}">
  <input type="hidden" name="shelves" value="{shelves}">
  <input type="hidden" name="bins" value="{bins}">
  <input type="hidden" name="sections" value="{_e(','.join(section_list))}">
  <div class="actions"><button type="submit" class="primary">Create all {len(ids)} locations</button>
  <a href="/setup">← adjust</a></div>
</form>""")


@router.post("/setup/storage/commit")
def setup_storage_commit(
    units: int = Form(...),
    shelves: int = Form(...),
    bins: int = Form(...),
    sections: str = Form(""),
) -> RedirectResponse:
    section_list = [s for s in sections.split(",") if s]
    for location_id in generate_location_ids(units, shelves, bins, section_list):
        _ensure_location(location_id)
    return RedirectResponse(url="/setup?step=2", status_code=303)


@router.post("/setup/suppliers")
async def setup_suppliers(request: Request) -> RedirectResponse:
    from . import store

    form = await request.form()
    conn = db.connect()
    try:
        existing = {r["name"] for r in conn.execute("SELECT name FROM suppliers")}
    finally:
        conn.close()
    for i, seed in enumerate(SEED_SUPPLIERS):
        if not form.get(f"use_{i}") or seed["name"] in existing:
            continue
        threshold = str(form.get(f"threshold_{i}", "")).strip() or None
        shipping = str(form.get(f"shipping_{i}", "")).strip() or None
        lead = str(form.get(f"lead_{i}", "")).strip()
        store.create_supplier(
            seed["name"],
            free_shipping_threshold_aud=threshold,
            typical_shipping_aud=shipping,
            typical_lead_days=int(lead) if lead.isdigit() else None,
        )
    return RedirectResponse(url="/setup?step=4", status_code=303)
