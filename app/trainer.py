"""
trainer.py — YOLO model training orchestration.

Endpoints:
  GET  /api/train/system-info      — GPU/CUDA/Ultralytics/CPU one-time info
  GET  /api/train/system-stats     — Real-time CPU/RAM/GPU/VRAM usage
  GET  /api/train/pick-dataset     — Open native folder picker for dataset
  POST /api/train/validate-dataset — Validate a dataset folder path
  POST /api/train/start            — Start training session
  POST /api/train/stop             — Stop running training session
  GET  /api/train/status           — Current session state + history (for reload/reconnect)
  GET  /api/train/models           — List saved .pt files in models/
  GET  /api/train/models/{name}    — Download a specific .pt file (media cookie or Bearer)
  GET  /api/train/runs             — Every run (local and imported cloud runs) with metrics
  POST /api/train/runs/{id}/resume — Continue a stopped / interrupted run from last.pt
  DELETE /api/train/runs/{id}      — Delete a run and its weights

Training runs in a subprocess (sys.executable → train_worker.py). Its stdout is read in
chunks (the progress bar writes '\\r' without newlines) and forwarded over Socket.IO.
torch is never imported in this web process, so the server holds no CUDA context.

The worker gets a cleaned copy of data.yaml with absolute paths (no `download:` key) and
may only load known base weights or .pt files inside models/ / training_runs/ (a .pt file
is a pickle — loading one runs code). Finished weights are hard-linked into models/<run>/
so a run costs its disk space once.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

import psutil
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import get_admin_header_or_query, get_admin_user
from app.config import DATA_DIR, MODEL_CACHE_DIR, MODELS_DIR, TRAINING_RUNS_DIR
from app.database import connect, new_id, now_iso
from app.socket_manager import (
    emit_train_comment,
    emit_train_complete,
    emit_train_error,
    emit_train_epoch,
    emit_train_hardware,
    emit_train_log,
    emit_train_status,
)

router = APIRouter(tags=["trainer"])

WORKER_SCRIPT = Path(__file__).parent / "train_worker.py"

# ── Image extensions recognised as training data ───────────────────────────────
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

TERMINAL_STATES = {"idle", "complete", "error", "stopped"}


# ── In-memory training session ────────────────────────────────────────────────

class _Session:
    def __init__(self):
        self.session_id: Optional[str] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.config: Optional[dict] = None
        self.status: str = "idle"      # idle | running | stopping | complete | error | stopped
        self.epoch: int = 0
        self.total_epochs: int = 0
        self.epoch_history: list = []
        self.log_tail: deque = deque(maxlen=500)
        self.final_result: Optional[dict] = None
        self.error: Optional[str] = None
        self.stop_file: Optional[Path] = None
        self.run_id: Optional[str] = None
        self.run_name: Optional[str] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._stop_task: Optional[asyncio.Task] = None


_session = _Session()
_session_lock = asyncio.Lock()


# ── Pydantic models ───────────────────────────────────────────────────────────

class ValidateDatasetRequest(BaseModel):
    path: str

class StartTrainingRequest(BaseModel):
    dataset_path: str = ""
    yaml_path: str
    model: str                        # base weights ("yolo11n.pt") or a .pt inside models/
    dataset_name: str                 # used for naming the output .pt
    run_name_override: Optional[str] = None  # optional custom run name from user
    epochs: int = Field(100, ge=1, le=3000)
    batch: int = Field(16, ge=-1, le=512)         # -1 = AutoBatch (GPU)
    imgsz: int = Field(640, ge=32, le=4096)
    patience: int = Field(50, ge=0, le=3000)
    lr0: float = Field(0.01, gt=0, le=1)
    optimizer: Literal["SGD", "Adam", "AdamW", "NAdam", "RAdam", "RMSProp", "auto"] = "SGD"
    workers: int = Field(2, ge=0, le=32)
    device: str = Field("auto", max_length=32)    # auto = first GPU when CUDA works, else cpu
    cos_lr: bool = False
    warmup_epochs: float = Field(3.0, ge=0, le=50)
    weight_decay: float = Field(0.0005, ge=0, le=0.1)
    mosaic: float = Field(1.0, ge=0, le=1)
    memory_budget_gb: Optional[float] = Field(None, gt=0, le=1024)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_images(folder: Path) -> int:
    if not folder.exists() or not folder.is_dir():
        return 0
    return sum(1 for f in folder.iterdir() if f.suffix.lower() in _IMG_EXTS)


def _find_yaml(folder: Path) -> Optional[Path]:
    for ext in ("*.yaml", "*.yml"):
        found = list(folder.glob(ext))
        if found:
            # Prefer data.yaml if present
            for f in found:
                if f.stem.lower() == "data":
                    return f
            return found[0]
    return None


def _folder_size_mb(folder: Path, max_files: int = 200_000) -> float:
    """Recursively compute folder size in MB (stops after max_files so a drive root can't hang it)."""
    total = 0
    seen = 0
    try:
        for f in folder.rglob("*"):
            if f.is_file():
                seen += 1
                if seen > max_files:
                    break
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return round(total / (1024 ** 2), 1)


def _parse_yaml_info(yaml_path: Path) -> dict:
    """Extract nc and names from data.yaml without requiring PyYAML."""
    result: dict[str, Any] = {"num_classes": None, "class_names": []}
    if not yaml_path or not yaml_path.exists():
        return result
    try:
        # Try PyYAML first
        import yaml  # type: ignore
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            nc = data.get("nc", data.get("NC"))
            names = data.get("names", [])
            result["num_classes"] = int(nc) if nc is not None else None
            if isinstance(names, list):
                result["class_names"] = [str(n) for n in names]
            elif isinstance(names, dict):
                # names may be {0: 'cat', 1: 'dog'}
                result["class_names"] = [str(names[k]) for k in sorted(names.keys())]
    except ImportError:
        # Fallback: simple regex parse
        try:
            text = yaml_path.read_text(encoding="utf-8", errors="replace")
            nc_match = re.search(r"^\s*nc\s*:\s*(\d+)", text, re.MULTILINE)
            if nc_match:
                result["num_classes"] = int(nc_match.group(1))
            # names: [a, b, c] or names:\n  - a\n  - b
            names_match = re.search(r"names\s*:\s*\[([^\]]+)\]", text)
            if names_match:
                raw = names_match.group(1)
                result["class_names"] = [n.strip().strip("'\"")
                                         for n in raw.split(",") if n.strip()]
            else:
                # YAML list form
                names_block = re.findall(r"^\s*-\s+(.+)", text, re.MULTILINE)
                if names_block:
                    result["class_names"] = [n.strip() for n in names_block]
        except Exception:
            pass
    except Exception:
        pass
    return result


def _recommend_params(dataset: dict, sysinfo: dict) -> dict:
    """
    Pure rule-based recommendation engine.
    dataset keys: total_images, num_classes, cuda_available, vram_total_gb,
                  ram_available_gb, cpu_count_physical, memory_budget_gb
    Returns recommended training config + per-field rationale.
    """
    total   = dataset.get("total_images", 0) or 0
    nc      = dataset.get("num_classes") or 1
    cuda    = dataset.get("cuda_available", False)
    vram    = dataset.get("vram_total_gb", 0) or 0
    ram_avail = dataset.get("ram_available_gb", 8) or 8
    cpu_phy = dataset.get("cpu_count_physical", 2) or 2
    budget  = dataset.get("memory_budget_gb")  # may be None
    chosen  = dataset.get("model")             # the model the user picked (batch is sized for it)

    # Effective memory: if GPU, use VRAM; else RAM
    eff_mem = vram if cuda else ram_avail
    if budget is not None:
        eff_mem = min(budget, eff_mem)

    # ── Model selection ──────────────────────────────────────────────
    if cuda:
        if total < 500:
            model, model_reason = "yolo11n.pt", "Small dataset → nano model avoids overfitting."
        elif total < 3000:
            model, model_reason = "yolo11s.pt", "Medium dataset → small model balances speed & accuracy."
        elif total < 10000:
            if vram >= 4:
                model, model_reason = "yolo11m.pt", "Large dataset with sufficient VRAM → medium model recommended."
            else:
                model, model_reason = "yolo11s.pt", "Large dataset but limited VRAM → small model is safer."
        else:
            if vram >= 8:
                model, model_reason = "yolo11l.pt", "Very large dataset + ample VRAM → large model for max accuracy."
            elif vram >= 4:
                model, model_reason = "yolo11m.pt", "Very large dataset + moderate VRAM → medium model."
            else:
                model, model_reason = "yolo11s.pt", "Very large dataset but limited VRAM → small model."
    else:
        # CPU mode — always nano or small
        if total < 2000:
            model, model_reason = "yolo11n.pt", "CPU mode + small dataset → nano model is fastest."
        else:
            model, model_reason = "yolo11s.pt", "CPU mode + larger dataset → small model for better accuracy."

    # ── Batch size ──────────────────────────────────────────────────
    # Very rough estimate: ~100-200 MB per batch item at 416px on CPU
    # For GPU: VRAM budget / ~300MB per item at 640px
    if cuda:
        # GPU batch estimation based on effective memory and model size
        model_mem_factor = {"n": 0.15, "s": 0.25, "m": 0.45, "l": 0.70, "x": 1.1}
        m = re.search(r"([nsmlx])(?:-\w+)?\.pt$", chosen or model)
        mfac = model_mem_factor.get(m.group(1) if m else "", 0.3)
        safe_batch = max(1, int((eff_mem - mfac) / 0.25))
        batch = min(64, max(4, (safe_batch // 4) * 4))  # round to multiple of 4
        batch_reason = f"Estimated from {eff_mem:.1f} GB VRAM budget."
    else:
        # CPU: keep small to not exhaust RAM
        if eff_mem >= 14:
            batch = 16
        elif eff_mem >= 8:
            batch = 8
        else:
            batch = 4
        batch_reason = f"CPU mode with {eff_mem:.1f} GB available RAM — conservative batch."

    # ── Image size ──────────────────────────────────────────────────
    if cuda and eff_mem >= 8:
        imgsz = 640
        imgsz_reason = "Sufficient VRAM → standard 640 px."
    elif cuda and eff_mem >= 4:
        imgsz = 640
        imgsz_reason = "Moderate VRAM → 640 px (reduce batch if OOM)."
    elif not cuda:
        imgsz = 416
        imgsz_reason = "CPU mode → 416 px balances speed and accuracy."
    else:
        imgsz = 416
        imgsz_reason = "Limited VRAM → 416 px to avoid OOM."

    # ── Epochs ──────────────────────────────────────────────────────
    if total < 200:
        epochs = 80
        ep_reason = "Very small dataset — few epochs prevent overfitting."
    elif total < 1000:
        epochs = 100
        ep_reason = "Small dataset — standard 100 epochs."
    elif total < 5000:
        epochs = 150
        ep_reason = "Medium dataset — 150 epochs for solid convergence."
    else:
        epochs = 200
        ep_reason = "Large dataset — more epochs for full convergence."
    # Adjust for many classes
    if nc > 20:
        epochs = int(epochs * 1.2)
        ep_reason += f" +20% for {nc} classes."

    # ── Patience ────────────────────────────────────────────────────
    patience = max(20, epochs // 3)
    patience_reason = "Early stopping at ~⅓ of total epochs."

    # ── Learning rate ───────────────────────────────────────────────
    if nc > 20:
        lr0 = 0.005
        lr_reason = "Many classes → lower LR for stable convergence."
    else:
        lr0 = 0.01
        lr_reason = "Standard LR for detection tasks."

    # ── Optimizer ───────────────────────────────────────────────────
    if nc > 20 or total > 5000:
        optimizer = "AdamW"
        opt_reason = "AdamW handles large class counts and datasets better."
    elif not cuda:
        optimizer = "SGD"
        opt_reason = "SGD is most efficient for CPU training."
    else:
        optimizer = "SGD"
        opt_reason = "SGD is reliable for standard detection tasks."

    # ── Workers ─────────────────────────────────────────────────────
    workers = min(cpu_phy, 8)
    if not cuda:
        workers = min(workers, 4)  # fewer workers on CPU to avoid contention
    workers_reason = f"Matched to {cpu_phy} physical CPU cores (capped at 8)."

    # ── Device ──────────────────────────────────────────────────────
    device = "0" if cuda else "cpu"
    device_reason = "GPU 0 detected." if cuda else "No GPU detected — CPU mode."

    return {
        "model":           model,
        "epochs":          epochs,
        "batch":           batch,
        "imgsz":           imgsz,
        "patience":        patience,
        "lr0":             lr0,
        "optimizer":       optimizer,
        "workers":         workers,
        "device":          device,
        "rationale": {
            "model":     model_reason,
            "epochs":    ep_reason,
            "batch":     batch_reason,
            "imgsz":     imgsz_reason,
            "patience":  patience_reason,
            "lr0":       lr_reason,
            "optimizer": opt_reason,
            "workers":   workers_reason,
            "device":    device_reason,
        },
    }



def _run_sync(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    """Run a command synchronously with no window (Windows safe)."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 2, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"


def _kill_tree(pid: int) -> None:
    """Kill a process and all its children (DataLoader workers)."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass


# ── GPU via NVML (no torch / CUDA context in the web process) ─────────────────

_nvml: dict[str, Any] = {"ok": None}


def _nvml_handle():
    if _nvml["ok"] is None:
        try:
            import pynvml
            pynvml.nvmlInit()
            _nvml.update(ok=True, mod=pynvml, handle=pynvml.nvmlDeviceGetHandleByIndex(0))
        except Exception:
            _nvml["ok"] = False
    return (_nvml["mod"], _nvml["handle"]) if _nvml["ok"] else (None, None)


def _gpu_static_info() -> dict:
    nv, h = _nvml_handle()
    if not nv:
        return {}
    info: dict[str, Any] = {}
    try:
        name = nv.nvmlDeviceGetName(h)
        info["gpu_name"] = name.decode() if isinstance(name, bytes) else name
        info["vram_total_mb"] = nv.nvmlDeviceGetMemoryInfo(h).total // (1024 ** 2)
        drv = nv.nvmlSystemGetDriverVersion()
        info["driver_version"] = drv.decode() if isinstance(drv, bytes) else drv
        cuda = nv.nvmlSystemGetCudaDriverVersion()
        info["cuda_driver_version"] = f"{cuda // 1000}.{(cuda % 1000) // 10}"
    except Exception:
        pass
    return info


_PROBE = (
    "import json\n"
    "out = {}\n"
    "try:\n"
    "    import torch\n"
    "    out['torch_version'] = torch.__version__\n"
    "    out['cuda_available'] = bool(torch.cuda.is_available())\n"
    "    out['cuda_version'] = torch.version.cuda or 'N/A'\n"
    "except Exception:\n"
    "    out['torch_version'] = None\n"
    "try:\n"
    "    import ultralytics\n"
    "    out['ultralytics_version'] = ultralytics.__version__\n"
    "except Exception:\n"
    "    out['ultralytics_version'] = None\n"
    "print(json.dumps(out))\n"
)
_runtime_cache: dict[str, Any] = {}


def _runtime_info() -> dict:
    """torch / CUDA / ultralytics versions, probed once in a throwaway subprocess."""
    if not _runtime_cache:
        rc, out, _ = _run_sync([sys.executable, "-c", _PROBE], timeout=90)
        try:
            info = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
        except (ValueError, IndexError):
            info = {}
        if info.get("torch_version"):          # a timed-out probe is retried next time, not cached
            _runtime_cache.update(info)
        return {"torch_version": None, **info}
    return dict(_runtime_cache)


# ── Performance commentary (rule-based, every 5 epochs) ──────────────────────

def _generate_commentary(history: list) -> Optional[str]:
    if len(history) < 5:
        return None

    recent = history[-5:]
    comments: list[str] = []

    maps = []
    for e in recent:
        m = e.get("metrics", {})
        v = m.get("metrics/mAP50(B)", m.get("mAP50", None))
        if v is not None:
            maps.append(float(v))

    if len(maps) >= 3:
        trend = maps[-1] - maps[0]
        if trend > 0.02:
            comments.append(f"mAP50 up +{trend:.3f} over the last 5 epochs.")
        elif trend < -0.015:
            comments.append("mAP50 dropped recently — possible overfitting; watch validation loss.")
        elif maps[-1] >= 0.75:
            comments.append(f"mAP50 stable at {maps[-1]:.3f} — converging well.")
        else:
            comments.append(f"mAP50 flat at {maps[-1]:.3f}. More epochs or LR tuning may help.")

    box_losses = []
    for e in recent:
        items = e.get("loss_items", [])
        if items and items[0] is not None:
            box_losses.append(float(items[0]))

    if len(box_losses) >= 3:
        loss_trend = box_losses[-1] - box_losses[0]
        if loss_trend < -0.05:
            comments.append("Box loss still falling — the model is learning.")
        elif loss_trend > 0.05:
            comments.append("Box loss increased — possible instability; consider a lower LR.")
        else:
            comments.append(f"Box loss levelling off at {box_losses[-1]:.4f}.")

    if not comments:
        return "Training progressing normally."
    return "  ".join(comments)


# ── Training stream reader ────────────────────────────────────────────────────

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


async def _log(session: _Session, line: str) -> None:
    session.log_tail.append(line)
    await emit_train_log(line)


async def _dispatch_line(session: _Session, line: str) -> None:
    line = _ANSI.sub("", line).rstrip()
    if not line:
        return
    if line.startswith('{"__event__"'):
        try:
            event = json.loads(line)
            etype = event.pop("__event__", "")
            try:
                await _handle_worker_event(session, etype, event)
            except Exception as exc:          # one bad event must never stop the reader
                await _log(session, f"[WARN] Could not handle '{etype}' event: {exc}")
            return
        except json.JSONDecodeError:
            pass
    await _log(session, line)


async def _stream_training(session: _Session):
    """
    Drain the worker's stdout in chunks. Newline-terminated lines are forwarded as log
    lines; carriage-return progress-bar redraws are coalesced to one line every 2 s.
    Reading never stops early, so the worker can never block on a full pipe.
    """
    import codecs
    proc = session.process
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    last_progress = 0.0
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += decoder.decode(chunk)
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                segments = [s for s in line.split("\r") if s.strip()]
                if segments:
                    await _dispatch_line(session, segments[-1])
            if "\r" in buf:
                segments = buf.split("\r")
                buf = segments[-1]
                latest = next((s for s in reversed(segments[:-1]) if s.strip()), None)
                if latest and time.monotonic() - last_progress > 2:
                    last_progress = time.monotonic()
                    await _dispatch_line(session, latest)
            if len(buf) > 262144:
                buf = buf[-65536:]
        if buf.strip():
            await _dispatch_line(session, buf.split("\r")[-1])
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        await _log(session, f"[WARN] Log reader error: {exc}")

    rc = await proc.wait()
    session.end_time = time.time()
    if session.status == "running":
        if rc == 0:
            session.status = "complete"
        else:
            session.status = "error"
            session.error = session.error or "Training process exited unexpectedly."
            await emit_train_error(session.error)
    elif session.status == "stopping":
        session.status = "stopped"
    if session._monitor_task and not session._monitor_task.done():
        session._monitor_task.cancel()
    if session.stop_file:
        session.stop_file.unlink(missing_ok=True)
    await _finalise_run(session)
    await emit_train_status(_status_payload(session, brief=True))


async def _finalise_run(session: _Session) -> None:
    """Link the run's weights into models/<run>/ and record how it ended."""
    weights = TRAINING_RUNS_DIR / (session.run_name or "") / "weights"
    dest = MODELS_DIR / (session.run_name or "")
    if session.run_name:
        for f in ("best.pt", "last.pt"):
            await asyncio.to_thread(link_or_copy, weights / f, dest / f)
    await _update_run(session, status=session.status, finished_at=now_iso(), error=session.error)


def _final_metrics(session: _Session) -> dict:
    if session.final_result and session.final_result.get("final_metrics"):
        return session.final_result["final_metrics"]
    if session.epoch_history:
        return session.epoch_history[-1].get("metrics", {})
    return {}


async def _update_run(session: _Session, **fields) -> None:
    if not session.run_id:
        return
    fields.setdefault("epochs_done", session.epoch)
    fields["metrics"] = json.dumps(_final_metrics(session))
    if session.epoch_history:
        fields["best_epoch"] = session.epoch_history[-1].get("best_epoch")
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with connect() as db:
        await db.execute(f"UPDATE training_runs SET {sets} WHERE id = ?", (*fields.values(), session.run_id))
        await db.commit()


async def _handle_worker_event(session: _Session, etype: str, data: dict):
    """Dispatch a structured event from the worker subprocess."""
    cfg = session.config or {}
    dest_dir = MODELS_DIR / cfg.get("name", "dataset_run")
    weights = TRAINING_RUNS_DIR / cfg.get("name", "dataset_run") / "weights"

    if etype == "started":
        await _log(session, f"[INFO] Training started — model: {data.get('model')}")

    elif etype == "log":
        await _log(session, data.get("line", ""))

    elif etype == "epoch":
        session.epoch = data["epoch"]
        session.total_epochs = data["epochs"]
        session.epoch_history.append(data)
        # Weights stay in training_runs/<run>/weights until the run ends (then linked into models/)
        await _update_run(session)

        await emit_train_epoch(data)
        await emit_train_status(_status_payload(session, brief=True))
        if session.epoch % 5 == 0:
            comment = _generate_commentary(session.epoch_history)
            if comment:
                await emit_train_comment(comment, session.epoch)

    elif etype == "complete":
        saved_rel = None
        if await asyncio.to_thread(link_or_copy, weights / "best.pt", dest_dir / "best.pt"):
            saved_rel = (dest_dir / "best.pt").relative_to(MODELS_DIR).as_posix()
            await _log(session, f"[INFO] Best weights saved → {dest_dir / 'best.pt'}")
        await asyncio.to_thread(link_or_copy, weights / "last.pt", dest_dir / "last.pt")
        session.status = "complete"
        session.final_result = {**data, "saved_model_path": str(dest_dir / "best.pt") if saved_rel else None,
                                "saved_model_rel": saved_rel}
        await emit_train_complete(session.final_result)
        await emit_train_status(_status_payload(session, brief=True))

    elif etype == "stopped":
        session.status = "stopped"
        await _log(session, f"[INFO] {data.get('message', 'Training stopped.')}")
        await emit_train_status(_status_payload(session, brief=True))

    elif etype == "error":
        session.status = "error"
        session.error = _ANSI.sub("", data.get("message", "Unknown error")).strip()
        await emit_train_error(session.error)
        await emit_train_status(_status_payload(session, brief=True))


async def _hardware_monitor(session: _Session):
    """Push hardware stats to training room every 2 s during active training."""
    while session.status in ("running", "stopping"):
        try:
            await emit_train_hardware(await asyncio.to_thread(_build_hardware_stats))
        except Exception:
            pass
        await asyncio.sleep(2)


def _build_hardware_stats() -> dict:
    """Collect CPU/RAM/GPU/VRAM stats (GPU via NVML only)."""
    mem = psutil.virtual_memory()
    stats: dict[str, Any] = {
        "cpu_percent":  round(psutil.cpu_percent(interval=None), 1),
        "ram_percent":  round(mem.percent, 1),
        "ram_used_gb":  round(mem.used / (1024 ** 3), 1),
        "ram_total_gb": round(mem.total / (1024 ** 3), 1),
        "gpu_available": False,
        "gpu_util": 0, "vram_used_mb": 0, "vram_total_mb": 0, "vram_percent": 0.0, "gpu_temp": 0,
    }
    nv, h = _nvml_handle()
    if nv:
        try:
            util = nv.nvmlDeviceGetUtilizationRates(h)
            mem_info = nv.nvmlDeviceGetMemoryInfo(h)
            try:
                temp = nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = 0
            stats.update(
                gpu_available=True,
                gpu_util=util.gpu,
                vram_used_mb=mem_info.used // (1024 ** 2),
                vram_total_mb=mem_info.total // (1024 ** 2),
                vram_percent=round(mem_info.used / mem_info.total * 100, 1) if mem_info.total else 0,
                gpu_temp=temp,
            )
        except Exception:
            pass
    return stats


def _status_payload(session: _Session, brief: bool = False) -> dict:
    elapsed = None
    if session.start_time:
        elapsed = round((session.end_time or time.time()) - session.start_time)
    out = {
        "status":       session.status,
        "session_id":   session.session_id,
        "run_id":       session.run_id,
        "run_name":     session.run_name,
        "epoch":        session.epoch,
        "total_epochs": session.total_epochs,
        "elapsed_sec":  elapsed,
        "error":        session.error,
        "final_result": session.final_result,
    }
    if not brief:
        out.update(config=session.config, epoch_history=session.epoch_history,
                   log_tail=list(session.log_tail))
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/train/system-info")
async def get_system_info(_admin: dict = Depends(get_admin_user)):
    """One-shot system snapshot for the info banner."""
    info: dict = await asyncio.to_thread(_runtime_info)
    if not info.get("torch_version"):
        info["torch_version"] = "Not installed — run pip install"
    if not info.get("ultralytics_version"):
        info["ultralytics_version"] = "Not installed — run pip install ultralytics"
    cuda_avail = bool(info.get("cuda_available"))
    info["cuda_available"] = cuda_avail
    info.setdefault("cuda_version", "N/A")

    gpu = await asyncio.to_thread(_gpu_static_info) if cuda_avail else {}
    info["gpu_name"]            = gpu.get("gpu_name", "GPU not detected" if cuda_avail else "None (CPU Mode)")
    info["gpu_name_torch"]      = info["gpu_name"]
    info["vram_total_mb"]       = gpu.get("vram_total_mb", 0)
    info["driver_version"]      = gpu.get("driver_version", "N/A")
    info["cuda_driver_version"] = gpu.get("cuda_driver_version", "N/A")
    info["default_device"]      = "0" if cuda_avail else "cpu"

    info["cpu_count"]          = psutil.cpu_count(logical=True)
    info["cpu_count_physical"] = psutil.cpu_count(logical=False) or 1
    mem = psutil.virtual_memory()
    info["ram_total_gb"]       = round(mem.total     / (1024 ** 3), 1)
    info["ram_available_gb"]   = round(mem.available / (1024 ** 3), 1)
    try:
        freq = psutil.cpu_freq()
        info["cpu_freq_mhz"] = round(freq.max or freq.current, 0) if freq else None
    except Exception:
        info["cpu_freq_mhz"] = None
    info["vram_total_gb"] = round(info["vram_total_mb"] / 1024, 1) if info["vram_total_mb"] else 0.0
    info["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return info


class RecommendRequest(BaseModel):
    """Combined dataset + system info for the recommendation engine."""
    total_images: int = 0
    num_classes: Optional[int] = None
    cuda_available: bool = False
    vram_total_gb: float = 0.0
    ram_available_gb: float = 8.0
    cpu_count_physical: int = 2
    memory_budget_gb: Optional[float] = None
    model: Optional[str] = None


@router.post("/api/train/recommend")
async def get_recommendation(req: RecommendRequest, _admin: dict = Depends(get_admin_user)):
    """Rule-based suggested settings with a rationale for each field."""
    return _recommend_params(req.model_dump(), req.model_dump())


@router.get("/api/train/system-stats")
async def get_system_stats(_admin: dict = Depends(get_admin_user)):
    """Real-time hardware usage (polled by the frontend while idle)."""
    return await asyncio.to_thread(_build_hardware_stats)


@router.get("/api/train/pick-dataset")
async def pick_dataset(_admin: dict = Depends(get_admin_user)):
    """Open native folder picker (on the server's screen) and return the selected path."""
    script = Path(__file__).parent / "pick_folder.py"
    rc, out, _ = await asyncio.to_thread(_run_sync, [sys.executable, str(script)], 300)
    if rc != 0 or not out.strip():
        raise HTTPException(status_code=400, detail="No folder selected")
    return {"path": out.strip()}


@router.get("/api/train/pick-yaml")
async def pick_yaml(_admin: dict = Depends(get_admin_user)):
    """Open native file picker specifically for .yaml/.yml files."""
    script = Path(__file__).parent / "pick_file.py"
    rc, out, _ = await asyncio.to_thread(_run_sync, [sys.executable, str(script), "--yaml"], 300)
    if rc != 0 or not out.strip():
        raise HTTPException(status_code=400, detail="No file selected")
    path = out.strip()
    if not path.lower().endswith((".yaml", ".yml")):
        raise HTTPException(status_code=400, detail="Selected file is not a YAML file")
    return {"path": path}


_SCAN_CAP = 200_000


def _count_split(target: Path) -> int:
    """Images in a split directory (recursive, capped) or lines in an image-list .txt."""
    if target.is_file() and target.suffix.lower() == ".txt":
        try:
            return sum(1 for ln in target.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
        except OSError:
            return 0
    if not target.is_dir():
        return 0
    n = 0
    for f in target.rglob("*"):
        if f.suffix.lower() in _IMG_EXTS:
            n += 1
            if n >= _SCAN_CAP:
                break
    return n


def resolve_data_yaml(yaml_path: Path) -> Optional[dict]:
    """
    Read a data.yaml and resolve every split to an absolute path. A relative `path:` (or none)
    counts from the yaml's own folder — what people mean, and how Roboflow / our exports are
    laid out — and "../x" entries fall back to "x" like Ultralytics does. The training worker
    receives these absolute paths, so the validator and the trainer can't disagree.
    Returns the yaml dict with absolute paths, or None if it is not a dataset yaml.
    """
    import yaml
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict) or "train" not in data:
        return None
    base = Path(data["path"]) if data.get("path") else yaml_path.parent
    if not base.is_absolute():
        in_yaml_dir = (yaml_path.parent / base).resolve()
        base = in_yaml_dir if in_yaml_dir.exists() or not base.resolve().exists() else base.resolve()

    def resolve(entry: str) -> Path:
        x = Path(entry) if Path(entry).is_absolute() else (base / entry).resolve()
        if not x.exists() and str(entry).startswith("../"):
            x = (base / entry[3:]).resolve()
        return x

    out = {k: v for k, v in data.items() if k != "download"}     # never run a download script
    out["path"] = str(base)
    for key in ("train", "val", "test"):
        entries = data.get(key)
        if not entries:
            out.pop(key, None)
            continue
        resolved = [str(resolve(e)) for e in (entries if isinstance(entries, list) else [entries])]
        out[key] = resolved if isinstance(entries, list) else resolved[0]
    if "val" not in out and data.get("validation"):
        out["val"] = str(resolve(data["validation"]))
    return out


def _splits_from_yaml(yaml_path: Path) -> Optional[dict]:
    data = resolve_data_yaml(yaml_path)
    if data is None:
        return None
    out = {}
    for key, split in (("train", "train"), ("val", "valid"), ("test", "test")):
        entries = data.get(key)
        paths = [Path(e) for e in (entries if isinstance(entries, list) else ([entries] if entries else []))]
        count = sum(_count_split(p) for p in paths)
        out[split] = {"exists": bool(paths) and any(p.exists() for p in paths), "count": count,
                      "path": str(paths[0]) if paths else None}
    return out


_BASE_WEIGHTS = re.compile(r"^(yolo[\w.-]*|rtdetr[\w.-]*|yoloe[\w.-]*)\.pt$", re.IGNORECASE)


def resolve_model(name: str) -> str:
    """
    What the worker may load: an official base-weights name (looked up / downloaded in the
    model cache) or a .pt inside models/, training_runs/ or the model cache. Anything else —
    URLs, UNC paths, files elsewhere — is refused: a .pt is a pickle, loading it runs code.
    """
    name = (name or "").strip().strip('"')
    if _BASE_WEIGHTS.match(name):
        return name
    for root in (MODELS_DIR, TRAINING_RUNS_DIR, MODEL_CACHE_DIR):
        cand = Path(name) if Path(name).is_absolute() else root / name
        try:
            cand = cand.resolve()
            cand.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        if cand.suffix == ".pt" and cand.is_file():
            return str(cand)
    raise HTTPException(status_code=400, detail="Model must be a base model name (e.g. yolo11n.pt) or a .pt in models/")


def link_or_copy(src: Path, dst: Path) -> bool:
    """Hard-link (same drive: no extra space) or copy a finished checkpoint into models/."""
    if not src or not Path(src).exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".pt.tmp")
    tmp.unlink(missing_ok=True)
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


def recover_unlinked_runs() -> int:
    """Startup: runs whose weights never reached models/ (server stopped mid-run) get linked now."""
    n = 0
    for run in TRAINING_RUNS_DIR.iterdir() if TRAINING_RUNS_DIR.exists() else []:
        weights = run / "weights"
        dest = MODELS_DIR / run.name
        for f in ("best.pt", "last.pt"):
            if (weights / f).exists() and not (dest / f).exists():
                n += link_or_copy(weights / f, dest / f)
    return n


def _validate_dataset_sync(path: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    yaml_path = _find_yaml(path)
    splits = _splits_from_yaml(yaml_path) if yaml_path else None
    if splits is None:
        splits = {}
        for split, names in (("train", ("train",)), ("valid", ("valid", "val")), ("test", ("test",))):
            info = {"exists": False, "count": 0, "path": None}
            for name in names:
                for candidate in (path / name / "images", path / "images" / name, path / name):
                    if candidate.is_dir():
                        cnt = _count_split(candidate)
                        if cnt:
                            info = {"exists": True, "count": cnt, "path": str(candidate)}
                            break
                if info["exists"]:
                    break
            splits[split] = info

    if not splits["train"]["exists"]:
        errors.append("Training split not found — required for training")
    elif splits["train"]["count"] == 0:
        errors.append("Training split contains no images")
    if not splits.get("valid", {}).get("exists"):
        warnings.append("Validation split not found (optional, but needed for mAP tracking)")

    yaml_info = {"exists": bool(yaml_path), "path": str(yaml_path) if yaml_path else None}
    if not yaml_path:
        warnings.append("No data.yaml found in this folder — you can provide one manually below")

    total_images = sum(s["count"] for s in splits.values() if s.get("exists"))
    yaml_data = _parse_yaml_info(yaml_path) if yaml_path else {"num_classes": None, "class_names": []}
    split_counts = {s: splits[s]["count"] for s in ("train", "valid", "test") if splits[s].get("exists")}
    split_total = sum(split_counts.values()) or 1

    return {
        "is_valid":      not errors,
        "path":          str(path),
        "splits":        splits,
        "yaml":          yaml_info,
        "errors":        errors,
        "warnings":      warnings,
        "total_images":  total_images,
        "total_size_mb": _folder_size_mb(path),
        "num_classes":   yaml_data["num_classes"],
        "class_names":   yaml_data["class_names"],
        "split_ratios":  {s: round(c / split_total * 100, 1) for s, c in split_counts.items()},
    }


@router.post("/api/train/validate-dataset")
async def validate_dataset(req: ValidateDatasetRequest, _admin: dict = Depends(get_admin_user)):
    """Validate a dataset folder — splits (from data.yaml when present), yaml, extended stats."""
    raw = req.path.strip().strip('"')
    if not raw:
        return {"is_valid": False, "errors": ["Enter a dataset folder"], "warnings": [], "splits": {}, "yaml": None}
    path = Path(raw)
    if not path.exists():
        return {"is_valid": False, "errors": ["Folder does not exist"], "warnings": [], "splits": {}, "yaml": None}
    if not path.is_dir():
        return {"is_valid": False, "errors": ["Path is not a directory"], "warnings": [], "splits": {}, "yaml": None}
    return await asyncio.to_thread(_validate_dataset_sync, path)


@router.post("/api/train/start")
async def start_training(req: StartTrainingRequest, _admin: dict = Depends(get_admin_user)):
    """Launch a new training subprocess. Only one session at a time."""
    yaml_path = Path(req.yaml_path.strip().strip('"'))
    if not yaml_path.is_file():
        raise HTTPException(status_code=400, detail=f"YAML not found: {req.yaml_path}")
    data = await asyncio.to_thread(resolve_data_yaml, yaml_path)
    if data is None:
        raise HTTPException(status_code=400, detail="This file is not a dataset yaml (it needs a 'train:' entry)")
    model = resolve_model(req.model)

    safe_ds = re.sub(r"[^\w\-]", "_", req.dataset_name)[:40] or "dataset"
    model_tag = re.sub(r"[^\w\-]", "_", Path(req.model).stem)[:30] or "model"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if req.run_name_override and req.run_name_override.strip():
        run_name = f"{re.sub(r'[^\w\-]', '_', req.run_name_override.strip())[:40]}_{ts}"
    else:
        run_name = f"{safe_ds}_{model_tag}_{ts}"

    # The worker trains on a cleaned copy with absolute paths, kept with the run
    import yaml as _yaml
    run_dir = TRAINING_RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    clean_yaml = run_dir / "data.yaml"
    clean_yaml.write_text(_yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    config = {k: v for k, v in req.model_dump().items() if k not in ("dataset_path", "run_name_override")}
    return await _launch(run_name, {**config, "model": model, "data": clean_yaml.as_posix(),
                                    "dataset_name": safe_ds, "source_yaml": str(yaml_path)}, resume=False)


async def _launch(run_name: str, cfg: dict, resume: bool) -> dict:
    global _session
    from app.compute import default_device

    async with _session_lock:
        old = _session
        if old.status in ("running", "stopping"):
            raise HTTPException(status_code=409, detail="Training is already in progress")
        if old.process and old.process.returncode is None:
            raise HTTPException(status_code=409, detail="The previous run is still shutting down — try again shortly")

        device = str(cfg.get("device") or "auto").strip().lower()
        if device in ("auto", ""):
            device = await asyncio.to_thread(default_device)

        session = _Session()
        session.session_id = uuid.uuid4().hex[:8]
        session.run_name = run_name
        session.stop_file = DATA_DIR / f"train_stop_{session.session_id}.flag"
        session.stop_file.unlink(missing_ok=True)

        worker_config = {
            "model":            cfg["model"],
            "data":             cfg["data"],
            "resume":           resume,
            "dataset_name":     cfg.get("dataset_name"),
            "epochs":           cfg["epochs"],
            "batch":            cfg["batch"],
            "imgsz":            cfg["imgsz"],
            "patience":         cfg["patience"],
            "lr0":              cfg["lr0"],
            "optimizer":        cfg["optimizer"],
            "workers":          cfg["workers"],
            "device":           device,
            "cos_lr":           cfg["cos_lr"],
            "warmup_epochs":    cfg["warmup_epochs"],
            "weight_decay":     cfg["weight_decay"],
            "mosaic":           cfg["mosaic"],
            "memory_budget_gb": cfg.get("memory_budget_gb"),
            "project":          TRAINING_RUNS_DIR.as_posix(),
            "name":             run_name,
            "stop_file":        str(session.stop_file),
            "parent_pid":       os.getpid(),
        }
        session.start_time = time.time()
        session.config = worker_config
        session.total_epochs = cfg["epochs"]

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(WORKER_SCRIPT), json.dumps(worker_config),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,   # merge stderr into stdout
                creationflags=flags,
                cwd=str(MODEL_CACHE_DIR),           # base weights download here, not into the repo
            )
        except Exception as exc:
            session.status = "error"
            session.error = f"Could not start the training process: {exc}"
            session.end_time = time.time()
            _session = session
            raise HTTPException(status_code=500, detail=session.error)

        session.process = proc
        session.status = "running"
        _session = session
        session.run_id = await _record_run_start(run_name, cfg, worker_config, resume)
        session._stream_task = asyncio.create_task(_stream_training(session))
        session._monitor_task = asyncio.create_task(_hardware_monitor(session))

    await emit_train_status(_status_payload(session, brief=True))   # other tabs switch to "training" at once
    return {"session_id": session.session_id, "run_name": run_name, "run_id": session.run_id,
            "total_epochs": cfg["epochs"], "status": "running", "device": device}


async def _record_run_start(run_name: str, cfg: dict, worker_config: dict, resume: bool) -> str:
    async with connect() as db:
        cur = await db.execute("SELECT id FROM training_runs WHERE name = ?", (run_name,))
        row = await cur.fetchone()
        if row:                                          # resume continues the same run
            await db.execute("UPDATE training_runs SET status='running', finished_at=NULL, error=NULL WHERE id = ?",
                             (row["id"],))
            run_id = row["id"]
        else:
            run_id = new_id()
            public = {k: v for k, v in worker_config.items() if k not in ("stop_file", "parent_pid", "project")}
            await db.execute(
                "INSERT INTO training_runs (id, name, source, status, model, dataset, config, total_epochs, started_at) "
                "VALUES (?, ?, 'local', 'running', ?, ?, ?, ?, ?)",
                (run_id, run_name, Path(cfg["model"]).name, cfg.get("dataset_name"), json.dumps(public),
                 cfg["epochs"], now_iso()),
            )
        await db.commit()
    return run_id


async def _stop(session: _Session, grace: float) -> None:
    """Ask the worker to stop between batches; kill the whole process tree if it doesn't."""
    proc = session.process
    if not proc or proc.returncode is not None:
        return
    if session.stop_file:
        session.stop_file.touch()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        await asyncio.to_thread(_kill_tree, proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


@router.post("/api/train/stop", status_code=202)
async def stop_training(_admin: dict = Depends(get_admin_user)):
    """Ask the run to stop after the current batch (forced after 30 s). Returns at once."""
    session = _session
    if session.status == "stopping":
        return {"status": "stopping"}
    if session.status != "running":
        raise HTTPException(status_code=400, detail="No active training session to stop")

    session.status = "stopping"
    await _log(session, "[INFO] Stop requested — finishing the current batch…")
    await emit_train_status(_status_payload(session, brief=True))

    async def finish():
        await _stop(session, grace=30)
        if session._stream_task:
            try:
                await asyncio.wait_for(asyncio.shield(session._stream_task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if session.status == "stopping":
            session.status = "stopped"
            session.end_time = time.time()
            await _finalise_run(session)
            await emit_train_status(_status_payload(session, brief=True))

    session._stop_task = asyncio.create_task(finish())
    return {"status": "stopping"}


@router.get("/api/train/status")
async def get_training_status(_admin: dict = Depends(get_admin_user)):
    """Full session state including epoch history and recent log lines (reload / reconnect)."""
    return _status_payload(_session)


@router.get("/api/train/models")
async def list_models(_admin: dict = Depends(get_admin_user)):
    """List all saved .pt files in the models/ directory."""
    models = []
    for f in sorted(MODELS_DIR.rglob("*.pt"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = f.stat()
        models.append({
            "name":     f.relative_to(MODELS_DIR).as_posix(),
            "size_mb":  round(stat.st_size / (1024 ** 2), 1),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "path":     str(f),
        })
    return {"models": models}


@router.get("/api/train/models/{model_path:path}")
async def download_model(model_path: str, _admin: dict = Depends(get_admin_header_or_query)):
    """Download a .pt model file (links pass ?token= because <a href> can't send headers)."""
    model_file = (MODELS_DIR / model_path).resolve()
    try:
        model_file.relative_to(MODELS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not model_file.is_file() or model_file.suffix != ".pt":
        raise HTTPException(status_code=404, detail="Model not found")

    # e.g. "HelmetSet_yolo11n_20260927_best.pt" instead of a bare "best.pt"
    parent = model_file.parent.name if model_file.parent != MODELS_DIR.resolve() else ""
    download_name = f"{parent}_{model_file.name}" if parent else model_file.name
    return FileResponse(path=str(model_file), filename=download_name, media_type="application/octet-stream")


def _run_out(r: dict) -> dict:
    out = dict(r)
    out["config"] = json.loads(out.get("config") or "{}")
    out["metrics"] = json.loads(out.get("metrics") or "{}")
    folder = MODELS_DIR / out["name"]
    out["best_pt"] = f"{out['name']}/best.pt" if (folder / "best.pt").exists() else None
    out["last_pt"] = f"{out['name']}/last.pt" if (folder / "last.pt").exists() else None
    out["can_resume"] = (out["source"] == "local" and out["status"] in ("stopped", "interrupted", "error")
                         and (TRAINING_RUNS_DIR / out["name"] / "weights" / "last.pt").exists())
    plots = sorted({f.name for f in (TRAINING_RUNS_DIR / out["name"]).glob("*.png")}
                   | {f"plots/{f.name}" for f in (folder / "plots").glob("*.png")})
    out["plots"] = [p for p in plots if not p.split("/")[-1].startswith(("train_batch", "val_batch", "labels"))]
    return out


_RUN_FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".csv": "text/csv", ".yaml": "text/plain"}


@router.get("/api/train/runs/{run_id}/files/{rel:path}")
async def run_file(run_id: str, rel: str, _admin: dict = Depends(get_admin_header_or_query)):
    """A plot / results.csv / args.yaml of a run (from training_runs/<run>/ or models/<run>/)."""
    async with connect() as db:
        cur = await db.execute("SELECT name FROM training_runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    for root in (TRAINING_RUNS_DIR / row["name"], MODELS_DIR / row["name"]):
        f = (root / rel).resolve()
        try:
            f.relative_to(root.resolve())
        except ValueError:
            continue
        if f.suffix.lower() in _RUN_FILE_TYPES and f.is_file():
            return FileResponse(f, media_type=_RUN_FILE_TYPES[f.suffix.lower()])
    raise HTTPException(status_code=404, detail="File not found")


@router.get("/api/train/runs")
async def list_runs(_admin: dict = Depends(get_admin_user)):
    async with connect() as db:
        cur = await db.execute("SELECT * FROM training_runs ORDER BY started_at DESC")
        rows = [dict(r) for r in await cur.fetchall()]
    return {"runs": await asyncio.to_thread(lambda: [_run_out(r) for r in rows])}


@router.post("/api/train/runs/{run_id}/resume")
async def resume_run(run_id: str, _admin: dict = Depends(get_admin_user)):
    """Continue an interrupted run from its last.pt (same epochs, data and settings)."""
    async with connect() as db:
        cur = await db.execute("SELECT * FROM training_runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _run_out(dict(row))
    if not run["can_resume"]:
        raise HTTPException(status_code=400, detail="This run has no checkpoint to resume from")
    last = TRAINING_RUNS_DIR / run["name"] / "weights" / "last.pt"
    cfg = {**run["config"], "model": str(last), "epochs": run["total_epochs"] or run["config"].get("epochs", 100)}
    for k, v in StartTrainingRequest.model_fields.items():
        cfg.setdefault(k, v.default)
    return await _launch(run["name"], cfg, resume=True)


@router.delete("/api/train/runs/{run_id}")
async def delete_run(run_id: str, _admin: dict = Depends(get_admin_user)):
    """Delete a run: its row, models/<run>/ and training_runs/<run>/ (frees the disk space)."""
    async with connect() as db:
        cur = await db.execute("SELECT name, status FROM training_runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if row["status"] == "running" or (_session.run_id == run_id and _session.status in ("running", "stopping")):
            raise HTTPException(status_code=409, detail="Stop the run before deleting it")
        await db.execute("DELETE FROM training_runs WHERE id = ?", (run_id,))
        await db.commit()
    name = row["name"]
    for root in (MODELS_DIR, TRAINING_RUNS_DIR):
        target = (root / name).resolve()
        if target.parent == root.resolve() and target.exists():
            await asyncio.to_thread(shutil.rmtree, target, True)
    return {"detail": f"Deleted {name}"}


async def import_legacy_runs() -> int:
    """Startup: list runs made before run history existed (folders in training_runs/)."""
    async with connect() as db:
        cur = await db.execute("SELECT name FROM training_runs")
        known = {r["name"] for r in await cur.fetchall()}
        added = 0
        for run in sorted(TRAINING_RUNS_DIR.iterdir()) if TRAINING_RUNS_DIR.exists() else []:
            if run.name in known or not (run / "weights").exists():
                continue
            metrics, epochs, best_epoch = _metrics_from_results_csv(run / "results.csv")
            args = {}
            if (run / "args.yaml").exists():
                import yaml as _yaml
                try:
                    args = _yaml.safe_load((run / "args.yaml").read_text(encoding="utf-8")) or {}
                except Exception:
                    args = {}
            started = datetime.fromtimestamp(run.stat().st_mtime).isoformat()
            await db.execute(
                "INSERT INTO training_runs (id, name, source, status, model, dataset, config, metrics, best_epoch, "
                "epochs_done, total_epochs, started_at, finished_at) VALUES (?, ?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), run.name, "complete" if (run / "weights" / "best.pt").exists() else "stopped",
                 Path(str(args.get("model", ""))).name or None, Path(str(args.get("data", ""))).parent.name or None,
                 json.dumps({k: args.get(k) for k in ("epochs", "batch", "imgsz", "optimizer", "lr0", "device") if k in args}),
                 json.dumps(metrics), best_epoch, epochs, args.get("epochs") or epochs, started, started),
            )
            added += 1
        await db.commit()
    return added


def _metrics_from_results_csv(path: Path) -> tuple[dict, int, Optional[int]]:
    """Best-epoch metrics from an Ultralytics results.csv (fitness = 0.1·mAP50 + 0.9·mAP50-95)."""
    import csv
    if not path.exists():
        return {}, 0, None
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [{k.strip(): v.strip() for k, v in r.items() if k} for r in csv.DictReader(fh)]
    except Exception:
        return {}, 0, None
    if not rows:
        return {}, 0, None

    def f(r, k):
        try:
            return float(r.get(k, "nan"))
        except ValueError:
            return float("nan")
    best = max(rows, key=lambda r: (0.1 * f(r, "metrics/mAP50(B)") + 0.9 * f(r, "metrics/mAP50-95(B)"))
               if f(r, "metrics/mAP50(B)") == f(r, "metrics/mAP50(B)") else -1)
    metrics = {k: f(best, k) for k in best if k.startswith("metrics/") and f(best, k) == f(best, k)}
    try:
        best_epoch = int(float(best.get("epoch", 0)))
    except ValueError:
        best_epoch = None
    return metrics, len(rows), best_epoch


async def cleanup_session():
    """Stop an active training subprocess on application shutdown."""
    session = _session
    if session.process and session.process.returncode is None:
        session.status = "stopping"
        await _stop(session, grace=3)
    for task in (session._stream_task, session._monitor_task):
        if task and not task.done():
            task.cancel()
