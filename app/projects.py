"""
Project routes — multi-project support.
All project endpoints are now scoped to /api/projects/{project_id}.
"""
import json
import os
import shutil
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import get_admin_user, get_current_user, hash_password
from app.database import get_db, new_id, now_iso
from app.models import (
    CreateProjectRequest, CreateUserRequest, ImportResult,
    ProjectListItem, ProjectOut, UpdateClassesRequest, UserOut,
    ProjectHistoryItem,
)
from app.socket_manager import (
    emit_global_projects_changed, emit_project_updated, emit_stats,
)
from app.config import DATASET_DIR

router = APIRouter(tags=["project"])


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _get_project_or_404(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute("SELECT * FROM project WHERE id = ?", (project_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    d = dict(row)
    d["classes"] = json.loads(d["classes"])
    return d


async def _project_stats(project_id: str, db: aiosqlite.Connection) -> dict:
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


async def _enrich_project(row: dict, db: aiosqlite.Connection) -> dict:
    """Add computed image-count stats to a project dict."""
    stats = await _project_stats(row["id"], db)
    row["image_count"]     = stats["total"]
    row["annotated_count"] = stats["annotated"] + stats["ready_for_export"] + stats["exported"]
    row["ready_count"]     = stats["ready_for_export"]
    row["exported_count"]  = stats["exported"]
    row["unassigned_count"]= stats["unassigned"]
    return row


# ── List all projects ─────────────────────────────────────────────────────────

@router.get("/projects", response_model=List[ProjectListItem])
async def list_projects(
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Admin: sees all projects.
    Annotator: sees only projects that have images assigned to them.
    """
    if user["is_admin"]:
        cur = await db.execute("SELECT * FROM project ORDER BY created_at DESC")
    else:
        # Projects where this user has at least one image assigned
        cur = await db.execute("""
            SELECT DISTINCT p.*
            FROM project p
            INNER JOIN images i ON i.project_id = p.id
            WHERE i.assigned_to = ?
            ORDER BY p.created_at DESC
        """, (user["id"],))

    rows = await cur.fetchall()
    if not rows:
        return []

    # Batch query image counts per project & state in ONE query instead of N*2 queries
    stats_cur = await db.execute("""
        SELECT project_id, state, COUNT(*) AS cnt
        FROM images
        GROUP BY project_id, state
    """)
    stats_rows = await stats_cur.fetchall()

    project_stats_map = {}
    for r in stats_rows:
        pid = r["project_id"]
        st = r["state"]
        cnt = r["cnt"]
        if pid not in project_stats_map:
            project_stats_map[pid] = {
                "total": 0, "unassigned": 0, "assigned": 0,
                "unannotated": 0, "annotated": 0, "ready_for_export": 0, "exported": 0
            }
        project_stats_map[pid][st] = cnt
        project_stats_map[pid]["total"] += cnt

    result = []
    for row in rows:
        d = dict(row)
        d["classes"] = json.loads(d["classes"])
        pid = d["id"]
        stats = project_stats_map.get(pid, {
            "total": 0, "unassigned": 0, "assigned": 0,
            "unannotated": 0, "annotated": 0, "ready_for_export": 0, "exported": 0
        })
        d["image_count"]      = stats["total"]
        d["annotated_count"]  = stats["annotated"] + stats["ready_for_export"] + stats["exported"]
        d["ready_count"]      = stats["ready_for_export"]
        d["exported_count"]   = stats["exported"]
        d["unassigned_count"] = stats["unassigned"]
        result.append(ProjectListItem(**d))
    return result


# ── Create project ────────────────────────────────────────────────────────────

@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    pid = new_id()
    created_at = now_iso()
    await db.execute(
        "INSERT INTO project (id, name, classes, created_at, created_by) VALUES (?,?,?,?,?)",
        (pid, body.name.strip(), json.dumps(body.classes), created_at, admin["id"]),
    )
    await db.execute(
        "INSERT INTO project_history (id, project_id, name, date_created, dataset_size, date_deleted) "
        "VALUES (?, ?, ?, ?, 0, NULL)",
        (new_id(), pid, body.name.strip(), created_at)
    )
    await db.commit()

    await emit_global_projects_changed()

    cur = await db.execute("SELECT * FROM project WHERE id = ?", (pid,))
    row = dict(await cur.fetchone())
    row["classes"] = json.loads(row["classes"])
    row = await _enrich_project(row, db)
    return ProjectOut(**row)


# ── Project History ──────────────────────────────────────────────────────────

@router.get("/projects/history", response_model=List[ProjectHistoryItem])
async def get_project_history(
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    query = """
        SELECT 
            h.project_id,
            h.name, 
            h.date_created, 
            h.date_deleted,
            CASE 
                WHEN h.date_deleted IS NOT NULL THEN h.dataset_size
                ELSE (SELECT COUNT(*) FROM images i WHERE i.project_id = h.project_id)
            END as dataset_size
        FROM project_history h
        ORDER BY h.date_created DESC
    """
    cur = await db.execute(query)
    rows = await cur.fetchall()
    return [ProjectHistoryItem(**dict(r)) for r in rows]


# ── Get single project ────────────────────────────────────────────────────────

@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    d = await _get_project_or_404(project_id, db)
    d = await _enrich_project(d, db)
    return ProjectOut(**d)


# ── Delete project ────────────────────────────────────────────────────────────

@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    proj = await _get_project_or_404(project_id, db)
    project_name = proj["name"]

    # Count current images in the project
    cur = await db.execute("SELECT COUNT(*) AS cnt FROM images WHERE project_id = ?", (project_id,))
    img_count = (await cur.fetchone())["cnt"]

    # Mark as deleted in project_history
    now = now_iso()
    res = await db.execute(
        "UPDATE project_history SET date_deleted = ?, dataset_size = ? WHERE project_id = ?",
        (now, img_count, project_id)
    )
    if res.rowcount == 0:
        await db.execute(
            "INSERT INTO project_history (id, project_id, name, date_created, dataset_size, date_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), project_id, project_name, proj["created_at"], img_count, now)
        )

    # Cascade delete DB records: annotations → images → project
    await db.execute(
        "DELETE FROM annotations WHERE image_id IN (SELECT id FROM images WHERE project_id = ?)",
        (project_id,),
    )
    await db.execute("DELETE FROM images WHERE project_id = ?", (project_id,))
    await db.execute("DELETE FROM project WHERE id = ?", (project_id,))
    await db.commit()

    # Clean up extracted frames folder for this project
    extracted_frames_dir = DATASET_DIR / "extracted_frames" / project_id
    if extracted_frames_dir.exists():
        shutil.rmtree(extracted_frames_dir, ignore_errors=True)

    await emit_global_projects_changed()
    return {"detail": "Project deleted"}



# ── Update classes ────────────────────────────────────────────────────────────

@router.patch("/projects/{project_id}/classes", response_model=ProjectOut)
async def update_classes(
    project_id: str,
    body: UpdateClassesRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await _get_project_or_404(project_id, db)

    await db.execute(
        "UPDATE project SET classes = ? WHERE id = ?",
        (json.dumps(body.classes), project_id),
    )
    await db.commit()

    # Notify annotators in this project
    await emit_project_updated(body.classes, project_id)

    cur = await db.execute("SELECT * FROM project WHERE id = ?", (project_id,))
    r = dict(await cur.fetchone())
    r["classes"] = json.loads(r["classes"])
    r = await _enrich_project(r, db)
    return ProjectOut(**r)


# ── Import images ─────────────────────────────────────────────────────────────

class ImportFolderRequest(BaseModel):
    folder_path: Optional[str] = None

@router.post("/projects/{project_id}/import", response_model=ImportResult)
async def import_images(
    project_id: str,
    body: Optional[ImportFolderRequest] = None,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    """Imports JPGs from a folder path into the specified project."""
    await _get_project_or_404(project_id, db)

    import asyncio
    import sys

    folder = None
    if body and body.folder_path:
        folder = body.folder_path.strip()

    if not folder:
        script_path = Path(__file__).parent / "pick_folder.py"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=400, detail="No folder selected")
        folder = stdout.decode().strip()

    if not folder:
        raise HTTPException(status_code=400, detail="No folder selected")

    folder_path = Path(folder)
    if not folder_path.exists():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")

    VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

    raw_files = [f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in VALID_EXTS]
    seen_paths = set()
    jpg_files = []
    for f in sorted(raw_files):
        resolved_str = str(f.resolve())
        if resolved_str not in seen_paths:
            seen_paths.add(resolved_str)
            jpg_files.append(f)

    if not jpg_files:
        raise HTTPException(status_code=400, detail="No valid images (JPG/PNG/WEBP/BMP/TIFF) found in selected folder")

    # Get already-imported filenames and filepaths for this project
    cur = await db.execute("SELECT filename, filepath FROM images WHERE project_id = ?", (project_id,))
    rows = await cur.fetchall()
    existing_filenames = {r["filename"] for r in rows}
    existing_filepaths = {str(Path(r["filepath"]).resolve()) for r in rows if r["filepath"]}

    from PIL import Image as PILImage
    imported = 0
    skipped = 0
    first_new_image_id = None
    seen_in_batch = set()

    try:
        for fpath in jpg_files:
            fname = fpath.name
            abs_path = str(fpath.resolve())

            if fname in existing_filenames or abs_path in existing_filepaths or abs_path in seen_in_batch:
                skipped += 1
                continue

            seen_in_batch.add(abs_path)

            try:
                with PILImage.open(fpath) as img:
                    w, h = img.size
            except Exception:
                skipped += 1
                continue

            iid = new_id()
            try:
                await db.execute(
                    "INSERT INTO images (id, project_id, filename, filepath, width, height) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (iid, project_id, fname, str(fpath), w, h),
                )
                existing_filenames.add(fname)
                existing_filepaths.add(abs_path)
                if first_new_image_id is None:
                    first_new_image_id = iid
                imported += 1
            except aiosqlite.IntegrityError:
                skipped += 1
                continue

        await db.execute(
            "UPDATE project SET source_folder = ? WHERE id = ?",
            (str(folder_path), project_id),
        )

        # Set thumbnail to first image if not already set
        if first_new_image_id:
            cur = await db.execute(
                "SELECT thumbnail_image_id FROM project WHERE id = ?", (project_id,)
            )
            proj_row = await cur.fetchone()
            if not proj_row["thumbnail_image_id"]:
                await db.execute(
                    "UPDATE project SET thumbnail_image_id = ? WHERE id = ?",
                    (first_new_image_id, project_id),
                )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Broadcast updated stats
    stats = await _project_stats(project_id, db)
    await emit_stats(stats, project_id)
    await emit_global_projects_changed()

    return ImportResult(imported=imported, skipped=skipped, folder=str(folder_path))


# ── Per-project stats ─────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/stats")
async def project_stats(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    await _get_project_or_404(project_id, db)
    return await _project_stats(project_id, db)


# ── Deprecated global /project endpoints (backward compat) ───────────────────

@router.get("/project")
async def _deprecated_get_project():
    raise HTTPException(
        status_code=410,
        detail="Deprecated. Use GET /api/projects or GET /api/projects/{project_id}",
    )

@router.post("/project")
async def _deprecated_create_project():
    raise HTTPException(
        status_code=410,
        detail="Deprecated. Use POST /api/projects",
    )


# ── User management (admin only) ──────────────────────────────────────────────

@router.get("/users", response_model=List[UserOut])
async def list_users(
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    cur = await db.execute("SELECT * FROM users ORDER BY created_at")
    rows = await cur.fetchall()
    return [UserOut(**dict(r)) for r in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: CreateUserRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (body.username,))
    if await cur.fetchone():
        raise HTTPException(status_code=400, detail="Username already exists")

    uid = new_id()
    await db.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
        (uid, body.username, hash_password(body.password), int(body.is_admin), now_iso()),
    )
    await db.commit()

    cur = await db.execute("SELECT * FROM users WHERE id = ?", (uid,))
    return UserOut(**dict(await cur.fetchone()))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    # Unassign their images across all projects
    await db.execute(
        "UPDATE images SET state='unassigned', assigned_to=NULL, assigned_at=NULL "
        "WHERE assigned_to = ?",
        (user_id,),
    )
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()

    return {"detail": "User deleted and images unassigned"}


@router.patch("/users/{user_id}/password")
async def change_password(
    user_id: str,
    body: dict,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    new_pw = body.get("password", "")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Password too short")
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_pw), user_id),
    )
    await db.commit()
    return {"detail": "Password updated"}
