from decimal import Decimal

from fastapi.testclient import TestClient

from ravens_nest import bom, db, events, replay, store
from ravens_nest.app import app

CSV = """part_number,description,quantity,unit,reference_designators,notes
SG90,Micro servo SG90,2,each,"SV1,SV2",
RES-10K,Resistor 10k 0805,4,each,"R1,R2,R3,R4",dev only
WIRE-22,Hookup wire 22AWG red,500,mm,,
MYSTERY-01,Flux capacitor,1,each,,
"""


def _client() -> TestClient:
    return TestClient(app)


def _upload_bom(client, project_id, csv_text=CSV):
    return client.post(
        f"/projects/{project_id}/bom",
        files={"bom_csv": ("bom.csv", csv_text.encode(), "text/csv")},
    )


def _make_project(client) -> str:
    response = client.post(
        "/projects", data={"name": "Servo Tester", "description": "jig"},
        follow_redirects=False,
    )
    return response.headers["location"].rsplit("/", 1)[1]


def _seed_items():
    """Items covering the match ladder: part number, name, and alias."""
    servo = store.create_item(
        "SG90 9g servo", "each", qty_on_hand=10, part_number="SG90", last_paid_aud="4.50"
    )["payload"]["id"]
    resistor = store.create_item(
        "Resistor 10k 0805", "each", qty_on_hand=100, last_paid_aud="0.02"
    )["payload"]["id"]
    wire = store.create_item(
        "Red hookup wire", "mm", qty_on_hand=3000, min_qty=1000
    )["payload"]["id"]
    # Alias learned from an earlier project's BOM resolution (event-sourced,
    # so it survives replay).
    earlier = store.create_project("Earlier project")["payload"]["id"]
    store.match_bom_line(earlier, 1, wire, "manual", alias_text="WIRE-22")
    return servo, resistor, wire


def _free_stock(item_id) -> Decimal:
    conn = db.connect()
    try:
        on_hand = db.parse_qty(
            conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)).fetchone()[0]
        )
        reserved = bom.reserved_by_item(conn).get(item_id, Decimal(0))
        return on_hand - reserved
    finally:
        conn.close()


def _qty(item_id) -> str:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# ------------------------------------------------------------------ parsing


def test_parse_bom_csv_valid():
    lines, errors = bom.parse_bom_csv(CSV)
    assert errors == []
    assert len(lines) == 4
    assert lines[0] == {
        "line_no": 1,
        "part_number": "SG90",
        "description": "Micro servo SG90",
        "quantity": "2",
        "unit": "each",
        "reference_designators": "SV1,SV2",
        "notes": None,
    }
    assert lines[1]["notes"] == "dev only"
    assert lines[2]["quantity"] == "500"


def test_parse_bom_csv_reports_errors_by_line():
    bad = (
        "part_number,description,quantity,unit\n"
        "P1,thing,abc,each\n"
        "P2,thing,-1,each\n"
        "P3,thing,5,boxes\n"
        ",thing,5,each\n"
    )
    _, errors = bom.parse_bom_csv(bad)
    joined = " | ".join(errors)
    assert "line 1" in joined and "not a number" in joined
    assert "line 2" in joined and "positive" in joined
    assert "line 3" in joined and "boxes" in joined
    assert "line 4" in joined and "part_number" in joined


def test_parse_bom_csv_missing_columns():
    _, errors = bom.parse_bom_csv("part_number,quantity\nX,1\n")
    assert any("missing required column" in e for e in errors)


# ---------------------------------------------------------- import + match


def test_import_matches_ladder_and_reserves(data_dir):
    servo, resistor, wire = _seed_items()
    client = _client()
    pid = _make_project(client)
    response = _upload_bom(client, pid)
    assert "3 matched automatically" in response.text
    assert "1 need resolution" in response.text

    conn = db.connect()
    lines = {l["line_no"]: l for l in bom.matched_lines(conn, pid)}
    reservations = bom.active_reservations(conn, pid)
    conn.close()

    assert lines[1]["item_id"] == servo and lines[1]["match_method"] == "part_number"
    assert lines[2]["item_id"] == resistor and lines[2]["match_method"] == "name"
    assert lines[3]["item_id"] == wire and lines[3]["match_method"] == "alias"
    assert lines[4]["item_id"] is None  # MYSTERY-01 unresolved

    # Import created reservations, not consumption.
    assert _qty(servo) == "10"
    reserved = {r["item_id"]: r["qty"] for r in reservations}
    assert reserved == {servo: "2", resistor: "4", wire: "500"}
    assert _free_stock(servo) == Decimal(8)


def test_resolving_line_stores_alias_for_next_revision(data_dir):
    _seed_items()
    flux = store.create_item("Flux capacitor mk2", "each", qty_on_hand=3)["payload"]["id"]
    client = _client()
    pid = _make_project(client)
    _upload_bom(client, pid)

    # Resolve MYSTERY-01 (line 4) manually once.
    response = client.post(
        f"/projects/{pid}/match",
        data={"line_no": "4", "item_id": flux, "method": "manual"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = db.connect()
    alias = conn.execute(
        "SELECT item_id FROM aliases WHERE alias_text = 'MYSTERY-01'"
    ).fetchone()
    conn.close()
    assert alias["item_id"] == flux

    # The next revision of the BOM auto-matches by the learned alias.
    response = _upload_bom(client, pid)
    assert "4 matched automatically" in response.text
    conn = db.connect()
    line = conn.execute(
        "SELECT item_id, match_method FROM bom_lines WHERE project_id = ? AND line_no = 4",
        (pid,),
    ).fetchone()
    conn.close()
    assert line["item_id"] == flux
    assert line["match_method"] == "alias"


def test_reimport_refreshes_reservations(data_dir):
    servo, resistor, wire = _seed_items()
    client = _client()
    pid = _make_project(client)
    _upload_bom(client, pid)
    _upload_bom(client, pid)  # revision 2

    conn = db.connect()
    active = bom.active_reservations(conn, pid)
    total = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
    conn.close()
    assert len(active) == 3  # fresh set, not doubled
    assert total == 6  # old three kept as released history


def test_fuzzy_candidates_scored_for_unmatched_line(data_dir):
    _seed_items()
    store.create_item("Flux capacitor mk2", "each")
    client = _client()
    pid = _make_project(client)
    page = _upload_bom(client, pid).text
    assert "Fuzzy suggestions" in page
    assert "Flux capacitor mk2 (score" in page  # scored, confirmation required


def test_import_rejects_bad_csv(data_dir):
    client = _client()
    pid = _make_project(client)
    response = _upload_bom(client, pid, "part_number,quantity\nX,1\n")
    assert "BOM rejected" in response.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM bom_lines").fetchone()[0] == 0
    conn.close()


# ------------------------------------------------------- stock and reorder


def test_shortfall_goes_to_reorder_basket(data_dir):
    servo = store.create_item("SG90 9g servo", "each", qty_on_hand=5, part_number="SG90")[
        "payload"
    ]["id"]
    pid = store.create_project("Hexapod")["payload"]["id"]
    store.create_reservation(pid, servo, 8)  # reserved beyond stock

    conn = db.connect()
    basket = bom.reorder_basket(conn)
    conn.close()
    assert len(basket) == 1
    entry = basket[0]
    assert entry["needed"] == "3"  # integer count for 'each'
    assert entry["free"] == "-3"
    assert "reserved beyond stock" in entry["reasons"]

    page = _client().get("/reorder").text
    assert "SG90 9g servo" in page


def test_consumable_reorders_on_native_units_against_min_qty(data_dir):
    store.create_item("Solder paste", "g", qty_on_hand="12.5", min_qty="20")
    conn = db.connect()
    basket = bom.reorder_basket(conn)
    conn.close()
    assert basket[0]["needed"] == "7.5"  # native decimal, no integer rounding
    assert any("below min" in r for r in basket[0]["reasons"])


def test_each_shortfall_rounds_up_fractions(data_dir):
    item = store.create_item("Odd part", "each", qty_on_hand="1")["payload"]["id"]
    pid = store.create_project("P")["payload"]["id"]
    store.create_reservation(pid, item, "2.5")
    conn = db.connect()
    basket = bom.reorder_basket(conn)
    conn.close()
    assert basket[0]["needed"] == "2"  # ceil(1.5) — whole units only


# ------------------------------------------------------------------- build


def _matched_project(client):
    """Project with the three auto-matched lines resolved (mystery line too)."""
    servo, resistor, wire = _seed_items()
    flux = store.create_item("Flux capacitor mk2", "each", qty_on_hand=3)["payload"]["id"]
    pid = _make_project(client)
    _upload_bom(client, pid)
    client.post(f"/projects/{pid}/match", data={"line_no": "4", "item_id": flux})
    return pid, servo, resistor, wire, flux


def test_build_consumes_and_records_history(data_dir):
    client = _client()
    pid, servo, resistor, wire, flux = _matched_project(client)
    response = client.post(
        f"/projects/{pid}/build", data={"count": "2"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _qty(servo) == "6"  # 10 - 2x2
    assert _qty(resistor) == "92"  # 100 - 2x4
    assert _qty(wire) == "2000"  # 3000 - 2x500
    assert _qty(flux) == "1"  # 3 - 2x1

    executed = [e for e in events.read_all_events() if e["type"] == "build.executed"]
    assert len(executed) == 1
    assert executed[0]["payload"]["count"] == 2

    page = client.get(f"/projects/{pid}").text
    assert "Built ×2" in page
    assert "2 net build(s)" in page


def test_build_not_blocked_by_own_reservations(data_dir):
    # on_hand exactly covers the BOM; the project's own reservation must not count.
    item = store.create_item("Servo", "each", qty_on_hand=2, part_number="SV")["payload"]["id"]
    client = _client()
    pid = _make_project(client)
    _upload_bom(client, pid, "part_number,description,quantity,unit\nSV,Servo,2,each\n")
    assert _free_stock(item) == Decimal(0)  # fully reserved by this project
    response = client.post(
        f"/projects/{pid}/build", data={"count": "1"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _qty(item) == "0"


def test_build_rejected_lists_exact_shortages(data_dir):
    client = _client()
    pid, servo, *_ = _matched_project(client)
    response = client.post(f"/projects/{pid}/build", data={"count": "6"})
    assert response.status_code == 200
    assert "Insufficient free stock" in response.text
    # 6 builds need 12 servos; 10 on hand — short exactly 2.
    assert "need 12, have 10 free — short 2 each" in response.text
    assert _qty(servo) == "10"  # nothing consumed
    assert not [e for e in events.read_all_events() if e["type"] == "build.executed"]


def test_build_blocked_by_other_projects_reservations(data_dir):
    item = store.create_item("Servo", "each", qty_on_hand=4, part_number="SV")["payload"]["id"]
    other = store.create_project("Other project")["payload"]["id"]
    store.create_reservation(other, item, 3)
    client = _client()
    pid = _make_project(client)
    _upload_bom(client, pid, "part_number,description,quantity,unit\nSV,Servo,2,each\n")
    response = client.post(f"/projects/{pid}/build", data={"count": "1"})
    assert "short 1 each" in response.text  # 4 on hand - 3 reserved elsewhere = 1 free, need 2


def test_build_rejected_with_unresolved_lines(data_dir):
    _seed_items()
    client = _client()
    pid = _make_project(client)
    _upload_bom(client, pid)  # MYSTERY-01 stays unresolved
    response = client.post(f"/projects/{pid}/build", data={"count": "1"})
    assert "unresolved BOM line(s) 4" in response.text


def test_unbuild_returns_stock_and_respects_net_count(data_dir):
    client = _client()
    pid, servo, *_ = _matched_project(client)
    client.post(f"/projects/{pid}/build", data={"count": "2"})
    assert _qty(servo) == "6"

    response = client.post(
        f"/projects/{pid}/unbuild", data={"count": "1"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _qty(servo) == "8"  # servos come back

    response = client.post(f"/projects/{pid}/unbuild", data={"count": "2"})
    assert "only 1 net build(s) recorded" in response.text
    assert _qty(servo) == "8"

    page = client.get(f"/projects/{pid}").text
    assert "Un-built ×1" in page


def test_release_reservations(data_dir):
    client = _client()
    pid, servo, *_ = _matched_project(client)
    assert _free_stock(servo) == Decimal(8)
    client.post(f"/projects/{pid}/release", follow_redirects=False)
    assert _free_stock(servo) == Decimal(10)


# -------------------------------------------------------------------- cost


def test_bom_cost_view_shows_unit_and_extended_and_flags_unpriced(data_dir):
    client = _client()
    pid, *_ = _matched_project(client)  # wire and flux have no last_paid_aud
    page = client.get(f"/projects/{pid}").text
    assert ">4.5<" in page  # servo unit cost (canonical decimal)
    assert ">9.00<" in page  # servo extended: 2 x 4.50
    assert ">0.08<" in page  # resistor extended: 4 x 0.02
    assert "Build cost: 9.08 AUD" in page
    assert "2 line(s) without price data" in page


# ------------------------------------------------------------------ replay


def test_replay_rebuild_reproduces_project_state(data_dir):
    client = _client()
    pid, servo, resistor, wire, flux = _matched_project(client)
    client.post(f"/projects/{pid}/build", data={"count": "1"})
    client.post(f"/projects/{pid}/unbuild", data={"count": "1"})

    def snapshot():
        conn = db.connect()
        try:
            return {
                "items": sorted(
                    tuple(r) for r in conn.execute("SELECT id, qty_on_hand FROM items")
                ),
                "bom": sorted(
                    tuple(r)
                    for r in conn.execute(
                        "SELECT project_id, line_no, item_id, match_method FROM bom_lines"
                    )
                ),
                "reservations": sorted(
                    tuple(r)
                    for r in conn.execute("SELECT id, item_id, qty, status FROM reservations")
                ),
                "builds": sorted(
                    tuple(r) for r in conn.execute("SELECT id, kind, count FROM builds")
                ),
                "aliases": sorted(tuple(r) for r in conn.execute("SELECT * FROM aliases")),
            }
        finally:
            conn.close()

    before = snapshot()
    replay.rebuild()
    assert snapshot() == before
