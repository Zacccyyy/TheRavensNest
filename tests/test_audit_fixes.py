"""Regression tests for the audit remediation pass (C1, C2, C4, C3,
H1-H6, and the enforcement guards). Each section names its audit ID."""

import json
import threading
import time

from ravens_nest import events
from ravens_nest.sync import SyncManager

# ------------------------------------------------------------------ C1


def test_c1_sync_lock_is_the_event_write_lock(data_dir):
    """One process-wide lock: SyncManager must use events._write_lock, so
    every git rewrite excludes appends by construction."""
    manager = SyncManager(repo_root=data_dir.parent, debounce_seconds=0.1)
    assert manager._lock is events._write_lock


def test_c1_concurrent_appends_survive_locked_file_rewrites(data_dir, run_git):
    """A writer thread hammers append_to_log while the main thread does
    what sync does during a rebase: hold the shared lock, read the month
    file, dwell, and write it back (git checkout semantics). Without the
    lock, appends landing in the dwell window vanish; with it, none may.
    Uses the local-bare-repo layout from the sync tests."""
    repo = data_dir.parent
    run_git(repo.parent, "init", "--initial-branch=main", str(repo))
    (data_dir / "events").mkdir(parents=True, exist_ok=True)

    total = 150
    written_ids = []

    def writer():
        for i in range(total):
            event = events.new_event(
                "item.qty_adjusted",
                {"item_id": "x", "delta": "1", "reason": f"hammer {i}"},
            )
            events.append_to_log(event)
            written_ids.append(event["id"])

    thread = threading.Thread(target=writer)
    thread.start()
    # Simulated sync rewrites: exactly what _resolve_event_log_conflicts /
    # git checkout do, under the same shared lock the SyncManager holds.
    for _ in range(30):
        with events._write_lock:
            for path in (data_dir / "events").glob("*.jsonl"):
                content = path.read_text(encoding="utf-8")
                time.sleep(0.002)  # the dangerous window
                path.write_text(content, encoding="utf-8", newline="\n")
        time.sleep(0.001)
    thread.join()

    surviving = {e["id"] for e in events.read_all_events()}
    lost = [event_id for event_id in written_ids if event_id not in surviving]
    assert lost == [], f"{len(lost)} event(s) eaten by the rewrite race"


# ------------------------------------------------------------------ C2


def _month_file(data_dir):
    files = sorted((data_dir / "events").glob("*.jsonl"))
    assert files, "no event file written yet"
    return files[0]


def test_c2_midfile_corrupt_line_quarantined_not_fatal(data_dir):
    from ravens_nest import db, health, replay, store

    a = store.create_item("Good A", "each", qty_on_hand=1)["payload"]["id"]
    path = _month_file(data_dir)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"this is": not json at all\n')
    b = store.create_item("Good B", "each", qty_on_hand=2)["payload"]["id"]

    count = replay.rebuild()  # must not raise
    assert count == 2  # both good events applied
    conn = db.connect()
    names = {r["name"] for r in conn.execute("SELECT name FROM items")}
    report = health.report(conn)
    conn.close()
    assert names == {"Good A", "Good B"}
    assert report["quarantined"] == 1  # surfaced on the dashboard

    # The bad line was physically moved out of the log...
    remaining = path.read_text(encoding="utf-8")
    assert "not json at all" not in remaining
    quarantine_files = list((data_dir / "events").glob("quarantine-*.txt"))
    assert len(quarantine_files) == 1
    assert "not json at all" in quarantine_files[0].read_text(encoding="utf-8")
    # ...and a second read doesn't re-quarantine.
    assert len(events.read_all_events()) == 2
    assert events.quarantined_count() == 1
    # History and undo (both built on read_all_events) still work.
    from ravens_nest import undo

    assert len(undo.undo_stack()) == 2
    assert a != b


def test_c2_trailing_partial_line_is_safe_truncation(data_dir):
    """The disk-full / power-loss signature: an unterminated final line."""
    from ravens_nest import replay, store

    store.create_item("Survivor", "each", qty_on_hand=5)
    path = _month_file(data_dir)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write('{"id": "tr')  # echo -n equivalent — no newline, no close

    count = replay.rebuild()  # boot path: must not raise
    assert count == 1
    assert '{"id": "tr' not in path.read_text(encoding="utf-8")
    assert events.quarantined_count() == 1
    # Appends continue cleanly on the repaired file.
    store.create_item("After repair", "each")
    assert len(events.read_all_events()) == 2


def test_c2_json_valid_but_not_an_envelope_is_quarantined(data_dir):
    from ravens_nest import store

    store.create_item("Real", "each")
    path = _month_file(data_dir)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"just": "some dict", "no": "envelope keys"}\n')
    assert len(events.read_all_events()) == 1
    assert events.quarantined_count() == 1


# ------------------------------------------------------------------ C4


def _jpeg_with_exif() -> bytes:
    """A real JPEG carrying EXIF (including a GPS IFD where Pillow will
    write one)."""
    import io

    from PIL import Image

    image = Image.new("RGB", (32, 32), "red")
    exif = Image.Exif()
    exif[271] = "TestCam"  # Make
    exif[306] = "2026:07:19 12:00:00"  # DateTime
    exif[34853] = {  # GPSInfo IFD: 33°51'S 151°12'E-ish (floats → rationals)
        1: "S",
        2: (33.0, 51.0, 0.0),
        3: "E",
        4: (151.0, 12.0, 0.0),
    }
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def test_c4_exif_and_gps_stripped_at_ingest(data_dir, monkeypatch):
    import io

    from PIL import Image

    from ravens_nest import ingest, vision

    monkeypatch.setattr(
        vision,
        "extract_items",
        lambda data: [vision.blank_extraction("no key in test")],
    )
    original = _jpeg_with_exif()
    # Sanity: the source really carries EXIF before we claim we stripped it.
    assert len(Image.open(io.BytesIO(original)).getexif()) > 0

    result = ingest.ingest_photo(original)
    stored = ingest.asset_path(result["photo_hash"]).read_bytes()
    stored_exif = Image.open(io.BytesIO(stored)).getexif()
    assert len(stored_exif) == 0  # ALL metadata gone from the stored bytes
    assert len(stored_exif.get_ifd(0x8825)) == 0  # GPS specifically
    # Dedup still works: the same source photo re-ingested hits the same hash.
    again = ingest.ingest_photo(original)
    assert again["status"] == "duplicate_pending"
    assert again["photo_hash"] == result["photo_hash"]


def test_c4_corrupt_image_is_handled_not_raised(data_dir, monkeypatch):
    from ravens_nest import ingest, vision

    monkeypatch.setattr(
        vision,
        "extract_items",
        lambda data: [vision.blank_extraction("no key in test")],
    )
    garbage = b"\xff\xd8\xff\xe0" + b"definitely not decodable image data"
    result = ingest.ingest_photo(garbage)  # must not raise
    assert result["status"] == "new"
    assert ingest.asset_path(result["photo_hash"]).exists()

    truncated = _jpeg_with_exif()[: len(_jpeg_with_exif()) // 2]
    result = ingest.ingest_photo(truncated)  # truncated real JPEG: also fine
    assert result["status"] == "new"


def test_c4_health_flags_legacy_assets_with_gps(data_dir):
    from ravens_nest import config, health

    config.assets_dir().mkdir(parents=True, exist_ok=True)
    # Simulate a pre-fix asset: written directly, bypassing sanitize.
    (config.assets_dir() / ("ab" * 32 + ".jpg")).write_bytes(_jpeg_with_exif())
    flagged = health.assets_with_gps()
    assert flagged == ["ab" * 32 + ".jpg"]


def test_c4_followup_vision_api_receives_sanitized_bytes(data_dir, monkeypatch):
    """Audit C4 follow-up: the payload sent to the Anthropic API must be
    the same EXIF-stripped bytes that get stored — GPS never leaves the
    machine inside the capture request either."""
    import base64
    import io
    import json as json_module

    from PIL import Image

    from ravens_nest import ingest, vision

    captured_payloads = []

    def fake_call_model(messages):
        captured_payloads.append(messages[0]["content"][0]["source"]["data"])
        return json_module.dumps(
            {
                "items": [
                    {
                        "name": {"value": "Widget", "confidence": "high"},
                        "unit_type": {"value": "each", "confidence": "high"},
                        "questions": [],
                    }
                ]
            }
        )

    monkeypatch.setattr(vision, "_call_model", fake_call_model)
    result = ingest.ingest_photo(_jpeg_with_exif())

    assert len(captured_payloads) == 1
    sent_bytes = base64.standard_b64decode(captured_payloads[0])
    sent_image = Image.open(io.BytesIO(sent_bytes))
    assert len(sent_image.getexif()) == 0  # no EXIF at all in the API payload
    assert len(sent_image.getexif().get_ifd(0x8825)) == 0  # GPS specifically
    assert sent_image.format == "JPEG"  # still valid image data for the model
    # And it is byte-identical to what got hashed and stored.
    assert sent_bytes == ingest.asset_path(result["photo_hash"]).read_bytes()


# ------------------------------------------------------------------ C3


def _client():
    from fastapi.testclient import TestClient

    from ravens_nest.app import app

    return TestClient(app)


def test_c3_no_token_env_means_open_access(data_dir):
    assert _client().get("/").status_code == 200


def test_c3_token_required_when_set(data_dir, monkeypatch):
    monkeypatch.setenv("RAVENS_NEST_TOKEN", "shed-passphrase")
    client = _client()
    # Unauthenticated browser GET → login redirect; API POST → 401.
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    assert client.post("/command", data={"q": "low"}).status_code == 401
    # Health ping, login page, and static stay reachable.
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200

    # Wrong token → stays on login with an error, no cookie.
    response = client.post("/login", data={"token": "nope"})
    assert "Wrong token" in response.text
    # Right token → cookie set once, everything works after.
    response = client.post(
        "/login", data={"token": "shed-passphrase"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "rn_token" in response.cookies
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/").status_code == 200  # TestClient kept the cookie
    assert client.post("/command", data={"q": "low"}).status_code == 200

    # Header auth for scripts/curl.
    bare = _client()
    assert bare.get("/export/items.csv", follow_redirects=False).status_code == 302
    assert bare.get(
        "/export/items.csv", headers={"X-RN-Token": "shed-passphrase"}
    ).status_code == 200


def test_c3_ssrf_guard_refuses_private_addresses(data_dir):
    import pytest

    from ravens_nest import pricing

    for url in (
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/export/full.zip",
        "http://[::1]/",
        "http://10.0.0.5/x",
        "ftp://example.com/x",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            pricing.validate_link_url(url)


def test_c3_ssrf_guard_allows_public_and_allowlist(data_dir, monkeypatch):
    import socket as socket_module

    from ravens_nest import pricing

    # Public resolution → allowed (DNS mocked so the test runs offline).
    monkeypatch.setattr(
        socket_module,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    pricing.validate_link_url("https://example.com/product/123")  # no raise

    # A name resolving to a private address is refused even though the
    # literal looks public…
    import pytest

    monkeypatch.setattr(
        socket_module,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("192.168.1.50", 0))],
    )
    with pytest.raises(ValueError):
        pricing.validate_link_url("https://innocent-looking.example/")
    # …unless explicitly allowlisted.
    monkeypatch.setenv("RAVENS_NEST_FETCH_ALLOW_HOSTS", "innocent-looking.example")
    pricing.validate_link_url("https://innocent-looking.example/")  # no raise


def test_c3_add_link_route_rejects_bad_scheme(data_dir):
    from ravens_nest import store

    item = store.create_item("Servo", "each")["payload"]["id"]
    client = _client()
    client.post("/suppliers/seed", follow_redirects=False)
    import sqlite3

    from ravens_nest import db

    conn = db.connect()
    supplier = conn.execute("SELECT id FROM suppliers LIMIT 1").fetchone()[0]
    conn.close()
    response = client.post(
        f"/items/{item}/links",
        data={"supplier_id": supplier, "url": "file:///etc/passwd"},
    )
    assert response.status_code == 400
    assert "http" in response.json()["detail"]  # states what's expected


# ------------------------------------------------------------------ H2

QTY_MSG = "plain numbers like 8 or 12.5"


def test_h2_basket_add_garbage_qty_is_friendly(data_dir):
    from ravens_nest import store

    item = store.create_item("Widget", "each")["payload"]["id"]
    response = _client().post("/reorder/add", data={"item_id": item, "qty": "a few"})
    assert response.status_code == 200
    assert QTY_MSG in response.text
    assert "Reorder basket" in response.text  # re-rendered, not a 500


def test_h2_receive_order_garbage_qty_and_price_are_friendly(data_dir):
    from ravens_nest import db, store

    client = _client()
    client.post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    supplier = conn.execute("SELECT id FROM suppliers LIMIT 1").fetchone()[0]
    conn.close()
    item = store.create_item("Widget", "each")["payload"]["id"]

    response = client.post(
        "/orders/receive",
        data={"supplier_id": supplier, "reliability": "",
              "item_id": [item], "qty": ["5x"], "unit_price": [""]},
    )
    assert response.status_code == 200 and QTY_MSG in response.text

    response = client.post(
        "/orders/receive",
        data={"supplier_id": supplier, "reliability": "",
              "item_id": [item], "qty": ["5"], "unit_price": ["about 4"]},
    )
    assert response.status_code == 200 and QTY_MSG in response.text
    # Nothing half-applied on the price failure? The qty event fires before
    # the price parse per line — assert the guard runs before any write.
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (item,)).fetchone()[0]
    conn.close()
    assert qty == "0"


def test_h2_supplier_update_garbage_numbers_are_friendly(data_dir):
    from ravens_nest import db

    client = _client()
    client.post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    supplier = conn.execute("SELECT id FROM suppliers LIMIT 1").fetchone()[0]
    conn.close()
    response = client.post(
        f"/suppliers/{supplier}",
        data={"reliability": "", "free_shipping_threshold_aud": "free over $99",
              "typical_shipping_aud": "", "typical_lead_days": ""},
    )
    assert response.status_code == 200 and QTY_MSG in response.text
    response = client.post(
        f"/suppliers/{supplier}",
        data={"reliability": "", "free_shipping_threshold_aud": "",
              "typical_shipping_aud": "", "typical_lead_days": "a week"},
    )
    assert response.status_code == 200 and "whole number" in response.text


def test_h2_command_need_garbage_qty_is_friendly(data_dir):
    from ravens_nest import store

    item = store.create_item("Widget", "each")["payload"]["id"]
    response = _client().post("/command/need", data={"item_id": item, "qty": "lots"})
    assert response.status_code == 200
    assert QTY_MSG in response.text


# -------------------------------------------- atomic multi-line writes


def test_atomic_receive_order_bad_line_writes_nothing(data_dir):
    """A 3-line order with garbage in line 3 must write ZERO events and
    name the bad line — never leave lines 1-2 half-applied."""
    from ravens_nest import db, store

    client = _client()
    client.post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    supplier = conn.execute("SELECT id FROM suppliers LIMIT 1").fetchone()[0]
    conn.close()
    a = store.create_item("Part A", "each", qty_on_hand=1)["payload"]["id"]
    b = store.create_item("Part B", "each", qty_on_hand=2)["payload"]["id"]
    c = store.create_item("Part C", "each", qty_on_hand=3)["payload"]["id"]
    baseline = len(events.read_all_events())

    response = client.post(
        "/orders/receive",
        data={
            "supplier_id": supplier,
            "reliability": "4",
            "item_id": [a, b, c],
            "qty": ["5", "10", "banana"],
            "unit_price": ["1.00", "", ""],
        },
    )
    assert response.status_code == 200
    assert "line 3" in response.text and "banana" in response.text
    assert "Nothing was recorded" in response.text
    assert len(events.read_all_events()) == baseline  # ZERO events written
    conn = db.connect()
    quantities = [
        conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (x,)).fetchone()[0]
        for x in (a, b, c)
    ]
    rating = conn.execute(
        "SELECT reliability FROM suppliers WHERE id = ?", (supplier,)
    ).fetchone()[0]
    conn.close()
    assert quantities == ["1", "2", "3"]  # untouched
    assert rating is None  # the rating didn't apply either


def test_atomic_receive_order_all_valid_applies_every_line(data_dir):
    from ravens_nest import db, store

    client = _client()
    client.post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    supplier = conn.execute("SELECT id FROM suppliers LIMIT 1").fetchone()[0]
    conn.close()
    a = store.create_item("Part A", "each", qty_on_hand=1)["payload"]["id"]
    b = store.create_item("Part B", "each", qty_on_hand=2)["payload"]["id"]
    c = store.create_item("Part C", "each", qty_on_hand=3)["payload"]["id"]

    response = client.post(
        "/orders/receive",
        data={
            "supplier_id": supplier,
            "reliability": "5",
            "item_id": [a, b, c],
            "qty": ["5", "10", "0.5"],
            "unit_price": ["1.00", "", "2.50"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = db.connect()
    rows = {
        x: conn.execute(
            "SELECT qty_on_hand, last_paid_aud FROM items WHERE id = ?", (x,)
        ).fetchone()
        for x in (a, b, c)
    }
    rating = conn.execute(
        "SELECT reliability FROM suppliers WHERE id = ?", (supplier,)
    ).fetchone()[0]
    conn.close()
    assert (rows[a]["qty_on_hand"], rows[a]["last_paid_aud"]) == ("6", "1")
    assert (rows[b]["qty_on_hand"], rows[b]["last_paid_aud"]) == ("12", None)
    assert (rows[c]["qty_on_hand"], rows[c]["last_paid_aud"]) == ("3.5", "2.5")
    assert rating == 5


def test_atomic_recount_bad_count_writes_nothing(data_dir):
    from ravens_nest import db, store

    a = store.create_item("Part A", "each", qty_on_hand=10, location_id="A-1-1")["payload"]["id"]
    b = store.create_item("Part B", "each", qty_on_hand=5, location_id="A-1-1")["payload"]["id"]
    baseline = len(events.read_all_events())

    response = _client().post(
        "/command/recount",
        data={"location_id": "A-1-1", "item_id": [a, b], "counted": ["7", "some"]},
    )
    assert "Nothing recounted" in response.text and "line 2" in response.text
    assert len(events.read_all_events()) == baseline  # A's change did NOT apply
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (a,)).fetchone()[0]
    conn.close()
    assert qty == "10"

    # And a fully valid recount still applies both.
    response = _client().post(
        "/command/recount",
        data={"location_id": "A-1-1", "item_id": [a, b], "counted": ["7", "5"]},
    )
    assert "1 corrected, 1 already right" in response.text


def test_atomic_import_apply_row_no_orphan_event_for_vanished_target(data_dir):
    from ravens_nest import importexport

    rows, errors = importexport.parse_items_csv("name,qty\nGhost part,5\n")
    assert errors == []
    baseline = len(events.read_all_events())
    outcome = importexport.apply_row(rows[0], "no-such-item-id")
    assert "skipped" in outcome
    assert len(events.read_all_events()) == baseline  # no orphan item.updated


# ------------------------------------------------ enforcement (item 10)


def test_10a_every_event_type_has_an_undo_registration():
    """A new event type must be declared undoable (with an inverse) or
    explicitly not-undoable (with a reason) — it cannot silently miss the
    undo system."""
    from ravens_nest import undo
    from ravens_nest.events import EVENT_TYPES

    registered = undo.UNDOABLE | set(undo.NOT_UNDOABLE_REASONS)
    missing = EVENT_TYPES - registered
    assert missing == set(), (
        f"event type(s) {sorted(missing)} are registered in neither "
        f"undo.UNDOABLE nor undo.NOT_UNDOABLE_REASONS"
    )


def test_10b_every_event_type_narrates_without_raising():
    """history.narrate must produce SOME line for every event type, even
    with an empty payload — a generic fallback, never an exception."""
    from ravens_nest import history
    from ravens_nest.events import EVENT_TYPES

    for event_type in sorted(EVENT_TYPES):
        line = history.narrate(
            {"type": event_type, "payload": {}, "ts": "2026-07-19T00:00:00+00:00"},
            {},
        )
        assert isinstance(line, str) and line, f"no narration for {event_type}"
    # Unknown/future types get the generic line rather than a crash.
    line = history.narrate({"type": "future.event", "payload": {}}, {})
    assert "future.event" in line


def test_10c_schema_version_on_new_events_and_v1_default(data_dir):
    from ravens_nest import db, events, replay

    event = events.new_event("item.created", {"id": "x", "name": "N", "unit_type": "each"})
    assert event["v"] == 1

    # A pre-versioning event (no "v") still applies — defaulted to v1.
    legacy = {
        "id": "22222222-3333-4444-5555-666666666666",
        "ts": "2026-01-01T00:00:00+00:00",
        "actor": "old-machine",
        "type": "item.created",
        "payload": {"id": "legacy-item", "name": "Legacy", "unit_type": "each"},
    }
    events.append_to_log(legacy)
    conn = db.connect()
    with conn:
        assert replay.apply_event(conn, legacy) is True
    row = conn.execute("SELECT name FROM items WHERE id = 'legacy-item'").fetchone()
    conn.close()
    assert row["name"] == "Legacy"
    # And full replay (mixed v1 / no-v log) stays deterministic.
    count = replay.rebuild()
    assert count == 1


# ------------------------------------------------------------------ H6


def _priced_link(price="4.50"):
    from ravens_nest import db, store

    client = _client()
    client.post("/suppliers/seed", follow_redirects=False)
    conn = db.connect()
    supplier = conn.execute(
        "SELECT id FROM suppliers WHERE name='Core Electronics'"
    ).fetchone()[0]
    conn.close()
    item = store.create_item("Servo", "each", qty_on_hand=0, min_qty=2)["payload"]["id"]
    store.add_item_link(item, supplier, "https://core.example/sg90", last_price_aud=price)
    return item, supplier


def test_h6_wildly_different_price_is_rejected_and_noted(data_dir, monkeypatch):
    from ravens_nest import db, pricing, sourcing

    item, supplier = _priced_link("4.50")
    # A page redesign: the parser grabs a shipping fee.
    monkeypatch.setattr(
        pricing, "fetch_url", lambda url: '<meta itemprop="price" content="0.10">'
    )
    updated, total, stale = sourcing.run_pricing()
    assert updated == 0 and total == 1
    assert any("kept old price" in note and "0.10" in note for note in stale)
    conn = db.connect()
    row = conn.execute(
        "SELECT last_price_aud FROM item_links WHERE item_id = ?", (item,)
    ).fetchone()
    paid = conn.execute(
        "SELECT last_paid_aud FROM items WHERE id = ?", (item,)
    ).fetchone()
    conn.close()
    assert row["last_price_aud"] == "4.5"  # old price kept
    assert paid["last_paid_aud"] is None  # last_paid never touched by scraping


def test_h6_reasonable_price_change_is_accepted(data_dir, monkeypatch):
    from ravens_nest import db, pricing, sourcing

    item, _ = _priced_link("4.50")
    monkeypatch.setattr(
        pricing, "fetch_url", lambda url: '<meta itemprop="price" content="5.95">'
    )
    updated, total, stale = sourcing.run_pricing()
    assert updated == 1 and stale == []
    conn = db.connect()
    row = conn.execute(
        "SELECT last_price_aud FROM item_links WHERE item_id = ?", (item,)
    ).fetchone()
    conn.close()
    assert row["last_price_aud"] == "5.95"


def test_h6_thresholds_configurable_and_first_price_always_accepted(
    data_dir, monkeypatch
):
    from ravens_nest import db, pricing, sourcing, store

    item, supplier = _priced_link("4.50")
    # Widen the band → the same 0.10 now passes.
    monkeypatch.setenv("RAVENS_NEST_PRICE_RATIO_MIN", "0.01")
    monkeypatch.setattr(
        pricing, "fetch_url", lambda url: '<meta itemprop="price" content="0.10">'
    )
    updated, _, _ = sourcing.run_pricing()
    assert updated == 1

    # A link with NO stored price accepts whatever the page says.
    monkeypatch.delenv("RAVENS_NEST_PRICE_RATIO_MIN")
    conn = db.connect()
    other_supplier = conn.execute(
        "SELECT id FROM suppliers WHERE name='Jaycar'"
    ).fetchone()[0]
    conn.close()
    store.add_item_link(item, other_supplier, "https://jaycar.example/sg90")
    monkeypatch.setattr(
        pricing, "fetch_url", lambda url: '<meta itemprop="price" content="99.00">'
    )
    updated, _, _ = sourcing.run_pricing()
    assert updated >= 1  # nothing to compare against → accepted


# ------------------------------------------------------------------ H3


def test_h3_vision_client_has_timeout_and_degrades_to_blank_card(data_dir, monkeypatch):
    import anthropic
    import httpx

    from ravens_nest import vision

    seen_kwargs = {}

    class SlowClient:
        def __init__(self, **kwargs):
            seen_kwargs.update(kwargs)
            self.messages = self

        def create(self, **kwargs):
            raise anthropic.APITimeoutError(
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    monkeypatch.setattr(vision.anthropic, "Anthropic", SlowClient)
    extractions = vision.extract_items(b"\xff\xd8\xff\xe0fake")
    assert seen_kwargs.get("timeout") == 30.0  # bounded, not the 10-min default
    assert len(extractions) == 1
    assert extractions[0]["error"] is not None  # blank card, not a hang
    assert all(
        f["value"] is None for f in extractions[0]["fields"].values()
    )  # nothing invented on the failure path


# ------------------------------------------------------------------ H1


def test_h1_merge_repoints_bom_lines_and_unmerge_restores(data_dir):
    from ravens_nest import bom, db, merge, replay, store, undo

    a = store.create_item("sg90 dupe", "each", qty_on_hand=3)["payload"]["id"]
    b = store.create_item("SG90 servo", "each", qty_on_hand=4)["payload"]["id"]
    pid = store.create_project("Bot")["payload"]["id"]
    store.import_bom(pid, [{
        "line_no": 1, "part_number": "SG90", "description": "servo",
        "quantity": "2", "unit": "each", "reference_designators": None, "notes": None,
    }])
    store.match_bom_line(pid, 1, a, "manual")

    ok, message, merge_event = merge.perform_merge(a, b, None)
    assert ok, message

    conn = db.connect()
    line = conn.execute(
        "SELECT item_id FROM bom_lines WHERE project_id = ? AND line_no = 1", (pid,)
    ).fetchone()
    conn.close()
    assert line["item_id"] == b  # the BOM line followed the stock

    # A build now succeeds against the target's combined stock (3+4 = 7).
    ok, message, _ = bom.attempt_build(pid, 3)  # needs 6
    assert ok, message
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (b,)).fetchone()[0]
    conn.close()
    assert qty == "1"

    # Undo the build, then unmerge — the BOM ref returns to A.
    assert undo.perform_undo(undo.undo_stack()[0]["id"])[0] is True
    ok, message = undo.perform_undo(merge_event)
    assert ok, message
    conn = db.connect()
    line = conn.execute(
        "SELECT item_id FROM bom_lines WHERE project_id = ? AND line_no = 1", (pid,)
    ).fetchone()
    conn.close()
    assert line["item_id"] == a

    # Replay determinism with the enriched payload in the log.
    def snapshot():
        conn = db.connect()
        try:
            return {
                table: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))
                for table in ("items", "bom_lines", "aliases", "reservations", "builds")
            }
        finally:
            conn.close()

    before = snapshot()
    replay.rebuild()
    assert snapshot() == before
