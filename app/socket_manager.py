"""
Socket.IO server instance + room management + event handlers.
Now supports per-project rooms so events from one project don't
bleed into other projects' clients.
"""
import asyncio
from typing import Optional

import socketio

from app.auth import decode_token

# ── Server instance (shared across the whole app) ────────────────────────────
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",        # LAN tool — all origins OK
    ping_timeout=60,
    ping_interval=25,
)

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
        qs = environ.get("QUERY_STRING", "")
        for part in qs.split("&"):
            if part.startswith("token="):
                token = part[6:]
            elif part.startswith("project_id="):
                project_id = part[11:]

    if not token:
        await sio.disconnect(sid)
        return

    payload = decode_token(token)
    if payload is None:
        await sio.disconnect(sid)
        return

    user_info = {
        "user_id":    payload["sub"],
        "username":   payload["username"],
        "is_admin":   payload.get("is_admin", False),
        "project_id": project_id,
    }
    _connected[sid] = user_info

    # Everyone joins the global room (home-page project list updates)
    await sio.enter_room(sid, global_room())

    # Join project-specific room if a project_id was provided
    if project_id:
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
    if not new_pid:
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
    """Client on /train page calls this to receive training events."""
    if sid not in _connected:
        return
    await sio.enter_room(sid, TRAINING_ROOM)


# ── Emit helpers (called by route handlers) ───────────────────────────────────

async def emit_stats(stats: dict, project_id: str):
    """Broadcast image state counts to everyone in the project room."""
    await sio.emit("stats_update", stats, room=project_room(project_id))

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

async def emit_annotation_saved(image_id: str, box_count: int, saved_by: str):
    """Notify admin feed about a save event."""
    from datetime import datetime, timezone
    await sio.emit("annotation_saved", {
        "image_id":  image_id,
        "box_count": box_count,
        "saved_by":  saved_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, room=ADMIN_ROOM)

async def emit_image_deleted(image_id: str, project_id: str):
    """Notify all clients in the project room that an image was deleted."""
    await sio.emit("image_deleted", {"image_id": image_id}, room=project_room(project_id))

async def emit_export_progress(done: int, total: int, project_id: str):
    """Stream export progress to admin."""
    await sio.emit("export_progress", {
        "done":    done,
        "total":   total,
        "percent": round(done / total * 100) if total else 100,
    }, room=ADMIN_ROOM)

async def emit_project_updated(classes: list, project_id: str):
    """Notify all annotators in the project that the class list changed."""
    await sio.emit("project_updated", {"classes": classes}, room=project_room(project_id))


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
