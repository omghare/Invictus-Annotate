"""
Export — per-project YOLO dataset generation with data.yaml.
Each export writes a new folder dataset/{project name}_dataset[ (n)]/.

Images are hard-linked into the export when it is on the same drive as the originals
(an export then costs almost no extra disk space); otherwise they are copied. The same
writer feeds the zip-based cloud training bundles (app/cloud.py).
"""
import asyncio
import json
import os
import random
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import aiosqlite
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_admin_user
from app.common import chunks, get_project_or_404, placeholders, project_stats
from app.config import DATASET_DIR
from app.database import get_db
from app.models import ExportPreview, ExportRequest, ExportResult
from app.seq_utils import split_counts
from app.socket_manager import emit_export_progress, emit_state_change_bulk, emit_stats

router = APIRouter(tags=["export"])

SEQUENCE_CHUNK = 25   # consecutive frames kept together when group_sequences is on
_export_lock = asyncio.Lock()    # two exports at once would pick the same output folder


# ── Splitting ─────────────────────────────────────────────────────────────────

def plan_split(images: list[dict], ratios: list[float], group_sequences: bool, seed: int) -> list[list[dict]]:
    """
    Split images into [train, valid, test] following ratios (largest-remainder rounding,
    train always >= 1). With group_sequences, runs of up to SEQUENCE_CHUNK consecutive
    frames from the same folder/video stay in one split so near-identical neighbours do
    not leak between train and validation.
    """
    rng = random.Random(seed)
    n = len(images)
    targets = split_counts(n, ratios)

    if not group_sequences:
        lst = list(images)
        rng.shuffle(lst)
        out, start = [], 0
        for t in targets:
            out.append(lst[start:start + t])
            start += t
        return _ensure_nonempty(out, ratios)

    ordered = sorted(images, key=lambda i: (i.get("seq_group") or "", i.get("seq") or 0, i["filename"]))
    # Small datasets need small blocks, otherwise one block can swallow the whole validation share
    chunk = max(1, min(SEQUENCE_CHUNK, n // 20))
    blocks: list[list[dict]] = []
    for img in ordered:
        if blocks and len(blocks[-1]) < chunk and blocks[-1][-1].get("seq_group") == img.get("seq_group"):
            blocks[-1].append(img)
        else:
            blocks.append([img])
    rng.shuffle(blocks)

    out: list[list[dict]] = [[] for _ in ratios]
    for block in blocks:
        # Give the block to the split furthest below its target (ties → train)
        deficits = [targets[i] - len(out[i]) for i in range(len(ratios))]
        best = max(range(len(ratios)), key=lambda i: (deficits[i], -i))
        out[best].extend(block)
    if n and not out[0]:
        donor = max(range(1, len(out)), key=lambda i: len(out[i]))
        out[0].append(out[donor].pop())
    return _ensure_nonempty(out, ratios)


def _ensure_nonempty(parts: list[list[dict]], ratios: list[float]) -> list[list[dict]]:
    """A split that was asked for (ratio > 0) gets at least one image when possible —
    YOLO refuses to train with an empty validation folder."""
    for i in range(1, len(parts)):
        if ratios[i] > 0 and not parts[i] and len(parts[0]) > 1:
            parts[i].append(parts[0].pop())
    return parts


def _ratios(train: float, valid: float, test: float, include_test: bool) -> list[float]:
    return [train, valid, test] if include_test else [train, valid, 0.0]


# ── data.yaml ─────────────────────────────────────────────────────────────────

def build_data_yaml(classes: List[str], include_test: bool, dataset_path: Optional[str] = None) -> str:
    """
    YOLO-compatible data.yaml (safe for class names containing quotes / colons).
    No `path:` key — Ultralytics then resolves the splits relative to the yaml file itself,
    so the folder can be moved or zipped (a relative `path:` would resolve against the
    working directory instead).
    """
    data = {"train": "train/images", "val": "valid/images"}
    if include_test:
        data["test"] = "test/images"
    data["nc"] = len(classes)
    data["names"] = list(classes)
    head = f"# Exported by Invictus Annotate to {dataset_path}\n" if dataset_path else ""
    return head + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None)


def _safe_dir_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in name).strip() or "project"


def _next_dataset_dir(project_name: str) -> Path:
    base = f"{_safe_dir_name(project_name)}_dataset"
    candidate = DATASET_DIR / base
    counter = 1
    while candidate.exists():
        candidate = DATASET_DIR / f"{base} ({counter})"
        counter += 1
    return candidate


# ── Routes ────────────────────────────────────────────────────────────────────

async def _exportable_images(project_id: str, db: aiosqlite.Connection, only_approved: bool = False) -> list[dict]:
    approved = " AND review_status = 'approved'" if only_approved else ""
    cur = await db.execute(
        "SELECT id, filename, filepath, width, height, seq, seq_group FROM images "
        f"WHERE project_id=? AND state IN ('ready_for_export', 'exported'){approved} ORDER BY COALESCE(seq,0), filename",
        (project_id,),
    )
    seen, images = set(), []
    for r in await cur.fetchall():
        img = dict(r)
        key = str(Path(img["filepath"]).resolve()) if img["filepath"] else img["filename"]
        if key not in seen:
            seen.add(key)
            images.append(img)
    return images


@router.get("/projects/{project_id}/export/preview", response_model=ExportPreview)
async def export_preview(
    project_id: str,
    train_ratio: float = 0.8,
    valid_ratio: float = 0.1,
    test_ratio:  float = 0.1,
    include_test: bool = True,
    group_sequences: bool = True,
    only_approved: bool = False,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await get_project_or_404(project_id, db)
    images = await _exportable_images(project_id, db, only_approved)
    parts = plan_split(images, _ratios(train_ratio, valid_ratio, test_ratio, include_test), group_sequences, seed=0)
    return ExportPreview(
        total_ready=len(images),
        train_count=len(parts[0]),
        valid_count=len(parts[1]),
        test_count=len(parts[2]),
    )


@router.get("/projects/{project_id}/export/yaml-preview")
async def export_yaml_preview(
    project_id: str,
    include_test: bool = Query(True),
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """The exact data.yaml an export would write (path shows the next output folder)."""
    proj = await get_project_or_404(project_id, db)
    target = _next_dataset_dir(proj["name"])
    return {"yaml": build_data_yaml(proj["classes"], include_test, target.as_posix()),
            "output_path": str(target)}


class FolderWriter:
    """Writes the dataset into a folder; images are hard-linked when possible."""

    def __init__(self, root: Path):
        self.root = root
        self.linked = 0
        self.copied = 0

    def add_image(self, src: Path, rel: str) -> None:
        dst = self.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)                 # same NTFS/ext4 volume → no extra space
            self.linked += 1
        except OSError:
            shutil.copy2(src, dst)
            self.copied += 1

    def add_text(self, rel: str, text: str) -> None:
        dst = self.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")

    def add_dir(self, rel: str) -> None:
        (self.root / rel).mkdir(parents=True, exist_ok=True)


class ZipWriter:
    """Writes the dataset straight into a zip (images stored, text compressed)."""

    def __init__(self, zf: zipfile.ZipFile, prefix: str = ""):
        self.zf = zf
        self.prefix = prefix
        self.linked = 0
        self.copied = 0

    def add_image(self, src: Path, rel: str) -> None:
        self.zf.write(src, self.prefix + rel, compress_type=zipfile.ZIP_STORED)   # JPG/PNG are compressed already
        self.copied += 1

    def add_text(self, rel: str, text: str) -> None:
        self.zf.writestr(self.prefix + rel, text, compress_type=zipfile.ZIP_DEFLATED)

    def add_dir(self, rel: str) -> None:
        self.zf.writestr(self.prefix + rel.rstrip("/") + "/", "")


def yolo_label_lines(anns: list[dict], classes: list[str], W: int, H: int, filename: str,
                     warnings: list[str]) -> list[str]:
    W, H = max(1, W), max(1, H)
    lines = []
    for ann in anns:
        if ann["class_name"] not in classes:
            warnings.append(f"Skipped box in {filename}: class '{ann['class_name']}' not in project classes")
            continue
        # Clip in corner space, then normalise
        x1 = min(max(ann["x"], 0), W); y1 = min(max(ann["y"], 0), H)
        x2 = min(max(ann["x"] + ann["w"], 0), W); y2 = min(max(ann["y"] + ann["h"], 0), H)
        if x2 - x1 < 1 or y2 - y1 < 1:
            continue
        cid = classes.index(ann["class_name"])
        lines.append(f"{cid} {(x1 + x2) / 2 / W:.6f} {(y1 + y2) / 2 / H:.6f} {(x2 - x1) / W:.6f} {(y2 - y1) / H:.6f}")
    return lines


def write_dataset(
    writer,
    split_plan: list[tuple[str, list[dict]]],
    labels: dict[str, list[dict]],
    classes: list[str],
    progress,
) -> tuple[list[str], dict, list[str]]:
    """Blocking file work — runs in a thread. Returns (exported_ids, counts, warnings)."""
    warnings: list[str] = []
    exported_ids: list[str] = []
    counts = {name: 0 for name, _ in split_plan}
    used_stems: set[str] = set()
    total = sum(len(items) for _, items in split_plan)
    done = 0
    for split_name, _ in split_plan:           # YOLO wants every listed split folder to exist
        for sub in ("images", "labels"):
            writer.add_dir(f"{split_name}/{sub}")

    for split_name, items in split_plan:
        for img in items:
            done += 1
            src = Path(img["filepath"])
            if not src.exists():
                warnings.append(f"File not found on disk: {img['filename']} ({src})")
                progress(done, total)
                continue

            # Unique stem per dataset (a.jpg and a.png would otherwise share a.txt)
            stem, ext = Path(img["filename"]).stem, Path(img["filename"]).suffix
            if stem in used_stems:
                stem = f"{stem}_{img['id'][:8]}"
            used_stems.add(stem)
            writer.add_image(src, f"{split_name}/images/{stem}{ext}")
            lines = yolo_label_lines(labels.get(img["id"], []), classes, img["width"], img["height"],
                                     img["filename"], warnings)
            writer.add_text(f"{split_name}/labels/{stem}.txt", "\n".join(lines))

            exported_ids.append(img["id"])
            counts[split_name] += 1
            progress(done, total)
    return exported_ids, counts, warnings


async def prepare_export(project_id: str, body: ExportRequest, db: aiosqlite.Connection):
    """Validate ratios, pick images, split them and load their boxes. Shared with cloud bundles."""
    total_r = body.train_ratio + body.valid_ratio + (body.test_ratio if body.include_test else 0)
    if abs(total_r - 1.0) > 0.01:
        raise HTTPException(status_code=400, detail=f"Ratios must sum to 100% (got {total_r * 100:.0f}%)")

    proj = await get_project_or_404(project_id, db)
    images = await _exportable_images(project_id, db, body.only_approved)
    if not images:
        detail = "No approved images in the dataset yet" if body.only_approved else "No images ready for export in this project"
        raise HTTPException(status_code=400, detail=detail)

    seed = body.seed if body.seed is not None else random.randrange(1_000_000)
    parts = plan_split(
        images, _ratios(body.train_ratio, body.valid_ratio, body.test_ratio, body.include_test),
        body.group_sequences, seed,
    )
    split_plan = [("train", parts[0]), ("valid", parts[1])]
    if body.include_test:
        split_plan.append(("test", parts[2]))

    labels: dict[str, list[dict]] = {}
    for batch in chunks([i["id"] for i in images]):
        cur = await db.execute(
            f"SELECT image_id, class_name, x, y, w, h FROM annotations WHERE image_id IN ({placeholders(len(batch))})",
            batch,
        )
        for r in await cur.fetchall():
            labels.setdefault(r["image_id"], []).append(dict(r))
    return proj, split_plan, labels, seed


def manifest(project_id: str, proj: dict, body: ExportRequest, seed: int, counts: dict, n: int) -> dict:
    return {
        "project_id": project_id, "project_name": proj["name"], "classes": proj["classes"],
        "exported_at": datetime.now(timezone.utc).isoformat(), "seed": seed,
        "group_sequences": body.group_sequences, "only_approved": body.only_approved,
        "counts": counts, "images": n,
    }


async def mark_exported(db: aiosqlite.Connection, project_id: str, exported_ids: list[str], summary: str) -> None:
    changes: list[dict] = []
    for batch in chunks(exported_ids):
        cur = await db.execute(
            f"UPDATE images SET state='exported' WHERE state != 'exported' "
            f"AND id IN ({placeholders(len(batch))}) RETURNING id, assigned_to",
            batch,
        )
        changes.extend(
            {"image_id": r["id"], "new_state": "exported", "assigned_to": r["assigned_to"]}
            for r in await cur.fetchall()
        )
    await db.commit()
    await emit_state_change_bulk(changes, project_id, summary)
    await emit_stats(await project_stats(project_id, db), project_id)


@router.post("/projects/{project_id}/export", response_model=ExportResult)
async def run_export(
    project_id: str,
    body: ExportRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    if _export_lock.locked():
        raise HTTPException(status_code=409, detail="Another export is running — try again when it finishes")
    async with _export_lock:
        return await _run_export(project_id, body, db)


async def _run_export(project_id: str, body: ExportRequest, db: aiosqlite.Connection) -> ExportResult:
    proj, split_plan, labels, seed = await prepare_export(project_id, body, db)
    classes: List[str] = proj["classes"]

    dataset_dir = _next_dataset_dir(proj["name"])
    loop = asyncio.get_running_loop()
    last_pct = [-1]

    def progress(done: int, total: int):
        pct = done * 100 // total if total else 100
        if pct != last_pct[0] or done == total:
            last_pct[0] = pct
            asyncio.run_coroutine_threadsafe(emit_export_progress(done, total, project_id), loop)

    writer = FolderWriter(dataset_dir)
    exported_ids, counts, warnings = await asyncio.to_thread(
        write_dataset, writer, split_plan, labels, classes, progress
    )
    writer.add_text("data.yaml", build_data_yaml(classes, body.include_test, dataset_dir.as_posix()))
    writer.add_text("manifest.json", json.dumps(
        {**manifest(project_id, proj, body, seed, counts, len(exported_ids)),
         "hard_linked_images": writer.linked}, indent=2))
    await mark_exported(db, project_id, exported_ids, f"Exported {len(exported_ids)} images")

    return ExportResult(
        exported=len(exported_ids),
        train=counts.get("train", 0),
        valid=counts.get("valid", 0),
        test=counts.get("test", 0),
        orphan_warnings=warnings,
        output_path=str(dataset_dir),
        seed=seed,
        linked=writer.linked,
    )

