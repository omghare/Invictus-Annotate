"""
Model assist — use your own trained models inside the annotation loop.

  POST /api/projects/{pid}/assist/predict     run a model over frames as a background job
        mode=prelabel → frames with no boxes get the model's boxes as suggestions
                        (source "model", dashed in the annotator, accepted with Enter);
                        one undoable batch, frames stay "to do" until a person accepts them
        mode=check    → predictions on labelled frames are kept for the label-error finder
  GET  /api/projects/{pid}/assist/findings    frames where labels and the model disagree, worst first
  GET  /api/annotations/{image_id}/predictions   model boxes + issues for one frame (annotator overlay)
  GET  /api/assist/models                     trained models that can be used

Inference runs in app/infer_worker.py (a separate process). On this laptop it uses the CPU
(~0.3 s per image with a nano model); with a CUDA GPU it is used automatically.
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.annotations import can_edit, normalise_boxes
from app.auth import get_admin_user, get_current_user
from app.common import chunks, get_project_or_404, placeholders
from app.config import DATA_DIR, MODEL_CACHE_DIR, MODELS_DIR
from app.database import connect, get_db, now_iso
from app.jobs import JobContext, job_kind, submit
from app.qa import compare, score
from app.sequence import commit_batch

router = APIRouter(tags=["assist"])

WORKER = Path(__file__).parent / "infer_worker.py"
JOB_DIR = DATA_DIR / "jobs"
MAX_IMAGES = 20000
TODO_STATES = ("unassigned", "assigned", "unannotated")


# ── Models ────────────────────────────────────────────────────────────────────

def list_model_files() -> list[dict]:
    """Every .pt under models/ (best.pt first within a run), newest runs first."""
    out = []
    for f in MODELS_DIR.rglob("*.pt"):
        st = f.stat()
        out.append({"name": f.relative_to(MODELS_DIR).as_posix(), "size_mb": round(st.st_size / 1024 ** 2, 1),
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(), "is_last": f.name == "last.pt"})
    out.sort(key=lambda m: (m["modified"][:16], not m["is_last"]), reverse=True)
    return out


def resolve_model_file(rel: str) -> Path:
    """A .pt inside models/ — never an arbitrary path or URL (a .pt is pickle, i.e. code)."""
    p = (MODELS_DIR / rel).resolve()
    try:
        p.relative_to(MODELS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Model must be one of the trained models in models/")
    if p.suffix != ".pt" or not p.is_file():
        raise HTTPException(status_code=404, detail="Model not found")
    return p


@router.get("/api/assist/models")
async def assist_models(_admin: dict = Depends(get_admin_user)):
    return {"models": await asyncio.to_thread(list_model_files)}


# ── Starting a run ────────────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    model: str
    mode: Literal["prelabel", "check"] = "prelabel"
    scope: Literal["todo", "labeled", "all", "selected"] = "todo"
    image_ids: Optional[List[str]] = Field(None, max_length=MAX_IMAGES)
    conf: float = Field(0.25, ge=0.01, le=0.99)
    imgsz: int = Field(640, ge=160, le=1920)
    device: str = Field("auto", max_length=16)
    fixture: Optional[dict] = None          # tests only (INVICTUS_TEST_PROVIDERS=1)


_SCOPES = {
    "todo": (f"i.state IN ({','.join(repr(s) for s in TODO_STATES)}) AND i.is_background = 0 "
             "AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.image_id = i.id)"),
    "labeled": ("i.state IN ('annotated', 'ready_for_export', 'exported') AND "
                "(i.is_background = 1 OR EXISTS (SELECT 1 FROM annotations a WHERE a.image_id = i.id))"),
    "all": "1 = 1",
}


@router.post("/api/projects/{project_id}/assist/predict")
async def start_predict(
    project_id: str,
    body: PredictRequest,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    await get_project_or_404(project_id, db)
    testing = body.fixture is not None and os.getenv("INVICTUS_TEST_PROVIDERS") == "1"
    if not testing:
        resolve_model_file(body.model)

    if body.scope == "selected":
        ids = list(dict.fromkeys(body.image_ids or []))
        found = []
        for batch in chunks(ids):
            cur = await db.execute(f"SELECT id FROM images WHERE project_id = ? AND id IN ({placeholders(len(batch))})",
                                   (project_id, *batch))
            found += [r["id"] for r in await cur.fetchall()]
        ids = [i for i in ids if i in set(found)]
    else:
        cur = await db.execute(f"SELECT i.id FROM images i WHERE i.project_id = ? AND {_SCOPES[body.scope]} "
                               f"ORDER BY COALESCE(i.seq, 0), i.filename LIMIT ?", (project_id, MAX_IMAGES))
        ids = [r["id"] for r in await cur.fetchall()]
    if not ids:
        raise HTTPException(status_code=400, detail={
            "todo": "No empty frames left to pre-label", "labeled": "No labelled frames to check yet",
        }.get(body.scope, "No frames selected"))

    params = {**body.model_dump(exclude={"image_ids"}), "image_ids": ids}
    return await submit("predict", params, admin, project_id)


# ── The job ───────────────────────────────────────────────────────────────────

async def _image_rows(db, project_id: str, ids: list[str]) -> list[dict]:
    cur = await db.execute("SELECT source_folder FROM project WHERE id = ?", (project_id,))
    proj = await cur.fetchone()
    folder = Path(proj["source_folder"]) if proj and proj["source_folder"] else None
    rows = []
    for batch in chunks(ids):
        cur = await db.execute(f"SELECT id, filename, filepath, width, height FROM images "
                               f"WHERE project_id = ? AND id IN ({placeholders(len(batch))})", (project_id, *batch))
        rows += [dict(r) for r in await cur.fetchall()]
    order = {iid: n for n, iid in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])
    for r in rows:
        p = Path(r["filepath"])
        if not p.exists() and folder and (folder / r["filename"]).exists():
            p = folder / r["filename"]
        r["path"] = str(p) if p.exists() else None
    return rows


@job_kind("predict")
async def run_predict(ctx: JobContext) -> dict:
    prm = ctx.params
    testing = prm.get("fixture") is not None and os.getenv("INVICTUS_TEST_PROVIDERS") == "1"
    model_label = "fixture" if testing else prm["model"]
    async with connect() as db:
        rows = await _image_rows(db, ctx.project_id, prm["image_ids"])
    images = [{"id": r["id"], "path": r["path"], "w": r["width"], "h": r["height"]} for r in rows if r["path"]]
    missing = len(rows) - len(images)
    if not images:
        raise RuntimeError("None of the selected image files can be found on disk")

    JOB_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path, stop_file = JOB_DIR / f"{ctx.id}.json", JOB_DIR / f"{ctx.id}.stop"
    cfg = {"provider": "yolo", "model": "" if testing else str(resolve_model_file(prm["model"])),
           "images": images, "conf": prm["conf"], "imgsz": prm["imgsz"], "device": prm.get("device", "auto"),
           "stop_file": str(stop_file)}
    if testing:
        cfg.update(provider="fixture", **prm["fixture"])
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    await ctx.progress(0, "Loading the model…", force=True)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(WORKER), str(cfg_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(MODEL_CACHE_DIR), limit=2 ** 24,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    done, device, names, error, tail = 0, None, [], None, []
    buffer: list[tuple] = []

    async def flush():
        if buffer:
            async with connect() as db:
                await db.executemany(
                    "INSERT OR REPLACE INTO model_predictions (job_id, image_id, model, boxes, created_at) "
                    "VALUES (?, ?, ?, ?, ?)", buffer)
                await db.commit()
            buffer.clear()

    async def watch_cancel():
        await ctx.cancel_event.wait()
        stop_file.touch()
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()

    watcher = asyncio.create_task(watch_cancel())
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith('{"__event__"'):
                if line:
                    tail = (tail + [line])[-20:]
                continue
            ev = json.loads(line)
            kind = ev.pop("__event__")
            if kind == "ready":
                device, names = ev.get("device"), ev.get("names", [])
                await ctx.progress(0, f"Running on {'GPU' if device not in (None, 'cpu') else 'CPU'}…", force=True)
            elif kind in ("pred", "skip"):
                done += 1
                if kind == "pred":
                    buffer.append((ctx.id, ev["image_id"], model_label, json.dumps(ev["boxes"]), now_iso()))
                if len(buffer) >= 20:
                    await flush()
                await ctx.progress(done / len(images) * 95, f"{done} / {len(images)} frames")
            elif kind == "error":
                error = ev.get("message")
        await proc.wait()
        await flush()
    finally:
        watcher.cancel()
        cfg_path.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
    ctx.check_cancelled()
    if proc.returncode != 0 and error is None:
        error = "\n".join(tail[-5:]) or f"Inference process exited with code {proc.returncode}"
    if error:
        raise RuntimeError(error)

    result = {"model": model_label, "device": device, "model_classes": names, "predicted": done,
              "missing_files": missing, "mode": prm["mode"]}
    if prm["mode"] == "prelabel":
        result.update(await _apply_prelabels(ctx, prm["conf"]))
        result["summary"] = (f"Pre-labelled {result['frames']} frames with {result['boxes']} boxes"
                             + (f" · {result['skipped']} already had boxes" if result["skipped"] else ""))
    else:
        findings = await findings_for_job(ctx.id, ctx.project_id)
        result.update(checked=done, with_issues=len(findings))
        result["summary"] = f"Checked {done} frames — {len(findings)} look worth a second look"
    return result


async def _apply_prelabels(ctx: JobContext, conf: float) -> dict:
    """Write predictions onto frames that are still empty, as one undoable batch."""
    async with connect() as db:
        project = await get_project_or_404(ctx.project_id, db)
        by_lower = {c.lower(): c for c in project["classes"]}
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (ctx.user_id,))
        user = dict(await cur.fetchone())
        cur = await db.execute(
            "SELECT p.image_id, p.boxes, i.* , "
            "(SELECT COUNT(*) FROM annotations a WHERE a.image_id = i.id) AS box_count "
            "FROM model_predictions p JOIN images i ON i.id = p.image_id WHERE p.job_id = ?", (ctx.id,))
        rows = [dict(r) for r in await cur.fetchall()]

        plan, unknown, skipped, empty = [], {}, 0, 0
        for r in rows:
            if r["box_count"] or r["is_background"] or r["state"] not in TODO_STATES:
                skipped += 1
                continue
            payload = []
            for b in json.loads(r["boxes"]):
                name = by_lower.get(b["class_name"].lower())
                if name is None:
                    unknown[b["class_name"]] = unknown.get(b["class_name"], 0) + 1
                    continue
                if b["conf"] < conf:
                    continue
                payload.append({"class_name": name, "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
                                "source": "model", "confidence": b["conf"]})
            boxes, _ = normalise_boxes(payload, project["classes"], r["width"], r["height"])
            if not boxes:
                empty += 1
                continue
            plan.append((r, boxes))
        result = await commit_batch(db, user, ctx.project_id, "prelabel", plan, keep_state=True) if plan else {"batch_id": None}
    return {"frames": len(plan), "boxes": sum(len(b) for _, b in plan), "skipped": skipped,
            "nothing_found": empty, "unknown_classes": unknown, "batch_id": result["batch_id"]}


# ── Findings ──────────────────────────────────────────────────────────────────

async def _latest_job(db, project_id: str, mode: Optional[str] = None) -> Optional[str]:
    cur = await db.execute(
        "SELECT id, params FROM jobs WHERE project_id = ? AND kind = 'predict' AND status = 'done' "
        "ORDER BY created_at DESC LIMIT 20", (project_id,))
    for r in await cur.fetchall():
        if mode is None or json.loads(r["params"]).get("mode") == mode:
            return r["id"]
    return None


async def findings_for_job(job_id: str, project_id: str, limit: int = 1000) -> list[dict]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT p.image_id, p.boxes, i.filename, i.state, i.seq, i.width, i.height, u.username "
            "FROM model_predictions p JOIN images i ON i.id = p.image_id "
            "LEFT JOIN users u ON u.id = i.assigned_to WHERE p.job_id = ? AND i.project_id = ?",
            (job_id, project_id))
        preds = [dict(r) for r in await cur.fetchall()]
        labels: dict[str, list[dict]] = {}
        for batch in chunks([p["image_id"] for p in preds]):
            cur = await db.execute(
                f"SELECT image_id, class_name, x, y, w, h FROM annotations WHERE image_id IN ({placeholders(len(batch))})",
                batch)
            for r in await cur.fetchall():
                labels.setdefault(r["image_id"], []).append(dict(r))
    out = []
    for p in preds:
        issues = compare(labels.get(p["image_id"], []), json.loads(p["boxes"]))
        if issues:
            out.append({"image_id": p["image_id"], "filename": p["filename"], "state": p["state"], "seq": p["seq"],
                        "assignee": p["username"], "issues": issues, "score": score(issues)})
    out.sort(key=lambda f: -f["score"])
    return out[:limit]


@router.get("/api/projects/{project_id}/assist/findings")
async def get_findings(
    project_id: str,
    job_id: Optional[str] = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await get_project_or_404(project_id, db)
    job_id = job_id or await _latest_job(db, project_id, "check")
    if not job_id:
        return {"job_id": None, "findings": []}
    findings = await findings_for_job(job_id, project_id)
    counts: dict[str, int] = {}
    for f in findings:
        for i in f["issues"]:
            counts[i["type"]] = counts.get(i["type"], 0) + 1
    return {"job_id": job_id, "findings": findings, "counts": counts}


@router.get("/api/annotations/{image_id}/predictions")
async def image_predictions(
    image_id: str,
    job_id: Optional[str] = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    cur = await db.execute("SELECT * FROM images WHERE id = ?", (image_id,))
    img = await cur.fetchone()
    if img is None or not can_edit(user, img):
        raise HTTPException(status_code=404, detail="Image not found")
    if job_id:
        cur = await db.execute("SELECT job_id, model, boxes FROM model_predictions WHERE image_id = ? AND job_id = ?",
                               (image_id, job_id))
    else:
        cur = await db.execute("SELECT p.job_id, p.model, p.boxes FROM model_predictions p JOIN jobs j ON j.id = p.job_id "
                               "WHERE p.image_id = ? ORDER BY j.created_at DESC LIMIT 1", (image_id,))
    row = await cur.fetchone()
    if row is None:
        return {"job_id": None, "boxes": [], "issues": []}
    cur = await db.execute("SELECT class_name, x, y, w, h FROM annotations WHERE image_id = ?", (image_id,))
    labels = [dict(r) for r in await cur.fetchall()]
    boxes = json.loads(row["boxes"])
    return {"job_id": row["job_id"], "model": row["model"], "boxes": boxes, "issues": compare(labels, boxes)}
