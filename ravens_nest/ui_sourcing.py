"""Rendering for suppliers, item sourcing links, the reorder basket with
candidate order baskets, and the received-order form."""

from __future__ import annotations

from typing import Any

from .ui import _e, page
from .ui_projects import _NAV, _TABLE_STYLE

_EXTRA_STYLE = """<style>
.candidate { border: 1px solid #cbd5e1; border-radius: 8px; padding: .7rem; margin: .6rem 0; }
.candidate h3 { margin: 0 0 .3rem; font-size: 1rem; }
.candidate .meta { color: #555; font-size: .85rem; }
.stale { color: #b45309; font-size: .8rem; }
.stars { color: #b45309; }
.orderrow select, .orderrow input { margin-right: .3rem; }
</style>"""


def _stars(rating: Any) -> str:
    if rating is None:
        return '<span class="count">unrated</span>'
    return f'<span class="stars">{"★" * int(rating)}{"☆" * (5 - int(rating))}</span>'


def suppliers_page(suppliers: list[dict[str, Any]]) -> str:
    if suppliers:
        rows = []
        for s in suppliers:
            rows.append(f"""<tr>
<td>{_e(s["name"])}</td>
<td>{_stars(s["reliability"])}</td>
<form method="post" action="/suppliers/{_e(s["id"])}">
<td><select name="reliability">
  <option value="">keep</option>
  {"".join(f'<option value="{n}">{n}</option>' for n in range(1, 6))}
</select></td>
<td><input class="narrow" name="free_shipping_threshold_aud"
     value="{_e(s["free_shipping_threshold_aud"]) if s["free_shipping_threshold_aud"] is not None else ""}"></td>
<td><input class="narrow" name="typical_shipping_aud"
     value="{_e(s["typical_shipping_aud"]) if s["typical_shipping_aud"] is not None else ""}"></td>
<td><input class="narrow" name="typical_lead_days"
     value="{_e(s["typical_lead_days"]) if s["typical_lead_days"] is not None else ""}"></td>
<td><button type="submit">Save</button></td>
</form>
</tr>""")
        table = (
            "<table><tr><th>Supplier</th><th>Reliability</th><th>Set rating</th>"
            "<th>Free ship ≥ AUD</th><th>Shipping AUD</th><th>Lead days</th><th></th></tr>"
            + "".join(rows)
            + "</table><p class='count'>Reliability is yours to set — rate a supplier "
            "after an order arrives (or from the receive-order form). It is never "
            "scraped or inferred. Shipping defaults are rough; edit them here.</p>"
        )
    else:
        table = """<p>No suppliers yet.</p>
<form method="post" action="/suppliers/seed">
  <button type="submit" class="primary">Seed standard suppliers</button>
  <span class="count">Core Electronics, element14, RS Components, DigiKey, Mouser,
  AliExpress, Bunnings, Jaycar — unrated, with rough shipping defaults.</span>
</form>"""
    body = f"""{_NAV}{_TABLE_STYLE}{_EXTRA_STYLE}
<h1>Suppliers</h1>
{table}
<p><a href="/orders/receive">Record a received order</a></p>"""
    return page("The Raven's Nest — Suppliers", body)


def item_sourcing_page(
    item: dict[str, Any], links: list[dict[str, Any]], suppliers: list[dict[str, Any]]
) -> str:
    link_rows = "".join(
        f"<tr><td>{_e(l['supplier_name'])}</td>"
        f"<td><a href=\"{_e(l['url'])}\" rel=\"noopener\">{_e(l['sku'] or 'link')}</a></td>"
        f"<td class='num'>{_e(l['pack_qty'])}</td>"
        f"<td class='num'>{_e(l['last_price_aud']) if l['last_price_aud'] is not None else '—'}</td>"
        f"<td>{_e(l['last_checked_ts'][:10]) if l['last_checked_ts'] else 'never'}</td></tr>"
        for l in links
    )
    links_table = (
        f"<table><tr><th>Supplier</th><th>Product</th><th class='num'>Pack qty</th>"
        f"<th class='num'>Pack price AUD</th><th>Last checked</th></tr>{link_rows}</table>"
        if links
        else "<p>No supplier links yet.</p>"
    )
    supplier_options = "".join(
        f'<option value="{_e(s["id"])}">{_e(s["name"])}</option>' for s in suppliers
    )
    add_form = (
        f"""<h2>Add supplier link</h2>
<form method="post" action="/items/{_e(item["id"])}/links">
  <div class="row"><label>Supplier</label><select name="supplier_id">{supplier_options}</select></div>
  <div class="row"><label>Product URL</label><input name="url" required placeholder="https://…"></div>
  <div class="row"><label>SKU</label><input name="sku"></div>
  <div class="row"><label>Pack qty</label><input name="pack_qty" value="1"
    title="units of this item per pack as sold"></div>
  <div class="row"><label>Pack price AUD</label><input name="last_price_aud"
    placeholder="optional — filled by Price the basket"></div>
  <div class="actions"><button type="submit" class="primary">Save link</button></div>
</form>"""
        if suppliers
        else '<p>Seed suppliers first: <a href="/suppliers">Suppliers</a></p>'
    )
    body = f"""{_NAV}{_TABLE_STYLE}{_EXTRA_STYLE}
<h1>Sourcing — {_e(item["name"])}</h1>
<p class="count">{_e(item["qty_on_hand"])} {_e(item["unit_type"])} on hand
{" · part " + _e(item["part_number"]) if item["part_number"] else ""}</p>
{links_table}
{add_form}
<p><a href="/reorder">Back to reorder basket</a></p>"""
    return page(f"The Raven's Nest — Sourcing {item['name']}", body)


def _candidate_card(candidate: dict[str, Any]) -> str:
    supplier_lines = []
    for s in candidate["suppliers"]:
        if s["free_shipping"]:
            shipping = f"free shipping (over ${_e(s['threshold'])})"
        elif s["shipping"] != "0.00":
            shipping = f"+ ${_e(s['shipping'])} shipping"
        else:
            shipping = "shipping unknown" if s["threshold"] is None and s["shipping"] == "0.00" else "free shipping"
        lead = f"{s['lead']} days" if s["lead"] is not None else "lead unknown"
        supplier_lines.append(
            f"<li><strong>{_e(s['supplier_name'])}</strong>: {s['line_count']} item(s), "
            f"${_e(s['subtotal'])} {shipping}, {lead}, {_stars(s['reliability'])}</li>"
        )
    lead = (
        f"{candidate['lead_days']} days"
        if candidate["lead_days"] is not None
        else "unknown"
    )
    if candidate["lead_days"] is not None and not candidate["lead_certain"]:
        lead += " (some suppliers unknown)"
    mean = (
        f"{candidate['mean_reliability']}★ (from {candidate['rated_suppliers']} rated)"
        if candidate["mean_reliability"] is not None
        else "unrated"
    )
    return f"""<div class="candidate">
<h3>{_e(candidate["label"])} — ${_e(candidate["total"])} AUD inc GST</h3>
<p class="meta">Coverage {candidate["covered"]}/{candidate["total_items"]} items ·
{candidate["supplier_count"]} supplier(s) · lead ~{lead} · mean reliability {mean}</p>
<ul>{"".join(supplier_lines)}</ul>
</div>"""


def basket_page(
    entries: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    items: list[dict[str, Any]],
    notice: str | None = None,
    stale_notes: list[str] | None = None,
) -> str:
    banner = f'<div class="note">{_e(notice)}</div>' if notice else ""
    if stale_notes:
        banner += "".join(
            f'<div class="stale">stale: {_e(note)}</div>' for note in stale_notes
        )

    if entries:
        rows = []
        for entry in entries:
            priced = [o for o in entry["options"] if o.get("packs") is not None]
            if priced:
                best = min(priced, key=lambda o: o["cost_dec"])
                checked = (
                    best["last_checked_ts"][:10] if best["last_checked_ts"] else "never"
                )
                option_text = (
                    f"{_e(best['supplier_name'])}: {best['packs']} pack(s) of "
                    f"{_e(best['pack_qty'])} = {_e(best['order_qty'])} for ${_e(best['cost'])} "
                    f'<span class="stale">(price from {checked})</span>'
                )
            elif entry["options"]:
                option_text = '<span class="flag">links exist but no price yet</span>'
            else:
                option_text = '<span class="flag">no supplier links</span>'
            remove = (
                f'<form class="inline" method="post" action="/reorder/remove">'
                f'<input type="hidden" name="item_id" value="{_e(entry["id"])}">'
                f"<button type='submit'>Remove</button></form>"
                if entry["manual"]
                else ""
            )
            rows.append(
                f"<tr><td><a href=\"/items/{_e(entry['id'])}/sourcing\">{_e(entry['name'])}</a></td>"
                f"<td>{_e(entry['unit_type'])}</td>"
                f"<td class='num'>{_e(entry['free'])}</td>"
                f"<td class='num'><strong>{_e(entry['needed'])}</strong></td>"
                f"<td>{_e('; '.join(entry['reasons']))}</td>"
                f"<td>{option_text}</td><td>{remove}</td></tr>"
            )
        table = (
            "<table><tr><th>Item</th><th>Unit</th><th class='num'>Free</th>"
            "<th class='num'>Needed</th><th>Why</th><th>Best option</th><th></th></tr>"
            + "".join(rows)
            + "</table><p class='count'>'each' items round up to whole units; g/mm/mL "
            "amounts are native. Order quantities account for pack sizes.</p>"
        )
    else:
        table = "<p>Nothing needs reordering. 🎉</p>"

    item_options = "".join(
        f'<option value="{_e(i["id"])}">{_e(i["name"])}</option>' for i in items
    )
    candidates_html = (
        "<h2>Candidate baskets</h2>" + "".join(_candidate_card(c) for c in candidates)
        if candidates
        else ""
    )
    body = f"""{_NAV}{_TABLE_STYLE}{_EXTRA_STYLE}
<h1>Reorder basket</h1>
{banner}
{table}
<form class="inline" method="post" action="/reorder/add">
  <select name="item_id">{item_options}</select>
  <input class="narrow" name="qty" value="1">
  <button type="submit">Add to basket</button>
</form>
<form class="inline" method="post" action="/reorder/price">
  <button type="submit" class="primary">Price the basket</button>
  <span class="count">fetches each stored product URL now — nothing runs in the background</span>
</form>
{candidates_html}
<p><a href="/orders/receive">Record a received order</a> · <a href="/suppliers">Suppliers</a></p>"""
    return page("The Raven's Nest — Reorder basket", body)


def receive_order_page(
    suppliers: list[dict[str, Any]], items: list[dict[str, Any]]
) -> str:
    supplier_options = "".join(
        f'<option value="{_e(s["id"])}">{_e(s["name"])}</option>' for s in suppliers
    )
    item_options = '<option value="">—</option>' + "".join(
        f'<option value="{_e(i["id"])}">{_e(i["name"])} ({_e(i["unit_type"])})</option>'
        for i in items
    )
    line_rows = "".join(
        f"""<div class="row orderrow">
  <select name="item_id">{item_options}</select>
  <input class="narrow" name="qty" placeholder="qty">
  <input class="narrow" name="unit_price" placeholder="AUD/unit">
</div>"""
        for _ in range(6)
    )
    body = f"""{_NAV}{_TABLE_STYLE}{_EXTRA_STYLE}
<h1>Record received order</h1>
<p class="count">Sets each item's last paid price, adds the received stock
(qty_adjusted events), and prompts for the supplier's reliability rating.</p>
<form method="post" action="/orders/receive">
  <div class="row"><label>Supplier</label><select name="supplier_id">{supplier_options}</select></div>
  <h2>Lines received</h2>
  {line_rows}
  <div class="row"><label>Reliability rating</label>
    <select name="reliability">
      <option value="">leave unchanged</option>
      {"".join(f'<option value="{n}">{n} — {"★" * n}</option>' for n in range(1, 6))}
    </select>
    <span class="count">your call, after seeing how the order went</span>
  </div>
  <div class="actions"><button type="submit" class="primary">Record order</button></div>
</form>"""
    return page("The Raven's Nest — Receive order", body)
