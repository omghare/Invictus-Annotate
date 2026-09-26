"""
Export — per-project YOLO dataset generation with data.yaml.
Each project exports to dataset/{project_id}/ to prevent collisions.
"""
import json
import math
import random
import shutil
from pathlib import Path
from typing import List, Tuple

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_admin_user
from app.config import DATASET_DIR
from app.database import get_db, now_iso
from app.models import ExportPreview, ExportRequest, ExportResult
from app.socket_manager import emit_export_progress, emit_stats, emit_state_change

router = APIRouter(tags=["export"])


def _split_list(items: list, train_r: float, valid_r: float, test_r: float, include_test: bool) -> Tuple[list, list, list]:
    """Randomly split items according to ratios."""
    lst = list(items)
    random.shuffle(lst)
    n = len(lst)

    if not include_test or test_r == 0:
        split_valid = math.ceil(n * valid_r)
        valid_part  = lst[:split_valid]
        train_part  = lst[split_valid:]
        return train_part, valid_part, []
    else:
        split_test  = math.ceil(n * test_r)
        split_valid = math.ceil(n * valid_r)
        test_part   = lst[:split_test]
        valid_part  = lst[split_test: split_test + split_valid]
        train_part  = lst[split_test + split_valid:]
        return train_part, valid_part, test_part


async def _get_stats_dict(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute(
        "SELECT state, COUNT(*) AS cnt FROM images WHERE project_id=? GROUP BY state",
        (project_id,),
    )
    rows = await cur.fetchall()
    counts = {r["state"]: r["cnt"] for r in rows}
    total_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM images WHERE project_id=?", (project_id,)
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


async def _get_project_or_404(project_id: str, db: aiosqlite.Connection) -> dict:
    cur = await db.execute("SELECT * FROM project WHERE id = ?", (project_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


@router.get("/projects/{project_id}/export/preview", response_model=ExportPreview)
async def export_preview(
    project_id: str,
    train_ratio: float = 0.8,
    valid_ratio: float = 0.1,
    test_ratio:  float = 0.1,
    include_test: bool = True,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    await _get_project_or_404(project_id, db)

    cur = await db.execute(
        "SELECT filepath, filename FROM images WHERE project_id=? AND state IN ('ready_for_export', 'exported')",
        (project_id,),
    )
    rows = await cur.fetchall()
    seen = set()
    for r in rows:
        norm_p = str(Path(r["filepath"]).resolve()) if r["filepath"] else r["filename"]
        seen.add(norm_p)
    total = len(seen)

    train_lst, valid_lst, test_lst = _split_list(
        list(range(total)), train_ratio, valid_ratio, test_ratio, include_test
    )
    return ExportPreview(
        total_ready=total,
        train_count=len(train_lst),
        valid_count=len(valid_lst),
        test_count=len(test_lst),
    )


@router.post("/projects/{project_id}/export", response_model=ExportResult)
async def run_export(
    project_id: str,
    body: ExportRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    # Validate ratios
    total_r = body.train_ratio + body.valid_ratio + (body.test_ratio if body.include_test else 0)
    if abs(total_r - 1.0) > 0.01:
        raise HTTPException(status_code=400, detail=f"Ratios must sum to 1.0 (got {total_r:.3f})")

    proj = await _get_project_or_404(project_id, db)
    classes: List[str] = json.loads(proj["classes"])

    # Sanitise project name to be a safe directory name
    safe_name = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in proj["name"]).strip()
    base_folder_name = f"{safe_name}_dataset"
    project_dataset = DATASET_DIR / base_folder_name

    # Check for naming collisions and append (1), (2), etc.
    if project_dataset.exists():
        counter = 1
        while True:
            candidate_name = f"{base_folder_name} ({counter})"
            candidate_dir = DATASET_DIR / candidate_name
            if not candidate_dir.exists():
                project_dataset = candidate_dir
                break
            counter += 1

    # Get ready/exported images with deduplication safeguard
    cur = await db.execute("""
        SELECT i.id, i.filename, i.filepath, i.width, i.height
        FROM images i
        WHERE i.project_id=? AND i.state IN ('ready_for_export', 'exported')
        ORDER BY i.filename
    """, (project_id,))
    raw_images = [dict(r) for r in await cur.fetchall()]

    seen_paths = set()
    images = []
    for img in raw_images:
        norm_p = str(Path(img["filepath"]).resolve()) if img.get("filepath") else img["filename"]
        if norm_p not in seen_paths:
            seen_paths.add(norm_p)
            images.append(img)

    if not images:
        raise HTTPException(status_code=400, detail="No images ready for export in this project")

    # Split
    train_imgs, valid_imgs, test_imgs = _split_list(
        images, body.train_ratio, body.valid_ratio, body.test_ratio, body.include_test
    )

    split_map = [
        ("train", train_imgs),
        ("valid", valid_imgs),
        ("test",  test_imgs),
    ]

    # Ensure per-project dataset folder structure (clear and recreate each split)
    for split_name, _ in split_map:
        shutil.rmtree(project_dataset / split_name, ignore_errors=True)
        for sub in ["images", "labels"]:
            (project_dataset / split_name / sub).mkdir(parents=True, exist_ok=True)

    orphan_warnings: List[str] = []
    done = 0
    total_count = len(images)
    exported_ids: List[str] = []
    counts = {"train": 0, "valid": 0, "test": 0}
    used_dst_filenames = set()

    for split_name, split_imgs in split_map:
        for img in split_imgs:
            image_id = img["id"]
            fname    = img["filename"]
            src_path = Path(img["filepath"])

            # Resolve destination filename to prevent collision overwrites
            target_fname = fname
            if target_fname in used_dst_filenames:
                stem_base = Path(fname).stem
                ext_base = Path(fname).suffix
                target_fname = f"{stem_base}_{image_id[:8]}{ext_base}"
            used_dst_filenames.add(target_fname)

            target_stem = Path(target_fname).stem
            dst_img = project_dataset / split_name / "images" / target_fname

            if src_path.exists():
                shutil.copy2(src_path, dst_img)
            else:
                orphan_warnings.append(f"File not found on disk: {fname} ({src_path})")
                done += 1
                await emit_export_progress(done, total_count, project_id)
                continue

            # Fetch annotations
            cur = await db.execute(
                "SELECT * FROM annotations WHERE image_id=?", (image_id,)
            )
            ann_rows = await cur.fetchall()

            # Write YOLO label file
            dst_label = project_dataset / split_name / "labels" / f"{target_stem}.txt"
            lines = []
            img_w = max(1, img["width"])
            img_h = max(1, img["height"])

            for ann in ann_rows:
                ann = dict(ann)
                if ann["class_name"] not in classes:
                    orphan_warnings.append(
                        f"Skipped box in {fname}: class '{ann['class_name']}' not in project classes"
                    )
                    continue
                cid = classes.index(ann["class_name"])
                x_c = (ann["x"] + ann["w"] / 2) / img_w
                y_c = (ann["y"] + ann["h"] / 2) / img_h
                w_n = ann["w"] / img_w
                h_n = ann["h"] / img_h
                x_c = max(0.0, min(1.0, x_c))
                y_c = max(0.0, min(1.0, y_c))
                w_n = max(0.0, min(1.0, w_n))
                h_n = max(0.0, min(1.0, h_n))
                lines.append(f"{cid} {x_c:.6f} {y_c:.6f} {w_n:.6f} {h_n:.6f}")

            with open(dst_label, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            exported_ids.append(image_id)
            counts[split_name] += 1
            done += 1
            await emit_export_progress(done, total_count, project_id)

    # Mark exported images in DB in safe batches of 500
    if exported_ids:
        batch_size = 500
        for i in range(0, len(exported_ids), batch_size):
            batch = exported_ids[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            await db.execute(
                f"UPDATE images SET state='exported' WHERE id IN ({placeholders})",
                batch,
            )
        await db.commit()

    # Generate per-project data.yaml
    _write_data_yaml(classes, body.include_test, project_dataset)

    # Broadcast final stats
    stats = await _get_stats_dict(project_id, db)
    await emit_stats(stats, project_id)

    return ExportResult(
        exported=len(exported_ids),
        train=counts["train"],
        valid=counts["valid"],
        test=counts["test"],
        orphan_warnings=orphan_warnings,
        output_path=str(project_dataset),
    )


def _write_data_yaml(classes: List[str], include_test: bool, project_dataset: Path) -> None:
    """Writes a YOLO-compatible data.yaml to the project's dataset root."""
    lines = [
        f"path: {project_dataset.as_posix()}",
        "train: train/images",
        "val: valid/images",
    ]
    if include_test:
        lines.append("test: test/images")
    lines.append("")
    lines.append(f"nc: {len(classes)}")
    names_str = "[" + ", ".join(f"'{c}'" for c in classes) + "]"
    lines.append(f"names: {names_str}")

    yaml_path = project_dataset / "data.yaml"
    yaml_path.write_text("\n".join(lines), encoding="utf-8")
