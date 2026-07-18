"""Photo ingest pipeline: inbox → content-addressed asset → vision
extraction → pending review card.

Photos are stored at data/assets/<sha256>.jpg and deduplicated by hash —
the same photo ingested twice (upload + folder sync, say) produces one
asset, one card, and one vision call. Cards awaiting review live as JSON
files under data/pending/ so they survive cache rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db, vision

log = logging.getLogger(__name__)

JPEG_MAGIC = b"\xff\xd8"


def asset_path(photo_hash: str) -> Path:
    return config.assets_dir() / f"{photo_hash}.jpg"


def store_asset(data: bytes) -> tuple[str, bool]:
    """Store a photo content-addressed. Returns (sha256, already_existed)."""
    photo_hash = hashlib.sha256(data).hexdigest()
    path = asset_path(photo_hash)
    if path.exists():
        return photo_hash, True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return photo_hash, False


def _card_path(photo_hash: str) -> Path:
    return config.pending_dir() / f"{photo_hash}.json"


def load_card(photo_hash: str) -> dict[str, Any] | None:
    path = _card_path(photo_hash)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_card(card: dict[str, Any]) -> None:
    path = _card_path(card["photo_hash"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_card(photo_hash: str) -> None:
    _card_path(photo_hash).unlink(missing_ok=True)


def list_cards() -> list[dict[str, Any]]:
    """Pending cards, oldest first."""
    directory = config.pending_dir()
    cards = []
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                cards.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                log.warning("skipping unreadable pending card %s", path.name)
    cards.sort(key=lambda c: c.get("created_ts", ""))
    return cards


def _item_with_photo(photo_hash: str) -> str | None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM items WHERE photo_hash = ?", (photo_hash,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def ingest_photo(data: bytes) -> dict[str, Any]:
    """Run one photo through the pipeline.

    Returns {"photo_hash", "status", "card"} where status is "new",
    "duplicate_pending" (card already queued), or "already_cataloged"
    (an item already carries this photo)."""
    photo_hash, _ = store_asset(data)

    existing = load_card(photo_hash)
    if existing is not None:
        return {"photo_hash": photo_hash, "status": "duplicate_pending", "card": existing}

    item_id = _item_with_photo(photo_hash)
    if item_id is not None:
        return {
            "photo_hash": photo_hash,
            "status": "already_cataloged",
            "item_id": item_id,
            "card": None,
        }

    extraction = vision.extract_fields(data)
    card = {
        "photo_hash": photo_hash,
        "created_ts": datetime.now(timezone.utc).isoformat(),
        "fields": extraction["fields"],
        "questions": extraction["questions"],
        "error": extraction.get("error"),
    }
    save_card(card)
    return {"photo_hash": photo_hash, "status": "new", "card": card}


def scan_inbox() -> dict[str, Any]:
    """Ingest every *.jpg in the inbox folder, consuming files on success.
    Files that fail (unreadable, not a JPEG) are left in place and reported."""
    results: dict[str, Any] = {"ingested": 0, "duplicates": 0, "errors": []}
    inbox = config.inbox_dir()
    if not inbox.is_dir():
        return results
    for path in sorted(inbox.glob("*.jpg")) + sorted(inbox.glob("*.jpeg")):
        try:
            data = path.read_bytes()
            if not data.startswith(JPEG_MAGIC):
                results["errors"].append(f"{path.name}: not a JPEG")
                continue
            outcome = ingest_photo(data)
            if outcome["status"] == "new":
                results["ingested"] += 1
            else:
                results["duplicates"] += 1
            path.unlink()
        except Exception as exc:
            log.exception("inbox ingest failed for %s", path.name)
            results["errors"].append(f"{path.name}: {exc}")
    return results
