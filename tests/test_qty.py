import json

from ravens_nest import config, db, store


def _qty(conn, item_id):
    return conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)).fetchone()[0]


def test_adjustment_arithmetic(data_dir):
    conn = db.connect()
    item = store.create_item("M3 screw", "each", qty_on_hand=10, conn=conn)["payload"]["id"]
    store.adjust_qty(item, 5, "restock", conn=conn)
    store.adjust_qty(item, -2, "used in build", conn=conn)
    assert _qty(conn, item) == "13"
    conn.close()


def test_fractional_quantities_are_exact(data_dir):
    # 0.1 + 0.2 must be exactly 0.3 — floats would give 0.30000000000000004.
    conn = db.connect()
    item = store.create_item("solder paste", "g", qty_on_hand="0", conn=conn)["payload"]["id"]
    store.adjust_qty(item, "0.1", "restock", conn=conn)
    store.adjust_qty(item, "0.2", "restock", conn=conn)
    assert _qty(conn, item) == "0.3"
    conn.close()


def test_recount_sets_absolute_and_logs_delta(data_dir):
    conn = db.connect()
    item = store.create_item("M3 screw", "each", qty_on_hand=10, conn=conn)["payload"]["id"]
    store.adjust_qty(item, -3, "used", conn=conn)  # cache now says 7
    event = store.recount_item(item, 12, conn=conn)

    assert _qty(conn, item) == "12"
    assert event["payload"]["qty"] == "12"
    assert event["payload"]["delta"] == "5"  # correction: 12 counted - 7 expected
    conn.close()

    # The correction delta is durably in the log, not just the return value.
    logged = []
    for path in sorted(config.events_dir().glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            logged.extend(json.loads(line) for line in f if line.strip())
    recounts = [e for e in logged if e["type"] == "item.recounted"]
    assert len(recounts) == 1
    assert recounts[0]["payload"] == {"item_id": item, "qty": "12", "delta": "5"}


def test_recount_downward(data_dir):
    conn = db.connect()
    item = store.create_item("standoff", "each", qty_on_hand=20, conn=conn)["payload"]["id"]
    event = store.recount_item(item, 8, conn=conn)
    assert _qty(conn, item) == "8"
    assert event["payload"]["delta"] == "-12"
    conn.close()


def test_write_appends_to_current_month_file(data_dir):
    conn = db.connect()
    event = store.create_item("widget", "each", conn=conn)
    conn.close()
    month = event["ts"][:7]
    log_file = config.events_dir() / f"{month}.jsonl"
    assert log_file.is_file()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["id"] == event["id"]
