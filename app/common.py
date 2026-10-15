"""
Shared route helpers — project lookup and image-state statistics.
(Previously copy-pasted into four modules.)

Stat definitions used everywhere in the UI:
  pending = assigned + unannotated                 → "To annotate"
  done    = annotated + ready_for_export + exported → "Annotated (total)"
"""
import json
from typing import Optional

import aiosqlite
from fastapi import HTTPException

STATES = ("unassigned", "assigned", "unannotated", "annotated", "ready_for_export", "exported")


async def get_project_or_404(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute("SELECT * FROM project WHERE id = ?", (project_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    d = dict(row)
    d["classes"] = json.loads(d["classes"])
    return d


async def has_project_access(project_id: str, user: dict, db: aiosqlite.Connection) -> bool:
    """Admins see every project; annotators only projects with at least one image assigned to them."""
    if user["is_admin"]:
        return True
    cur = await db.execute(
        "SELECT 1 FROM images WHERE project_id = ? AND assigned_to = ? LIMIT 1", (project_id, user["id"])
    )
    return await cur.fetchone() is not None


async def get_accessible_project(project_id: str, user: dict, db: aiosqlite.Connection) -> dict:
    """get_project_or_404 + access check (404 rather than 403, so ids can't be probed)."""
    project = await get_project_or_404(project_id, db)
    if not await has_project_access(project_id, user, db):
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _derive(counts: dict) -> dict:
    out = {s: counts.get(s, 0) for s in STATES}
    out["pending"] = out["assigned"] + out["unannotated"]
    out["done"] = out["annotated"] + out["ready_for_export"] + out["exported"]
    return out


async def project_stats(project_id: str, db: aiosqlite.Connection) -> dict:
    """Image counts per state for the whole project."""
    cur = await db.execute(
        "SELECT state, COUNT(*) AS cnt FROM images WHERE project_id = ? GROUP BY state",
        (project_id,),
    )
    counts = {r["state"]: r["cnt"] for r in await cur.fetchall()}
    stats = _derive(counts)
    stats["total"] = sum(counts.values())
    stats["project_total"] = stats["total"]
    cur = await db.execute(
        "SELECT SUM(review_status = 'approved' AND state IN ('ready_for_export', 'exported')) AS approved, "
        "SUM(review_status = 'rejected') AS rejected, "
        "SUM(review_status IS NULL AND state = 'ready_for_export') AS awaiting_review "
        "FROM images WHERE project_id = ?", (project_id,))
    row = await cur.fetchone()
    stats.update({k: row[k] or 0 for k in ("approved", "rejected", "awaiting_review")})
    return stats


async def user_stats(project_id: str, user_id: str, db: aiosqlite.Connection) -> dict:
    """Image counts per state for one user's assignments (total = the user's own total)."""
    cur = await db.execute(
        "SELECT state, COUNT(*) AS cnt FROM images WHERE project_id = ? AND assigned_to = ? GROUP BY state",
        (project_id, user_id),
    )
    counts = {r["state"]: r["cnt"] for r in await cur.fetchall()}
    stats = _derive(counts)
    stats["total"] = sum(counts.values())
    cur = await db.execute("SELECT COUNT(*) AS cnt FROM images WHERE project_id = ?", (project_id,))
    stats["project_total"] = (await cur.fetchone())["cnt"]
    cur = await db.execute("SELECT COUNT(*) AS cnt FROM images WHERE project_id = ? AND assigned_to = ? "
                           "AND review_status = 'rejected'", (project_id, user_id))
    stats["rejected"] = (await cur.fetchone())["cnt"]
    return stats


def placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def resolve_class(class_name: Optional[str], class_id: Optional[int], classes: list[str]) -> tuple[str, int]:
    """
    Class names are the source of truth (export maps by name). Returns (name, id);
    id is -1 for an orphan class that is no longer in the project's list.
    """
    if class_name and class_name in classes:
        return class_name, classes.index(class_name)
    if class_name:
        return class_name, -1
    if class_id is not None and 0 <= class_id < len(classes):
        return classes[class_id], class_id
    raise ValueError("Box has no class")
