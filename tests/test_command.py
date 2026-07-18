from fastapi.testclient import TestClient

from ravens_nest import db, events, store
from ravens_nest.app import app
from ravens_nest.commands import parse


def _client() -> TestClient:
    return TestClient(app)


# ------------------------------------------------------------------ parser


def test_parse_grammar():
    assert parse("3mm heat shrink") == {
        "kind": "search", "query": "3mm heat shrink", "scope": "default",
    }
    assert parse("A-2-3b") == {"kind": "bin", "location_id": "A-2-3b"}
    assert parse("move to A-2-3b") == {"kind": "move", "location_id": "A-2-3b"}
    assert parse("MOVE A-2-3b") == {"kind": "move", "location_id": "A-2-3b"}
    assert parse("build RPSRobot x2") == {"kind": "build", "query": "RPSRobot", "count": 2}
    assert parse("build RPSRobot") == {"kind": "build", "query": "RPSRobot", "count": 1}
    assert parse("build Servo Tester ×3") == {"kind": "build", "query": "Servo Tester", "count": 3}
    assert parse("need 20 more heat shrink") == {"kind": "need", "qty": "20", "query": "heat shrink"}
    assert parse("need 5 m3 screws") == {"kind": "need", "qty": "5", "query": "m3 screws"}
    assert parse("recount A-2-3b") == {"kind": "recount", "location_id": "A-2-3b"}
    assert parse("low") == {"kind": "low"}
    assert parse("price basket") == {"kind": "price_basket"}
    assert parse("  ") == {"kind": "empty"}
    assert parse("move to nowhere")["kind"] == "invalid"


# ------------------------------------------------------------------ search


def test_search_shows_stock_summary_in_spec_format(data_dir):
    item = store.create_item(
        "3mm heat shrink", "mm", qty_on_hand=12, location_id="A-2-3b"
    )["payload"]["id"]
    project = store.create_project("P")["payload"]["id"]
    store.create_reservation(project, item, 8)

    response = _client().get("/command", params={"q": "heat shrink"})
    assert "3mm heat shrink" in response.text
    assert "A-2-3b, 12 left, 4 free (8 reserved)" in response.text
    assert f"/items/{item}" in response.text  # result links to the item card


def test_search_matches_aliases_and_part_numbers(data_dir):
    item = store.create_item(
        "Hex bolt M6", "each", qty_on_hand=5, part_number="HB-M6"
    )["payload"]["id"]
    store.match_bom_line("some-project", 1, item, "manual", alias_text="BOLT-HEX-6")
    client = _client()
    assert "Hex bolt M6" in client.get("/command", params={"q": "hb-m6"}).text
    assert "Hex bolt M6" in client.get("/command", params={"q": "BOLT-HEX-6"}).text
    assert "Hex bolt M6" in client.get("/command", params={"q": "hex blot"}).text  # fuzzy typo


def test_bin_command_lists_contents(data_dir):
    store.create_location("A-2-3b", "small fasteners")
    store.create_item("M3 screw", "each", qty_on_hand=40, location_id="A-2-3b")
    response = _client().get("/command", params={"q": "A-2-3b"})
    assert "small fasteners" in response.text
    assert "M3 screw" in response.text
    assert "40 each on hand" in response.text


def test_low_command_lists_under_min(data_dir):
    store.create_item("Solder", "g", qty_on_hand=5, min_qty=20)
    store.create_item("Plenty", "each", qty_on_hand=100, min_qty=10)
    response = _client().get("/command", params={"q": "low"})
    assert "Solder" in response.text
    assert "Plenty" not in response.text


def test_live_typing_never_executes_actions(data_dir):
    item = store.create_item("Unique widget", "each")["payload"]["id"]
    response = _client().get("/command", params={"q": "need 5 more unique widget"})
    assert "Press Enter" in response.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0] == 0
    conn.close()


# ----------------------------------------------------------------- actions


def test_need_unique_match_adds_to_basket_additively(data_dir):
    store.create_item("Unique widget", "each")
    client = _client()
    response = client.post("/command", data={"q": "need 20 more unique widget"})
    assert "Added 20" in response.text
    response = client.post("/command", data={"q": "need 5 more unique widget"})
    assert "manual total now 25" in response.text


def test_need_ambiguous_asks_instead_of_guessing(data_dir):
    store.create_item("M3 screw 12mm", "each")
    store.create_item("M3 screw 16mm", "each")
    client = _client()
    response = client.post("/command", data={"q": "need 10 more m3 screw"})
    assert "which one" in response.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0] == 0
    conn.close()
    # Resolving via the pick-list button performs the add.
    item = store.create_item("M4 nut", "each")["payload"]["id"]
    client.post("/command/need", data={"item_id": item, "qty": "10"})
    conn = db.connect()
    row = conn.execute("SELECT qty FROM basket_items WHERE item_id = ?", (item,)).fetchone()
    conn.close()
    assert row["qty"] == "10"


def _built_project():
    servo = store.create_item("Servo", "each", qty_on_hand=4, part_number="SV")["payload"]["id"]
    pid = store.create_project("RPSRobot")["payload"]["id"]
    store.import_bom(pid, [{
        "line_no": 1, "part_number": "SV", "description": "Servo",
        "quantity": "2", "unit": "each", "reference_designators": None, "notes": None,
    }])
    store.match_bom_line(pid, 1, servo, "part_number")
    return pid, servo


def test_build_command_confirms_then_executes(data_dir):
    pid, servo = _built_project()
    client = _client()
    response = client.post("/command", data={"q": "build RPSRobot x2"})
    assert "Build RPSRobot ×2" in response.text
    assert "Confirm build ×2" in response.text  # confirmation, not execution
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 0
    conn.close()

    response = client.post("/command/build", data={"project_id": pid, "count": "2"})
    assert "Built ×2" in response.text
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (servo,)).fetchone()[0]
    conn.close()
    assert qty == "0"


def test_build_command_shows_shortage_without_confirm_button(data_dir):
    _built_project()
    response = _client().post("/command", data={"q": "build RPSRobot x9"})
    assert "Short on" in response.text
    assert "Confirm build" not in response.text  # blocked, no button offered


def test_build_fuzzy_project_asks(data_dir):
    _built_project()
    store.create_project("RPS Rover")
    response = _client().post("/command", data={"q": "build rps x1"})
    assert "Which project" in response.text


def test_recount_command_flow(data_dir):
    store.create_location("A-1-1")
    a = store.create_item("Part A", "each", qty_on_hand=10, location_id="A-1-1")["payload"]["id"]
    b = store.create_item("Part B", "each", qty_on_hand=5, location_id="A-1-1")["payload"]["id"]
    client = _client()
    response = client.post("/command", data={"q": "recount A-1-1"})
    assert "Recount A-1-1" in response.text and "Part A" in response.text

    response = client.post(
        "/command/recount",
        data={"location_id": "A-1-1", "item_id": [a, b], "counted": ["8", "5"]},
    )
    assert "1 corrected, 1 already right" in response.text
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (a,)).fetchone()[0]
    conn.close()
    assert qty == "8"
    recounts = [e for e in events.read_all_events() if e["type"] == "item.recounted"]
    assert len(recounts) == 1  # unchanged count emitted nothing


def test_move_command_opens_move_panel(data_dir):
    client = _client()
    response = client.post("/command", data={"q": "move to B-1-2"})
    assert "Move items → B-1-2" in response.text
    assert 'hx-post="/move/scan"' in response.text  # reuses the move flow
    # And the flow works end-to-end from here:
    item = store.create_item("Gearbox", "each")["payload"]["id"]
    client.post("/move/scan", data={"code": item, "location_id": "B-1-2"})
    conn = db.connect()
    loc = conn.execute("SELECT location_id FROM items WHERE id = ?", (item,)).fetchone()[0]
    conn.close()
    assert loc == "B-1-2"


def test_price_basket_command(data_dir, monkeypatch):
    from ravens_nest import pricing

    monkeypatch.setattr(
        pricing, "fetch_url", lambda url: '<meta itemprop="price" content="2.50">'
    )
    _client().post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    core = conn.execute("SELECT id FROM suppliers WHERE name='Core Electronics'").fetchone()[0]
    conn.close()
    item = store.create_item("Widget", "each", qty_on_hand=0, min_qty=2)["payload"]["id"]
    store.add_item_link(item, core, "https://core.example/w", last_price_aud="9.99")

    response = _client().post("/command", data={"q": "price basket"})
    assert "Priced 1 of 1 link(s)" in response.text
    conn = db.connect()
    price = conn.execute("SELECT last_price_aud FROM item_links").fetchone()[0]
    conn.close()
    assert price == "2.5"


# --------------------------------------------------------------- item card


def test_item_card_shows_everything(data_dir):
    _client().post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    core = conn.execute("SELECT id FROM suppliers WHERE name='Jaycar'").fetchone()[0]
    conn.close()
    item = store.create_item(
        "SG90 servo", "each", qty_on_hand=10, part_number="SG90",
        location_id="A-2-3b", last_paid_aud="4.50", photo_hash="ab" * 32,
    )["payload"]["id"]
    pid = store.create_project("Hexapod")["payload"]["id"]
    store.create_reservation(pid, item, 8)
    store.adjust_qty(item, -2, "used in prototype")
    store.move_item(item, "B-1-1")
    store.add_item_link(item, core, "https://jaycar.example/sg90", sku="SM-SG90",
                        pack_qty=1, last_price_aud="5.95")

    page = _client().get(f"/items/{item}").text
    assert "SG90 servo" in page
    assert f"/assets/{'ab' * 32}.jpg" in page  # photo
    assert "SG90" in page  # part number
    assert "B-1-1" in page  # current location
    assert ">8<" in page and "Hexapod" in page  # reservation by project
    assert ">0<" in page  # free = 8 on hand - 8 reserved
    assert "4.5" in page  # last paid
    assert "Jaycar" in page and "SM-SG90" in page  # supplier links
    # Event history — narrated, with actor and prior locations:
    assert "Created with 10 each in A-2-3b" in page
    assert "used in prototype" in page  # "Adjusted -2, reason: '...'"
    assert "Moved A-2-3b → B-1-1" in page
    assert "Reserved 8 for Hexapod" in page


# ------------------------------------------------------------ entry points


def test_index_is_the_command_bar(data_dir):
    page = _client().get("/").text
    assert 'id="cmd"' in page
    assert 'hx-get="/command"' in page  # instant results as you type
    assert "/static/command.js" in page
    assert "<nav>" not in page  # no navigation tree


def test_mobile_page_wiring(data_dir):
    page = _client().get("/m").text
    assert "Capture item" in page and 'capture="environment"' in page
    assert "Scan location label" in page
    assert "/static/jsQR.js" in page  # client-side QR decode of still photos
    assert "/static/mobile.js" in page  # offline capture queue
    assert "m-search" in page
