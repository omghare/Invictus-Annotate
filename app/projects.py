"""
Project routes — multi-project support.
All project endpoints are scoped to /api/projects/{project_id}.
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import bump_token_version, get_admin_user, get_current_user, hash_password
from app.common import STATES, chunks, get_accessible_project, get_project_or_404, project_stats
from app.config import EXTRACTED_FRAMES_DIR, IMAGE_EXTS
from app.database import get_db, new_id, now_iso
from app.images import remove_thumbnails
from app.models import (
    CreateProjectRequest, CreateUserRequest, ImportResult,
    ProjectListItem, ProjectOut, SetPasswordRequest, UpdateClassesRequest, UserOut,
    ProjectHistoryItem,
)
from app.seq_utils import natural_key, seq_group_for
from app.socket_manager import emit_global_projects_changed, emit_project_updated, emit_stats

router = APIRouter(tags=["project"])


async def _enrich_project(row: dict, db: aiosqlite.Connection) -> dict:
    """Add computed image-count stats to a project dict."""
    stats = await project_stats(row["id"], db)
    row["image_count"]      = stats["total"]
    row["annotated_count"]  = stats["done"]
    row["ready_count"]      = stats["ready_for_export"]
    row["exported_count"]   = stats["exported"]
    row["unassigned_count"] = stats["unassigned"]
    return row


# ── List all projects ─────────────────────────────────────────────────────────

@router.get("/projects", response_model=List[ProjectListItem])
async def list_projects(
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Admin: sees all projects. Annotator: only projects with images assigned to them."""
    if user["is_admin"]:
        cur = await db.execute("SELECT * FROM project ORDER BY created_at DESC")
    else:
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

    # One grouped query for all projects' counts
    stats_cur = await db.execute(
        "SELECT project_id, state, COUNT(*) AS cnt FROM images GROUP BY project_id, state"
    )
    per_project: dict[str, dict] = {}
    for r in await stats_cur.fetchall():
        per_project.setdefault(r["project_id"], {})[r["state"]] = r["cnt"]

    result = []
    for row in rows:
        d = dict(row)
        d["classes"] = json.loads(d["classes"])
        c = per_project.get(d["id"], {})
        d["image_count"]      = sum(c.values())
        d["annotated_count"]  = c.get("annotated", 0) + c.get("ready_for_export", 0) + c.get("exported", 0)
        d["ready_count"]      = c.get("ready_for_export", 0)
        d["exported_count"]   = c.get("exported", 0)
        d["unassigned_count"] = c.get("unassigned", 0)
        d["stats"]            = {s: c.get(s, 0) for s in STATES}
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
        (new_id(), pid, body.name.strip(), created_at),
    )
    await db.commit()
    await emit_global_projects_changed()
    row = await get_project_or_404(pid, db)
    return ProjectOut(**await _enrich_project(row, db))


# ── Project History ──────────────────────────────────────────────────────────

@router.get("/projects/history", response_model=List[ProjectHistoryItem])
async def get_project_history(
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    cur = await db.execute("""
        SELECT
            h.project_id, h.name, h.date_created, h.date_deleted,
            CASE
                WHEN h.date_deleted IS NOT NULL THEN h.dataset_size
                ELSE (SELECT COUNT(*) FROM images i WHERE i.project_id = h.project_id)
            END AS dataset_size
        FROM project_history h
        ORDER BY h.date_created DESC
    """)
    return [ProjectHistoryItem(**dict(r)) for r in await cur.fetchall()]


# ── Get single project ────────────────────────────────────────────────────────

@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    d = await get_accessible_project(project_id, user, db)
    return ProjectOut(**await _enrich_project(d, db))


# ── Delete project ────────────────────────────────────────────────────────────

@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    proj = await get_project_or_404(project_id, db)

    cur = await db.execute("SELECT id FROM images WHERE project_id = ?", (project_id,))
    image_ids = [r["id"] for r in await cur.fetchall()]

    now = now_iso()
    res = await db.execute(
        "UPDATE project_history SET date_deleted = ?, dataset_size = ? WHERE project_id = ?",
        (now, len(image_ids), project_id),
    )
    if res.rowcount == 0:
        await db.execute(
            "INSERT INTO project_history (id, project_id, name, date_created, dataset_size, date_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), project_id, proj["name"], proj["created_at"], len(image_ids), now),
        )

    await db.execute(
        "DELETE FROM annotations WHERE image_id IN (SELECT id FROM images WHERE project_id = ?)", (project_id,)
    )
    await db.execute(
        "DELETE FROM annotation_batch_items WHERE batch_id IN "
        "(SELECT id FROM annotation_batches WHERE project_id = ?)", (project_id,)
    )
    await db.execute("DELETE FROM annotation_batches WHERE project_id = ?", (project_id,))
    await db.execute(
        "DELETE FROM model_predictions WHERE job_id IN (SELECT id FROM jobs WHERE project_id = ?)", (project_id,)
    )
    await db.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
    await db.execute("DELETE FROM images WHERE project_id = ?", (project_id,))
    await db.execute("DELETE FROM project WHERE id = ?", (project_id,))
    await db.commit()

    # Frames this app extracted for the project are ours to delete; imported folders are not
    frames_dir = EXTRACTED_FRAMES_DIR / project_id
    if frames_dir.exists():
        await asyncio.to_thread(shutil.rmtree, frames_dir, True)
    await asyncio.to_thread(remove_thumbnails, image_ids)

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
    await get_project_or_404(project_id, db)
    renames = {old: new.strip() for old, new in body.renames.items() if old != new.strip() and new.strip() in body.classes}
    # Two passes through placeholder names, so swaps (a↔b) and chains (a→b→c) don't merge classes
    scope = "AND image_id IN (SELECT id FROM images WHERE project_id = ?)"
    for i, old in enumerate(renames):
        await db.execute(f"UPDATE annotations SET class_name = ? WHERE class_name = ? {scope}",
                         (f"\x00rename{i}", old, project_id))
    for i, new in enumerate(renames.values()):
        await db.execute(f"UPDATE annotations SET class_name = ? WHERE class_name = ? {scope}",
                         (new, f"\x00rename{i}", project_id))
    await db.execute("UPDATE project SET classes = ? WHERE id = ?", (json.dumps(body.classes), project_id))
    # Keep stored class ids in step with the new order (names are the source of truth)
    await db.execute(
        "UPDATE annotations SET class_id = COALESCE("
        "  (SELECT CAST(j.key AS INTEGER) FROM json_each(?) j WHERE j.value = annotations.class_name), -1) "
        "WHERE image_id IN (SELECT id FROM images WHERE project_id = ?)",
        (json.dumps(body.classes), project_id),
    )
    await db.commit()
    await emit_project_updated(body.classes, project_id, renames)
    r = await get_project_or_404(project_id, db)
    return ProjectOut(**await _enrich_project(r, db))


@router.get("/projects/{project_id}/classes/usage")
async def class_usage(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Box count per class name (including orphans no longer in the class list)."""
    await get_accessible_project(project_id, user, db)
    cur = await db.execute(
        "SELECT a.class_name, COUNT(*) AS boxes, COUNT(DISTINCT a.image_id) AS images "
        "FROM annotations a JOIN images i ON i.id = a.image_id WHERE i.project_id = ? GROUP BY a.class_name",
        (project_id,),
    )
    return {r["class_name"]: {"boxes": r["boxes"], "images": r["images"]} for r in await cur.fetchall()}


# ── Import images ─────────────────────────────────────────────────────────────

class ImportFolderRequest(BaseModel):
    folder_path: Optional[str] = None
    include_subfolders: bool = False


def _scan_folder(folder: Path, recursive: bool) -> tuple[list[tuple[Path, int, int]], int]:
    """
    Runs in a worker thread: list images (natural order per folder) and read their sizes.
    Returns (found, unreadable_count).
    """
    from PIL import Image as PILImage

    pattern = folder.rglob("*") if recursive else folder.iterdir()
    files = [f for f in pattern if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
    files.sort(key=lambda f: (natural_key(str(f.parent.relative_to(folder))), natural_key(f.name)))

    found, unreadable = [], 0
    for f in files:
        try:
            with PILImage.open(f) as img:
                w, h = img.size
                # EXIF orientation 5–8 = rotated 90°: browsers (and OpenCV) show it as h × w
                if img.getexif().get(0x0112) in (5, 6, 7, 8):
                    w, h = h, w
        except Exception:
            unreadable += 1
            continue
        found.append((f, w, h))
    return found, unreadable


async def import_folder(project_id: str, folder_path: Path, db: aiosqlite.Connection,
                        recursive: bool = False) -> ImportResult:
    """Import every image in folder_path into the project (dedupe by resolved file path)."""
    found, unreadable = await asyncio.to_thread(_scan_folder, folder_path, recursive)
    if not found:
        raise HTTPException(status_code=400, detail="No valid images (JPG/PNG/WEBP/BMP/TIFF) found in selected folder")

    cur = await db.execute("SELECT filepath FROM images WHERE project_id = ?", (project_id,))
    existing = {str(Path(r["filepath"]).resolve()) for r in await cur.fetchall()}
    cur = await db.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM images WHERE project_id = ?", (project_id,))
    seq = (await cur.fetchone())["s"]

    rows, skipped, group_cache = [], unreadable, {}
    for fpath, w, h in found:
        abs_path = str(fpath.resolve())
        if abs_path in existing:
            skipped += 1
            continue
        existing.add(abs_path)
        seq += 1
        parent = fpath.parent
        group = group_cache.setdefault(parent, seq_group_for(parent))
        rows.append((new_id(), project_id, fpath.name, abs_path, w, h, seq, group))

    # Short transactions so annotators' saves are never blocked for long
    for batch in chunks(rows, 500):
        await db.executemany(
            "INSERT OR IGNORE INTO images (id, project_id, filename, filepath, width, height, seq, seq_group) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        await db.commit()

    await db.execute("UPDATE project SET source_folder = ? WHERE id = ?", (str(folder_path), project_id))
    if rows:
        await db.execute(
            "UPDATE project SET thumbnail_image_id = ? WHERE id = ? AND thumbnail_image_id IS NULL",
            (rows[0][0], project_id),
        )
    await db.commit()

    await emit_stats(await project_stats(project_id, db), project_id)
    await emit_global_projects_changed()
    return ImportResult(imported=len(rows), skipped=skipped, folder=str(folder_path))


@router.post("/projects/{project_id}/import", response_model=ImportResult)
async def import_images(
    project_id: str,
    body: Optional[ImportFolderRequest] = None,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """Imports images from a folder path (or a native folder picker on the server) into the project."""
    await get_project_or_404(project_id, db)

    folder = (body.folder_path or "").strip().strip('"') if body else ""
    if not folder:
        script_path = Path(__file__).parent / "pick_folder.py"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            # The dialog opens on the server's screen; nobody there → give up instead of hanging
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=408, detail="No folder was picked on the server within 5 minutes")
        if proc.returncode != 0:
            raise HTTPException(status_code=400, detail="No folder selected")
        folder = stdout.decode("utf-8", errors="replace").strip()

    if not folder:
        raise HTTPException(status_code=400, detail="No folder selected")
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")

    return await import_folder(project_id, folder_path, db, bool(body and body.include_subfolders))


# ── Missing files / relink ────────────────────────────────────────────────────

class RelinkRequest(BaseModel):
    folder_path: str
    dry_run: bool = False


def _missing_files(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not Path(r["filepath"]).exists()]


@router.get("/projects/{project_id}/files/check")
async def check_files(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """How many of the project's image files are missing on disk (e.g. after moving a folder)."""
    await get_project_or_404(project_id, db)
    cur = await db.execute("SELECT id, filename, filepath FROM images WHERE project_id = ?", (project_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    missing = await asyncio.to_thread(_missing_files, rows)
    folders = sorted({str(Path(r["filepath"]).parent) for r in missing})
    return {"total": len(rows), "missing": len(missing), "missing_folders": folders[:20]}


@router.post("/projects/{project_id}/files/relink")
async def relink_files(
    project_id: str,
    body: RelinkRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """
    Point missing images at a new folder: each missing file is matched by filename
    anywhere under folder_path. Annotations are untouched.
    """
    await get_project_or_404(project_id, db)
    root = Path(body.folder_path.strip().strip('"'))
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {root}")

    cur = await db.execute("SELECT id, filename, filepath FROM images WHERE project_id = ?", (project_id,))
    rows = [dict(r) for r in await cur.fetchall()]

    def plan() -> tuple[list[tuple[str, str]], int]:
        missing = _missing_files(rows)
        if not missing:
            return [], 0
        by_name: dict[str, list[Path]] = {}
        for f in root.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                by_name.setdefault(f.name, []).append(f)
        updates, unresolved = [], 0
        for r in missing:
            candidates = by_name.get(r["filename"], [])
            if len(candidates) > 1 and len({c.stat().st_size for c in candidates}) == 1:
                # Identical copies (e.g. the same frame in several exports): any of them will do
                candidates = [max(candidates, key=lambda c: c.stat().st_mtime)]
            if len(candidates) == 1:
                updates.append((str(candidates[0].resolve()), r["id"]))
            else:
                # prefer the candidate whose parent folder name matches the old one
                old_parent = Path(r["filepath"]).parent.name
                same = [c for c in candidates if c.parent.name == old_parent]
                if len(same) == 1:
                    updates.append((str(same[0].resolve()), r["id"]))
                else:
                    unresolved += 1
        return updates, unresolved

    updates, unresolved = await asyncio.to_thread(plan)
    if not body.dry_run and updates:
        for batch in chunks(updates):
            await db.executemany("UPDATE OR IGNORE images SET filepath = ? WHERE id = ?", batch)
        await db.execute("UPDATE project SET source_folder = ? WHERE id = ?", (str(root), project_id))
        await db.commit()
        await asyncio.to_thread(remove_thumbnails, [iid for _, iid in updates])
    return {"relinked": len(updates), "unresolved": unresolved, "dry_run": body.dry_run}


# ── Per-project stats ─────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/stats")
async def get_project_stats(
    project_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    await get_accessible_project(project_id, user, db)
    return await project_stats(project_id, db)


# ── User management (admin only) ──────────────────────────────────────────────

@router.get("/users", response_model=List[UserOut])
async def list_users(
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    cur = await db.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY created_at")
    return [UserOut(**dict(r)) for r in await cur.fetchall()]


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
    """
    Deactivates the user (their annotations keep their author) and returns their
    not-yet-annotated images to the unassigned pool. Annotated work keeps its state.
    """
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    cur = await db.execute("SELECT username FROM users WHERE id = ? AND is_active = 1", (user_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    cur = await db.execute(
        "UPDATE images SET state='unassigned', assigned_to=NULL, assigned_at=NULL "
        "WHERE assigned_to = ? AND state IN ('assigned', 'unannotated') RETURNING project_id",
        (user_id,),
    )
    touched = {r["project_id"] for r in await cur.fetchall()}
    cur = await db.execute(
        "UPDATE images SET assigned_to=NULL, assigned_at=NULL WHERE assigned_to = ? RETURNING project_id",
        (user_id,),
    )
    touched |= {r["project_id"] for r in await cur.fetchall()}
    # Free the username for re-use while keeping the row for annotation history
    await db.execute(
        "UPDATE users SET is_active = 0, username = ? WHERE id = ?",
        (f"{row['username']}~deleted~{user_id[:8]}", user_id),
    )
    await bump_token_version(db, user_id)
    await db.commit()

    for pid in touched:
        await emit_stats(await project_stats(pid, db), pid)
    return {"detail": "User deleted and their pending images unassigned"}


@router.patch("/users/{user_id}/password")
async def change_password(
    user_id: str,
    body: SetPasswordRequest,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    """Admin sets a temporary password: the user is signed out and must pick their own next time."""
    other = user_id != admin["id"]
    res = await db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = ?, "
        "token_version = token_version + ? WHERE id = ? AND is_active = 1",
        (hash_password(body.password), int(other), int(other), user_id),
    )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()
    return {"detail": "Password updated"}
