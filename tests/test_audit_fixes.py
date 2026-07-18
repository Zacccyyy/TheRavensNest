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
