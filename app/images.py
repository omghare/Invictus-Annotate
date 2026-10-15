"""
Image routes — project-scoped listing, assignment, deletion, file serving.
All image operations require a project_id and support pagination + state filtering.
"""
import asyncio
import math
import mimetypes
import uuid
from pathlib import Path
from typing import List, Literal, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import get_admin_user, get_current_user, get_user_header_or_query
from app.common import (
    chunks, get_accessible_project, get_project_or_404, has_project_access, placeholders,
    project_stats, user_stats,
)
from app.config import THUMB_SIZES, THUMBS_DIR
from app.database import get_db, now_iso
from app.models import (
    AssignRequest, AutoAssignRequest, ImageOut, PaginatedImages,
    RemoveUserFromProjectRequest, UnassignRequest,
)
from app.socket_manager import (
    emit_image_deleted, emit_images_deleted, emit_state_change, emit_state_change_bulk, emit_stats,
)

router = APIRouter(tags=["images"])

# Per-row subqueries use idx_ann_image, so the cost follows the rows returned, not the
# size of the whole annotations table
_BOX_COUNT = "(SELECT COUNT(*) FROM annotations a WHERE a.image_id = i.id)"
_MANUAL_COUNT = "(SELECT COUNT(*) FROM annotations a WHERE a.image_id = i.id AND a.source = 'manual')"
_ORDER = "ORDER BY COALESCE(i.seq, 0), i.filename"


def _row_to_image_out(row: aiosqlite.Row) -> ImageOut:
    d = dict(row)
    return ImageOut(
        id=d["id"],
        filename=d["filename"],
        width=d["width"],
        height=d["height"],
        state=d["state"],
        assigned_to=d.get("assigned_to"),
        assigned_username=d.get("username"),
        seq=d.get("seq"),
        seq_group=d.get("seq_group"),
        box_count=d.get("box_count") or 0,
        is_background=bool(d.get("is_background")),
        review_status=d.get("review_status"),
    )


def remove_thumbnails(image_ids: list[str]) -> None:
    for iid in image_ids:
        for size in THUMB_SIZES:
            (THUMBS_DIR / f"{iid}_{size}.webp").unlink(missing_ok=True)


# ── List images (paginated + filtered) ───────────────────────────────────────

VALID_STATES = {"unassigned", "assigned", "unannotated", "annotated", "ready_for_export", "exported"}
VALID_PER_PAGE = {50, 100, 200}


def _scope(project_id: str, user: dict, state: Optional[str], assigned_to: Optional[str],
           mine: bool = False) -> tuple[str, list]:
    params: list = [project_id]
    wheres = ["i.project_id = ?"]
    if not user["is_admin"] or mine:
        wheres.append("i.assigned_to = ?")
        params.append(user["id"])
    elif assigned_to:
        wheres.append("i.assigned_to = ?")
        params.append(assigned_to)
    # Review pseudo-states (not stored in images.state)
    review_filters = {
        "rejected": "i.review_status = 'rejected'",
        "approved": "i.review_status = 'approved' AND i.state IN ('ready_for_export', 'exported')",
        "awaiting_review": "i.review_status IS NULL AND i.state = 'ready_for_export'",
    }
    if state in review_filters:
        wheres.append(review_filters[state])
        state = None
    if state:
        states = [s for s in state.split(",") if s]
        bad = [s for s in states if s not in VALID_STATES]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid state. Valid: {', '.join(sorted(VALID_STATES))}")
        wheres.append(f"i.state IN ({placeholders(len(states))})")
        params.extend(states)
    return "WHERE " + " AND ".join(wheres), params


@router.get("/projects/{project_id}/images", response_model=PaginatedImages)
async def list_images(
    project_id: str,
    state: Optional[str] = Query(None, description="One state or a comma-separated list"),
    assigned_to: Optional[str] = Query(None),
    mine: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Admin: sees all images (filterable by state/assigned_to). Annotator: only their own.
    per_page must be one of 50, 100, 200.
    """
    await get_accessible_project(project_id, user, db)
    if per_page not in VALID_PER_PAGE:
        per_page = 50

    where_clause, params = _scope(project_id, user, state, assigned_to, mine)
    count_cur = await db.execute(f"SELECT COUNT(*) AS cnt FROM images i {where_clause}", params)
    total = (await count_cur.fetchone())["cnt"]

    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    cur = await db.execute(
        f"""
        SELECT i.*, u.username, {_BOX_COUNT} AS box_count
        FROM images i
        LEFT JOIN users u ON u.id = i.assigned_to
        {where_clause}
        {_ORDER}
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    )
    items = [_row_to_image_out(r) for r in await cur.fetchall()]
    return PaginatedImages(items=items, total=total, page=page, per_page=per_page, total_pages=total_pages)


@router.get("/projects/{project_id}/images/index")
async def image_index(
    project_id: str,
    state: Optional[str] = Query(None),
    mine: bool = Query(False),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Lightweight, unpaginated list of every image in scope (annotate filmstrip, range
    selection). Includes is_keyframe = has at least one manually drawn box.
    """
    await get_accessible_project(project_id, user, db)
    where_clause, params = _scope(project_id, user, state, None, mine)
    cur = await db.execute(
        f"""
        SELECT i.id, i.filename, i.width, i.height, i.state, i.seq, i.seq_group,
               i.assigned_to, i.is_background, i.review_status,
               {_BOX_COUNT} AS box_count, {_MANUAL_COUNT} AS manual,
               (SELECT COUNT(*) FROM annotations a WHERE a.image_id = i.id AND a.source = 'model') AS model_boxes
        FROM images i
        {where_clause}
        {_ORDER}
        """,
        params,
    )
    items = []
    for r in await cur.fetchall():
        d = dict(r)
        d["is_keyframe"] = (d.pop("manual") or 0) > 0
        d["is_background"] = bool(d["is_background"])
        items.append(d)
    return {"total": len(items), "items": items}


# ── Project stats ─────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/images/stats")
async def image_stats(
    project_id: str,
    user_id: Optional[str] = Query(None),
    mine: bool = Query(False),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    await get_accessible_project(project_id, user, db)
    if not user["is_admin"] or mine:
        target_user_id = user["id"]
    else:
        target_user_id = user_id
    if target_user_id:
        return await user_stats(project_id, target_user_id, db)
    return await project_stats(project_id, db)


# ── Assign images ─────────────────────────────────────────────────────────────

async def _assign(db, project_id: str, assignments: dict[str, list[str]], ts: str) -> list[dict]:
    """assignments: user_id → image ids. Only unassigned images change. Returns the changes."""
    changes = []
    for uid, ids in assignments.items():
        for batch in chunks(ids):
            cur = await db.execute(
                f"UPDATE images SET state='assigned', assigned_to=?, assigned_at=? "
                f"WHERE project_id=? AND state='unassigned' AND id IN ({placeholders(len(batch))}) "
                f"RETURNING id",
                (uid, ts, project_id, *batch),
            )
            changes.extend(
                {"image_id": r["id"], "new_state": "assigned", "assigned_to": uid}
                for r in await cur.fetchall()
            )
    return changes


@router.post("/projects/{project_id}/images/assign")
async def assign_images(
    project_id: str,
    body: AssignRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await get_project_or_404(project_id, db)
    cur = await db.execute("SELECT username FROM users WHERE id = ? AND is_active = 1", (body.user_id,))
    target = await cur.fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    changes = await _assign(db, project_id, {body.user_id: body.image_ids}, now_iso())
    await db.commit()

    await emit_state_change_bulk(changes, project_id, f"Assigned {len(changes)} images to {target['username']}")
    await emit_stats(await project_stats(project_id, db), project_id)
    skipped = len(body.image_ids) - len(changes)
    detail = f"{len(changes)} images assigned"
    if skipped:
        detail += f" ({skipped} were not unassigned and were skipped)"
    return {"detail": detail, "assigned": len(changes), "skipped": skipped}


@router.post("/projects/{project_id}/images/auto-assign")
async def auto_assign(
    project_id: str,
    body: AutoAssignRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """
    Distribute unassigned images among users.
    contiguous (default): each user gets one consecutive run of frames, so sequence
    tools (carry-over, propagate, interpolate) work within their share.
    round_robin: image i goes to user i % n.
    """
    await get_project_or_404(project_id, db)
    user_ids = list(dict.fromkeys(body.user_ids))
    if not user_ids:
        raise HTTPException(status_code=400, detail="No users provided")
    cur = await db.execute(
        f"SELECT id FROM users WHERE is_active = 1 AND id IN ({placeholders(len(user_ids))})", user_ids
    )
    if len(await cur.fetchall()) != len(user_ids):
        raise HTTPException(status_code=404, detail="One or more users not found")

    params: list = [project_id]
    extra = ""
    if body.image_ids:
        extra = f" AND id IN ({placeholders(len(body.image_ids))})"
        params.extend(body.image_ids)
    cur = await db.execute(
        f"SELECT id FROM images WHERE project_id=? AND state='unassigned'{extra} "
        f"ORDER BY COALESCE(seq, 0), filename",
        params,
    )
    to_assign = [r["id"] for r in await cur.fetchall()]
    if not to_assign:
        detail = "None of the selected images are unassigned" if body.image_ids else "No unassigned images in this project"
        raise HTTPException(status_code=400, detail=detail)

    k = len(user_ids)
    assignments: dict[str, list[str]] = {uid: [] for uid in user_ids}
    if body.strategy == "round_robin":
        for i, iid in enumerate(to_assign):
            assignments[user_ids[i % k]].append(iid)
    else:
        n, start = len(to_assign), 0
        for i, uid in enumerate(user_ids):
            size = n // k + (1 if i < n % k else 0)
            assignments[uid] = to_assign[start:start + size]
            start += size

    changes = await _assign(db, project_id, assignments, now_iso())
    await db.commit()
    await emit_state_change_bulk(changes, project_id, f"Distributed {len(changes)} images among {k} users")
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"detail": f"{len(changes)} images distributed among {k} users", "assigned": len(changes)}


@router.get("/projects/{project_id}/user-workloads")
async def get_user_workloads(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """Image state counts per user in the project, plus review outcomes."""
    await get_project_or_404(project_id, db)
    cur = await db.execute(
        """
        SELECT assigned_to, state, COUNT(*) AS cnt,
               SUM(review_status = 'approved') AS approved, SUM(review_status = 'rejected') AS rejected
        FROM images
        WHERE project_id = ? AND assigned_to IS NOT NULL
        GROUP BY assigned_to, state
        """,
        (project_id,),
    )
    workloads: dict[str, dict] = {}
    for r in await cur.fetchall():
        w = workloads.setdefault(r["assigned_to"], {"assigned": 0, "annotated": 0, "ready": 0, "exported": 0, "total": 0,
                                                    "approved": 0, "rejected": 0})
        w["approved"] += r["approved"] or 0
        w["rejected"] += r["rejected"] or 0
        if r["state"] in ("assigned", "unannotated"):
            w["assigned"] += r["cnt"]
        elif r["state"] == "annotated":
            w["annotated"] += r["cnt"]
        elif r["state"] == "ready_for_export":
            w["ready"] += r["cnt"]
        elif r["state"] == "exported":
            w["exported"] += r["cnt"]
        w["total"] += r["cnt"]
    return workloads


@router.post("/projects/{project_id}/users/{user_id}/remove")
async def remove_user_from_project(
    project_id: str,
    user_id: str,
    body: RemoveUserFromProjectRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """
    Remove all image assignments for a user in a project.
    keep_annotated=False: discard their boxes on annotated/ready/exported frames and return
                          those frames to the unassigned pool (the images stay in the project).
    keep_annotated=True : keep annotated images (assignee cleared); revert the rest.
    """
    await get_project_or_404(project_id, db)
    cur = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="User not found")

    cur = await db.execute(
        "SELECT id, state FROM images WHERE project_id = ? AND assigned_to = ?", (project_id, user_id)
    )
    user_images = [dict(r) for r in await cur.fetchall()]
    done_states = ("annotated", "ready_for_export", "exported")
    annotated = [i for i in user_images if i["state"] in done_states]
    discarded_ids = [] if body.keep_annotated else [i["id"] for i in annotated]
    kept = annotated if body.keep_annotated else []
    reverted_ids = [i["id"] for i in user_images if i["state"] not in done_states]

    for batch in chunks(discarded_ids):
        ph = placeholders(len(batch))
        await db.execute(f"DELETE FROM annotations WHERE image_id IN ({ph})", batch)
        await db.execute(
            f"UPDATE images SET state='unassigned', assigned_to=NULL, assigned_at=NULL, is_background=0, "
            f"submitted_at=NULL, review_status=NULL, review_note=NULL, version=version+1 WHERE id IN ({ph})", batch
        )
    for batch in chunks([i["id"] for i in kept]):
        await db.execute(
            f"UPDATE images SET assigned_to = NULL, assigned_at = NULL WHERE id IN ({placeholders(len(batch))})", batch
        )
    for batch in chunks(reverted_ids):
        await db.execute(
            f"UPDATE images SET state = 'unassigned', assigned_to = NULL, assigned_at = NULL "
            f"WHERE id IN ({placeholders(len(batch))})", batch
        )
    await db.commit()

    changes = [{"image_id": i["id"], "new_state": i["state"], "assigned_to": None} for i in kept]
    changes += [{"image_id": iid, "new_state": "unassigned", "assigned_to": None} for iid in reverted_ids + discarded_ids]
    await emit_state_change_bulk(changes, project_id, "User removed from project")
    await emit_stats(await project_stats(project_id, db), project_id)

    msg = "Removed user from project."
    if discarded_ids:
        msg += f" Discarded their boxes on {len(discarded_ids)} frames (now unassigned)."
    if reverted_ids:
        msg += f" Reverted {len(reverted_ids)} unannotated images to unassigned."
    if kept:
        msg += f" Kept {len(kept)} annotated images in project (unassigned)."
    return {"detail": msg}


@router.post("/projects/{project_id}/images/unassign")
async def unassign_images(
    project_id: str,
    body: UnassignRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """Unassign specific images that have not been annotated yet."""
    await get_project_or_404(project_id, db)
    changed: list[str] = []
    for batch in chunks(body.image_ids):
        cur = await db.execute(
            f"UPDATE images SET state='unassigned', assigned_to=NULL, assigned_at=NULL "
            f"WHERE project_id=? AND state IN ('assigned','unannotated') "
            f"AND id IN ({placeholders(len(batch))}) RETURNING id",
            (project_id, *batch),
        )
        changed.extend(r["id"] for r in await cur.fetchall())
    await db.commit()
    await emit_state_change_bulk(
        [{"image_id": iid, "new_state": "unassigned", "assigned_to": None} for iid in changed],
        project_id, f"Unassigned {len(changed)} images",
    )
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"detail": f"{len(changed)} images unassigned", "unassigned": len(changed)}


# ── Submit images ─────────────────────────────────────────────────────────────

# An image can be submitted if it has at least one box or is marked as background
_SUBMITTABLE = (
    "(images.is_background = 1 OR EXISTS (SELECT 1 FROM annotations a WHERE a.image_id = images.id))"
)


@router.post("/projects/{project_id}/images/submit-all-annotated")
async def submit_all_annotated(
    project_id: str,
    mine: bool = Query(False, description="Admins: only submit your own images"),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Move every annotated image (with boxes, or marked background) to ready_for_export."""
    await get_project_or_404(project_id, db)
    params: list = [now_iso(), project_id]
    scope = ""
    if not user["is_admin"] or mine:
        scope = " AND images.assigned_to = ?"
        params.append(user["id"])

    cur = await db.execute(
        f"UPDATE images SET state='ready_for_export', submitted_at=?, review_status=NULL "
        f"WHERE images.project_id=? AND images.state='annotated'{scope} AND {_SUBMITTABLE} "
        f"RETURNING id, assigned_to",
        params,
    )
    rows = await cur.fetchall()
    await db.commit()

    # Annotated images that could not be submitted (no boxes, not marked background)
    cur = await db.execute(
        f"SELECT COUNT(*) AS cnt FROM images WHERE images.project_id=? AND images.state='annotated'{scope}",
        [project_id] + params[2:],
    )
    left_over = (await cur.fetchone())["cnt"]

    if rows:
        await emit_state_change_bulk(
            [{"image_id": r["id"], "new_state": "ready_for_export", "assigned_to": r["assigned_to"]} for r in rows],
            project_id, f"{user['username']} added {len(rows)} images to the dataset",
        )
        await emit_stats(await project_stats(project_id, db), project_id)

    detail = f"Added {len(rows)} images to the dataset"
    if left_over:
        detail += f". {left_over} annotated image(s) have no boxes — draw boxes or mark them as background."
    return {"detail": detail, "count": len(rows), "skipped_empty": left_over}


@router.post("/projects/{project_id}/images/{image_id}/submit")
async def submit_image(
    project_id: str,
    image_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    await get_project_or_404(project_id, db)
    cur = await db.execute("SELECT * FROM images WHERE id=? AND project_id=?", (image_id, project_id))
    img = await cur.fetchone()
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")
    if not user["is_admin"] and img["assigned_to"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your image")
    if img["state"] != "annotated":
        raise HTTPException(
            status_code=400, detail=f"Image must be in 'annotated' state (currently '{img['state']}')"
        )

    cur = await db.execute("SELECT COUNT(*) AS cnt FROM annotations WHERE image_id=?", (image_id,))
    if (await cur.fetchone())["cnt"] == 0 and not img["is_background"]:
        raise HTTPException(
            status_code=400, detail="This image has no boxes. Draw a box or mark it as background (no objects)."
        )

    await db.execute(
        "UPDATE images SET state='ready_for_export', submitted_at=?, review_status=NULL WHERE id=?",
        (now_iso(), image_id),
    )
    await db.commit()
    await emit_state_change(image_id, "ready_for_export", img["assigned_to"], project_id)
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"detail": "Image added to dataset queue"}


# ── Review (admin) ────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    image_ids: List[str] = Field(..., min_length=1, max_length=5000)
    action: Literal["approve", "reject"]
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/projects/{project_id}/images/review")
async def review_images(
    project_id: str,
    body: ReviewRequest,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    """
    approve: submitted frames are marked approved (export can be limited to these).
    reject : frames go back to the annotator ("annotated" again) with a note explaining what to fix.
    """
    await get_project_or_404(project_id, db)
    ts, note = now_iso(), (body.note or "").strip() or None
    rows: list = []
    for batch in chunks(body.image_ids):
        ph = placeholders(len(batch))
        if body.action == "approve":
            cur = await db.execute(
                f"UPDATE images SET review_status='approved', review_note=NULL, reviewed_by=?, reviewed_at=? "
                f"WHERE project_id=? AND state IN ('ready_for_export', 'exported') AND id IN ({ph}) "
                f"RETURNING id, state, assigned_to",
                (admin["id"], ts, project_id, *batch),
            )
        else:
            cur = await db.execute(
                f"UPDATE images SET review_status='rejected', review_note=?, reviewed_by=?, reviewed_at=?, "
                f"state='annotated', submitted_at=NULL "
                f"WHERE project_id=? AND state IN ('annotated', 'ready_for_export', 'exported') AND id IN ({ph}) "
                f"RETURNING id, state, assigned_to",
                (note, admin["id"], ts, project_id, *batch),
            )
        rows.extend(await cur.fetchall())
    await db.commit()
    if body.action == "reject":
        await emit_state_change_bulk(
            [{"image_id": r["id"], "new_state": "annotated", "assigned_to": r["assigned_to"]} for r in rows],
            project_id, f"{admin['username']} sent {len(rows)} frames back for fixes",
        )
        await emit_stats(await project_stats(project_id, db), project_id)
    verb = "Approved" if body.action == "approve" else "Sent back"
    return {"detail": f"{verb} {len(rows)} frames", "count": len(rows),
            "skipped": len(body.image_ids) - len(rows)}


# ── Delete images (admin only) ────────────────────────────────────────────────

async def _fix_thumbnail(db: aiosqlite.Connection, project_id: str) -> None:
    """Point the project thumbnail at an existing image (or NULL)."""
    await db.execute(
        """
        UPDATE project SET thumbnail_image_id = (
            SELECT id FROM images WHERE project_id = ? ORDER BY COALESCE(seq, 0), filename LIMIT 1
        )
        WHERE id = ? AND (thumbnail_image_id IS NULL OR thumbnail_image_id NOT IN
                          (SELECT id FROM images WHERE project_id = ?))
        """,
        (project_id, project_id, project_id),
    )


@router.delete("/projects/{project_id}/images/{image_id}")
async def delete_image(
    project_id: str,
    image_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await get_project_or_404(project_id, db)
    cur = await db.execute("SELECT id FROM images WHERE id=? AND project_id=?", (image_id, project_id))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Image not found")

    await db.execute("DELETE FROM annotations WHERE image_id=?", (image_id,))
    await db.execute("DELETE FROM annotation_batch_items WHERE image_id=?", (image_id,))
    await db.execute("DELETE FROM model_predictions WHERE image_id=?", (image_id,))
    await db.execute("DELETE FROM images WHERE id=?", (image_id,))
    await _fix_thumbnail(db, project_id)
    await db.commit()

    remove_thumbnails([image_id])
    await emit_image_deleted(image_id, project_id)
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"detail": "Image deleted"}


class BulkDeleteRequest(BaseModel):
    image_ids: List[str] = Field(..., min_length=1, max_length=20000)


@router.post("/projects/{project_id}/images/delete")
async def delete_images(
    project_id: str,
    body: BulkDeleteRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """Delete many images (and their boxes) in one transaction. Files on disk are kept."""
    await get_project_or_404(project_id, db)
    deleted: list[str] = []
    for batch in chunks(list(dict.fromkeys(body.image_ids))):
        ph = placeholders(len(batch))
        cur = await db.execute(f"SELECT id FROM images WHERE project_id=? AND id IN ({ph})", (project_id, *batch))
        ids = [r["id"] for r in await cur.fetchall()]
        if not ids:
            continue
        ph = placeholders(len(ids))
        await db.execute(f"DELETE FROM annotations WHERE image_id IN ({ph})", ids)
        await db.execute(f"DELETE FROM annotation_batch_items WHERE image_id IN ({ph})", ids)
        await db.execute(f"DELETE FROM model_predictions WHERE image_id IN ({ph})", ids)
        await db.execute(f"DELETE FROM images WHERE id IN ({ph})", ids)
        deleted.extend(ids)
    await _fix_thumbnail(db, project_id)
    await db.commit()
    await asyncio.to_thread(remove_thumbnails, deleted)
    await emit_images_deleted(deleted, project_id)
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"detail": f"Deleted {len(deleted)} images", "deleted": len(deleted)}


# ── Serve image file ──────────────────────────────────────────────────────────

async def _image_row_for(image_id: str, db: aiosqlite.Connection, user: Optional[dict] = None):
    cur = await db.execute("SELECT filepath, filename, project_id, assigned_to FROM images WHERE id=?", (image_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found")
    # Admins, the assignee, and anyone with work in the same project (home-page cover images)
    if user is not None and row["assigned_to"] != user["id"] and not await has_project_access(row["project_id"], user, db):
        raise HTTPException(status_code=404, detail="Image not found")
    return row


async def _resolve_image_path(image_id: str, db: aiosqlite.Connection, user: Optional[dict] = None) -> Path:
    row = await _image_row_for(image_id, db, user)

    fpath = Path(row["filepath"])
    if not fpath.exists():
        # Fallback: the project's source folder + filename (folder was moved)
        proj_cur = await db.execute("SELECT source_folder FROM project WHERE id=?", (row["project_id"],))
        proj_row = await proj_cur.fetchone()
        if proj_row and proj_row["source_folder"]:
            fallback = Path(proj_row["source_folder"]) / row["filename"]
            if fallback.exists():
                fpath = fallback
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Image file missing from disk")
    return fpath


@router.get("/images/{image_id}/file")
async def serve_image(
    image_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_user_header_or_query),
):
    """Serves the original image. Auth via the media cookie (for <img> tags) or Bearer header."""
    fpath = await _resolve_image_path(image_id, db, user)
    media_type = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
    return FileResponse(path=fpath, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})


def _make_thumbnail(src: Path, dst: Path, size: int) -> None:
    from PIL import Image as PILImage, ImageOps
    with PILImage.open(src) as im:
        im.draft("RGB", (size, size))          # fast JPEG downscale on decode
        im = ImageOps.exif_transpose(im)       # same orientation the browser shows
        im = im.convert("RGB")
        im.thumbnail((size, size))
        # Unique temp name: two requests for the same thumbnail must not write one file
        tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex[:8]}.tmp")
        im.save(tmp, "WEBP", quality=80, method=4)
        try:
            tmp.replace(dst)
        except OSError:                        # another request won the race (Windows locks open files)
            tmp.unlink(missing_ok=True)
            if not dst.exists():
                raise


@router.get("/images/{image_id}/thumb")
async def serve_thumbnail(
    image_id: str,
    size: int = Query(320),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_user_header_or_query),
):
    """Small cached WEBP preview for grids and the filmstrip (instead of full-size originals)."""
    size = min(THUMB_SIZES, key=lambda s: abs(s - size))
    thumb = THUMBS_DIR / f"{image_id}_{size}.webp"
    if thumb.exists():
        await _image_row_for(image_id, db, user)              # access check only
    else:
        src = await _resolve_image_path(image_id, db, user)
        try:
            await asyncio.to_thread(_make_thumbnail, src, thumb, size)
        except Exception:
            raise HTTPException(status_code=500, detail="Could not create thumbnail")
    return FileResponse(path=thumb, media_type="image/webp", headers={"Cache-Control": "private, max-age=604800"})
