"""
Sequence tools for video-frame annotation.

  POST /api/projects/{pid}/annotations/propagate     copy one frame's boxes to the next N frames
  POST /api/projects/{pid}/annotations/interpolate   fill frames between keyframes by linear motion
  POST /api/projects/{pid}/annotations/bulk          write many frames at once (generic)
  GET  /api/projects/{pid}/annotations/batches       recent undoable operations
  POST /api/projects/{pid}/annotations/batches/{id}/undo

Every write goes through annotations.write_image and is recorded as a batch so it can be
undone. A *keyframe* is a frame with at least one manually drawn box; generated boxes carry
source = propagated / interpolated and a track_id that links the same object across frames.
"""
import json
from typing import List, Literal, Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.annotations import can_edit, normalise_boxes, write_image
from app.auth import get_current_user
from app.common import chunks, get_project_or_404, placeholders, project_stats
from app.config import BATCH_HISTORY_KEEP, MAX_BULK_ITEMS, MIN_BOX_PX
from app.database import get_db, new_id, now_iso
from app.models import AnnotationSource, BoundingBox
from app.seq_utils import ann_hash, fit_box, iou, lerp_box, match_boxes
from app.socket_manager import emit_annotations_bulk_saved, emit_state_change_bulk, emit_stats

router = APIRouter(tags=["sequence"])

Mode = Literal["only_empty", "replace", "append"]
GENERATED = {"carried", "propagated", "interpolated", "tracked", "model"}


# ── Request models ────────────────────────────────────────────────────────────

class PropagateRequest(BaseModel):
    source_image_id: str
    target_image_ids: Optional[List[str]] = Field(None, max_length=MAX_BULK_ITEMS)
    count: int = Field(10, ge=1, le=MAX_BULK_ITEMS)
    direction: Literal["forward", "backward"] = "forward"
    until_next_annotated: bool = False
    mode: Mode = "only_empty"
    allow_submitted: bool = False
    dry_run: bool = False

class InterpolateRequest(BaseModel):
    start_image_id: str
    end_image_id: str
    overwrite: Literal["empty_only", "generated"] = "empty_only"
    allow_submitted: bool = False
    dry_run: bool = False

class BulkItem(BaseModel):
    image_id: str
    annotations: List[BoundingBox]

class BulkRequest(BaseModel):
    mode: Mode = "only_empty"
    source: AnnotationSource = "propagated"
    allow_submitted: bool = False
    dry_run: bool = False
    items: List[BulkItem] = Field(..., max_length=MAX_BULK_ITEMS)


# ── Loading helpers ───────────────────────────────────────────────────────────

_IMG_COLS = ("SELECT i.*, "
             "(SELECT COUNT(*) FROM annotations a WHERE a.image_id = i.id) AS box_count, "
             "(SELECT COUNT(*) FROM annotations a WHERE a.image_id = i.id AND a.source = 'manual') AS manual "
             "FROM images i ")


async def _images_by_id(db, project_id: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for batch in chunks(ids):
        cur = await db.execute(
            _IMG_COLS + f"WHERE i.project_id = ? AND i.id IN ({placeholders(len(batch))})",
            (project_id, *batch),
        )
        out.update({r["id"]: dict(r) for r in await cur.fetchall()})
    return out


async def _boxes_by_image(db, ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {i: [] for i in ids}
    for batch in chunks(ids):
        cur = await db.execute(
            f"SELECT * FROM annotations WHERE image_id IN ({placeholders(len(batch))}) ORDER BY rowid", batch
        )
        for r in await cur.fetchall():
            out.setdefault(r["image_id"], []).append(dict(r))
    return out


async def _source_image(db, project_id: str, image_id: str, user: dict) -> dict:
    imgs = await _images_by_id(db, project_id, [image_id])
    img = imgs.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    if not can_edit(user, img):
        raise HTTPException(status_code=403, detail="Not your image")
    return img


def _decide(img: Optional[dict], user: dict, mode: str, allow_submitted: bool) -> Optional[str]:
    """None → write; otherwise the skip reason."""
    if img is None:
        return "not_found"
    if not can_edit(user, img):
        return "forbidden"
    if img["state"] == "exported":
        return "exported"
    if img["state"] == "ready_for_export" and not allow_submitted:
        return "submitted"
    if mode in ("only_empty", "empty_only"):
        if img["box_count"] > 0 or img["is_background"] or img["state"] in ("annotated", "ready_for_export"):
            return "not_empty"
    return None


def _target_row(img: dict, action: str, reason: Optional[str], box_count_new: int = 0, **extra) -> dict:
    return {
        "image_id": img["id"], "filename": img["filename"], "seq": img.get("seq"),
        "state": img["state"], "box_count": img["box_count"],
        "action": action, "reason": reason, "box_count_new": box_count_new, **extra,
    }


def _box_payload(b: dict, source: str) -> dict:
    return {
        "class_id": b.get("class_id", -1), "class_name": b["class_name"],
        "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
        "source": source, "track_id": b.get("track_id"),
    }


# ── Committing a batch (single transaction + undo record) ─────────────────────

async def commit_batch(db, user: dict, project_id: str, kind: str,
                       plan: list[tuple[dict, list[dict]]], keep_state: bool = False) -> dict:
    """plan: [(image_row, normalised_boxes)] — writes all frames atomically as one undoable batch."""
    if not plan:
        return {"batch_id": None, "applied": []}

    ts = now_iso()
    batch_id = new_id()
    ids = [img["id"] for img, _ in plan]
    prev = await _boxes_by_image(db, ids)
    applied, changes = [], []
    try:
        await db.execute(
            "INSERT INTO annotation_batches (id, project_id, user_id, kind, created_at) VALUES (?,?,?,?,?)",
            (batch_id, project_id, user["id"], kind, ts),
        )
        for img, boxes in plan:
            new_state = await write_image(db, img, boxes, user, ts, keep_state=keep_state)
            prev_boxes = [
                {k: a[k] for k in ("class_id", "class_name", "x", "y", "w", "h", "source", "track_id", "confidence")}
                for a in prev.get(img["id"], [])
            ]
            await db.execute(
                "INSERT INTO annotation_batch_items (batch_id, image_id, prev_state, prev_assigned_to, "
                "prev_background, prev_annotations, new_hash) VALUES (?,?,?,?,?,?,?)",
                (batch_id, img["id"], img["state"], img["assigned_to"], img["is_background"],
                 json.dumps(prev_boxes), ann_hash(boxes)),
            )
            applied.append({"image_id": img["id"], "state": new_state, "box_count": len(boxes)})
            if new_state != img["state"]:
                changes.append({"image_id": img["id"], "new_state": new_state,
                                "assigned_to": img["assigned_to"] or user["id"]})
        await _prune_batches(db, project_id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await emit_state_change_bulk(changes, project_id)
    await emit_annotations_bulk_saved(project_id, ids, user["username"], kind, batch_id)
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"batch_id": batch_id, "applied": applied}


async def _prune_batches(db, project_id: str) -> None:
    cur = await db.execute(
        "SELECT id FROM annotation_batches WHERE project_id = ? ORDER BY created_at DESC LIMIT -1 OFFSET ?",
        (project_id, BATCH_HISTORY_KEEP),
    )
    old = [r["id"] for r in await cur.fetchall()]
    for batch in chunks(old):
        ph = placeholders(len(batch))
        await db.execute(f"DELETE FROM annotation_batch_items WHERE batch_id IN ({ph})", batch)
        await db.execute(f"DELETE FROM annotation_batches WHERE id IN ({ph})", batch)


def _merge_append(existing: list[dict], new: list[dict]) -> list[dict]:
    merged = [dict(e) for e in existing]
    for b in new:
        if not any(e["class_name"] == b["class_name"] and iou(e, b) > 0.9 for e in merged):
            merged.append(b)
    return merged


# ── Propagate ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/annotations/propagate")
async def propagate(
    project_id: str,
    body: PropagateRequest,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Copy the source frame's saved boxes onto following (or preceding) frames — for objects
    that stay put. Stays inside the source's video/folder and the caller's own frames.
    """
    project = await get_project_or_404(project_id, db)
    src = await _source_image(db, project_id, body.source_image_id, user)
    src_boxes = (await _boxes_by_image(db, [src["id"]]))[src["id"]]
    if not src_boxes:
        raise HTTPException(status_code=400, detail="The source frame has no saved boxes to propagate")

    if body.target_image_ids:
        ids = [i for i in dict.fromkeys(body.target_image_ids) if i != src["id"]]
        found = await _images_by_id(db, project_id, ids)
        targets = sorted((found.get(i) or {"id": i} for i in ids), key=lambda r: r.get("seq") or 0)
    else:
        forward = body.direction == "forward"
        params: list = [project_id, src["seq"] or 0]
        where = f"i.project_id = ? AND COALESCE(i.seq, 0) {'>' if forward else '<'} ?"
        if src["seq_group"] is None:
            where += " AND i.seq_group IS NULL"
        else:
            where += " AND i.seq_group = ?"
            params.append(src["seq_group"])
        if not user["is_admin"]:
            where += " AND i.assigned_to = ?"
            params.append(user["id"])
        cur = await db.execute(
            _IMG_COLS + f"WHERE {where} ORDER BY COALESCE(i.seq, 0) {'ASC' if forward else 'DESC'} LIMIT ?",
            (*params, body.count),
        )
        targets = [dict(r) for r in await cur.fetchall()]

    # Copies share the source box's track_id so interpolation can match them later
    missing_tracks = [b for b in src_boxes if not b.get("track_id")]
    for b in missing_tracks:
        b["track_id"] = new_id()[:8]

    existing = await _boxes_by_image(db, [t["id"] for t in targets if "state" in t]) if body.mode == "append" else {}
    rows, plan = [], []
    for t in targets:
        if "state" not in t:
            rows.append({"image_id": t["id"], "action": "skip", "reason": "not_found"})
            continue
        if body.until_next_annotated and (t["box_count"] > 0 or t["state"] in ("annotated", "ready_for_export", "exported")):
            break
        reason = _decide(t, user, body.mode, body.allow_submitted)
        warning = "other_group" if t.get("seq_group") != src["seq_group"] else None
        if reason:
            rows.append(_target_row(t, "skip", reason, warning=warning))
            continue
        fitted = [
            fb for b in src_boxes
            if (fb := fit_box(b, src["width"], src["height"], t["width"], t["height"], MIN_BOX_PX))
        ]
        payload = [_box_payload(b, "propagated") for b in fitted]
        if body.mode == "append":
            payload = _merge_append(existing.get(t["id"], []), payload)
        boxes, _ = normalise_boxes(payload, project["classes"], t["width"], t["height"])
        rows.append(_target_row(t, "write", None, len(boxes), warning=warning))
        plan.append((t, boxes))

    summary = {"write": len(plan), "skip": len(rows) - len(plan)}
    if body.dry_run:
        return {"source": {"image_id": src["id"], "box_count": len(src_boxes)}, "targets": rows, "summary": summary}

    for b in missing_tracks:
        await db.execute("UPDATE annotations SET track_id = ? WHERE id = ?", (b["track_id"], b["id"]))
    result = await commit_batch(db, user, project_id, "propagate", plan)
    return {**result, "targets": rows, "summary": summary,
            "skipped": [r for r in rows if r["action"] == "skip"]}


# ── Interpolate ───────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/annotations/interpolate")
async def interpolate(
    project_id: str,
    body: InterpolateRequest,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Fill every frame between keyframes by moving and resizing each box linearly.
    All keyframes inside the range are used, so fixing one middle frame by hand and
    re-running bends the motion around it.
    """
    project = await get_project_or_404(project_id, db)
    a = await _source_image(db, project_id, body.start_image_id, user)
    b = await _source_image(db, project_id, body.end_image_id, user)
    if (a["seq"] or 0) > (b["seq"] or 0):
        a, b = b, a
    if a["id"] == b["id"]:
        raise HTTPException(status_code=400, detail="Pick two different frames")
    if a["seq_group"] != b["seq_group"]:
        raise HTTPException(status_code=400, detail="Both frames must come from the same video / folder")

    params: list = [project_id, a["seq"] or 0, b["seq"] or 0]
    where = "i.project_id = ? AND COALESCE(i.seq, 0) BETWEEN ? AND ?"
    if a["seq_group"] is None:
        where += " AND i.seq_group IS NULL"
    else:
        where += " AND i.seq_group = ?"
        params.append(a["seq_group"])
    if not user["is_admin"]:
        where += " AND i.assigned_to = ?"
        params.append(user["id"])
    cur = await db.execute(_IMG_COLS + f"WHERE {where} ORDER BY COALESCE(i.seq, 0)", params)
    frames = [dict(r) for r in await cur.fetchall()]
    if len(frames) > MAX_BULK_ITEMS:
        raise HTTPException(status_code=400, detail=f"Range too long (max {MAX_BULK_ITEMS} frames)")

    keyframes = [f for f in frames if f["manual"] > 0]
    if not keyframes or keyframes[0]["id"] != a["id"] or keyframes[-1]["id"] != b["id"]:
        raise HTTPException(
            status_code=400,
            detail="Both ends must be keyframes — frames with boxes you drew or adjusted by hand",
        )

    all_boxes = await _boxes_by_image(db, [f["id"] for f in frames])
    rows, plan, segments = [], [], []
    for ka, kb in zip(keyframes, keyframes[1:]):
        A = [x for x in all_boxes[ka["id"]]]
        B = [x for x in all_boxes[kb["id"]]]
        W, H = ka["width"], ka["height"]
        # Compare in keyframe A's pixel space if the two frames differ in size
        B_in_a = [fit_box(x, kb["width"], kb["height"], W, H) or x for x in B]
        pairs, un_a, un_b = match_boxes(A, B_in_a, W, H)

        # Stable ids so the next run (and later edits) keep matching the same objects
        for i, j in pairs:
            tid = A[i].get("track_id") or B[j].get("track_id") or new_id()[:8]
            for box in (A[i], B[j]):
                if box.get("track_id") != tid:
                    box["track_id"] = tid
                    box["_dirty_track"] = True

        inner = [f for f in frames if (ka["seq"] or 0) < (f["seq"] or 0) < (kb["seq"] or 0)]
        seg = {"from": ka["id"], "to": kb["id"], "matched": len(pairs),
               "unmatched_from": [A[i]["class_name"] for i in un_a],
               "unmatched_to": [B[j]["class_name"] for j in un_b],
               "targets": len(inner), "write": 0, "skip": 0}
        span = (kb["seq"] or 0) - (ka["seq"] or 0)
        for t in inner:
            reason = _decide(t, user, "only_empty" if body.overwrite == "empty_only" else "replace",
                             body.allow_submitted)
            if reason is None and body.overwrite == "generated":
                existing = all_boxes[t["id"]]
                if any(x["source"] not in GENERATED for x in existing) or (t["is_background"]) or \
                        (not existing and t["state"] in ("annotated", "ready_for_export")):
                    reason = "manual"
            if reason:
                rows.append(_target_row(t, "skip", reason)); seg["skip"] += 1
                continue
            alpha = ((t["seq"] or 0) - (ka["seq"] or 0)) / span
            payload = []
            for i, j in pairs:
                box = lerp_box(A[i], B_in_a[j], alpha)
                box = fit_box(box, W, H, t["width"], t["height"], MIN_BOX_PX)
                if box:
                    payload.append(_box_payload({**box, "track_id": A[i]["track_id"]}, "interpolated"))
            boxes, _ = normalise_boxes(payload, project["classes"], t["width"], t["height"])
            rows.append(_target_row(t, "write", None, len(boxes))); seg["write"] += 1
            plan.append((t, boxes))
        segments.append(seg)

    summary = {"write": len(plan), "skip": len(rows) - len(plan), "keyframes": len(keyframes)}
    kf_out = [{"image_id": k["id"], "seq": k["seq"], "filename": k["filename"], "box_count": k["box_count"]}
              for k in keyframes]
    if body.dry_run:
        return {"keyframes": kf_out, "segments": segments, "targets": rows, "summary": summary}

    for k in keyframes:
        for box in all_boxes[k["id"]]:
            if box.pop("_dirty_track", False):
                await db.execute("UPDATE annotations SET track_id = ? WHERE id = ?", (box["track_id"], box["id"]))
    result = await commit_batch(db, user, project_id, "interpolate", plan)
    return {**result, "keyframes": kf_out, "segments": segments, "targets": rows, "summary": summary}


# ── Generic bulk write ────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/annotations/bulk")
async def bulk_write(
    project_id: str,
    body: BulkRequest,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    project = await get_project_or_404(project_id, db)
    ids = [it.image_id for it in body.items]
    imgs = await _images_by_id(db, project_id, ids)
    existing = await _boxes_by_image(db, list(imgs)) if body.mode == "append" else {}
    rows, plan = [], []
    for it in body.items:
        img = imgs.get(it.image_id)
        reason = _decide(img, user, body.mode, body.allow_submitted)
        if reason:
            rows.append(_target_row(img, "skip", reason) if img else
                        {"image_id": it.image_id, "action": "skip", "reason": reason})
            continue
        payload = [{**a.model_dump(), "source": body.source if a.source == "manual" else a.source}
                   for a in it.annotations]
        if body.mode == "append":
            payload = _merge_append(existing.get(img["id"], []), payload)
        boxes, _ = normalise_boxes(payload, project["classes"], img["width"], img["height"])
        rows.append(_target_row(img, "write", None, len(boxes)))
        plan.append((img, boxes))
    summary = {"write": len(plan), "skip": len(rows) - len(plan)}
    if body.dry_run:
        return {"targets": rows, "summary": summary}
    result = await commit_batch(db, user, project_id, "bulk", plan)
    return {**result, "targets": rows, "summary": summary, "skipped": [r for r in rows if r["action"] == "skip"]}


# ── Undo ──────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/annotations/batches")
async def list_batches(
    project_id: str,
    mine: bool = Query(True),
    limit: int = Query(5, ge=1, le=50),
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    await get_project_or_404(project_id, db)
    params: list = [project_id]
    where = "b.project_id = ?"
    if mine or not user["is_admin"]:
        where += " AND b.user_id = ?"
        params.append(user["id"])
    cur = await db.execute(
        f"SELECT b.*, (SELECT COUNT(*) FROM annotation_batch_items it WHERE it.batch_id = b.id) AS frames "
        f"FROM annotation_batches b WHERE {where} ORDER BY b.created_at DESC LIMIT ?",
        (*params, limit),
    )
    return {"batches": [dict(r) for r in await cur.fetchall()]}


@router.post("/projects/{project_id}/annotations/batches/{batch_id}/undo")
async def undo_batch(
    project_id: str,
    batch_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Restore every frame of a batch unless it was edited or submitted since."""
    await get_project_or_404(project_id, db)
    cur = await db.execute("SELECT * FROM annotation_batches WHERE id = ? AND project_id = ?", (batch_id, project_id))
    batch = await cur.fetchone()
    if not batch:
        raise HTTPException(status_code=404, detail="Operation not found")
    if batch["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(status_code=403, detail="You can only undo your own operations")
    if batch["undone_at"]:
        raise HTTPException(status_code=409, detail="Already undone")

    cur = await db.execute("SELECT * FROM annotation_batch_items WHERE batch_id = ?", (batch_id,))
    items = [dict(r) for r in await cur.fetchall()]
    ids = [it["image_id"] for it in items]
    imgs = await _images_by_id(db, project_id, ids)
    current = await _boxes_by_image(db, ids)

    ts = now_iso()
    restored, conflicts, changes = [], [], []
    try:
        for it in items:
            img = imgs.get(it["image_id"])
            if img is None:
                conflicts.append({"image_id": it["image_id"], "reason": "deleted"}); continue
            if img["state"] in ("ready_for_export", "exported"):
                conflicts.append({"image_id": img["id"], "reason": "submitted"}); continue
            if ann_hash(current.get(img["id"], [])) != it["new_hash"]:
                conflicts.append({"image_id": img["id"], "reason": "edited"}); continue

            prev = json.loads(it["prev_annotations"])
            await db.execute("DELETE FROM annotations WHERE image_id = ?", (img["id"],))
            if prev:
                await db.executemany(
                    "INSERT INTO annotations (id, image_id, class_id, class_name, x, y, w, h, created_by, "
                    "updated_at, source, track_id, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(new_id(), img["id"], p["class_id"], p["class_name"], p["x"], p["y"], p["w"], p["h"],
                      user["id"], ts, p.get("source") or "manual", p.get("track_id"), p.get("confidence")) for p in prev],
                )
            # Keep the current assignee if the frame was reassigned since the operation
            assignee = it["prev_assigned_to"] if img["assigned_to"] in (None, it["prev_assigned_to"], user["id"])                 else img["assigned_to"]
            await db.execute(
                "UPDATE images SET state = ?, assigned_to = ?, is_background = ?, version = version + 1 WHERE id = ?",
                (it["prev_state"], assignee, it["prev_background"], img["id"]),
            )
            restored.append({"image_id": img["id"], "state": it["prev_state"], "box_count": len(prev)})
            if it["prev_state"] != img["state"]:
                changes.append({"image_id": img["id"], "new_state": it["prev_state"],
                                "assigned_to": it["prev_assigned_to"]})
        await db.execute("UPDATE annotation_batches SET undone_at = ? WHERE id = ?", (ts, batch_id))
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await emit_state_change_bulk(changes, project_id)
    await emit_annotations_bulk_saved(project_id, [r["image_id"] for r in restored], user["username"], "undo", batch_id)
    await emit_stats(await project_stats(project_id, db), project_id)
    return {"restored": restored, "conflicts": conflicts}
