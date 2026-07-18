import json
import sqlite3

from ravens_nest import config, db, events, replay


def _event(id, ts, type, payload):
    return {"id": id, "ts": ts, "actor": "test-host", "type": type, "payload": payload}


ITEM = "11111111-1111-1111-1111-111111111111"

# Deliberately appended out of chronological order, spanning two month files,
# to prove replay sorts by (ts, id) rather than trusting file/line order.
EVENTS = [
    _event(
        "e3",
        "2026-07-02T10:00:00+00:00",
        "item.qty_adjusted",
        {"item_id": ITEM, "delta": "5", "reason": "restock"},
    ),
    _event(
        "e1",
        "2026-06-15T09:00:00+00:00",
        "item.created",
        {"id": ITEM, "name": "M3 screw", "unit_type": "each", "qty_on_hand": "10"},
    ),
    _event(
        "e4",
        "2026-07-03T10:00:00+00:00",
        "item.moved",
        {"item_id": ITEM, "location_id": "A-2-3b"},
    ),
    _event(
        "e2",
        "2026-06-20T09:00:00+00:00",
        "location.created",
        {"id": "A-2-3b", "description": "small fasteners"},
    ),
    _event(
        "e5",
        "2026-07-04T10:00:00+00:00",
        "item.recounted",
        {"item_id": ITEM, "qty": "12", "delta": "-3"},
    ),
]


def _write_log(evts):
    for e in evts:
        events.append_to_log(e)


def _dump(path):
    conn = sqlite3.connect(path)
    try:
        tables = ["items", "locations", "aliases", "events_applied"]
        return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}"))) for t in tables}
    finally:
        conn.close()


def test_replay_rebuilds_expected_state(data_dir):
    _write_log(EVENTS)
    assert replay.rebuild() == 5

    conn = db.connect()
    item = conn.execute("SELECT * FROM items WHERE id = ?", (ITEM,)).fetchone()
    assert item["qty_on_hand"] == "12"  # 10 + 5, then recount wins with 12
    assert item["location_id"] == "A-2-3b"
    assert item["created_ts"] == "2026-06-15T09:00:00+00:00"
    assert item["updated_ts"] == "2026-07-04T10:00:00+00:00"

    loc = conn.execute("SELECT * FROM locations WHERE id = ?", ("A-2-3b",)).fetchone()
    assert (loc["unit"], loc["shelf"], loc["bin"], loc["section"]) == ("A", 2, 3, "b")
    conn.close()


def test_replay_is_deterministic(data_dir):
    _write_log(EVENTS)
    replay.rebuild()
    first = _dump(config.cache_path())
    replay.rebuild()
    second = _dump(config.cache_path())
    assert first == second


def test_replay_sorts_by_ts_then_id(data_dir):
    # Same timestamp: the event with the lower id must be applied first,
    # so the higher id's recount is what sticks.
    ts = "2026-07-05T00:00:00+00:00"
    _write_log(
        [
            _event(
                "aaa",
                "2026-07-01T00:00:00+00:00",
                "item.created",
                {"id": ITEM, "name": "widget", "unit_type": "each"},
            ),
            _event("zzz", ts, "item.recounted", {"item_id": ITEM, "qty": "7", "delta": "7"}),
            _event("bbb", ts, "item.recounted", {"item_id": ITEM, "qty": "3", "delta": "3"}),
        ]
    )
    replay.rebuild()
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (ITEM,)).fetchone()[0]
    conn.close()
    assert qty == "7"


def test_apply_event_is_idempotent(data_dir):
    replay.rebuild()
    conn = db.connect()
    created = _event(
        "e1",
        "2026-07-01T00:00:00+00:00",
        "item.created",
        {"id": ITEM, "name": "widget", "unit_type": "each", "qty_on_hand": "0"},
    )
    adjust = _event(
        "e2",
        "2026-07-02T00:00:00+00:00",
        "item.qty_adjusted",
        {"item_id": ITEM, "delta": "4", "reason": "restock"},
    )
    assert replay.apply_event(conn, created) is True
    assert replay.apply_event(conn, adjust) is True
    assert replay.apply_event(conn, adjust) is False  # second apply is a no-op
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (ITEM,)).fetchone()[0]
    conn.close()
    assert qty == "4"


def test_replay_ignores_duplicate_log_lines(data_dir):
    # A bad Git merge could leave the same event in two files; replay must
    # still apply it exactly once.
    created = _event(
        "e1",
        "2026-06-01T00:00:00+00:00",
        "item.created",
        {"id": ITEM, "name": "widget", "unit_type": "each"},
    )
    adjust = _event(
        "e2",
        "2026-06-02T00:00:00+00:00",
        "item.qty_adjusted",
        {"item_id": ITEM, "delta": "4", "reason": "restock"},
    )
    _write_log([created, adjust])
    dupe = config.events_dir() / "2026-07.jsonl"
    dupe.parent.mkdir(parents=True, exist_ok=True)
    dupe.write_text(json.dumps(adjust) + "\n", encoding="utf-8")

    replay.rebuild()
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (ITEM,)).fetchone()[0]
    conn.close()
    assert qty == "4"
