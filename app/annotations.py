"""
Annotation routes — CRUD for bounding boxes with atomic last-write-wins save.
Stats helpers are now project-scoped.
"""
import json
from datetime import datetime, timezone
from typing import List

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.database import get_db, new_id, now_iso
from app.models import AnnotationOut, ImageAnnotationsResponse, SaveAnnotationsRequest
from app.socket_manager import emit_annotation_saved, emit_state_change, emit_stats

router = APIRouter(tags=["annotations"])


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _get_stats_dict(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute(
        "SELECT state, COUNT(*) AS cnt FROM images WHERE project_id=? GROUP BY state",
        (project_id,),
    )
    rows = await cur.fetchall()
    counts = {r["state"]: r["cnt"] for r in rows}
    total_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM images WHERE project_id=?", (project_id,)
    )
    total_row = await total_cur.fetchone()
    return {
        "total":            total_row["cnt"],
        "unassigned":       counts.get("unassigned", 0),
        "assigned":         counts.get("assigned", 0),
        "unannotated":      counts.get("unannotated", 0),
        "annotated":        counts.get("annotated", 0),
        "ready_for_export": counts.get("ready_for_export", 0),
        "exported":         counts.get("exported", 0),
    }


# ── GET annotations ───────────────────────────────────────────────────────────

@router.get("/annotations/{image_id}", response_model=ImageAnnotationsResponse)
async def get_annotations(
    image_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    cur = await db.execute("SELECT * FROM images WHERE id=?", (image_id,))
    img = await cur.fetchone()
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")

    project_id = img["project_id"]

    # Non-admins can only see their own images
    if not user["is_admin"] and img["assigned_to"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your image")

    # Mark as unannotated on first open (if still in assigned or unassigned state)
    state = img["state"]
    if img["state"] in ("assigned", "unassigned"):
        state = "unannotated"
        assigned_user = img["assigned_to"] or user["id"]
        await db.execute(
            "UPDATE images SET state='unannotated', opened_at=?, assigned_to=? WHERE id=?",
            (now_iso(), assigned_user, image_id),
        )
        await db.commit()
        await emit_state_change(image_id, "unannotated", assigned_user, project_id)
        stats = await _get_stats_dict(project_id, db)
        await emit_stats(stats, project_id)

    cur = await db.execute(
        "SELECT * FROM annotations WHERE image_id=? ORDER BY rowid",
        (image_id,),
    )
    ann_rows = await cur.fetchall()
    annotations = [AnnotationOut(**dict(r)) for r in ann_rows]

    return ImageAnnotationsResponse(
        image_id=image_id,
        filename=img["filename"],
        width=img["width"],
        height=img["height"],
        state=state,
        annotations=annotations,
    )


# ── SAVE annotations (last-write-wins, atomic) ────────────────────────────────

@router.put("/annotations/{image_id}", response_model=ImageAnnotationsResponse)
async def save_annotations(
    image_id: str,
    body: SaveAnnotationsRequest,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    cur = await db.execute("SELECT * FROM images WHERE id=?", (image_id,))
    img = await cur.fetchone()
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")

    project_id = img["project_id"]

    # Non-admins can only annotate their own images
    if not user["is_admin"] and img["assigned_to"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your image")

    # Exported images cannot be re-annotated (protect the final dataset)
    if img["state"] == "exported":
        raise HTTPException(status_code=400, detail="Cannot modify exported image")

    ts = now_iso()

    # Atomic replace: DELETE old + INSERT new in one transaction
    await db.execute("DELETE FROM annotations WHERE image_id=?", (image_id,))

    for ann in body.annotations:
        await db.execute(
            "INSERT INTO annotations (id, image_id, class_id, class_name, x, y, w, h, created_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id(), image_id, ann.class_id, ann.class_name,
             ann.x, ann.y, ann.w, ann.h, user["id"], ts),
        )

    # Update image state: unannotated/unassigned → annotated (only upgrade, never downgrade)
    new_state = img["state"]
    assigned_user = img["assigned_to"] or user["id"]
    if img["state"] in ("unannotated", "assigned", "unassigned"):
        new_state = "annotated"
        await db.execute(
            "UPDATE images SET state='annotated', annotated_at=?, assigned_to=? WHERE id=?",
            (ts, assigned_user, image_id),
        )
    elif img["state"] == "ready_for_export":
        # Re-saving an already-submitted image: revert to annotated
        new_state = "annotated"
        await db.execute(
            "UPDATE images SET state='annotated', submitted_at=NULL WHERE id=?",
            (image_id,),
        )
    elif img["state"] == "annotated":
        # Update timestamp only
        await db.execute(
            "UPDATE images SET annotated_at=? WHERE id=?",
            (ts, image_id),
        )

    await db.commit()

    # Real-time notifications
    await emit_annotation_saved(image_id, len(body.annotations), user["username"])
    if new_state != img["state"]:
        await emit_state_change(image_id, new_state, img["assigned_to"], project_id)
        stats = await _get_stats_dict(project_id, db)
        await emit_stats(stats, project_id)

    # Return fresh data
    cur = await db.execute("SELECT * FROM annotations WHERE image_id=? ORDER BY rowid", (image_id,))
    ann_rows = await cur.fetchall()
    return ImageAnnotationsResponse(
        image_id=image_id,
        filename=img["filename"],
        width=img["width"],
        height=img["height"],
        state=new_state,
        annotations=[AnnotationOut(**dict(r)) for r in ann_rows],
    )
