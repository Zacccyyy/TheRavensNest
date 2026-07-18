"""Event log: one JSON object per line in data/events/YYYY-MM.jsonl.

Events are the source of truth. They are only ever appended, never
edited, so the files merge cleanly across machines via Git.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

log = logging.getLogger(__name__)

# THE process-wide event-file lock (audit C1). Appending an event and any
# git operation that can rewrite an event file (pull/rebase/union merge)
# must hold this same lock, or a checkout can silently eat an append.
# SyncManager adopts this as its own lock rather than inventing a second.
_write_lock = threading.RLock()

EVENT_TYPES = frozenset(
    {
        "item.created",
        "item.updated",
        "item.qty_adjusted",
        "item.moved",
        "item.recounted",
        "location.created",
        "location.updated",
        "project.created",
        "bom.imported",
        "bom.line_matched",
        "reservation.created",
        "reservation.released",
        "build.executed",
        "build.reversed",
        "supplier.created",
        "supplier.updated",
        "item.link_added",
        "item.link_price_checked",
        "basket.item_added",
        "basket.item_removed",
        "item.archived",
        "item.unarchived",
        "item.merged",
        "item.unmerged",
        "item.alias_added",
    }
)


def new_event(type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type!r}")
    return {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": socket.gethostname(),
        "type": type,
        "payload": payload,
    }


def append_to_log(event: dict[str, Any]) -> None:
    """Append one event to the log file for its timestamp's month.

    Serialised against sync's file rewrites via _write_lock (audit C1)."""
    month = event["ts"][:7]  # "YYYY-MM" prefix of the ISO8601 timestamp
    path = config.events_dir() / f"{month}.jsonl"
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            # Audit C2: the log is the source of truth — flush every line
            # so a crash mid-write leaves at most one partial trailing line
            # (which read_all_events quarantines rather than choking on).
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:  # some filesystems can't fsync — flush is the floor
                pass


def read_all_events() -> list[dict[str, Any]]:
    """All events from every log file, sorted by (ts, id) for deterministic replay."""
    with _write_lock:
        return _read_all_events_locked()


def _read_all_events_locked() -> list[dict[str, Any]]:
    """Read every log file. Corrupt lines (invalid JSON or missing the
    envelope keys) never brick replay/history/undo — they are moved to
    events/quarantine-<ts>.txt, logged loudly, and skipped (audit C2).
    A single trailing partial line is the disk-full / power-loss
    signature and gets exactly the same treatment."""
    events: list[dict[str, Any]] = []
    directory = config.events_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            good_lines: list[str] = []
            bad: list[tuple[int, str]] = []
            with path.open("r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    event = _parse_event_line(line)
                    if event is None:
                        bad.append((lineno, line))
                        continue
                    good_lines.append(line)
                    events.append(event)
            if bad:
                _quarantine(path, bad, good_lines)
    events.sort(key=lambda e: (e["ts"], e["id"]))
    return events


def _parse_event_line(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    if not all(key in event for key in ("id", "ts", "type", "payload")):
        return None  # JSON-valid but not an event envelope — same quarantine
    return event


def _quarantine(path, bad: list[tuple[int, str]], good_lines: list[str]) -> None:
    """Set corrupt lines aside and repair the log file in place. The
    repair is atomic (write temp + replace) and happens under _write_lock
    (we are called from the locked read)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_path = path.parent / f"quarantine-{stamp}.txt"
    with quarantine_path.open("a", encoding="utf-8", newline="\n") as q:
        for lineno, line in bad:
            q.write(f"# from {path.name} line {lineno}\n")
            q.write(line + "\n")
    temp = path.with_suffix(".jsonl.tmp")
    temp.write_text(
        "".join(line + "\n" for line in good_lines), encoding="utf-8", newline="\n"
    )
    os.replace(temp, path)
    log.error(
        "QUARANTINED %d corrupt event line(s) from %s -> %s. The rest of the "
        "log applied normally; inspect the quarantine file to recover or "
        "discard the damaged line(s).",
        len(bad),
        path.name,
        quarantine_path.name,
    )


def quarantined_count() -> int:
    """Corrupt lines set aside so far — surfaced on the health dashboard."""
    directory = config.events_dir()
    if not directory.is_dir():
        return 0
    count = 0
    for path in directory.glob("quarantine-*.txt"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    count += 1
        except OSError:
            pass
    return count
