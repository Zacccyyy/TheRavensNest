"""Event log: one JSON object per line in data/events/YYYY-MM.jsonl.

Events are the source of truth. They are only ever appended, never
edited, so the files merge cleanly across machines via Git.
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

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
    """Append one event to the log file for its timestamp's month."""
    month = event["ts"][:7]  # "YYYY-MM" prefix of the ISO8601 timestamp
    path = config.events_dir() / f"{month}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def read_all_events() -> list[dict[str, Any]]:
    """All events from every log file, sorted by (ts, id) for deterministic replay."""
    events: list[dict[str, Any]] = []
    directory = config.events_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{path.name}:{lineno}: invalid JSON") from exc
                    events.append(event)
    events.sort(key=lambda e: (e["ts"], e["id"]))
    return events
