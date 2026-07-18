"""FastAPI app. Run with: uv run uvicorn ravens_nest.app:app --reload

Startup: git pull + replay new events, then ingest any photos waiting in
the inbox folder. Event writes through the store schedule a debounced
commit+push.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from decimal import InvalidOperation
from pathlib import Path

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

from . import db, ingest, setup_wizard, store, ui, ui_command
from .commands import router as commands_router
from .importexport import router as importexport_router
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
    if not os.environ.get("RAVENS_NEST_TOKEN"):
        log.warning(
            "RAVENS_NEST_TOKEN is not set — anyone who can reach this server "
            "can read and write the whole inventory (and download it via "
            "/export/full.zip). Fine on a trusted single-user machine; set the "
            "variable to require a passphrase before serving on a network."
        )
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
app.include_router(importexport_router)
app.include_router(setup_wizard.router)


# ------------------------------------------------------------------- auth
# Audit C3: single shared token. Unset = open (single-user default, warned
# about at startup); set = every route except the health ping, the login
# page, and static files requires it — via HttpOnly cookie (entered once
# per browser) or an X-RN-Token header (for curl/scripts).

_AUTH_EXEMPT = ("/health", "/login")

_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Raven's Nest — unlock</title>
<style>body{{font-family:system-ui,sans-serif;max-width:24rem;margin:15vh auto;padding:1rem}}
input{{width:100%;font-size:1.1rem;padding:.6rem;box-sizing:border-box;margin:.5rem 0}}
button{{padding:.6rem 1.2rem;font-size:1rem;background:#1d4ed8;color:#fff;border:none;border-radius:6px}}
.err{{color:#991b1b}}</style></head>
<body>
<h1>🐦‍⬛ The Raven's Nest</h1>
<p>This inventory is protected. Enter the access token
(the <code>RAVENS_NEST_TOKEN</code> value set on the server) — once per browser.</p>
{error}
<form method="post" action="/login">
  <input type="password" name="token" autofocus autocomplete="current-password"
         placeholder="access token">
  <button type="submit">Unlock</button>
</form>
</body></html>"""


def _required_token() -> str | None:
    return os.environ.get("RAVENS_NEST_TOKEN") or None


def _token_ok(request: Request, token: str) -> bool:
    supplied = request.cookies.get("rn_token") or request.headers.get("x-rn-token") or ""
    return hmac.compare_digest(supplied, token)


@app.middleware("http")
async def require_token(request: Request, call_next):
    token = _required_token()
    if token is None:
        return await call_next(request)
    path = request.url.path
    if path in _AUTH_EXEMPT or path.startswith("/static/"):
        return await call_next(request)
    if _token_ok(request, token):
        return await call_next(request)
    if request.method == "GET":
        return RedirectResponse(url="/login", status_code=302)
    return JSONResponse(
        status_code=401,
        content={
            "detail": "authentication required — open /login in a browser, or "
            "send the token in an X-RN-Token header"
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_form() -> str:
    if _required_token() is None:
        return _LOGIN_HTML.format(error="<p class='err'>No token is configured — the app is open. Just go to <a href='/'>the command bar</a>.</p>")
    return _LOGIN_HTML.format(error="")


@app.post("/login")
def login_submit(request: Request, token: str = Form("")):
    required = _required_token()
    if required is None:
        return RedirectResponse(url="/", status_code=303)
    if not hmac.compare_digest(token, required):
        return HTMLResponse(
            _LOGIN_HTML.format(error="<p class='err'>Wrong token — check the RAVENS_NEST_TOKEN value on the server.</p>"),
            status_code=200,
        )
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "rn_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=180 * 24 * 3600,  # once per browser, effectively
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """The primary interface: one command bar plus results. No nav tree.
    A fresh install gets pointed at the guided setup."""
    return ui_command.command_page(
        len(ingest.list_cards()), show_setup=setup_wizard.is_fresh_install()
    )


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
            "SELECT id, name, qty_on_hand, unit_type FROM items WHERE archived = 0 ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _near_matches_for(card: dict) -> list[dict]:
    """Proactive duplicate check at confirm time — surfaced ABOVE the
    confirm button so a duplicate is impossible to miss."""
    from . import merge

    fields = card.get("fields", {})
    name = (fields.get("name") or {}).get("value")
    part = (fields.get("part_number") or {}).get("value")
    if not name and not part:
        return []
    conn = db.connect()
    try:
        return merge.near_matches(conn, name or "", part)
    finally:
        conn.close()


def _next_card() -> str:
    cards = ingest.list_cards()
    if not cards:
        return ui.empty_partial()
    return ui.card_partial(
        cards[0], len(cards), _merge_targets(), near=_near_matches_for(cards[0])
    )


@app.get("/queue", response_class=HTMLResponse)
def queue() -> str:
    cards = ingest.list_cards()
    near = _near_matches_for(cards[0]) if cards else []
    return ui.queue_page(cards, _merge_targets(), near)


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
            photo_hash=card["photo_hash"],
        )
    except InvalidLocationId as exc:
        return rerender(str(exc))
    except (InvalidOperation, TypeError, ValueError) as exc:
        return rerender(f"Invalid quantity: {exc}")
    ingest.delete_card(card["id"])
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
        store.adjust_qty(
            item_id, qty.strip() or "0", f"merged photo capture {card['photo_hash'][:12]}"
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        return rerender(f"Invalid quantity: {exc}")
    if row["photo_hash"] is None:
        store.update_item(item_id, photo_hash=card["photo_hash"])
    ingest.delete_card(card["id"])
    return _next_card()


@app.post("/queue/{photo_hash}/skip", response_class=HTMLResponse)
def skip_card(photo_hash: str) -> str:
    """Dismiss one card — sibling detections from the same photo stay."""
    ingest.delete_card(photo_hash)
    return _next_card()
