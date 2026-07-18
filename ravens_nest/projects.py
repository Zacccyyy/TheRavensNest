"""Project and BOM routes: import, matching resolution, reservations,
builds, and the reorder basket.

Importing a BOM creates reservations, not consumption — building is the
explicit act that consumes stock, and it is rejected up front with an
exact shortage list when free stock can't cover it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import bom, db, store, ui_projects

log = logging.getLogger(__name__)

router = APIRouter()


def _project_or_404(project_id: str) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    return dict(row)


def _project_view(project_id: str, error: str | None = None, notice: str | None = None) -> str:
    project = _project_or_404(project_id)
    conn = db.connect()
    try:
        cost_rows, total_cost, unpriced = bom.bom_cost_rows(conn, project_id)
        context = bom.load_match_context(conn)
        unresolved = [
            {**line, "candidates": bom.fuzzy_candidates(line, context)}
            for line in cost_rows
            if not line["item_id"]
        ]
        reservations = bom.active_reservations(conn, project_id)
        reserved_all = bom.reserved_by_item(conn)
        reservation_rows = []
        for res in reservations:
            item = conn.execute(
                "SELECT name, qty_on_hand, unit_type FROM items WHERE id = ?",
                (res["item_id"],),
            ).fetchone()
            if item is None:
                continue
            on_hand = db.parse_qty(item["qty_on_hand"])
            total_reserved = reserved_all.get(res["item_id"], db.parse_qty("0"))
            reservation_rows.append(
                {
                    "item_name": item["name"],
                    "unit_type": item["unit_type"],
                    "qty": res["qty"],
                    "on_hand": db.qty_str(on_hand),
                    "free": db.qty_str(on_hand - total_reserved),
                    "shortfall": total_reserved > on_hand,
                }
            )
        history = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM builds WHERE project_id = ? ORDER BY ts DESC", (project_id,)
            )
        ]
        built = bom.net_builds(conn, project_id)
        all_items = sorted(context["items"], key=lambda i: i["name"].lower())
    finally:
        conn.close()
    return ui_projects.project_page(
        project,
        cost_rows,
        total_cost,
        unpriced,
        unresolved,
        all_items,
        reservation_rows,
        history,
        built,
        error=error,
        notice=notice,
    )


def _reserve_line(project_id: str, line: dict) -> None:
    store.create_reservation(project_id, line["item_id"], line["quantity"])


# ------------------------------------------------------------------- pages


@router.get("/projects", response_class=HTMLResponse)
def projects_list() -> str:
    conn = db.connect()
    try:
        projects = []
        for row in conn.execute("SELECT * FROM projects ORDER BY name"):
            lines = bom.matched_lines(conn, row["id"])
            projects.append(
                {
                    **dict(row),
                    "line_count": len(lines),
                    "matched_count": sum(1 for l in lines if l["item_id"]),
                    "reservation_count": len(bom.active_reservations(conn, row["id"])),
                    "built": bom.net_builds(conn, row["id"]),
                }
            )
    finally:
        conn.close()
    return ui_projects.projects_page(projects)


@router.post("/projects")
def create_project(name: str = Form(...), description: str = Form("")) -> RedirectResponse:
    if not name.strip():
        raise HTTPException(status_code=400, detail="project name is required")
    event = store.create_project(name.strip(), description.strip())
    return RedirectResponse(url=f"/projects/{event['payload']['id']}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: str) -> str:
    return _project_view(project_id)


# ------------------------------------------------------------------ import


@router.post("/projects/{project_id}/bom", response_class=HTMLResponse)
async def import_bom(project_id: str, bom_csv: UploadFile = File(...)) -> str:
    _project_or_404(project_id)
    text = (await bom_csv.read()).decode("utf-8-sig", errors="replace")
    lines, errors = bom.parse_bom_csv(text)
    if errors:
        return _project_view(project_id, error="BOM rejected: " + "; ".join(errors))

    # A re-import is a new revision: refresh this project's reservations.
    conn = db.connect()
    try:
        old_reservations = bom.active_reservations(conn, project_id)
        context = bom.load_match_context(conn)
    finally:
        conn.close()
    for res in old_reservations:
        store.release_reservation(res["id"])

    store.import_bom(project_id, lines)
    auto_matched = 0
    for line in lines:
        match = bom.auto_match(line, context)
        if match:
            store.match_bom_line(
                project_id,
                line["line_no"],
                match["item_id"],
                match["method"],
                alias_text=match["alias_text"],
            )
            store.create_reservation(project_id, match["item_id"], line["quantity"])
            auto_matched += 1
    unresolved = len(lines) - auto_matched
    notice = f"Imported {len(lines)} line(s): {auto_matched} matched automatically"
    if unresolved:
        notice += f", {unresolved} need resolution below"
    return _project_view(project_id, notice=notice)


# ---------------------------------------------------------------- matching


def _line_or_404(project_id: str, line_no: int) -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM bom_lines WHERE project_id = ? AND line_no = ?",
            (project_id, line_no),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="BOM line not found")
    return dict(row)


@router.post("/projects/{project_id}/match")
def resolve_line(
    project_id: str,
    line_no: int = Form(...),
    item_id: str = Form(...),
    method: str = Form("manual"),
    score: float | None = Form(None),
) -> RedirectResponse:
    """User picked an item for an unmatched line. The BOM's part number is
    stored as an alias so the next revision matches automatically."""
    line = _line_or_404(project_id, line_no)
    store.match_bom_line(
        project_id,
        line_no,
        item_id,
        method if method in ("manual", "fuzzy") else "manual",
        score=score,
        alias_text=line["part_number"] or None,
    )
    _reserve_line(project_id, {**line, "item_id": item_id})
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/match-new")
def resolve_line_with_new_item(
    project_id: str,
    line_no: int = Form(...),
    name: str = Form(...),
) -> RedirectResponse:
    """User created a fresh item for an unmatched line, seeded from it."""
    line = _line_or_404(project_id, line_no)
    if not name.strip():
        raise HTTPException(status_code=400, detail="item name is required")
    event = store.create_item(
        name.strip(),
        line["unit"],
        part_number=line["part_number"] or None,
        description=line["description"],
    )
    item_id = event["payload"]["id"]
    store.match_bom_line(
        project_id, line_no, item_id, "created", alias_text=line["part_number"] or None
    )
    _reserve_line(project_id, {**line, "item_id": item_id})
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


# ------------------------------------------------------------ reservations


@router.post("/projects/{project_id}/release")
def release_reservations(project_id: str) -> RedirectResponse:
    _project_or_404(project_id)
    conn = db.connect()
    try:
        reservations = bom.active_reservations(conn, project_id)
    finally:
        conn.close()
    for res in reservations:
        store.release_reservation(res["id"])
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


# ------------------------------------------------------------------- build


@router.post("/projects/{project_id}/build", response_class=HTMLResponse)
def build(project_id: str, count: int = Form(...)):
    _project_or_404(project_id)
    ok, message = bom.attempt_build(project_id, count)
    if not ok:
        return _project_view(project_id, error=message)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/unbuild", response_class=HTMLResponse)
def unbuild(project_id: str, count: int = Form(...)):
    """Reverse N builds, returning stock per the current BOM revision.
    (Prototype gets scrapped, servos come back.)"""
    _project_or_404(project_id)
    if count < 1:
        return _project_view(project_id, error="Un-build count must be at least 1.")
    conn = db.connect()
    try:
        built = bom.net_builds(conn, project_id)
        if count > built:
            return _project_view(
                project_id,
                error=f"Cannot un-build ×{count}: only {built} net build(s) recorded.",
            )
        needs = {
            item_id: per_build * count
            for item_id, per_build in bom.per_build_needs(
                bom.matched_lines(conn, project_id)
            ).items()
        }
    finally:
        conn.close()
    returned = [
        {"item_id": item_id, "qty": db.qty_str(qty)} for item_id, qty in needs.items()
    ]
    store.reverse_build(project_id, count, returned)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


# The reorder basket route lives in sourcing.py (it grew supplier links,
# pricing, and candidate baskets).
