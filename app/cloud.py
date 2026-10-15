"""
Cloud training — train on a free Kaggle / Colab GPU, bring the model back.

  POST   /api/projects/{pid}/cloud-bundle   build a .zip (dataset + train.py + notebook + README) as a job
  GET    /api/cloud/bundles                 bundles built so far
  GET    /api/cloud/bundles/{name}          download one (media cookie or Bearer)
  DELETE /api/cloud/bundles/{name}
  POST   /api/train/import-result           upload the invictus_result_*.zip (or a bare .pt) from the cloud run

The bundle is written straight into a zip (no extra copy of the dataset on disk). The
imported run lands in models/<run>/ and in the run history (with per-class accuracy), so it
can be used for pre-labelling right away — the full annotate → train → pre-label loop
without a local GPU.
"""
import asyncio
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path


import aiosqlite
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import get_admin_header_or_query, get_admin_user
from app.cloud_templates import render_notebook, render_readme, render_script
from app.config import BUNDLES_DIR, DATA_DIR, MODELS_DIR
from app.database import connect, get_db, new_id, now_iso
from app.export import (
    ZipWriter, _exportable_images, _safe_dir_name, build_data_yaml, manifest, mark_exported, prepare_export,
    write_dataset,
)
from app.jobs import JobCancelled, JobContext, job_kind, submit
from app.models import ExportRequest
from app.common import get_project_or_404

router = APIRouter(tags=["cloud"])

DISK_RESERVE = 1024 ** 3
_RESULT_FILES = re.compile(r"^(invictus_result\.json|results\.csv|args\.yaml|weights/(best|last)\.pt|"
                           r"weights/[\w.-]+\.onnx|plots/[\w.-]+\.(png|jpg))$")
MAX_RESULT_BYTES = 4 * 1024 ** 3


def _ultralytics_version() -> str:
    from importlib import metadata
    try:
        return metadata.version("ultralytics")
    except metadata.PackageNotFoundError:
        return "8.4.30"


class CloudTrainConfig(BaseModel):
    model: str = Field("yolo11s.pt", pattern=r"^(yolo|rtdetr)[\w.-]*\.pt$")
    epochs: int = Field(100, ge=1, le=3000)
    imgsz: int = Field(640, ge=64, le=2048)
    batch: int = Field(-1, ge=-1, le=512)        # -1 = AutoBatch: largest batch that fits the cloud GPU
    patience: int = Field(50, ge=0, le=3000)
    optimizer: str = Field("auto", pattern=r"^(auto|SGD|Adam|AdamW|NAdam|RAdam|RMSProp)$")
    lr0: float = Field(0.01, gt=0, le=1)
    cos_lr: bool = False
    save_period: int = Field(10, ge=-1, le=1000)  # also checkpoint every N epochs (Colab resumes from these)


class CloudBundleRequest(ExportRequest):
    train: CloudTrainConfig = CloudTrainConfig()
    mark_exported: bool = True


@router.post("/api/projects/{project_id}/cloud-bundle")
async def create_bundle(
    project_id: str,
    body: CloudBundleRequest,
    db: aiosqlite.Connection = Depends(get_db),
    admin: dict = Depends(get_admin_user),
):
    total_r = body.train_ratio + body.valid_ratio + (body.test_ratio if body.include_test else 0)
    if abs(total_r - 1.0) > 0.01:
        raise HTTPException(status_code=400, detail=f"Ratios must sum to 100% (got {total_r * 100:.0f}%)")
    await get_project_or_404(project_id, db)
    if not await _exportable_images(project_id, db, body.only_approved):
        raise HTTPException(status_code=400, detail="No images in the dataset yet — add annotated frames first")
    return await submit("cloud_bundle", body.model_dump(), admin, project_id)


@job_kind("cloud_bundle")
async def run_bundle(ctx: JobContext) -> dict:
    body = CloudBundleRequest(**ctx.params)
    async with connect() as db:
        proj, split_plan, labels, seed = await prepare_export(ctx.project_id, body, db)
    classes = proj["classes"]
    images = [img for _, items in split_plan for img in items]
    need = sum(os.stat(i["filepath"]).st_size for i in images if os.path.exists(i["filepath"]))
    BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(BUNDLES_DIR).free
    if need > free - DISK_RESERVE:
        raise RuntimeError(f"Not enough disk space for the bundle: ~{need / 1024 ** 3:.1f} GB needed, "
                           f"{max(0, free - DISK_RESERVE) / 1024 ** 3:.1f} GB free (1 GB kept free)")

    bundle_id = uuid.uuid4().hex[:8]
    safe = _safe_dir_name(proj["name"]).replace(" ", "_")[:40]
    train = {**body.train.model_dump(), "seed": seed,
             "run_name": f"{safe}_{Path(body.train.model).stem}_cloud_{bundle_id}"}
    path = BUNDLES_DIR / f"invictus_bundle_{safe}_{bundle_id}.zip"
    tmp = path.with_suffix(".part")
    loop = asyncio.get_running_loop()

    def progress(done: int, total: int):
        if ctx.cancelled:
            raise JobCancelled()
        asyncio.run_coroutine_threadsafe(ctx.progress(done / max(1, total) * 95, f"{done} / {total} images"), loop)

    def build() -> tuple[list[str], dict, list[str]]:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
            writer = ZipWriter(zf, prefix="dataset/")
            exported_ids, counts, warnings = write_dataset(writer, split_plan, labels, classes, progress)
            writer.add_text("data.yaml", build_data_yaml(classes, body.include_test))
            bundle = {"id": bundle_id, "project": proj["name"], "project_id": ctx.project_id, "classes": classes,
                      "images": len(exported_ids), "counts": counts, "created_at": now_iso(),
                      "ultralytics_version": _ultralytics_version(),
                      "export": manifest(ctx.project_id, proj, body, seed, counts, len(exported_ids))}
            zf.writestr("invictus_bundle.json", json.dumps(bundle, indent=2))
            zf.writestr("train.py", render_script(bundle, train))
            zf.writestr("train.ipynb", render_notebook(bundle, train))
            zf.writestr("README.md", render_readme(bundle, train))
        return exported_ids, counts, warnings

    try:
        exported_ids, counts, warnings = await asyncio.to_thread(build)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    if body.mark_exported:
        async with connect() as db:
            await mark_exported(db, ctx.project_id, exported_ids, f"Packed {len(exported_ids)} images for cloud training")
    size = path.stat().st_size
    return {"file": path.name, "bytes": size, "bundle_id": bundle_id, "images": len(exported_ids), "counts": counts,
            "warnings": warnings[:50], "run_name": train["run_name"],
            "summary": f"Bundle ready: {len(exported_ids)} images, {size / 1024 ** 2:.1f} MB"}


# ── Bundle files ──────────────────────────────────────────────────────────────

def _bundle_path(name: str) -> Path:
    p = (BUNDLES_DIR / name).resolve()
    if p.parent != BUNDLES_DIR.resolve() or p.suffix != ".zip" or not p.is_file():
        raise HTTPException(status_code=404, detail="Bundle not found")
    return p


@router.get("/api/cloud/bundles")
async def list_bundles(_admin: dict = Depends(get_admin_user)):
    out = []
    for f in sorted(BUNDLES_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True) if BUNDLES_DIR.exists() else []:
        st = f.stat()
        out.append({"name": f.name, "bytes": st.st_size, "created": datetime.fromtimestamp(st.st_mtime).isoformat()})
    return {"bundles": out, "folder": str(BUNDLES_DIR)}


@router.get("/api/cloud/bundles/{name}")
async def download_bundle(name: str, _admin: dict = Depends(get_admin_header_or_query)):
    p = _bundle_path(name)
    return FileResponse(p, filename=p.name, media_type="application/zip")


@router.delete("/api/cloud/bundles/{name}")
async def delete_bundle(name: str, _admin: dict = Depends(get_admin_user)):
    _bundle_path(name).unlink()
    return {"detail": "Bundle deleted"}


# ── Importing the result ──────────────────────────────────────────────────────

def _unique_run_name(base: str) -> str:
    base = re.sub(r"[^\w\-]", "_", base)[:80] or "cloud_run"
    name, n = base, 1
    while (MODELS_DIR / name).exists():
        n += 1
        name = f"{base}_{n}"
    return name


def _extract_result(upload: Path, original_name: str) -> dict:
    """Validate and unpack a result zip (or a bare .pt) into models/<run>/. Blocking."""
    if original_name.lower().endswith(".pt"):
        name = _unique_run_name(f"imported_{Path(original_name).stem}_{datetime.now():%Y%m%d_%H%M%S}")
        dest = MODELS_DIR / name
        dest.mkdir(parents=True)
        shutil.move(str(upload), dest / "best.pt")
        return {"run_name": name, "result": {}, "files": ["best.pt"]}

    if not zipfile.is_zipfile(upload):
        raise HTTPException(status_code=400, detail="Upload the invictus_result_….zip from the cloud run (or a .pt file)")
    with zipfile.ZipFile(upload) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        names = {m.filename for m in members}
        if "invictus_result.json" not in names or "weights/best.pt" not in names:
            raise HTTPException(status_code=400, detail="This zip is not an Invictus training result (no weights/best.pt)")
        if sum(m.file_size for m in members) > MAX_RESULT_BYTES or len(members) > 500:
            raise HTTPException(status_code=400, detail="Result zip is too large")
        result = json.loads(zf.read("invictus_result.json"))
        name = _unique_run_name(result.get("run_name") or Path(original_name).stem)
        dest = MODELS_DIR / name
        dest.mkdir(parents=True)
        kept = []
        for m in members:
            if not _RESULT_FILES.match(m.filename):      # also rejects absolute paths and ".."
                continue
            target = dest / (Path(m.filename).name if m.filename.startswith("weights/") else m.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            kept.append(target.relative_to(dest).as_posix())
    return {"run_name": name, "result": result, "files": kept}


@router.post("/api/train/import-result")
async def import_result(file: UploadFile = File(...), _admin: dict = Depends(get_admin_user)):
    """Bring a cloud-trained model back: it joins the run history and the model library."""
    tmp_dir = DATA_DIR / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}.upload"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_RESULT_BYTES:
                    raise HTTPException(status_code=400, detail="File is too large")
                out.write(chunk)
        info = await asyncio.to_thread(_extract_result, tmp, file.filename or "result.zip")
    finally:
        tmp.unlink(missing_ok=True)

    r = info["result"]
    metrics = {**(r.get("metrics") or {}), "per_class": r.get("per_class") or {}}
    if r.get("test"):
        metrics["test"] = {k: v for k, v in r["test"].items() if k != "per_class"}
    run_id = new_id()
    async with connect() as db:
        await db.execute(
            "INSERT INTO training_runs (id, name, source, status, model, dataset, config, metrics, epochs_done, "
            "total_epochs, started_at, finished_at) VALUES (?, ?, 'cloud', 'imported', ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, info["run_name"], r.get("model"), r.get("project"),
             json.dumps({**(r.get("train_config") or {}), "environment": r.get("environment"),
                         "device": r.get("device"), "train_seconds": r.get("train_seconds"),
                         "bundle_id": r.get("bundle_id"), "classes": r.get("classes")}),
             json.dumps(metrics), r.get("epochs_done") or 0, (r.get("train_config") or {}).get("epochs"),
             now_iso(), now_iso()),
        )
        await db.commit()
    return {"run_id": run_id, "run_name": info["run_name"], "files": info["files"],
            "model": f"{info['run_name']}/best.pt", "metrics": metrics}
