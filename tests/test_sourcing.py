import json
from decimal import Decimal

from fastapi.testclient import TestClient

from ravens_nest import db, events, pricing, replay, store
from ravens_nest.app import app
from ravens_nest.sourcing import SEED_SUPPLIERS, assemble_basket, candidate_baskets


def _client() -> TestClient:
    return TestClient(app)


def _supplier_id(name: str) -> str:
    conn = db.connect()
    try:
        return conn.execute("SELECT id FROM suppliers WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def _seed(client=None):
    (client or _client()).post("/suppliers/seed", follow_redirects=False)


# --------------------------------------------------------------- suppliers


def test_seed_suppliers_idempotent_and_unrated(data_dir):
    client = _client()
    _seed(client)
    _seed(client)  # second run adds nothing
    conn = db.connect()
    rows = conn.execute("SELECT name, reliability FROM suppliers ORDER BY name").fetchall()
    conn.close()
    assert len(rows) == len(SEED_SUPPLIERS) == 8
    assert {r["name"] for r in rows} == {s["name"] for s in SEED_SUPPLIERS}
    # Reliability is never seeded or inferred — the user sets it manually.
    assert all(r["reliability"] is None for r in rows)


def test_supplier_update_sets_manual_reliability(data_dir):
    _seed()
    sid = _supplier_id("Jaycar")
    response = _client().post(
        f"/suppliers/{sid}",
        data={
            "reliability": "4",
            "free_shipping_threshold_aud": "120",
            "typical_shipping_aud": "8.95",
            "typical_lead_days": "3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = db.connect()
    row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (sid,)).fetchone()
    conn.close()
    assert row["reliability"] == 4
    assert row["free_shipping_threshold_aud"] == "120"
    assert row["typical_lead_days"] == 3


# ------------------------------------------------------------- extraction


def test_extract_price_from_json_ld():
    html = (
        '<script type="application/ld+json">'
        + json.dumps({"@type": "Product", "offers": {"price": "12.95", "priceCurrency": "AUD"}})
        + "</script>"
    )
    assert pricing.extract_price("Core Electronics", html) == Decimal("12.95")


def test_extract_price_from_meta_tag():
    html = '<meta property="product:price:amount" content="4.50">'
    assert pricing.extract_price("Jaycar", html) == Decimal("4.50")


def test_extract_price_from_inline_json_key():
    html = '<script>window.state = {"salePrice": "3.99", "stock": 4}</script>'
    assert pricing.extract_price("AliExpress", html) == Decimal("3.99")


def test_extract_price_never_invents():
    assert pricing.extract_price("Core Electronics", "<html>no prices here</html>") is None
    assert pricing.extract_price("Core Electronics", '<meta itemprop="price" content="nope">') is None
    # A crashing strategy is isolated, not fatal.
    broken = '<script type="application/ld+json">{invalid json</script>'
    assert pricing.extract_price("Core Electronics", broken) is None


# ------------------------------------------------------- basket + pricing


def _setup_basket():
    """Two shortfall items with links at two suppliers."""
    _seed()
    core, ali = _supplier_id("Core Electronics"), _supplier_id("AliExpress")
    servo = store.create_item("SG90 servo", "each", qty_on_hand=0, min_qty=2)["payload"]["id"]
    screw = store.create_item("M3 screw", "each", qty_on_hand=0, min_qty=7)["payload"]["id"]
    store.add_item_link(servo, core, "https://core.example/sg90", pack_qty=1, last_price_aud="8.00")
    store.add_item_link(servo, ali, "https://ali.example/sg90", pack_qty=1, last_price_aud="3.00")
    store.add_item_link(screw, core, "https://core.example/m3", pack_qty=10, last_price_aud="5.00")
    return servo, screw, core, ali


def test_pack_qty_rounds_order_up(data_dir):
    _setup_basket()
    conn = db.connect()
    entries = assemble_basket(conn)
    conn.close()
    screw_entry = next(e for e in entries if e["name"] == "M3 screw")
    assert screw_entry["needed"] == "7"
    option = next(o for o in screw_entry["options"] if o["pack_qty"] == "10")
    assert option["packs"] == 1  # don't suggest 7 when it comes in packs of 10
    assert option["order_qty"] == "10"
    assert option["cost"] == "5.00"


def test_manual_basket_add_and_remove(data_dir):
    _seed()
    item = store.create_item("Random part", "each", qty_on_hand=50)["payload"]["id"]
    client = _client()
    client.post("/reorder/add", data={"item_id": item, "qty": "3"}, follow_redirects=False)
    page = client.get("/reorder").text
    assert "Random part" in page and "added manually" in page
    client.post("/reorder/remove", data={"item_id": item}, follow_redirects=False)
    page = client.get("/reorder").text
    assert "added manually" not in page  # basket row gone
    assert "Nothing needs reordering" in page  # (name stays in the add dropdown)


def test_price_basket_updates_success_and_falls_back_stale(data_dir, monkeypatch):
    servo, screw, core, ali = _setup_basket()

    def fake_fetch(url):
        if "core.example/sg90" in url:
            return '<meta itemprop="price" content="9.25">'
        if "ali.example" in url:
            raise RuntimeError("connection timed out")
        return "<html>page redesign, no price</html>"

    monkeypatch.setattr(pricing, "fetch_url", fake_fetch)
    page = _client().post("/reorder/price").text
    assert "Priced 1 of 3 link(s)" in page
    assert "2 fell back to their stored price (marked stale)" in page

    conn = db.connect()
    updated = conn.execute(
        "SELECT last_price_aud, last_checked_ts FROM item_links WHERE item_id = ? AND supplier_id = ?",
        (servo, core),
    ).fetchone()
    stale = conn.execute(
        "SELECT last_price_aud FROM item_links WHERE item_id = ? AND supplier_id = ?",
        (servo, ali),
    ).fetchone()
    conn.close()
    assert updated["last_price_aud"] == "9.25"  # success updates price + ts
    assert updated["last_checked_ts"] is not None
    assert stale["last_price_aud"] == "3"  # failure falls back, never invents
    checks = [e for e in events.read_all_events() if e["type"] == "item.link_price_checked"]
    assert len(checks) == 1


# ------------------------------------------------------------- candidates


def test_candidate_baskets_cheapest_fewest_fastest(data_dir):
    _seed()
    client = _client()
    core, ali = _supplier_id("Core Electronics"), _supplier_id("AliExpress")
    # Make lead/reliability contrast: rate Core, leave Ali unrated.
    client.post(f"/suppliers/{core}", data={
        "reliability": "5", "free_shipping_threshold_aud": "99",
        "typical_shipping_aud": "7.50", "typical_lead_days": "2",
    })
    servo = store.create_item("SG90 servo", "each", qty_on_hand=0, min_qty=2)["payload"]["id"]
    driver = store.create_item("Servo driver", "each", qty_on_hand=0, min_qty=1)["payload"]["id"]
    nolink = store.create_item("Unsourceable", "each", qty_on_hand=0, min_qty=1)["payload"]["id"]
    store.add_item_link(servo, core, "https://core.example/sg90", last_price_aud="8.00")
    store.add_item_link(servo, ali, "https://ali.example/sg90", last_price_aud="3.00")
    store.add_item_link(driver, core, "https://core.example/drv", last_price_aud="30.00")

    conn = db.connect()
    entries = assemble_basket(conn)
    conn.close()
    candidates = candidate_baskets(entries)
    by_label = {c["label"]: c for c in candidates}

    cheapest = next(c for l, c in by_label.items() if "Cheapest" in l)
    # servo x2 from Ali (2x3=6, free ship) + driver from Core (30 + 7.50 ship)
    assert cheapest["total"] == "43.50"
    assert cheapest["covered"] == 2 and cheapest["total_items"] == 3  # coverage 2/3
    assert cheapest["supplier_count"] == 2
    assert cheapest["lead_days"] == 21  # slowest supplier bounds the basket

    fewest = next(c for l, c in by_label.items() if "Fewest" in l or "fewest" in l)
    # Everything from Core: 2x8 + 30 = 46 + 7.50 shipping (under $99 threshold)
    assert fewest["supplier_count"] == 1
    assert fewest["total"] == "53.50"
    assert fewest["lead_days"] == 2
    assert fewest["mean_reliability"] == 5.0

    fastest = next(c for l, c in by_label.items() if "Fastest" in l or "fastest" in l)
    assert fastest["lead_days"] == 2  # all-Core is also the fastest
    # Fastest assignment == fewest assignment, so they merged into one card.
    assert fastest is fewest


def test_free_shipping_threshold_applies(data_dir):
    _seed()
    core = _supplier_id("Core Electronics")
    item = store.create_item("Big spend", "each", qty_on_hand=0, min_qty=2)["payload"]["id"]
    store.add_item_link(item, core, "https://core.example/big", last_price_aud="60.00")
    conn = db.connect()
    entries = assemble_basket(conn)
    conn.close()
    candidate = candidate_baskets(entries)[0]
    # 2 x 60 = 120 >= 99 threshold -> free shipping
    assert candidate["total"] == "120.00"
    assert candidate["suppliers"][0]["free_shipping"] is True


def test_basket_page_shows_candidates_and_uncovered(data_dir):
    _setup_basket()
    store.create_item("Unsourceable", "each", qty_on_hand=0, min_qty=1)
    page = _client().get("/reorder").text
    assert "Candidate baskets" in page
    assert "Cheapest total" in page
    assert "inc GST" in page
    assert "no supplier links" in page  # uncovered item flagged
    assert "unrated" in page  # unrated suppliers surfaced, never guessed


# ----------------------------------------------------------------- orders


def test_receive_order_sets_price_stock_and_rating(data_dir):
    _seed()
    core = _supplier_id("Core Electronics")
    servo = store.create_item("SG90 servo", "each", qty_on_hand=1)["payload"]["id"]
    screw = store.create_item("M3 screw", "each", qty_on_hand=0)["payload"]["id"]
    store.add_basket_item(servo, 5)  # manual basket entry should clear on receipt

    client = _client()
    response = client.post(
        "/orders/receive",
        data={
            "supplier_id": core,
            "reliability": "4",
            "item_id": [servo, screw, ""],
            "qty": ["5", "100", ""],
            "unit_price": ["7.95", "0.03", ""],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    conn = db.connect()
    servo_row = conn.execute(
        "SELECT qty_on_hand, last_paid_aud FROM items WHERE id = ?", (servo,)
    ).fetchone()
    screw_row = conn.execute(
        "SELECT qty_on_hand, last_paid_aud FROM items WHERE id = ?", (screw,)
    ).fetchone()
    rating = conn.execute(
        "SELECT reliability FROM suppliers WHERE id = ?", (core,)
    ).fetchone()[0]
    basket = conn.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0]
    conn.close()

    assert (servo_row["qty_on_hand"], servo_row["last_paid_aud"]) == ("6", "7.95")
    assert (screw_row["qty_on_hand"], screw_row["last_paid_aud"]) == ("100", "0.03")
    assert rating == 4  # prompted, manually chosen
    assert basket == 0  # received item cleared from the manual basket

    adjustments = [e for e in events.read_all_events() if e["type"] == "item.qty_adjusted"]
    assert len(adjustments) == 2
    assert all("order received: Core Electronics" == e["payload"]["reason"] for e in adjustments)


def test_receive_order_without_rating_keeps_supplier_unrated(data_dir):
    _seed()
    core = _supplier_id("Core Electronics")
    item = store.create_item("Part", "each")["payload"]["id"]
    _client().post(
        "/orders/receive",
        data={"supplier_id": core, "reliability": "", "item_id": [item], "qty": ["2"], "unit_price": [""]},
        follow_redirects=False,
    )
    conn = db.connect()
    assert conn.execute(
        "SELECT reliability FROM suppliers WHERE id = ?", (core,)
    ).fetchone()[0] is None
    conn.close()


# ------------------------------------------------------------------ replay


def test_replay_rebuild_reproduces_sourcing_state(data_dir):
    _setup_basket()
    core = _supplier_id("Core Electronics")
    _client().post(f"/suppliers/{core}", data={"reliability": "5"})
    item = store.create_item("Manual thing", "each")["payload"]["id"]
    store.add_basket_item(item, 2)

    def snapshot():
        conn = db.connect()
        try:
            return {
                "suppliers": sorted(tuple(r) for r in conn.execute("SELECT * FROM suppliers")),
                "links": sorted(tuple(r) for r in conn.execute("SELECT * FROM item_links")),
                "basket": sorted(tuple(r) for r in conn.execute("SELECT item_id, qty FROM basket_items")),
            }
        finally:
            conn.close()

    before = snapshot()
    replay.rebuild()
    assert snapshot() == before
