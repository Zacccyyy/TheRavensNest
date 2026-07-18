from fastapi.testclient import TestClient

from ravens_nest import db, events, store
from ravens_nest.app import app


def _client() -> TestClient:
    return TestClient(app)


def _item_location(item_id: str) -> str | None:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT location_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------------------------ labels


def test_label_sheet_renders_qr_and_readable_id(data_dir):
    store.create_location("A-1-1", "small parts")
    store.create_location("A-1-2")
    html = _client().get("/labels").text
    assert html.count("<svg") == 2  # one QR per location
    assert 'class="lid">A-1-1<' in html  # human-readable ID under the QR
    assert 'class="lid">A-1-2<' in html
    assert "@media print" in html  # print stylesheet present


def test_label_sheet_unit_filter(data_dir):
    store.create_location("A-1-1")
    store.create_location("B-1-1")
    html = _client().get("/labels", params={"unit": "b"}).text
    assert 'class="lid">B-1-1<' in html
    assert 'class="lid">A-1-1<' not in html


def test_batch_generate_creates_missing_locations_idempotently(data_dir):
    store.create_location("C-1-1f", "already here")
    client = _client()
    form = {"unit": "c", "shelves": "2", "bins": "2", "sections": "f,b"}
    response = client.post("/labels/generate", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/labels?unit=C"

    conn = db.connect()
    count = conn.execute("SELECT COUNT(*) FROM locations WHERE unit = 'C'").fetchone()[0]
    conn.close()
    assert count == 8  # 2 shelves x 2 bins x 2 sections

    client.post("/labels/generate", data=form, follow_redirects=False)
    creation_events = [
        e for e in events.read_all_events() if e["type"] == "location.created"
    ]
    assert len(creation_events) == 8  # 7 new + 1 pre-existing; rerun added none


def test_batch_generate_rejects_bad_input(data_dir):
    client = _client()
    assert client.post(
        "/labels/generate", data={"unit": "AB", "shelves": "2", "bins": "2"}
    ).status_code == 400
    assert client.post(
        "/labels/generate", data={"unit": "A", "shelves": "99", "bins": "2"}
    ).status_code == 400


# ------------------------------------------------------------ move console


def test_scan_location_sets_target_and_creates_record(data_dir):
    response = _client().post("/move/scan", data={"code": "A-2-3b", "location_id": ""})
    assert response.status_code == 200
    assert "Target location set to A-2-3b" in response.text
    assert "new location record created" in response.text
    assert 'name="location_id" value="A-2-3b"' in response.text  # OOB target box
    conn = db.connect()
    assert conn.execute("SELECT 1 FROM locations WHERE id = 'A-2-3b'").fetchone()
    conn.close()


def test_scan_item_id_with_target_moves_immediately(data_dir):
    item = store.create_item("M3 screw", "each")["payload"]["id"]
    response = _client().post("/move/scan", data={"code": item, "location_id": "A-1-1"})
    assert "Moved M3 screw → A-1-1" in response.text
    assert _item_location(item) == "A-1-1"
    moved = [e for e in events.read_all_events() if e["type"] == "item.moved"]
    assert len(moved) == 1
    assert moved[0]["payload"] == {"item_id": item, "location_id": "A-1-1"}


def test_scan_part_number_case_insensitive_rattle_through(data_dir):
    # The bulk pattern: one location scan, then several item scans in a row.
    first = store.create_item("M3 screw", "each", part_number="M3X12")["payload"]["id"]
    second = store.create_item("M4 screw", "each", part_number="PN-77")["payload"]["id"]
    client = _client()
    client.post("/move/scan", data={"code": "B-1-2", "location_id": ""})
    client.post("/move/scan", data={"code": "m3x12", "location_id": "B-1-2"})
    client.post("/move/scan", data={"code": "pn-77", "location_id": "B-1-2"})
    assert _item_location(first) == "B-1-2"
    assert _item_location(second) == "B-1-2"


def test_scan_alias_moves_item(data_dir):
    item = store.create_item("Hex bolt M6", "each")["payload"]["id"]
    conn = db.connect()
    with conn:  # aliases will be populated by BOM matching later
        conn.execute("INSERT INTO aliases VALUES (?, ?)", ("HEXBOLT", item))
    conn.close()
    _client().post("/move/scan", data={"code": "hexbolt", "location_id": "A-1-1"})
    assert _item_location(item) == "A-1-1"


def test_scan_item_without_target_prompts_for_location(data_dir):
    item = store.create_item("M3 screw", "each")["payload"]["id"]
    response = _client().post("/move/scan", data={"code": item, "location_id": ""})
    assert "Scan a bin label first" in response.text
    assert _item_location(item) is None


def test_name_search_shows_choices_and_never_automoves(data_dir):
    item = store.create_item("M3 screw", "each")["payload"]["id"]
    response = _client().post(
        "/move/scan", data={"code": "screw", "location_id": "A-1-1"}
    )
    assert 'type="checkbox"' in response.text  # pick list, not a silent move
    assert "Move selected here" in response.text
    assert _item_location(item) is None


def test_scan_unknown_code_reports_no_match(data_dir):
    response = _client().post(
        "/move/scan", data={"code": "does-not-exist", "location_id": "A-1-1"}
    )
    assert "No item matches" in response.text


def test_bulk_move_assigns_one_location_to_many_items(data_dir):
    ids = [
        store.create_item(f"part-{i}", "each")["payload"]["id"] for i in range(3)
    ]
    response = _client().post(
        "/move",
        data={"location_id": "B-2-1", "item_ids": ids[:2]},
    )
    assert "Moved 2 item(s) → B-2-1" in response.text
    assert _item_location(ids[0]) == "B-2-1"
    assert _item_location(ids[1]) == "B-2-1"
    assert _item_location(ids[2]) is None
    moved = [e for e in events.read_all_events() if e["type"] == "item.moved"]
    assert len(moved) == 2


def test_bulk_move_requires_location_and_items(data_dir):
    item = store.create_item("part", "each")["payload"]["id"]
    client = _client()
    assert "Scan a bin label" in client.post(
        "/move", data={"location_id": "", "item_ids": [item]}
    ).text
    assert "No items selected" in client.post(
        "/move", data={"location_id": "A-1-1"}
    ).text


def test_move_page_has_scanner_wiring(data_dir):
    html = _client().get("/move").text
    assert 'id="scan-input"' in html  # USB scanner target: focused input + Enter
    assert "/static/jsQR.js" in html  # camera fallback for iOS Safari
    assert "/static/scan.js" in html
    assert "playsinline" in html  # iOS inline camera video


# ------------------------------------------------------------------- tree


def test_location_tree_counts_contents_and_empty_bins(data_dir):
    store.create_location("A-1-1", "fasteners")
    store.create_location("A-1-2")
    store.create_location("B-2-3")
    store.create_item("M3 screw", "each", qty_on_hand=25, location_id="A-1-1")
    store.create_item("M4 screw", "each", qty_on_hand=10, location_id="A-1-1")
    store.create_item("Stray part", "each", location_id="Z-9-9")  # no location record
    store.create_item("Homeless part", "each")  # no location at all

    html = _client().get("/locations").text
    assert "A-1-1" in html and "fasteners" in html
    assert "2 item(s)" in html  # bin count
    assert "M3 screw" in html  # what's-in-this-bin listing
    # Empty-bin detection: both empty bins flagged and summarized as free space.
    assert "Free space: 2 empty bin(s)" in html
    assert "A-1-2, B-2-3" in html
    assert html.count("empty</div>") == 2
    # Items pointing at unregistered locations and unassigned items surface too.
    assert "Unregistered locations" in html and "Z-9-9" in html
    assert "Unassigned" in html and "Homeless part" in html


def test_add_location_form(data_dir):
    response = _client().post(
        "/locations",
        data={"location_id": "D-1-1", "description": "power tools"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = db.connect()
    row = conn.execute("SELECT description FROM locations WHERE id = 'D-1-1'").fetchone()
    conn.close()
    assert row["description"] == "power tools"
    assert _client().post(
        "/locations", data={"location_id": "not-valid"}
    ).status_code == 400
