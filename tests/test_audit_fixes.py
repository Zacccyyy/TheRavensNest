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
