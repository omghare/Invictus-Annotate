"""
Socket.IO server instance + room management + event handlers.
Now supports per-project rooms so events from one project don't
bleed into other projects' clients.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs

import aiosqlite
import socketio

from app.config import DB_PATH

# ── Server instance (shared across the whole app) ────────────────────────────
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=None,       # same origin only (whatever host/IP the page was opened on)
    ping_timeout=60,
    ping_interval=25,
)


async def _db_user(token: Optional[str]) -> Optional[dict]:
    """Same checks as HTTP auth: valid signature, not revoked, account active."""
    from app.auth import user_for_token
    if not token:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await user_for_token(token, db)


async def _may_join(user_info: dict, project_id: str) -> bool:
    if user_info["is_admin"]:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM images WHERE project_id = ? AND assigned_to = ? LIMIT 1",
                               (project_id, user_info["user_id"]))
        return await cur.fetchone() is not None

# ── Room names ────────────────────────────────────────────────────────────────
ADMIN_ROOM    = "admin_room"
TRAINING_ROOM = "training_room"

def project_room(project_id: str) -> str:
    """Returns the Socket.IO room name for a specific project."""
    return f"project_{project_id}"

def global_room() -> str:
    """Room that all authenticated clients join (for home-page global events)."""
    return "global_room"

# ── sid → user_info mapping ───────────────────────────────────────────────────
_connected: dict[str, dict] = {}  # sid → {user_id, username, is_admin, project_id}


# ── Connection lifecycle ──────────────────────────────────────────────────────

@sio.event
async def connect(sid: str, environ: dict, auth: Optional[dict] = None):
    token = (auth or {}).get("token")
    project_id = (auth or {}).get("project_id")  # may be None on home page

    if not token:
        # Also try query string (for clients that can't set auth)
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        token = (qs.get("token") or [None])[0]
        project_id = project_id or (qs.get("project_id") or [None])[0]

    user = await _db_user(token)
    if user is None:
        # Client receives connect_error with this message and can redirect to login
        raise ConnectionRefusedError("unauthorized")

    user_info = {
        "user_id":    user["id"],
        "username":   user["username"],
        "is_admin":   bool(user["is_admin"]),
        "project_id": None,
    }
    _connected[sid] = user_info

    # Everyone joins the global room (home-page project list updates)
    await sio.enter_room(sid, global_room())

    # Join project-specific room if a project_id was provided (and the user may see it)
    if project_id and await _may_join(user_info, project_id):
        user_info["project_id"] = project_id
        await sio.enter_room(sid, project_room(project_id))

    # Admins also join the admin room for the detailed activity feed
    if user_info["is_admin"]:
        await sio.enter_room(sid, ADMIN_ROOM)


@sio.event
async def disconnect(sid: str):
    _connected.pop(sid, None)


# ── join_project event (client can call this to switch project room) ──────────

@sio.event
async def join_project(sid: str, data: dict):
    """
    Allows a client to join/switch to a project room dynamically.
    Called when the user navigates to a project page.
    data: { "project_id": "..." }
    """
    if sid not in _connected:
        return
    new_pid = (data or {}).get("project_id")
    if not new_pid or not await _may_join(_connected[sid], new_pid):
        return

    old_pid = _connected[sid].get("project_id")
    # Leave old project room if switching
    if old_pid and old_pid != new_pid:
        await sio.leave_room(sid, project_room(old_pid))

    await sio.enter_room(sid, project_room(new_pid))
    _connected[sid]["project_id"] = new_pid


# ── join_training_room event ──────────────────────────────────────────────────

@sio.event
async def join_training_room(sid: str):
    """Client on /train page calls this to receive training events (admins only)."""
    info = _connected.get(sid)
    if not info or not info["is_admin"]:
        return
    await sio.enter_room(sid, TRAINING_ROOM)


# ── Emit helpers (called by route handlers) ───────────────────────────────────

# Home-page cards get a throttled copy of each project's stats (trailing edge, 1/s max)
_pending_global_stats: dict[str, dict] = {}
_global_stats_tasks: dict[str, asyncio.Task] = {}


async def _flush_global_stats(project_id: str):
    await asyncio.sleep(1.0)
    stats = _pending_global_stats.pop(project_id, None)
    if stats is not None:
        await sio.emit("project_stats_changed", {"project_id": project_id, "stats": stats},
                       room=global_room())


async def emit_stats(stats: dict, project_id: str):
    """Broadcast image state counts to the project room (and, throttled, to Home pages)."""
    await sio.emit("stats_update", {**stats, "project_id": project_id}, room=project_room(project_id))
    _pending_global_stats[project_id] = stats
    task = _global_stats_tasks.get(project_id)
    if task is None or task.done():
        _global_stats_tasks[project_id] = asyncio.create_task(_flush_global_stats(project_id))

async def emit_global_projects_changed():
    """Notify home page clients that the project list changed (create/delete)."""
    await sio.emit("projects_changed", {}, room=global_room())

async def emit_state_change(image_id: str, new_state: str, assigned_to: Optional[str], project_id: str):
    """Notify everyone in the project room that an image changed state."""
    await sio.emit("image_state_change", {
        "image_id":    image_id,
        "new_state":   new_state,
        "assigned_to": assigned_to,
    }, room=project_room(project_id))

async def emit_state_change_bulk(changes: list[dict], project_id: str, summary: Optional[str] = None):
    """
    One event for many state changes (assign / propagate / submit-all).
    changes: [{image_id, new_state, assigned_to}]
    """
    if not changes:
        return
    await sio.emit("image_state_change_bulk", {
        "changes": changes,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, room=project_room(project_id))

async def emit_annotation_saved(image_id: str, box_count: int, saved_by: str, project_id: str):
    """Activity-feed event for a single save."""
    await sio.emit("annotation_saved", {
        "image_id":   image_id,
        "box_count":  box_count,
        "saved_by":   saved_by,
        "project_id": project_id,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }, room=project_room(project_id))

async def emit_annotations_bulk_saved(project_id: str, image_ids: list[str], saved_by: str,
                                      kind: str, batch_id: Optional[str]):
    """Activity-feed + reload hint for propagate / interpolate / undo."""
    await sio.emit("annotations_bulk_saved", {
        "image_ids":  image_ids,
        "saved_by":   saved_by,
        "kind":       kind,
        "batch_id":   batch_id,
        "project_id": project_id,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }, room=project_room(project_id))

async def emit_image_deleted(image_id: str, project_id: str):
    """Notify all clients in the project room that an image was deleted."""
    await sio.emit("image_deleted", {"image_id": image_id}, room=project_room(project_id))

async def emit_images_deleted(image_ids: list[str], project_id: str):
    """One event for a bulk delete (clients drop all ids at once)."""
    if image_ids:
        await sio.emit("images_deleted", {"image_ids": image_ids}, room=project_room(project_id))

async def emit_export_progress(done: int, total: int, project_id: str):
    """Stream export progress to the project's room."""
    await sio.emit("export_progress", {
        "done":       done,
        "total":      total,
        "percent":    round(done / total * 100) if total else 100,
        "project_id": project_id,
    }, room=project_room(project_id))

async def emit_video_progress(project_id: str, percent: float, stage: str):
    """Frame-extraction progress for the Video → Frames page."""
    await sio.emit("video_progress", {
        "percent": round(percent, 1), "stage": stage, "project_id": project_id,
    }, room=project_room(project_id))

async def emit_project_updated(classes: list, project_id: str, renames: Optional[dict] = None):
    """Notify all annotators in the project that the class list changed (renames: {old: new})."""
    await sio.emit("project_updated", {"classes": classes, "renames": renames or {}}, room=project_room(project_id))

async def emit_job_update(job: dict):
    """Progress / completion of a background job (to its project room, or admins for global jobs)."""
    room = project_room(job["project_id"]) if job.get("project_id") else ADMIN_ROOM
    await sio.emit("job_update", job, room=room)


# ── Training emit helpers ─────────────────────────────────────────────────────────────────

async def emit_train_log(line: str):
    """Stream a raw log line to the training room terminal."""
    await sio.emit("train_log", {"line": line}, room=TRAINING_ROOM)

async def emit_train_epoch(data: dict):
    """Emit parsed epoch metrics (mAP, losses, LR, etc.)."""
    await sio.emit("train_epoch", data, room=TRAINING_ROOM)

async def emit_train_comment(text: str, epoch: int):
    """Emit a heuristic performance comment (every 5 epochs)."""
    await sio.emit("train_comment", {"text": text, "epoch": epoch}, room=TRAINING_ROOM)

async def emit_train_hardware(data: dict):
    """Emit real-time hardware usage stats."""
    await sio.emit("train_hardware", data, room=TRAINING_ROOM)

async def emit_train_complete(data: dict):
    """Notify training room that training finished successfully."""
    await sio.emit("train_complete", data, room=TRAINING_ROOM)

async def emit_train_error(message: str):
    """Notify training room of a fatal error."""
    await sio.emit("train_error", {"message": message}, room=TRAINING_ROOM)

async def emit_train_status(data: dict):
    """Broadcast current session state (used for reconnect sync)."""
    await sio.emit("train_status", data, room=TRAINING_ROOM)
