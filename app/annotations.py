"""
Annotation routes — CRUD for bounding boxes with an atomic, versioned save.

write_image() is the single place that persists a frame's boxes; the PUT route, the bulk
sequence tools (app/sequence.py) and model pre-labelling (app/assist.py) all go through
it so class mapping, clipping and state transitions are identical everywhere. Every write
bumps images.version; a PUT that carries an older base_version is rejected with 409, so
two people editing the same frame can't silently overwrite each other.
"""
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.common import get_project_or_404, project_stats, resolve_class
from app.database import get_db, new_id, now_iso
from app.models import AnnotationOut, ImageAnnotationsResponse, SaveAnnotationsRequest
from app.seq_utils import clip_box
from app.socket_manager import emit_annotation_saved, emit_state_change, emit_stats

router = APIRouter(tags=["annotations"])


# ── Shared writer ─────────────────────────────────────────────────────────────

def normalise_boxes(anns: list[dict], classes: list[str], img_w: int, img_h: int) -> tuple[list[dict], int]:
    """
    Resolve classes by name, clip to the image and drop degenerate boxes.
    Returns (clean_boxes, dropped_count). Orphan classes (removed from the project)
    are kept with class_id -1 so a re-save never silently deletes them.
    """
    clean, dropped = [], 0
    for a in anns:
        try:
            name, cid = resolve_class(a.get("class_name"), a.get("class_id"), classes)
        except ValueError:
            dropped += 1
            continue
        clipped = clip_box(a["x"], a["y"], a["w"], a["h"], img_w, img_h)
        if clipped is None:
            dropped += 1
            continue
        x, y, w, h = clipped
        clean.append({
            "class_id": cid, "class_name": name, "x": x, "y": y, "w": w, "h": h,
            "source": a.get("source") or "manual", "track_id": a.get("track_id"),
            "confidence": a.get("confidence"),
        })
    return clean, dropped


async def write_image(
    db: aiosqlite.Connection,
    img: dict,
    boxes: list[dict],
    user: dict,
    ts: str,
    is_background: bool = False,
    keep_state: bool = False,
) -> str:
    """
    Replace all boxes of one image (caller commits). `boxes` must already be normalised.
    keep_state=True writes suggestions (model pre-labels) without marking the frame done.
    Returns the new image state.
    """
    image_id = img["id"]
    await db.execute("DELETE FROM annotations WHERE image_id=?", (image_id,))
    if boxes:
        await db.executemany(
            "INSERT INTO annotations (id, image_id, class_id, class_name, x, y, w, h, "
            "created_by, updated_at, source, track_id, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (new_id(), image_id, b["class_id"], b["class_name"], b["x"], b["y"], b["w"], b["h"],
                 user["id"], ts, b.get("source") or "manual", b.get("track_id"), b.get("confidence"))
                for b in boxes
            ],
        )

    background = 1 if (is_background and not boxes) else 0
    state = img["state"]
    if keep_state:
        new_state = state
        await db.execute("UPDATE images SET is_background=?, version=version+1 WHERE id=?", (background, image_id))
    elif state in ("unannotated", "assigned", "unassigned"):
        new_state = "annotated"
        await db.execute(
            "UPDATE images SET state='annotated', annotated_at=?, assigned_to=?, is_background=?, "
            "version=version+1 WHERE id=?",
            (ts, img["assigned_to"] or user["id"], background, image_id),
        )
    else:
        # Editing a submitted / exported frame sends it back for (re-)submission and review.
        # A rejection stays visible until the frame is submitted again.
        new_state = "annotated"
        await db.execute(
            "UPDATE images SET state='annotated', submitted_at=NULL, annotated_at=?, is_background=?, "
            "version=version+1, review_status=CASE WHEN review_status='rejected' THEN 'rejected' END "
            "WHERE id=?",
            (ts, background, image_id),
        )
    return new_state


async def fetch_annotations(db: aiosqlite.Connection, image_id: str) -> list[AnnotationOut]:
    cur = await db.execute("SELECT * FROM annotations WHERE image_id=? ORDER BY rowid", (image_id,))
    return [AnnotationOut(**dict(r)) for r in await cur.fetchall()]


def can_edit(user: dict, img) -> bool:
    return bool(user["is_admin"]) or img["assigned_to"] == user["id"]


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
    if not can_edit(user, img):
        raise HTTPException(status_code=403, detail="Not your image")

    # Viewing never changes a frame's state (skimming through frames must not "claim" them);
    # the first save does.

    # Same lookup as the file endpoint: stored path, then the project's source folder
    missing = not Path(img["filepath"]).exists()
    if missing:
        cur = await db.execute("SELECT source_folder FROM project WHERE id=?", (project_id,))
        proj = await cur.fetchone()
        missing = not (proj and proj["source_folder"] and (Path(proj["source_folder"]) / img["filename"]).exists())
    reviewer = None
    if img["reviewed_by"]:
        cur = await db.execute("SELECT username FROM users WHERE id=?", (img["reviewed_by"],))
        r = await cur.fetchone()
        reviewer = r["username"] if r else None
    return ImageAnnotationsResponse(
        image_id=image_id,
        filename=img["filename"],
        width=img["width"],
        height=img["height"],
        state=img["state"],
        is_background=bool(img["is_background"]),
        file_missing=missing,
        filepath=img["filepath"] if user["is_admin"] else None,
        version=img["version"],
        review_status=img["review_status"],
        review_note=img["review_note"],
        reviewed_by=reviewer,
        annotations=await fetch_annotations(db, image_id),
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
    img = dict(img)
    project_id = img["project_id"]

    if not can_edit(user, img):
        raise HTTPException(status_code=403, detail="Not your image")
    if body.base_version is not None and body.base_version != img["version"]:
        raise HTTPException(status_code=409, detail="This frame was changed by someone else since you opened it")

    project = await get_project_or_404(project_id, db)
    boxes, dropped = normalise_boxes(
        [a.model_dump() for a in body.annotations], project["classes"], img["width"], img["height"]
    )

    ts = now_iso()
    try:
        new_state = await write_image(db, img, boxes, user, ts, body.is_background)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Real-time notifications
    await emit_annotation_saved(image_id, len(boxes), user["username"], project_id)
    if new_state != img["state"]:
        await emit_state_change(image_id, new_state, img["assigned_to"] or user["id"], project_id)
        await emit_stats(await project_stats(project_id, db), project_id)

    return ImageAnnotationsResponse(
        image_id=image_id,
        filename=img["filename"],
        width=img["width"],
        height=img["height"],
        state=new_state,
        is_background=bool(body.is_background and not boxes),
        dropped=dropped,
        version=img["version"] + 1,
        review_status="rejected" if img["review_status"] == "rejected" else None,
        review_note=img["review_note"],
        annotations=await fetch_annotations(db, image_id),
    )
