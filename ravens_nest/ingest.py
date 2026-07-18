"""Photo ingest pipeline: inbox → content-addressed asset → vision
extraction → pending review card.

Photos are stored at data/assets/<sha256>.jpg and deduplicated by hash —
the same photo ingested twice (upload + folder sync, say) produces one
asset, one card, and one vision call. Cards awaiting review live as JSON
files under data/pending/ so they survive cache rebuilds.
"""

from __future__ import annotations

import hashlib
import io
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


def sanitize_image(data: bytes) -> bytes:
    """Re-encode a photo through Pillow with NO metadata (audit C4).

    Phone photos routinely carry EXIF GPS — those bytes get committed to
    git, served on the LAN, and zipped into exports, so the location tags
    must die here, before hashing. Re-encoding also normalises truncated
    files. If Pillow can't parse the bytes at all we store them verbatim:
    unparseable data has no parseable EXIF either, and the queue's
    blank-card path still wants the photo preserved.
    """
    from PIL import Image, ImageFile, ImageOps

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        image = Image.open(io.BytesIO(data))
        # Bake the EXIF orientation into the pixels BEFORE dropping EXIF,
        # or portrait phone photos would render sideways.
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=90)  # no exif= → metadata gone
        return out.getvalue()
    except Exception as exc:
        log.warning("could not re-encode photo (%s) — storing bytes as-is", exc)
        return data


def store_asset(data: bytes, *, sanitized: bool = False) -> tuple[str, bool]:
    """Sanitize (EXIF stripped) and store a photo content-addressed.
    Returns (sha256, already_existed). The hash is of the SANITIZED
    bytes, so the same source photo re-uploaded still deduplicates.
    Existing assets from before EXIF stripping are left untouched —
    the health dashboard flags any that still carry GPS.

    sanitized=True means the caller already ran sanitize_image (so the
    same clean bytes can be shared with the vision call — audit C4
    follow-up: the ORIGINAL bytes must never leave the machine)."""
    if not sanitized:
        data = sanitize_image(data)
    photo_hash = hashlib.sha256(data).hexdigest()
    path = asset_path(photo_hash)
    if path.exists():
        return photo_hash, True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return photo_hash, False


def _card_path(card_id: str) -> Path:
    return config.pending_dir() / f"{card_id}.json"


def load_card(card_id: str) -> dict[str, Any] | None:
    path = _card_path(card_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_card(card: dict[str, Any]) -> None:
    path = _card_path(card["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_card(card_id: str) -> None:
    """Dismiss one card; sibling detections from the same photo stay."""
    _card_path(card_id).unlink(missing_ok=True)


def cards_for_photo(photo_hash: str) -> list[dict[str, Any]]:
    directory = config.pending_dir()
    if not directory.is_dir():
        return []
    cards = []
    for path in sorted(directory.glob(f"{photo_hash}*.json")):
        try:
            cards.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cards


def list_cards() -> list[dict[str, Any]]:
    """Pending cards, oldest first."""
    directory = config.pending_dir()
    cards = []
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
                card.setdefault("id", card.get("photo_hash", path.stem))
                cards.append(card)
            except (json.JSONDecodeError, OSError):
                log.warning("skipping unreadable pending card %s", path.name)
    cards.sort(key=lambda c: (c.get("created_ts", ""), c.get("index", 0)))
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
    """Run one photo through the pipeline. A photo of several distinct
    parts yields one review card per detection, all sharing the (single,
    content-addressed) source photo.

    Returns {"photo_hash", "status", "cards"} where status is "new",
    "duplicate_pending" (cards already queued), or "already_cataloged"
    (an item already carries this photo)."""
    # Sanitize ONCE, up front: the same EXIF-stripped, re-encoded bytes
    # are hashed, stored, AND sent to the vision API — the original
    # (potentially GPS-tagged) bytes never leave this function.
    data = sanitize_image(data)
    photo_hash, _ = store_asset(data, sanitized=True)

    existing = cards_for_photo(photo_hash)
    if existing:
        return {
            "photo_hash": photo_hash,
            "status": "duplicate_pending",
            "cards": existing,
            "card": existing[0],
        }

    item_id = _item_with_photo(photo_hash)
    if item_id is not None:
        return {
            "photo_hash": photo_hash,
            "status": "already_cataloged",
            "item_id": item_id,
            "cards": [],
            "card": None,
        }

    extractions = vision.extract_items(data)
    now = datetime.now(timezone.utc).isoformat()
    cards = []
    for index, extraction in enumerate(extractions):
        # First detection keeps the bare hash as its id (single-item photos
        # behave exactly as before); siblings get ~2, ~3, …
        card_id = photo_hash if index == 0 else f"{photo_hash}~{index + 1}"
        card = {
            "id": card_id,
            "photo_hash": photo_hash,
            "index": index,
            "sibling_count": len(extractions),
            "created_ts": now,
            "fields": extraction["fields"],
            "questions": extraction["questions"],
            "photo_region": extraction.get("photo_region"),
            "error": extraction.get("error"),
        }
        save_card(card)
        cards.append(card)
    return {"photo_hash": photo_hash, "status": "new", "cards": cards, "card": cards[0]}


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
