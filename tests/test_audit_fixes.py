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
