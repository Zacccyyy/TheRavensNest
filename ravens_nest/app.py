"""FastAPI app. Run with: uv run uvicorn ravens_nest.app:app --reload

Startup: git pull + replay new events, then ingest any photos waiting in
the inbox folder. Event writes through the store schedule a debounced
commit+push.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from decimal import InvalidOperation
from pathlib import Path

import anyio
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db, ingest, store, ui, ui_command
from .commands import router as commands_router
from .locations import InvalidLocationId
from .movement import router as movement_router
from .projects import router as projects_router
from .sourcing import router as sourcing_router
from .sync import SyncManager

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _get_manager() -> SyncManager:
    manager = getattr(app.state, "sync_manager", None)
    if manager is None:
        manager = SyncManager()
        app.state.sync_manager = manager
    return manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    manager = _get_manager()
    await anyio.to_thread.run_sync(manager.startup)
    await anyio.to_thread.run_sync(ingest.scan_inbox)
    store.add_write_listener(manager.on_event_written)
    try:
        yield
    finally:
        store.remove_write_listener(manager.on_event_written)
        await anyio.to_thread.run_sync(manager.stop)


app = FastAPI(title="The Raven's Nest", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(commands_router)
app.include_router(movement_router)
app.include_router(projects_router)
app.include_router(sourcing_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """The primary interface: one command bar plus results. No nav tree."""
    return ui_command.command_page(len(ingest.list_cards()))


# ------------------------------------------------------------------- sync


@app.post("/sync")
def sync_now() -> dict:
    """Manual full sync: commit pending events, pull+apply, push."""
    return _get_manager().sync_now()


@app.get("/sync/status")
def sync_status() -> dict:
    return _get_manager().status_dict()


# ---------------------------------------------------------------- capture


@app.post("/capture")
async def capture(photo: UploadFile = File(...)) -> dict:
    """Photo upload from the phone web UI: store, identify, queue for review."""
    data = await photo.read()
    if not data.startswith(ingest.JPEG_MAGIC):
        raise HTTPException(status_code=400, detail="only JPEG photos are supported")
    result = await anyio.to_thread.run_sync(ingest.ingest_photo, data)
    return {"photo_hash": result["photo_hash"], "status": result["status"]}


@app.post("/inbox/scan")
async def inbox_scan() -> dict:
    """Ingest data/inbox/*.jpg on demand."""
    return await anyio.to_thread.run_sync(ingest.scan_inbox)


@app.get("/assets/{photo_hash}.jpg")
def asset(photo_hash: str) -> FileResponse:
    if not _HASH_RE.match(photo_hash):
        raise HTTPException(status_code=404)
    path = ingest.asset_path(photo_hash)
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


# ------------------------------------------------------------------ queue


def _merge_targets() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, qty_on_hand, unit_type FROM items ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _next_card() -> str:
    cards = ingest.list_cards()
    if not cards:
        return ui.empty_partial()
    return ui.card_partial(cards[0], len(cards), _merge_targets())


@app.get("/queue", response_class=HTMLResponse)
def queue() -> str:
    return ui.queue_page(ingest.list_cards(), _merge_targets())


@app.post("/queue/{photo_hash}/confirm", response_class=HTMLResponse)
def confirm_card(
    photo_hash: str,
    name: str = Form(""),
    unit_type: str = Form("each"),
    qty: str = Form("0"),
    description: str = Form(""),
    part_number: str = Form(""),
    manufacturer: str = Form(""),
    package_type: str = Form(""),
    location_id: str = Form(""),
) -> str:
    card = ingest.load_card(photo_hash)
    if card is None:
        raise HTTPException(status_code=404, detail="card not found")

    def rerender(message: str) -> str:
        return ui.card_partial(card, len(ingest.list_cards()), _merge_targets(), error=message)

    if not name.strip():
        return rerender("Name is required.")
    if unit_type not in ui.UNIT_TYPES:
        return rerender(f"Invalid unit type {unit_type!r}.")
    # Manufacturer/package detail folds into the description column.
    parts = [description.strip()]
    if manufacturer.strip():
        parts.append(f"Manufacturer: {manufacturer.strip()}")
    if package_type.strip():
        parts.append(f"Package: {package_type.strip()}")
    try:
        store.create_item(
            name.strip(),
            unit_type,
            qty_on_hand=qty.strip() or "0",
            description="; ".join(p for p in parts if p),
            part_number=part_number.strip() or None,
            location_id=location_id.strip() or None,
            photo_hash=photo_hash,
        )
    except InvalidLocationId as exc:
        return rerender(str(exc))
    except (InvalidOperation, TypeError, ValueError) as exc:
        return rerender(f"Invalid quantity: {exc}")
    ingest.delete_card(photo_hash)
    return _next_card()


@app.post("/queue/{photo_hash}/merge", response_class=HTMLResponse)
def merge_card(
    photo_hash: str, item_id: str = Form(...), qty: str = Form("1")
) -> str:
    card = ingest.load_card(photo_hash)
    if card is None:
        raise HTTPException(status_code=404, detail="card not found")

    def rerender(message: str) -> str:
        return ui.card_partial(card, len(ingest.list_cards()), _merge_targets(), error=message)

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT photo_hash FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return rerender("That item no longer exists.")
    try:
        store.adjust_qty(item_id, qty.strip() or "0", f"merged photo capture {photo_hash[:12]}")
    except (InvalidOperation, TypeError, ValueError) as exc:
        return rerender(f"Invalid quantity: {exc}")
    if row["photo_hash"] is None:
        store.update_item(item_id, photo_hash=photo_hash)
    ingest.delete_card(photo_hash)
    return _next_card()


@app.post("/queue/{photo_hash}/skip", response_class=HTMLResponse)
def skip_card(photo_hash: str) -> str:
    ingest.delete_card(photo_hash)
    return _next_card()
