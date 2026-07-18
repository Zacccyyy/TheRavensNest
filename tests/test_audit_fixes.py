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
