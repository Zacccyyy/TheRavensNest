"""Tests for the ten multi-user improvements: zero-qty/archived
visibility, history views, universal undo, first-run setup, help,
merge, CSV round-trip, multi-item capture, item labels, and health."""

import pytest
from fastapi.testclient import TestClient

from ravens_nest import db, events, history, importexport, ingest, merge, replay, store, undo, vision
from ravens_nest.app import app
from ravens_nest.movement import normalize_scan
from ravens_nest.setup_wizard import generate_location_ids, parse_storage_text

JPEG = b"\xff\xd8\xff\xe0" + b"fake image bytes"


def _client() -> TestClient:
    return TestClient(app)


def _qty(item_id):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _item(item_id):
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    finally:
        conn.close()


# ------------------------------------------------- 1. zero-qty & archived


def test_zero_qty_hidden_by_default_with_count(data_dir):
    store.create_item("Empty widget", "each", qty_on_hand=0)
    store.create_item("Stocked widget", "each", qty_on_hand=5)
    client = _client()
    page = client.get("/command", params={"q": "widget"}).text
    assert "Stocked widget" in page
    assert "Empty widget" not in page
    assert "1 zero-qty hidden" in page  # never silently omitted
    assert "all:" in page  # the reveal is advertised


def test_all_modifier_reveals_zero_qty(data_dir):
    store.create_item("Empty widget", "each", qty_on_hand=0)
    page = _client().get("/command", params={"q": "all: widget"}).text
    assert "Empty widget" in page
    assert "zeroed" in page  # greyed, visibly zero


def test_zero_qty_stays_in_bin_view_and_reorder(data_dir):
    item = store.create_item(
        "Empty widget", "each", qty_on_hand=0, min_qty=5, location_id="A-1-1"
    )["payload"]["id"]
    client = _client()
    bin_page = client.get("/command", params={"q": "A-1-1"}).text
    assert "Empty widget" in bin_page  # greyed, not hidden — the bin is theirs
    assert "zeroed" in bin_page
    reorder = client.get("/reorder").text
    assert "Empty widget" in reorder  # zero-qty still reorders
    assert item in reorder


def test_archived_excluded_everywhere_except_modifier(data_dir):
    item = store.create_item(
        "Retired part", "each", qty_on_hand=3, min_qty=10, part_number="RET-1"
    )["payload"]["id"]
    store.archive_item(item, "never buying again")
    client = _client()
    assert "Retired part" not in client.get("/command", params={"q": "retired"}).text
    assert "Retired part" not in client.get("/command", params={"q": "all: retired"}).text
    assert "Retired part" not in client.get("/reorder").text  # no reorder
    # BOM matching excludes archived: exact part number no longer auto-matches.
    conn = db.connect()
    from ravens_nest import bom

    context = bom.load_match_context(conn)
    conn.close()
    line = {"line_no": 1, "part_number": "RET-1", "description": "Retired part", "quantity": "1", "unit": "each"}
    assert bom.auto_match(line, context) is None
    # Reachable only via archived:
    page = client.get("/command", params={"q": "archived: retired"}).text
    assert "Retired part" in page and "ARCHIVED" in page


def test_unarchive_restores_item(data_dir):
    item = store.create_item("Retired part", "each", qty_on_hand=3)["payload"]["id"]
    store.archive_item(item)
    store.unarchive_item(item)
    assert _item(item)["archived"] == 0
    assert "Retired part" in _client().get("/command", params={"q": "retired"}).text


# --------------------------------------------------------- 2. history views


def test_item_history_is_narrated_with_actor(data_dir):
    item = store.create_item("Servo", "each", qty_on_hand=10, location_id="A-1-1")["payload"]["id"]
    store.move_item(item, "B-2-2")
    store.recount_item(item, 8)
    log = history.load_log()
    conn = db.connect()
    data = history.build_entries(conn, history.item_events(log, item), log, focus_item=item)
    conn.close()
    texts = [e["text"] for e in data["entries"]]
    assert "Recounted 10 → 8 (correction -2)" in texts
    assert "Moved A-1-1 → B-2-2" in texts
    assert all(e["actor"] for e in data["entries"])  # actor always shown


def test_bin_history_tracks_arrivals_and_departures(data_dir):
    item = store.create_item("Servo", "each", location_id="A-1-1")["payload"]["id"]
    store.move_item(item, "B-2-2")
    page = _client().post("/command", data={"q": "history A-1-1"}).text
    assert "arrived (created here)" in page
    assert "left for B-2-2" in page


def test_history_pagination_and_type_filter(data_dir):
    item = store.create_item("Servo", "each")["payload"]["id"]
    for i in range(25):
        store.adjust_qty(item, 1, f"tick {i}")
    client = _client()
    page1 = client.get(
        "/command/history", params={"target": f"item:{item}"}
    ).text
    assert "page 1/2" in page1
    assert "older →" in page1
    filtered = client.get(
        "/command/history",
        params={"target": f"item:{item}", "type": "item.created"},
    ).text
    assert "1 event(s)" in filtered


# --------------------------------------------------------- 3. universal undo


def test_undo_move_and_undo_of_undo_redoes(data_dir):
    item = store.create_item("Servo", "each", location_id="A-1-1")["payload"]["id"]
    store.move_item(item, "B-2-2")
    client = _client()
    response = client.post("/command", data={"q": "undo"})
    assert "move back to A-1-1" in response.text
    assert _item(item)["location_id"] == "A-1-1"
    # The compensating move is itself the newest action — undoing again redoes.
    client.post("/command", data={"q": "undo"})
    assert _item(item)["location_id"] == "B-2-2"


def test_undo_each_action_type(data_dir):
    item = store.create_item("Servo", "each", qty_on_hand=10)["payload"]["id"]
    # qty_adjusted
    store.adjust_qty(item, 5, "restock")
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True
    assert _qty(item) == "10"
    # recounted
    store.recount_item(item, 7)
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True
    assert _qty(item) == "10"
    # archived / unarchived
    store.archive_item(item)
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True
    assert _item(item)["archived"] == 0
    # basket add
    store.add_basket_item(item, 4)
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0] == 0
    conn.close()
    # item.created → archived, never deleted
    ok, message = undo.perform_undo(
        next(e for e in undo.undo_stack() if e["type"] == "item.created")["id"]
    )
    assert ok and "archive" in message
    assert _item(item)["archived"] == 1  # photo + history intact, just retired


def test_undo_build_and_unbuild(data_dir):
    item = store.create_item("Servo", "each", qty_on_hand=10, part_number="SV")["payload"]["id"]
    pid = store.create_project("Bot")["payload"]["id"]
    store.import_bom(pid, [{"line_no": 1, "part_number": "SV", "description": "Servo",
                            "quantity": "2", "unit": "each", "reference_designators": None,
                            "notes": None}])
    store.match_bom_line(pid, 1, item, "part_number")
    from ravens_nest import bom

    ok, _msg, _eid = bom.attempt_build(pid, 2)
    assert ok and _qty(item) == "6"
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True  # → build.reversed
    assert _qty(item) == "10"
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True  # redo the build
    assert _qty(item) == "6"


def test_undo_refuses_cross_actor_work(data_dir):
    item = store.create_item("Servo", "each", location_id="A-1-1")["payload"]["id"]
    foreign = {
        "id": "11111111-2222-3333-4444-555555555555",
        "ts": "2099-01-01T00:00:00+00:00",
        "actor": "someone-elses-laptop",
        "type": "item.moved",
        "payload": {"item_id": item, "location_id": "B-9-9"},
    }
    events.append_to_log(foreign)
    conn = db.connect()
    with conn:
        replay.apply_event(conn, foreign)
    conn.close()
    ok, message = undo.perform_undo(foreign["id"])
    assert ok is False
    assert "someone-elses-laptop" in message  # explains the conflict
    # And it never appears in this machine's stack.
    assert all(e["id"] != foreign["id"] for e in undo.undo_stack())


def test_undo_refuses_stale_move_with_explanation(data_dir):
    item = store.create_item("Servo", "each", location_id="A-1-1")["payload"]["id"]
    store.move_item(item, "B-2-2")
    move_event = undo.undo_stack()[0]
    store.move_item(item, "C-3-3")  # moved again since
    ok, message = undo.perform_undo(move_event["id"])
    assert ok is False
    assert "moved again since" in message
    assert "move to" in message  # offers the manual path
    assert _item(item)["location_id"] == "C-3-3"  # nothing clobbered


def test_undo_list_and_numbered_undo(data_dir):
    item = store.create_item("Servo", "each", qty_on_hand=10)["payload"]["id"]
    store.adjust_qty(item, 1, "first")
    store.adjust_qty(item, 2, "second")
    client = _client()
    listing = client.post("/command", data={"q": "undo list"}).text
    assert "Undo stack" in listing and "first" in listing and "second" in listing
    client.post("/command", data={"q": "undo 2"})  # undo the older (+1)
    assert _qty(item) == "12"  # 10 +1 +2 -1


def test_action_toast_offers_undo(data_dir):
    store.create_item("Unique widget", "each", qty_on_hand=5)
    response = _client().post("/command", data={"q": "need 3 more unique widget"})
    assert "/command/undo" in response.text  # one-click inverse on the toast


# ------------------------------------------------------------- 6. merge


def _merge_fixture(units=("each", "each")):
    _client().post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    core = conn.execute("SELECT id FROM suppliers WHERE name='Core Electronics'").fetchone()[0]
    jaycar = conn.execute("SELECT id FROM suppliers WHERE name='Jaycar'").fetchone()[0]
    conn.close()
    target = store.create_item(
        "SG90 Micro Servo", units[0], qty_on_hand=8, part_number="SG90",
        location_id="A-1-2",
    )["payload"]["id"]
    source = store.create_item(
        "sg90 servo 9g", units[1], qty_on_hand=3, location_id="B-2-1",
        photo_hash="cd" * 32,
    )["payload"]["id"]
    store.add_alias(source, "TOWERPRO-SG90")
    store.add_item_link(source, jaycar, "https://jaycar.example/sg90")
    pid = store.create_project("Bot")["payload"]["id"]
    store.create_reservation(pid, source, 2)
    return source, target, core, jaycar


def test_merge_asks_on_location_conflict_then_merges(data_dir):
    source, target, _core, jaycar = _merge_fixture()
    ok, message, _ = merge.perform_merge(source, target, None)
    assert ok is False and "different bins" in message  # asks, never guesses

    ok, message, event_id = merge.perform_merge(source, target, "A-1-2")
    assert ok is True and event_id
    target_row = _item(target)
    source_row = _item(source)
    assert target_row["qty_on_hand"] == "11"  # quantities sum
    assert target_row["location_id"] == "A-1-2"
    assert target_row["photo_hash"] == "cd" * 32  # photo transferred
    assert source_row["archived"] == 1 and source_row["qty_on_hand"] == "0"

    conn = db.connect()
    aliases = {r["alias_text"] for r in conn.execute(
        "SELECT alias_text FROM aliases WHERE item_id = ?", (target,))}
    link = conn.execute(
        "SELECT item_id FROM item_links WHERE supplier_id = ?", (jaycar,)).fetchone()
    reservation = conn.execute(
        "SELECT item_id FROM reservations WHERE status='active'").fetchone()
    conn.close()
    assert "TOWERPRO-SG90" in aliases  # aliases transfer
    assert "sg90 servo 9g" in aliases  # source's name becomes an alias
    assert link["item_id"] == target  # links transfer
    assert reservation["item_id"] == target  # reservations follow the stock


def test_merge_history_readable_from_target_and_undoable(data_dir):
    source, target, *_ = _merge_fixture()
    _, _, event_id = merge.perform_merge(source, target, "A-1-2")
    page = _client().get(f"/items/{target}").text
    assert "Merged" in page and "sg90 servo 9g" in page
    assert "Created with 3 each in B-2-1" in page  # source history readable

    ok, message = undo.perform_undo(event_id)
    assert ok, message
    assert _item(source)["archived"] == 0
    assert _item(source)["qty_on_hand"] == "3"
    assert _item(source)["location_id"] == "B-2-1"
    assert _item(target)["qty_on_hand"] == "8"
    assert _item(target)["photo_hash"] is None


def test_merge_unit_type_guard(data_dir):
    source, target, *_ = _merge_fixture(units=("each", "g"))
    ok, message, _ = merge.perform_merge(source, target, "A-1-2")
    assert ok is False and "unit types differ" in message
    ok, _message, _ = merge.perform_merge(source, target, "A-1-2", allow_unit_mismatch=True)
    assert ok is True  # explicit confirmation overrides


def test_capture_card_shows_duplicate_warning(data_dir, monkeypatch):
    store.create_item("SG90 Micro Servo", "each", qty_on_hand=8, location_id="A-1-2")

    def fake(image_bytes):
        fields = {f: {"value": None, "confidence": "low"} for f in vision.FIELDS}
        fields["name"] = {"value": "SG90 Micro Servo", "confidence": "high"}
        fields["unit_type"] = {"value": "each", "confidence": "high"}
        return [{"fields": fields, "questions": [], "photo_region": None, "error": None}]

    monkeypatch.setattr(vision, "extract_items", fake)
    ingest.ingest_photo(JPEG)
    page = _client().get("/queue").text
    assert "dupwarn" in page  # unmissable block, not a corner link
    assert "same thing?" in page
    assert "Merge — adds qty" in page
    assert "No, this is different" in page


# ---------------------------------------------------- 7. CSV round-trip


def test_csv_round_trip_reproduces_state(data_dir, tmp_path, monkeypatch):
    store.create_item(
        "SG90 servo", "each", qty_on_hand=10, part_number="SG90",
        description="9g micro servo", min_qty=4, location_id="A-1-1",
        last_paid_aud="4.50",
    )
    store.create_item("Solder paste", "g", qty_on_hand="12.5", min_qty="20")
    conn = db.connect()
    exported = importexport.export_items_csv(conn, include_zero=True, include_archived=False)
    conn.close()

    # Fresh instance: point the app at a brand-new data directory.
    fresh = tmp_path / "fresh-data"
    monkeypatch.setenv("RAVENS_NEST_DATA", str(fresh))
    rows, errors = importexport.parse_items_csv(exported)
    assert errors == []
    conn = db.connect()
    importexport.classify_rows(conn, rows)
    conn.close()
    assert all(r["status"] == "new" for r in rows)
    for row in rows:
        importexport.apply_row(row, "new")

    conn = db.connect()
    state = {
        r["name"]: (r["qty_on_hand"], r["unit_type"], r["min_qty"], r["location_id"], r["last_paid_aud"])
        for r in conn.execute("SELECT * FROM items")
    }
    conn.close()
    assert state["SG90 servo"] == ("10", "each", "4", "A-1-1", "4.5")
    assert state["Solder paste"] == ("12.5", "g", "20", None, None)


def test_csv_import_preview_is_dry_run_and_confirm_applies(data_dir):
    store.create_item("SG90 servo", "each", qty_on_hand=2, part_number="SG90")
    csv_text = (
        "name,part_number,qty,location\n"
        "SG90 servo,SG90,10,A-1-1\n"
        "Brand new part,,5,\n"
        "Bad row,,not-a-number,\n"
    )
    client = _client()
    preview = client.post(
        "/import/preview", files={"items_csv": ("items.csv", csv_text.encode(), "text/csv")}
    ).text
    assert "1 new" in preview and "1 matched" in preview and "1 error(s)" in preview
    assert "not a number" in preview  # row-numbered reason
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1  # dry run wrote nothing
    conn.close()

    done = client.post(
        "/import/confirm",
        data={"csv_b64": importexport.encode_csv(csv_text)},
    ).text
    assert "1 created" in done and "1 updated" in done
    conn = db.connect()
    names = {r["name"]: r for r in conn.execute("SELECT * FROM items")}
    conn.close()
    assert names["SG90 servo"]["qty_on_hand"] == "10"  # matched row recounted
    assert names["SG90 servo"]["location_id"] == "A-1-1"
    assert names["Brand new part"]["qty_on_hand"] == "5"


# ------------------------------------------------- 8. multi-item capture


def _multi_mock(monkeypatch):
    def fake(image_bytes):
        cards = []
        for name, region in (
            ("M3 screw", "top-left"),
            ("M4 nut", "centre tray"),
            ("Zip ties", "bottom-right"),
        ):
            fields = {f: {"value": None, "confidence": "low"} for f in vision.FIELDS}
            fields["name"] = {"value": name, "confidence": "high"}
            fields["unit_type"] = {"value": "each", "confidence": "high"}
            cards.append(
                {"fields": fields, "questions": [], "photo_region": region, "error": None}
            )
        return cards

    monkeypatch.setattr(vision, "extract_items", fake)


def test_multi_item_photo_yields_sibling_cards(data_dir, monkeypatch):
    _multi_mock(monkeypatch)
    result = ingest.ingest_photo(JPEG)
    assert result["status"] == "new"
    cards = result["cards"]
    assert len(cards) == 3
    assert len({c["photo_hash"] for c in cards}) == 1  # photo stored once
    assert cards[0]["id"] == cards[0]["photo_hash"]  # single-item id shape preserved
    assert cards[1]["id"].endswith("~2") and cards[2]["id"].endswith("~3")
    page = _client().get("/queue").text
    assert "top-left" in page  # crop hint on the card
    assert "detection 1 of 3" in page


def test_reject_one_card_keeps_siblings(data_dir, monkeypatch):
    _multi_mock(monkeypatch)
    cards = ingest.ingest_photo(JPEG)["cards"]
    _client().post(f"/queue/{cards[1]['id']}/skip")
    remaining = ingest.list_cards()
    assert len(remaining) == 2
    assert {c["id"] for c in remaining} == {cards[0]["id"], cards[2]["id"]}
    # Confirming a sibling still works and carries the shared photo.
    _client().post(
        f"/queue/{cards[0]['id']}/confirm",
        data={"name": "M3 screw", "unit_type": "each", "qty": "1"},
    )
    conn = db.connect()
    row = conn.execute("SELECT photo_hash FROM items").fetchone()
    conn.close()
    assert row["photo_hash"] == cards[0]["photo_hash"]


def test_cannot_separate_path_returns_one_card_with_question(data_dir, monkeypatch):
    def fake(image_bytes):
        fields = {f: {"value": None, "confidence": "low"} for f in vision.FIELDS}
        return [
            {
                "fields": fields,
                "questions": [
                    {"field": None, "question": "Photo may contain multiple items — confirm what to split?"}
                ],
                "photo_region": None,
                "error": None,
            }
        ]

    monkeypatch.setattr(vision, "extract_items", fake)
    result = ingest.ingest_photo(JPEG)
    assert len(result["cards"]) == 1  # never split speculatively
    page = _client().get("/queue").text
    assert "may contain multiple items" in page


# ---------------------------------------------------- 4. first-run setup


def test_parse_storage_free_text():
    parsed = parse_storage_text(
        "I've got 3 shelving units, 5 shelves each, 6 bins per shelf, "
        "and the deep bins have a front and back"
    )
    assert parsed == {"units": 3, "shelves": 5, "bins": 6, "sections": ["f", "b"]}


def test_generate_location_ids():
    ids = generate_location_ids(3, 5, 6, ["b"])
    assert len(ids) == 90
    assert ids[0] == "A-1-1b"
    assert ids[-1] == "C-5-6b"
    plain = generate_location_ids(1, 2, 2, [])
    assert plain == ["A-1-1", "A-1-2", "A-2-1", "A-2-2"]


def test_setup_structured_flow_creates_locations(data_dir):
    client = _client()
    assert "guided setup" in client.get("/").text  # fresh install banner
    preview = client.post(
        "/setup/storage",
        data={"units": "2", "shelves": "2", "bins": "3", "sections": "", "free_text": ""},
    ).text
    assert "12 locations" in preview  # preview before commit
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0  # not yet
    conn.close()
    client.post(
        "/setup/storage/commit",
        data={"units": "2", "shelves": "2", "bins": "3", "sections": ""},
        follow_redirects=False,
    )
    conn = db.connect()
    ids = {r["id"] for r in conn.execute("SELECT id FROM locations")}
    conn.close()
    assert len(ids) == 12 and "B-2-3" in ids
    assert "guided setup" not in client.get("/").text  # banner gone


def test_setup_free_text_overrides_boxes(data_dir):
    preview = _client().post(
        "/setup/storage",
        data={
            "units": "1", "shelves": "1", "bins": "1", "sections": "",
            "free_text": "2 units, 2 shelves, 2 bins, sections: f/b",
        },
    ).text
    assert "16 locations" in preview  # 2*2*2*2


# --------------------------------------------------- 5. help & messages


def test_help_lists_every_verb_with_examples(data_dir):
    page = _client().get("/command", params={"q": "help"}).text
    for example in ("build RPSRobot x2", "need 20 more m3 screw", "recount A-2-3b",
                    "history A-2-3b", "undo list", "price basket", "all: heat shrink"):
        assert example in page
    detail = _client().get("/command", params={"q": "help build"}).text
    assert "Usage:" in detail and "build" in detail


def test_partial_command_shows_syntax_inline(data_dir):
    page = _client().get("/command", params={"q": "move"}).text
    assert "move to &lt;bin&gt;" in page  # syntax shown (HTML-escaped)
    assert "A-2-3b" in page  # a real example, not just grammar


def test_unknown_bin_error_names_what_exists(data_dir):
    for bin_no in range(1, 7):
        store.create_location(f"A-2-{bin_no}")
    page = _client().get("/command", params={"q": "A-2-9"}).text
    assert "Unknown location" in page and "A-2-9" in page
    assert "Unit A shelf 2 has bins 1-6" in page
    assert "Try `A-2-6`" in page


# -------------------------------------------------------- 9. item labels


def test_scan_prefixes_distinguish_item_and_location(data_dir):
    assert normalize_scan("RN-LOC:A-2-3b") == ("location", "A-2-3b")
    assert normalize_scan("RN-ITEM:abc-123") == ("item", "abc-123")
    assert normalize_scan("A-2-3b") == ("raw", "A-2-3b")  # old labels still work


def test_item_label_sheet_and_scan_jump(data_dir):
    item = store.create_item(
        "SG90 servo", "each", qty_on_hand=10, location_id="A-1-1"
    )["payload"]["id"]
    client = _client()
    sheet = client.get("/labels/items", params={"location": "A-1-1"}).text
    assert "SG90 servo" in sheet and "<svg" in sheet
    assert "A-1-1" in sheet  # home location printed on the label
    # Scanning the item QR payload jumps to the card.
    jump = client.get("/command", params={"q": f"RN-ITEM:{item}"}).text
    assert "Scanned item label" in jump and "SG90 servo" in jump
    # And in move mode, an item scan is an item — even though it's not a location.
    client.post("/move/scan", data={"code": "B-2-2", "location_id": ""})
    client.post("/move/scan", data={"code": f"RN-ITEM:{item}", "location_id": "B-2-2"})
    assert _item(item)["location_id"] == "B-2-2"


# ------------------------------------------------------------ 10. health


def test_health_reports_gaps_with_fix_links(data_dir):
    store.create_item("No-location part", "each", qty_on_hand=5)
    store.create_item(
        "Complete part", "each", qty_on_hand=5, min_qty=2, location_id="A-1-1",
        last_paid_aud="1.00", photo_hash="ab" * 32,
    )
    page = _client().post("/command", data={"q": "health"}).text
    assert "Data health" in page and "%" in page
    assert "Items with no location" in page
    assert "No-location part" in page  # clickable list, not just a count
    assert "/items/" in page  # fix flow links
    assert "no supplier link" in page


# ----------------------------------------------- replay determinism


def test_replay_determinism_with_all_new_event_types(data_dir):
    # Exercise archive/unarchive, alias, merge, unmerge, and undo events.
    source, target, *_ = _merge_fixture()
    _ok, _msg, merge_event = merge.perform_merge(source, target, "A-1-2")
    undo.perform_undo(merge_event)  # emits item.unmerged
    other = store.create_item("Loner", "each", qty_on_hand=1)["payload"]["id"]
    store.archive_item(other, "testing")
    store.unarchive_item(other)
    store.add_alias(other, "LONER-1")
    store.move_item(other, "C-1-1")
    undo.perform_undo(undo.undo_stack()[0]["id"])

    def snapshot():
        conn = db.connect()
        try:
            return {
                table: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))
                for table in ("items", "aliases", "item_links", "reservations", "basket_items")
            }
        finally:
            conn.close()

    before = snapshot()
    replay.rebuild()
    assert snapshot() == before
