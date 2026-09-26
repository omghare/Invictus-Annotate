"""
Image routes — project-scoped listing, assignment, deletion, file serving.
All image operations now require a project_id and support pagination + state filtering.
"""
import json
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from app.auth import get_admin_user, get_current_user, decode_token
from app.database import get_db, now_iso
from app.models import AssignRequest, AutoAssignRequest, ImageOut, ImageStats, PaginatedImages, RemoveUserFromProjectRequest
from app.socket_manager import emit_image_deleted, emit_state_change, emit_stats

router = APIRouter(tags=["images"])


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _get_project_or_404(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute("SELECT id FROM project WHERE id = ?", (project_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


async def _get_stats_dict(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute(
        "SELECT state, COUNT(*) AS cnt FROM images WHERE project_id = ? GROUP BY state",
        (project_id,),
    )
    rows = await cur.fetchall()
    counts = {r["state"]: r["cnt"] for r in rows}
    total_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM images WHERE project_id = ?", (project_id,)
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


async def _get_user_stats_dict(project_id: str, user_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute(
        "SELECT state, COUNT(*) AS cnt FROM images WHERE project_id = ? AND assigned_to = ? GROUP BY state",
        (project_id, user_id),
    )
    rows = await cur.fetchall()
    counts = {r["state"]: r["cnt"] for r in rows}

    total_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM images WHERE project_id = ? AND assigned_to = ?",
        (project_id, user_id),
    )
    total_row = await total_cur.fetchone()

    proj_total_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM images WHERE project_id = ?", (project_id,)
    )
    proj_total_row = await proj_total_cur.fetchone()

    return {
        "total":            proj_total_row["cnt"],
        "unassigned":       0,
        "assigned":         counts.get("assigned", 0),
        "unannotated":      counts.get("unannotated", 0),
        "annotated":        counts.get("annotated", 0),
        "ready_for_export": counts.get("ready_for_export", 0),
        "exported":         counts.get("exported", 0),
    }


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
    )


# ── List images (paginated + filtered) ───────────────────────────────────────

VALID_STATES = {"unassigned", "assigned", "unannotated", "annotated", "ready_for_export", "exported"}
VALID_PER_PAGE = {50, 100, 200}

@router.get("/projects/{project_id}/images", response_model=PaginatedImages)
async def list_images(
    project_id: str,
    state: Optional[str] = Query(None),
    assigned_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    List images in a project with optional state filtering and pagination.
    Admin: sees all images (filterable by state/assigned_to).
    Annotator: sees only their assigned images.
    per_page must be one of 50, 100, 200.
    """
    await _get_project_or_404(project_id, db)

    # Clamp per_page to valid options
    if per_page not in VALID_PER_PAGE:
        per_page = 50

    params = [project_id]
    wheres = ["i.project_id = ?"]

    if not user["is_admin"]:
        wheres.append("i.assigned_to = ?")
        params.append(user["id"])
    else:
        if assigned_to:
            wheres.append("i.assigned_to = ?")
            params.append(assigned_to)

    if state:
        if state not in VALID_STATES:
            raise HTTPException(status_code=400, detail=f"Invalid state. Valid: {', '.join(sorted(VALID_STATES))}")
        wheres.append("i.state = ?")
        params.append(state)

    where_clause = "WHERE " + " AND ".join(wheres)

    # Total count for pagination
    count_cur = await db.execute(
        f"SELECT COUNT(*) AS cnt FROM images i {where_clause}",
        params,
    )
    total = (await count_cur.fetchone())["cnt"]

    import math
    total_pages = max(1, math.ceil(total / per_page))
    # Clamp page to valid range
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    cur = await db.execute(
        f"""
        SELECT i.*, u.username
        FROM images i
        LEFT JOIN users u ON u.id = i.assigned_to
        {where_clause}
        ORDER BY i.filename
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    )
    rows = await cur.fetchall()
    items = [_row_to_image_out(r) for r in rows]

    return PaginatedImages(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


# ── Project stats ─────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/images/stats")
async def image_stats(
    project_id: str,
    user_id: Optional[str] = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    await _get_project_or_404(project_id, db)
    target_user_id = user["id"] if not user["is_admin"] else user_id

    if target_user_id:
        return await _get_user_stats_dict(project_id, target_user_id, db)
    else:
        return await _get_stats_dict(project_id, db)


# ── Assign images ─────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/images/assign")
async def assign_images(
    project_id: str,
    body: AssignRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await _get_project_or_404(project_id, db)

    cur = await db.execute("SELECT id FROM users WHERE id = ?", (body.user_id,))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="User not found")

    for iid in body.image_ids:
        await db.execute(
            "UPDATE images SET state='assigned', assigned_to=?, assigned_at=? "
            "WHERE id=? AND project_id=? AND state='unassigned'",
            (body.user_id, now_iso(), iid, project_id),
        )
        await emit_state_change(iid, "assigned", body.user_id, project_id)

    await db.commit()
    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)
    return {"detail": f"{len(body.image_ids)} images assigned"}


@router.post("/projects/{project_id}/images/auto-assign")
async def auto_assign(
    project_id: str,
    body: AutoAssignRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """Evenly distribute unassigned images among the given user list."""
    await _get_project_or_404(project_id, db)

    if not body.user_ids:
        raise HTTPException(status_code=400, detail="No users provided")

    if body.image_ids:
        # Distribute only the requested images (must be unassigned and in this project)
        placeholders = ",".join("?" for _ in body.image_ids)
        cur = await db.execute(
            f"SELECT id FROM images WHERE project_id=? AND state='unassigned' AND id IN ({placeholders}) ORDER BY filename",
            (project_id, *body.image_ids),
        )
        to_assign = [r["id"] for r in await cur.fetchall()]
        if not to_assign:
            raise HTTPException(status_code=400, detail="None of the selected images are unassigned")
    else:
        # Distribute all unassigned images
        cur = await db.execute(
            "SELECT id FROM images WHERE project_id=? AND state='unassigned' ORDER BY filename",
            (project_id,),
        )
        to_assign = [r["id"] for r in await cur.fetchall()]
        if not to_assign:
            raise HTTPException(status_code=400, detail="No unassigned images in this project")

    ts = now_iso()
    for i, iid in enumerate(to_assign):
        uid = body.user_ids[i % len(body.user_ids)]
        await db.execute(
            "UPDATE images SET state='assigned', assigned_to=?, assigned_at=? WHERE id=?",
            (uid, ts, iid),
        )
        await emit_state_change(iid, "assigned", uid, project_id)

    await db.commit()
    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)
    return {"detail": f"{len(to_assign)} images distributed among {len(body.user_ids)} users"}


@router.get("/projects/{project_id}/user-workloads")
async def get_user_workloads(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    """Retrieve precise image state counts per user in the project."""
    await _get_project_or_404(project_id, db)

    cur = await db.execute(
        """
        SELECT assigned_to, state, COUNT(*) AS cnt 
        FROM images 
        WHERE project_id = ? AND assigned_to IS NOT NULL
        GROUP BY assigned_to, state
        """,
        (project_id,),
    )
    rows = await cur.fetchall()

    workloads = {}
    for r in rows:
        uid = r["assigned_to"]
        state = r["state"]
        cnt = r["cnt"]
        if uid not in workloads:
            workloads[uid] = {
                "assigned": 0,
                "annotated": 0,
                "ready": 0,
                "exported": 0,
                "total": 0
            }

        # Mapping states to the client categories
        if state in ("assigned", "unannotated"):
            workloads[uid]["assigned"] += cnt
        elif state == "annotated":
            workloads[uid]["annotated"] += cnt
        elif state == "ready_for_export":
            workloads[uid]["ready"] += cnt
        elif state == "exported":
            workloads[uid]["exported"] += cnt

        # Total workload assigned to them
        workloads[uid]["total"] += cnt

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
    If keep_annotated is False:
       - Delete all annotated, ready, exported images assigned to this user from the project.
       - Revert all unannotated/assigned images back to unassigned state.
    If keep_annotated is True:
       - Keep annotated, ready, exported images in the project, but clear their assigned_to field.
       - Revert all unannotated/assigned images back to unassigned state.
    """
    await _get_project_or_404(project_id, db)

    # Check if user exists
    cur = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="User not found")

    # Find all images assigned to this user in this project
    cur = await db.execute(
        "SELECT id, state FROM images WHERE project_id = ? AND assigned_to = ?",
        (project_id, user_id)
    )
    user_images = await cur.fetchall()

    deleted_image_ids = []
    reverted_image_ids = []
    kept_annotated_image_ids = []

    for img in user_images:
        iid = img["id"]
        state = img["state"]
        is_annotated = state in ("annotated", "ready_for_export", "exported")

        if is_annotated:
            if body.keep_annotated:
                kept_annotated_image_ids.append(iid)
            else:
                deleted_image_ids.append(iid)
        else:
            reverted_image_ids.append(iid)

    # Apply deletions
    if deleted_image_ids:
        # Delete annotations first
        placeholders = ",".join("?" for _ in deleted_image_ids)
        await db.execute(
            f"DELETE FROM annotations WHERE image_id IN ({placeholders})",
            deleted_image_ids,
        )

        # If any of the deleted images was the project thumbnail, update the thumbnail
        cur_thumb = await db.execute(
            "SELECT thumbnail_image_id FROM project WHERE id = ?", (project_id,)
        )
        proj_row = await cur_thumb.fetchone()
        if proj_row and proj_row["thumbnail_image_id"] in deleted_image_ids:
            # Find next available image not being deleted
            next_cur = await db.execute(
                f"SELECT id FROM images WHERE project_id=? AND id NOT IN ({placeholders}) ORDER BY filename LIMIT 1",
                (project_id, *deleted_image_ids),
            )
            next_img = await next_cur.fetchone()
            new_thumb = next_img["id"] if next_img else None
            await db.execute(
                "UPDATE project SET thumbnail_image_id=? WHERE id=?",
                (new_thumb, project_id),
            )

        await db.execute(
            f"DELETE FROM images WHERE id IN ({placeholders})",
            deleted_image_ids,
        )
        for iid in deleted_image_ids:
            await emit_image_deleted(iid, project_id)

    # Apply kept annotated (update assigned_to=NULL)
    if kept_annotated_image_ids:
        placeholders = ",".join("?" for _ in kept_annotated_image_ids)
        await db.execute(
            f"UPDATE images SET assigned_to = NULL, assigned_at = NULL WHERE id IN ({placeholders})",
            kept_annotated_image_ids,
        )
        for iid in kept_annotated_image_ids:
            img_state = next(img["state"] for img in user_images if img["id"] == iid)
            await emit_state_change(iid, img_state, None, project_id)

    # Apply reverted unannotated (update state=unassigned, assigned_to=NULL)
    if reverted_image_ids:
        placeholders = ",".join("?" for _ in reverted_image_ids)
        await db.execute(
            f"UPDATE images SET state = 'unassigned', assigned_to = NULL, assigned_at = NULL WHERE id IN ({placeholders})",
            reverted_image_ids,
        )
        for iid in reverted_image_ids:
            await emit_state_change(iid, "unassigned", None, project_id)

    await db.commit()

    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)

    msg = f"Successfully removed user from project."
    if deleted_image_ids:
        msg += f" Deleted {len(deleted_image_ids)} annotated images."
    if reverted_image_ids:
        msg += f" Reverted {len(reverted_image_ids)} unannotated images to unassigned."
    if kept_annotated_image_ids:
        msg += f" Kept {len(kept_annotated_image_ids)} annotated images in project (unassigned)."

    return {"detail": msg}


@router.post("/projects/{project_id}/images/unassign")
async def unassign_images(
    project_id: str,
    body: dict,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """Unassign specific images (returns them to unassigned state)."""
    await _get_project_or_404(project_id, db)

    image_ids = body.get("image_ids", [])
    for iid in image_ids:
        await db.execute(
            "UPDATE images SET state='unassigned', assigned_to=NULL, assigned_at=NULL "
            "WHERE id=? AND project_id=? AND state IN ('assigned','unannotated')",
            (iid, project_id),
        )
        await emit_state_change(iid, "unassigned", None, project_id)
    await db.commit()
    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)
    return {"detail": f"{len(image_ids)} images unassigned"}


# ── Submit images ─────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/images/submit-all-annotated")
async def submit_all_annotated(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Submit all annotated images in this project to ready_for_export."""
    await _get_project_or_404(project_id, db)

    if user["is_admin"]:
        cur = await db.execute(
            "SELECT id, assigned_to FROM images WHERE project_id=? AND state='annotated'",
            (project_id,),
        )
    else:
        cur = await db.execute(
            "SELECT id, assigned_to FROM images WHERE project_id=? AND state='annotated' AND assigned_to=?",
            (project_id, user["id"]),
        )

    annotated_images = await cur.fetchall()
    if not annotated_images:
        return {"detail": "No annotated images to submit", "count": 0}

    submitted_count = 0
    ts = now_iso()
    for row in annotated_images:
        image_id = row["id"]
        ann_cur = await db.execute("SELECT COUNT(*) AS cnt FROM annotations WHERE image_id=?", (image_id,))
        cnt = (await ann_cur.fetchone())["cnt"]
        if cnt > 0:
            await db.execute(
                "UPDATE images SET state='ready_for_export', submitted_at=? WHERE id=?",
                (ts, image_id),
            )
            await emit_state_change(image_id, "ready_for_export", row["assigned_to"], project_id)
            submitted_count += 1

    if submitted_count > 0:
        await db.commit()
        stats = await _get_stats_dict(project_id, db)
        await emit_stats(stats, project_id)

    return {"detail": f"Added {submitted_count} images to dataset queue", "count": submitted_count}


@router.post("/projects/{project_id}/images/{image_id}/submit")
async def submit_image(
    project_id: str,
    image_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    await _get_project_or_404(project_id, db)

    cur = await db.execute("SELECT * FROM images WHERE id=? AND project_id=?", (image_id, project_id))
    img = await cur.fetchone()
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found in this project")

    if not user["is_admin"] and img["assigned_to"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your image")

    if img["state"] != "annotated":
        raise HTTPException(
            status_code=400,
            detail=f"Image must be in 'annotated' state (currently '{img['state']}')",
        )

    cur = await db.execute("SELECT COUNT(*) AS cnt FROM annotations WHERE image_id=?", (image_id,))
    cnt = (await cur.fetchone())["cnt"]
    if cnt == 0:
        raise HTTPException(status_code=400, detail="Cannot submit image with no annotations")

    await db.execute(
        "UPDATE images SET state='ready_for_export', submitted_at=? WHERE id=?",
        (now_iso(), image_id),
    )
    await db.commit()
    await emit_state_change(image_id, "ready_for_export", img["assigned_to"], project_id)
    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)
    return {"detail": "Image added to dataset queue"}


# ── Delete image (admin only) ─────────────────────────────────────────────────

@router.delete("/projects/{project_id}/images/{image_id}")
async def delete_image(
    project_id: str,
    image_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await _get_project_or_404(project_id, db)

    cur = await db.execute("SELECT id FROM images WHERE id=? AND project_id=?", (image_id, project_id))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Image not found in this project")

    # If this was the thumbnail, clear it
    cur2 = await db.execute(
        "SELECT thumbnail_image_id FROM project WHERE id=?", (project_id,)
    )
    proj_row = await cur2.fetchone()
    if proj_row and proj_row["thumbnail_image_id"] == image_id:
        # Find the next available image to use as thumbnail
        next_cur = await db.execute(
            "SELECT id FROM images WHERE project_id=? AND id != ? ORDER BY filename LIMIT 1",
            (project_id, image_id),
        )
        next_img = await next_cur.fetchone()
        new_thumb = next_img["id"] if next_img else None
        await db.execute(
            "UPDATE project SET thumbnail_image_id=? WHERE id=?", (new_thumb, project_id)
        )

    await db.execute("DELETE FROM annotations WHERE image_id=?", (image_id,))
    await db.execute("DELETE FROM images WHERE id=?", (image_id,))
    await db.commit()

    await emit_image_deleted(image_id, project_id)
    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)
    return {"detail": "Image deleted"}


# ── Serve image file ──────────────────────────────────────────────────────────

@router.get("/images/{image_id}/file")
async def serve_image(
    image_id: str,
    token: Optional[str] = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
):
    """
    Serves the raw JPG file.
    Auth via ?token= query param (needed for <img> tags that can't set headers).
    No project scoping needed — image_id is a globally unique UUID.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Token required")
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    cur = await db.execute("SELECT filepath, filename, project_id FROM images WHERE id=?", (image_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found")

    fpath = Path(row["filepath"])
    if not fpath.exists():
        # Fallback check: check project's source_folder + filename
        proj_cur = await db.execute("SELECT source_folder FROM project WHERE id=?", (row["project_id"],))
        proj_row = await proj_cur.fetchone()
        if proj_row and proj_row["source_folder"]:
            fallback = Path(proj_row["source_folder"]) / row["filename"]
            if fallback.exists():
                fpath = fallback

    if not fpath or not fpath.exists():
        raise HTTPException(status_code=404, detail="Image file missing from disk")

    return FileResponse(
        path=fpath,
        media_type="image/jpeg",
        headers={"Cache-Control": "max-age=86400, immutable"},
    )
