"""
Background jobs — long work (model inference, cloud training bundles) runs here instead of
inside an HTTP request. Progress streams over Socket.IO (`job_update`), jobs can be cancelled,
and their state survives a page reload (rows in the `jobs` table).

Jobs run one at a time: on a small CPU two inference jobs side by side are slower than two
in a row, and a GPU machine is just as happy with a queue. A job kind is an async function
registered with @job_kind("name"); it receives a JobContext and returns a JSON-able result.

  GET  /api/jobs?project_id=&active=1     recent jobs
  GET  /api/jobs/{id}
  POST /api/jobs/{id}/cancel
"""
import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_admin_user
from app.database import connect, new_id, now_iso
from app.socket_manager import emit_job_update

router = APIRouter(tags=["jobs"])

ACTIVE = ("queued", "running")
_KINDS: dict[str, Callable[["JobContext"], Awaitable[Any]]] = {}


class JobCancelled(Exception):
    pass


def job_kind(name: str):
    def register(fn):
        _KINDS[name] = fn
        return fn
    return register


class JobContext:
    def __init__(self, row: dict):
        self.id: str = row["id"]
        self.kind: str = row["kind"]
        self.project_id: Optional[str] = row["project_id"]
        self.params: dict = json.loads(row["params"] or "{}")
        self.user_id: str = row["created_by"]
        self.cancel_event = asyncio.Event()
        self._last_emit = 0.0
        self._row = row

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled()

    async def progress(self, pct: float, message: Optional[str] = None, force: bool = False) -> None:
        """Report progress (0–100). Throttled to ~2 updates/s unless force."""
        now = time.monotonic()
        if not force and now - self._last_emit < 0.5:
            return
        self._last_emit = now
        await _update(self.id, progress=round(max(0.0, min(100.0, pct)), 1), message=message)


# ── Runner ────────────────────────────────────────────────────────────────────

_state: dict[str, Any] = {"queue": None, "task": None, "loop": None, "running": {}}


def _ensure_runner() -> asyncio.Queue:
    """Start (or restart, e.g. after the event loop changed) the single runner task."""
    loop = asyncio.get_running_loop()
    task = _state["task"]
    if _state["loop"] is not loop or task is None or task.done():
        _state["queue"] = asyncio.Queue()
        _state["loop"] = loop
        _state["task"] = loop.create_task(_runner(_state["queue"]))
    return _state["queue"]


async def _runner(queue: asyncio.Queue) -> None:
    while True:
        job_id = await queue.get()
        try:
            await _run(job_id)
        except Exception:                       # never let one job kill the runner
            pass


async def _run(job_id: str) -> None:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
    if row is None or row["status"] != "queued":
        return
    ctx = JobContext(dict(row))
    _state["running"][job_id] = ctx
    await _update(job_id, status="running", started_at=now_iso(), progress=0)
    try:
        result = await _KINDS[ctx.kind](ctx)
        if ctx.cancelled:
            raise JobCancelled()
        await _update(job_id, status="done", progress=100, finished_at=now_iso(),
                      result=json.dumps(result or {}), message=(result or {}).get("summary"))
    except JobCancelled:
        await _update(job_id, status="cancelled", finished_at=now_iso(), message="Cancelled")
    except Exception as exc:
        await _update(job_id, status="failed", finished_at=now_iso(), message=str(exc)[:1000])
    finally:
        _state["running"].pop(job_id, None)


async def _update(job_id: str, **fields) -> None:
    async with connect() as db:
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            await db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
            await db.commit()
        cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
    if row:
        await emit_job_update(public(dict(row)))


def public(row: dict) -> dict:
    out = dict(row)
    out["params"] = json.loads(out.get("params") or "{}")
    out["result"] = json.loads(out["result"]) if out.get("result") else None
    return out


async def submit(kind: str, params: dict, user: dict, project_id: Optional[str] = None) -> dict:
    if kind not in _KINDS:
        raise ValueError(f"Unknown job kind {kind}")
    job_id = new_id()
    async with connect() as db:
        await db.execute(
            "INSERT INTO jobs (id, kind, project_id, status, params, created_by, created_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (job_id, kind, project_id, json.dumps(params), user["id"], now_iso()),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = dict(await cur.fetchone())
    _ensure_runner().put_nowait(job_id)
    await emit_job_update(public(row))
    return public(row)


async def wait(job_id: str, timeout: float = 120) -> dict:
    """Wait until a job finishes (tests, and callers that need the result)."""
    deadline = time.monotonic() + timeout
    while True:
        async with connect() as db:
            cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = dict(await cur.fetchone())
        if row["status"] not in ACTIVE or time.monotonic() > deadline:
            return public(row)
        await asyncio.sleep(0.1)


async def shutdown() -> None:
    for ctx in list(_state["running"].values()):
        ctx.cancel_event.set()
    task = _state["task"]
    if task and not task.done():
        task.cancel()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/jobs")
async def list_jobs(
    project_id: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    active: bool = Query(False),
    limit: int = Query(20, ge=1, le=200),
    _admin: dict = Depends(get_admin_user),
):
    wheres, params = [], []
    if project_id:
        wheres.append("project_id = ?"); params.append(project_id)
    if kind:
        wheres.append("kind = ?"); params.append(kind)
    if active:
        wheres.append("status IN ('queued', 'running')")
    where = f"WHERE {' AND '.join(wheres)}" if wheres else ""
    async with connect() as db:
        cur = await db.execute(f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", (*params, limit))
        return {"jobs": [public(dict(r)) for r in await cur.fetchall()]}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, _admin: dict = Depends(get_admin_user)):
    async with connect() as db:
        cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return public(dict(row))


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _admin: dict = Depends(get_admin_user)):
    ctx = _state["running"].get(job_id)
    if ctx:
        ctx.cancel_event.set()
        return {"detail": "Cancelling…"}
    async with connect() as db:
        cur = await db.execute(
            "UPDATE jobs SET status='cancelled', finished_at=?, message='Cancelled' "
            "WHERE id = ? AND status = 'queued' RETURNING id", (now_iso(), job_id))
        hit = await cur.fetchone()
        await db.commit()
    if not hit:
        raise HTTPException(status_code=400, detail="This job is not running")
    await _update(job_id)
    return {"detail": "Cancelled"}
