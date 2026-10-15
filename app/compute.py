"""
Compute profile — what this machine can run, worked out once and shown on the System page.

Everything that picks a device (training, model assist) asks here, so moving the project to
a machine with an NVIDIA GPU needs no code or setting change: install the CUDA build of
PyTorch (see INSTALL.md / requirements-gpu.txt) and the GPU is picked up automatically.

Providers are the pluggable inference back-ends behind "Model assist". Each declares what it
needs; app/infer_worker.py holds the implementations. Adding one later — e.g. SAM 2 for
click-to-segment or Grounding DINO for text prompts on a GPU machine — means one entry in
PROVIDERS plus one function in the worker; the job system, storage and UI plumbing stay as
they are. Weights for such providers belong in MODEL_CACHE_DIR, which can live on any drive.
"""
import asyncio
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil
from fastapi import APIRouter, Depends

from app.auth import get_admin_user
from app.config import (
    BACKUPS_DIR, BUNDLES_DIR, DATA_DIR, DATASET_DIR, EXTRACTED_FRAMES_DIR, MODEL_CACHE_DIR, MODELS_DIR,
    THUMBS_DIR, TRAINING_RUNS_DIR,
)

router = APIRouter(tags=["system"])


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    task: str                  # detect | segment | prompt_detect | track | embed
    needs_cuda: bool = False
    min_vram_gb: float = 0.0


PROVIDERS = [
    Provider("yolo", "Your trained YOLO models (detection)", "detect"),
]


_caps: dict[str, Any] = {}


def capabilities() -> dict:
    """torch build, CUDA, GPU and CPU facts (the torch probe runs once, in a subprocess)."""
    if not _caps:
        from app.trainer import _gpu_static_info, _runtime_info
        rt = _runtime_info()
        cuda = bool(rt.get("cuda_available"))
        gpu = _gpu_static_info() if cuda else {}
        caps = {
            "torch_version": rt.get("torch_version"),
            "ultralytics_version": rt.get("ultralytics_version"),
            "torch_build": ("cuda" if "+cu" in str(rt.get("torch_version")) or cuda else "cpu")
                           if rt.get("torch_version") else None,
            "cuda_available": cuda,
            "gpu_name": gpu.get("gpu_name"),
            "vram_gb": round(gpu.get("vram_total_mb", 0) / 1024, 1),
            "cpu_cores": psutil.cpu_count(logical=False) or 1,
            "cpu_threads": psutil.cpu_count(logical=True) or 1,
            "ram_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1),
            "python": sys.version.split()[0],
        }
        if rt.get("torch_version"):               # don't cache a failed probe
            _caps.update(caps)
        return caps
    return dict(_caps)


def default_device() -> str:
    """'0' (first GPU) when CUDA works, else 'cpu'. Workers also accept 'auto'."""
    return "0" if capabilities().get("cuda_available") else "cpu"


def provider_status(p: Provider, caps: dict) -> str:
    if p.needs_cuda and not caps.get("cuda_available"):
        return "needs_gpu"
    if p.min_vram_gb and caps.get("vram_gb", 0) < p.min_vram_gb:
        return "needs_gpu"
    return "ready"


def _folder_size(path: Path, max_files: int = 300_000) -> int:
    total, seen = 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
            seen += 1
        if seen > max_files:              # a huge tree: report what we counted
            break
    return total


def storage_report() -> dict:
    """Sizes of the folders the app writes to, plus free space per drive. Blocking."""
    folders = {
        "Extracted video frames": EXTRACTED_FRAMES_DIR,
        "Exports (dataset/)": DATASET_DIR,
        "Cloud bundles": BUNDLES_DIR,
        "Trained models (models/)": MODELS_DIR,
        "Training runs": TRAINING_RUNS_DIR,
        "Base weights cache": MODEL_CACHE_DIR,
        "Thumbnails": THUMBS_DIR,
        "Database backups": BACKUPS_DIR,
    }
    out = []
    for label, path in folders.items():
        out.append({"label": label, "path": str(path), "bytes": _folder_size(path) if path.exists() else 0})
    # dataset/ contains frames + bundles; report exports net of those
    by = {o["label"]: o for o in out}
    by["Exports (dataset/)"]["bytes"] = max(0, by["Exports (dataset/)"]["bytes"]
                                           - by["Extracted video frames"]["bytes"] - by["Cloud bundles"]["bytes"])
    drives = {}
    for p in (DATA_DIR, DATASET_DIR, MODELS_DIR, MODEL_CACHE_DIR):
        anchor = Path(p).resolve().anchor or "/"
        if anchor not in drives:
            u = shutil.disk_usage(p)
            drives[anchor] = {"drive": anchor, "free": u.free, "total": u.total}
    return {"folders": out, "drives": list(drives.values()),
            "note": "Exported images are hard links to the originals when on the same drive, "
                    "so their size above can be counted twice although it is stored once."}


@router.get("/api/system")
async def system_info(_admin: dict = Depends(get_admin_user)):
    caps = await asyncio.to_thread(capabilities)
    storage = await asyncio.to_thread(storage_report)
    return {
        "compute": {**caps, "default_device": "0" if caps.get("cuda_available") else "cpu"},
        "providers": [{**asdict(p), "status": provider_status(p, caps)} for p in PROVIDERS],
        "storage": storage,
        "paths": {"models": str(MODELS_DIR), "model_cache": str(MODEL_CACHE_DIR), "runs": str(TRAINING_RUNS_DIR)},
    }
