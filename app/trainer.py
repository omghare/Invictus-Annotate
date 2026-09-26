"""
trainer.py — YOLO model training orchestration.

Endpoints:
  GET  /api/train/system-info      — GPU/CUDA/Ultralytics/CPU one-time info
  GET  /api/train/system-stats     — Real-time CPU/RAM/GPU/VRAM usage
  GET  /api/train/pick-dataset     — Open native folder picker for dataset
  POST /api/train/validate-dataset — Validate a dataset folder path
  POST /api/train/start            — Start training session
  POST /api/train/stop             — Stop running training session
  GET  /api/train/status           — Current session state (for reconnect)
  GET  /api/train/models           — List saved .pt files in models/
  GET  /api/train/models/{name}    — Download a specific .pt file

Training runs in a subprocess (sys.executable → train_worker.py).
Output is streamed line-by-line and forwarded over Socket.IO.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import psutil
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import get_admin_user
from app.config import BASE_DIR, MODELS_DIR, TRAINING_RUNS_DIR
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
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}


# ── In-memory training session ────────────────────────────────────────────────

class _Session:
    def __init__(self):
        self.session_id: Optional[str] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.start_time: Optional[float] = None
        self.config: Optional[dict] = None
        self.status: str = "idle"      # idle | running | stopping | complete | error
        self.epoch: int = 0
        self.total_epochs: int = 0
        self.epoch_history: list = []
        self.final_result: Optional[dict] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

    def reset(self):
        self.__init__()


_session = _Session()


# ── Pydantic models ───────────────────────────────────────────────────────────

class ValidateDatasetRequest(BaseModel):
    path: str

class StartTrainingRequest(BaseModel):
    dataset_path: str
    yaml_path: str
    model: str                        # e.g. "yolov8n.pt" or "yolo11m.pt"
    dataset_name: str                 # used for naming the output .pt
    run_name_override: Optional[str] = None  # optional custom run name from user
    epochs: int = 100
    batch: int = 16
    imgsz: int = 640
    patience: int = 50
    lr0: float = 0.01
    optimizer: str = "SGD"
    workers: int = 2                  # default matching CPU physical core count
    device: str = "cpu"               # default CPU for non-GPU hardware
    cos_lr: bool = False
    warmup_epochs: float = 3.0
    weight_decay: float = 0.0005
    mosaic: float = 1.0
    memory_budget_gb: Optional[float] = None  # user-selected memory budget


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


def _folder_size_mb(folder: Path) -> float:
    """Recursively compute folder size in MB."""
    total = 0
    try:
        for f in folder.rglob("*"):
            if f.is_file():
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
        variant = model[-4]  # e.g. 'n', 's', 'm'
        mfac = model_mem_factor.get(variant, 0.3)
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
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            creationflags=flags,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 2, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"


def _get_gpu_stats_nvidia_smi() -> dict:
    """Fallback GPU stats via nvidia-smi --query-gpu."""
    rc, out, _ = _run_sync([
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ], timeout=4)
    result = {"gpu_util": 0, "vram_used_mb": 0, "vram_total_mb": 0,
              "vram_percent": 0.0, "gpu_temp": 0}
    if rc == 0:
        parts = [p.strip() for p in out.strip().split(",")]
        try:
            result["gpu_util"]     = int(parts[0])
            result["vram_used_mb"] = int(parts[1])
            result["vram_total_mb"]= int(parts[2])
            result["gpu_temp"]     = int(parts[3])
            if result["vram_total_mb"] > 0:
                result["vram_percent"] = round(
                    result["vram_used_mb"] / result["vram_total_mb"] * 100, 1
                )
        except (IndexError, ValueError):
            pass
    return result


# ── Performance commentary (rule-based, every 5 epochs) ──────────────────────

def _generate_commentary(history: list) -> Optional[str]:
    if len(history) < 5:
        return None

    recent = history[-5:]
    comments: list[str] = []

    # mAP50 trend
    maps = []
    for e in recent:
        m = e.get("metrics", {})
        v = m.get("metrics/mAP50(B)", m.get("mAP50", None))
        if v is not None:
            maps.append(float(v))

    if len(maps) >= 3:
        trend = maps[-1] - maps[0]
        if trend > 0.02:
            comments.append(f"📈 mAP50 up +{trend:.3f} — excellent momentum!")
        elif trend < -0.015:
            comments.append("📉 mAP50 dropped recently — possible overfitting, watch val loss.")
        elif maps[-1] >= 0.75:
            comments.append(f"✅ mAP50 stable at {maps[-1]:.3f} — strong convergence.")
        else:
            comments.append(f"⟳ mAP50 plateaued at {maps[-1]:.3f}. More epochs or LR tuning may help.")

    # Box loss trend
    box_losses = []
    for e in recent:
        items = e.get("loss_items", [])
        if items:
            try:
                box_losses.append(float(items[0]))
            except (TypeError, ValueError):
                pass

    if len(box_losses) >= 3:
        loss_trend = box_losses[-1] - box_losses[0]
        if loss_trend < -0.05:
            comments.append("✅ Box loss still declining — model actively learning.")
        elif loss_trend > 0.05:
            comments.append("⚠️ Box loss increased — possible gradient instability.")
        else:
            comments.append(f"ℹ️ Box loss stabilising at {box_losses[-1]:.4f}.")

    if not comments:
        return "Training progressing normally — keep monitoring."
    return "  ".join(comments)


# ── Training stream reader ────────────────────────────────────────────────────

async def _stream_training():
    """Read subprocess stdout line-by-line; emit log/epoch/complete events."""
    global _session

    proc = _session.process
    if not proc:
        return

    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break

            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")

            # Check for structured JSON event
            if line.startswith('{"__event__"'):
                try:
                    event = json.loads(line)
                    etype = event.pop("__event__", "")
                    await _handle_worker_event(etype, event)
                    continue
                except json.JSONDecodeError:
                    pass  # fall through to log line

            # Raw log line — forward to training room
            await emit_train_log(line)

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        await emit_train_error(f"Stream reader error: {exc}")

    # Process finished
    await proc.wait()
    if _session.status == "running":
        if proc.returncode == 0:
            _session.status = "complete"
        else:
            _session.status = "error"
            await emit_train_error("Training process exited unexpectedly.")
        await emit_train_status({
            "status": _session.status,
            "epoch": _session.epoch,
            "total_epochs": _session.total_epochs,
        })


async def _handle_worker_event(etype: str, data: dict):
    """Dispatch a structured event from the worker subprocess."""
    global _session

    if etype == "started":
        await emit_train_log(f"[INFO] Training started — model: {data.get('model')}")

    elif etype == "epoch":
        _session.epoch = data["epoch"]
        _session.total_epochs = data["epochs"]
        _session.epoch_history.append(data)

        # Continuously backup best.pt and last.pt to MODELS_DIR
        cfg = _session.config or {}
        if "name" in cfg:
            dest_dir = MODELS_DIR / cfg["name"]
            dest_dir.mkdir(parents=True, exist_ok=True)
            for key in ("best_pt", "last_pt"):
                src = data.get(key)
                if src and Path(src).exists():
                    try:
                        shutil.copy2(src, dest_dir / f"{key.replace('_', '.')}")
                    except Exception:
                        pass

        await emit_train_epoch(data)
        await emit_train_status({
            "status": "running",
            "epoch": _session.epoch,
            "total_epochs": _session.total_epochs,
        })

        # Commentary every 5 epochs
        if _session.epoch % 5 == 0:
            comment = _generate_commentary(_session.epoch_history)
            if comment:
                await emit_train_comment(comment, _session.epoch)

    elif etype == "complete":
        _session.status = "complete"
        _session.final_result = data

        # Copy best.pt and last.pt to models/<run_name>/
        saved_path = None
        cfg = _session.config or {}
        dest_dir = MODELS_DIR / cfg.get("name", "dataset_run")
        dest_dir.mkdir(parents=True, exist_ok=True)

        best_pt_src = data.get("best_pt")
        if best_pt_src and Path(best_pt_src).exists():
            dest_path = dest_dir / "best.pt"
            try:
                shutil.copy2(best_pt_src, dest_path)
                saved_path = str(dest_path)
                await emit_train_log(f"[INFO] Best weights saved → {dest_path}")
            except Exception as exc:
                await emit_train_log(f"[WARN] Could not copy best.pt: {exc}")

        last_pt_src = data.get("last_pt")
        if last_pt_src and Path(last_pt_src).exists():
            try:
                shutil.copy2(last_pt_src, dest_dir / "last.pt")
            except Exception as exc:
                await emit_train_log(f"[WARN] Could not copy last.pt: {exc}")

        await emit_train_complete({**data, "saved_model_path": saved_path})
        await emit_train_status({"status": "complete", "saved_model_path": saved_path})

    elif etype == "stopped":
        _session.status = "stopped"
        await emit_train_log(f"[INFO] {data.get('message', 'Training stopped.')}")
        await emit_train_status({"status": "stopped"})

    elif etype == "error":
        _session.status = "error"
        await emit_train_error(data.get("message", "Unknown error"))
        await emit_train_status({"status": "error", "message": data.get("message", "")})


async def _hardware_monitor():
    """Push hardware stats to training room every 2 s during active training."""
    while _session.status == "running":
        try:
            stats = _build_hardware_stats()
            await emit_train_hardware(stats)
        except Exception:
            pass
        await asyncio.sleep(2)


def _build_hardware_stats() -> dict:
    """Collect CPU/RAM/GPU/VRAM stats. Bypasses GPU polling if CUDA is unavailable."""
    cpu_pct  = psutil.cpu_percent(interval=None)
    mem      = psutil.virtual_memory()
    ram_pct  = mem.percent
    ram_used = round(mem.used / (1024 ** 3), 1)
    ram_total= round(mem.total / (1024 ** 3), 1)

    gpu_stats = {
        "gpu_util": 0,
        "vram_used_mb": 0,
        "vram_total_mb": 0,
        "vram_percent": 0.0,
        "gpu_temp": 0,
    }

    # Only attempt NVML / GPU polling if PyTorch CUDA is detected as available
    try:
        import torch
        if torch.cuda.is_available():
            try:
                from pynvml import (nvmlInit, nvmlDeviceGetHandleByIndex,
                                     nvmlDeviceGetUtilizationRates, nvmlDeviceGetMemoryInfo,
                                     nvmlDeviceGetTemperature, NVML_TEMPERATURE_GPU)
            except ImportError:
                from nvidia_ml_py import (nvmlInit, nvmlDeviceGetHandleByIndex,
                                           nvmlDeviceGetUtilizationRates, nvmlDeviceGetMemoryInfo,
                                           nvmlDeviceGetTemperature, NVML_TEMPERATURE_GPU)
            nvmlInit()
            h        = nvmlDeviceGetHandleByIndex(0)
            util     = nvmlDeviceGetUtilizationRates(h)
            mem_info = nvmlDeviceGetMemoryInfo(h)
            temp     = 0
            try:
                temp = nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU)
            except Exception:
                pass
            gpu_stats = {
                "gpu_util":      util.gpu,
                "vram_used_mb":  mem_info.used // (1024 ** 2),
                "vram_total_mb": mem_info.total // (1024 ** 2),
                "vram_percent":  round(mem_info.used / mem_info.total * 100, 1) if mem_info.total else 0,
                "gpu_temp":      temp,
            }
    except Exception:
        pass

    return {
        "cpu_percent":  round(cpu_pct, 1),
        "ram_percent":  round(ram_pct, 1),
        "ram_used_gb":  ram_used,
        "ram_total_gb": ram_total,
        **gpu_stats,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/train/system-info")
async def get_system_info(_admin: dict = Depends(get_admin_user)):
    """One-shot system snapshot for the info banner."""
    info: dict = {}

    # Torch / CUDA (runtime)
    cuda_avail = False
    try:
        import torch
        info["torch_version"]  = torch.__version__
        cuda_avail             = torch.cuda.is_available()
        info["cuda_available"] = cuda_avail
        info["cuda_version"]   = torch.version.cuda or "N/A"
        if cuda_avail:
            info["gpu_name_torch"] = torch.cuda.get_device_name(0)
    except ImportError:
        info["torch_version"]  = "Not installed — run pip install"
        info["cuda_available"] = False
        info["cuda_version"]   = "N/A"

    if cuda_avail:
        # GPU info via nvidia-smi (name, memory, driver version)
        rc, out, _ = _run_sync([
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ], timeout=4)
        if rc == 0:
            parts = [p.strip() for p in out.strip().split(",")]
            info["gpu_name"]       = parts[0] if len(parts) > 0 else "N/A"
            info["vram_total_mb"]  = int(parts[1]) if len(parts) > 1 else 0
            info["driver_version"] = parts[2] if len(parts) > 2 else "N/A"
        else:
            info["gpu_name"]      = "GPU not detected"
            info["vram_total_mb"] = 0

        # CUDA driver version from nvidia-smi
        rc2, out2, _ = _run_sync(["nvidia-smi"], timeout=4)
        if rc2 == 0:
            for line in out2.splitlines():
                if "CUDA Version:" in line:
                    m = re.search(r"CUDA Version:\s*([\d.]+)", line)
                    info["cuda_driver_version"] = m.group(1) if m else "N/A"
                    break
        if "cuda_driver_version" not in info:
            info["cuda_driver_version"] = "N/A"
    else:
        info["gpu_name"]            = "None (CPU Mode)"
        info["vram_total_mb"]       = 0
        info["driver_version"]      = "N/A (CPU Mode)"
        info["cuda_driver_version"] = "N/A"

    info["default_device"] = "0" if cuda_avail else "cpu"

    # Ultralytics
    try:
        import ultralytics
        info["ultralytics_version"] = ultralytics.__version__
    except ImportError:
        info["ultralytics_version"] = "Not installed — run pip install ultralytics"

    # CPU / RAM
    info["cpu_count"]          = psutil.cpu_count(logical=True)
    info["cpu_count_physical"] = psutil.cpu_count(logical=False) or 1
    mem = psutil.virtual_memory()
    info["ram_total_gb"]       = round(mem.total     / (1024 ** 3), 1)
    info["ram_available_gb"]   = round(mem.available / (1024 ** 3), 1)

    # CPU frequency
    try:
        freq = psutil.cpu_freq()
        if freq:
            info["cpu_freq_mhz"] = round(freq.max or freq.current, 0)
        else:
            info["cpu_freq_mhz"] = None
    except Exception:
        info["cpu_freq_mhz"] = None

    # VRAM in GB for convenience
    if info.get("vram_total_mb", 0) > 0:
        info["vram_total_gb"] = round(info["vram_total_mb"] / 1024, 1)
    else:
        info["vram_total_gb"] = 0.0

    # Python
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


@router.post("/api/train/recommend")
async def get_recommendation(req: RecommendRequest, _admin: dict = Depends(get_admin_user)):
    """
    Rule-based recommendation engine.
    Accepts combined dataset + system stats; returns optimal model + params with rationale.
    """
    rec = _recommend_params(req.model_dump(), req.model_dump())
    return rec


@router.get("/api/train/system-stats")
async def get_system_stats(_admin: dict = Depends(get_admin_user)):
    """Real-time hardware usage (polled every 2 s by the frontend)."""
    return _build_hardware_stats()


@router.get("/api/train/pick-dataset")
async def pick_dataset(_admin: dict = Depends(get_admin_user)):
    """Open native folder picker and return selected path."""
    import sys as _sys
    script = Path(__file__).parent / "pick_folder.py"
    loop = asyncio.get_event_loop()
    rc, out, _ = await loop.run_in_executor(
        None,
        lambda: _run_sync([_sys.executable, str(script)], timeout=120),
    )
    if rc != 0:
        raise HTTPException(status_code=400, detail="No folder selected")
    return {"path": out.strip()}


@router.get("/api/train/pick-yaml")
async def pick_yaml(_admin: dict = Depends(get_admin_user)):
    """Open native file picker specifically for .yaml/.yml files."""
    import sys as _sys
    script = Path(__file__).parent / "pick_file.py"
    loop = asyncio.get_event_loop()
    rc, out, _ = await loop.run_in_executor(
        None,
        lambda: _run_sync([_sys.executable, str(script), "--yaml"], timeout=120),
    )
    if rc != 0:
        raise HTTPException(status_code=400, detail="No file selected")
    path = out.strip()
    if not path.lower().endswith(('.yaml', '.yml')):
        raise HTTPException(status_code=400, detail="Selected file is not a YAML file")
    return {"path": path}


@router.post("/api/train/validate-dataset")
async def validate_dataset(req: ValidateDatasetRequest, _admin: dict = Depends(get_admin_user)):
    """Validate a dataset folder — checks splits + yaml presence + extended stats."""
    path = Path(req.path)

    errors: list[str]   = []
    warnings: list[str] = []
    splits: dict        = {}

    if not path.exists():
        return {"is_valid": False, "errors": ["Folder does not exist"], "warnings": [], "splits": {}, "yaml": None}
    if not path.is_dir():
        return {"is_valid": False, "errors": ["Path is not a directory"], "warnings": [], "splits": {}, "yaml": None}

    for split in ("train", "valid", "test"):
        img_subdir = path / split / "images"
        direct_dir = path / split
        if img_subdir.exists():
            cnt = _count_images(img_subdir)
            splits[split] = {"exists": True, "count": cnt, "path": str(img_subdir)}
        elif direct_dir.exists():
            cnt = _count_images(direct_dir)
            splits[split] = {"exists": bool(cnt), "count": cnt, "path": str(direct_dir)}
        else:
            splits[split] = {"exists": False, "count": 0, "path": None}

    if not splits["train"]["exists"]:
        errors.append("train/ split not found — required for training")
    elif splits["train"]["count"] == 0:
        errors.append("train/ split exists but contains no images")

    if not splits.get("valid", {}).get("exists"):
        warnings.append("valid/ split not found (optional but recommended for mAP tracking)")

    # YAML
    yaml_path = _find_yaml(path)
    yaml_info = {"exists": bool(yaml_path), "path": str(yaml_path) if yaml_path else None}
    if not yaml_path:
        warnings.append("No data.yaml found in this folder — you can provide one manually below")

    # ── Extended dataset stats ─────────────────────────────────────────────────
    total_images = sum(s["count"] for s in splits.values() if s.get("exists"))

    # Dataset disk size (compute async to avoid blocking too long on huge datasets)
    loop = asyncio.get_event_loop()
    total_size_mb = await loop.run_in_executor(None, _folder_size_mb, path)

    # Parse YAML for class info
    yaml_data = _parse_yaml_info(yaml_path) if yaml_path else {"num_classes": None, "class_names": []}

    # Split ratio (percentages, only among existing splits)
    split_counts = {s: splits[s]["count"] for s in ("train", "valid", "test") if splits[s].get("exists")}
    split_total  = sum(split_counts.values()) or 1
    split_ratios = {s: round(c / split_total * 100, 1) for s, c in split_counts.items()}

    is_valid = len(errors) == 0

    return {
        "is_valid":      is_valid,
        "path":          str(path),
        "splits":        splits,
        "yaml":          yaml_info,
        "errors":        errors,
        "warnings":      warnings,
        # Extended stats
        "total_images":  total_images,
        "total_size_mb": total_size_mb,
        "num_classes":   yaml_data["num_classes"],
        "class_names":   yaml_data["class_names"],
        "split_ratios":  split_ratios,
    }


@router.post("/api/train/start")
async def start_training(req: StartTrainingRequest, _admin: dict = Depends(get_admin_user)):
    """Launch a new training subprocess. Only one session at a time."""
    global _session

    if _session.status == "running":
        raise HTTPException(status_code=409, detail="Training is already in progress")

    # Validate critical paths
    yaml_path = Path(req.yaml_path)
    if not yaml_path.exists():
        raise HTTPException(status_code=400, detail=f"YAML not found: {req.yaml_path}")

    # Build a safe run name — prefer user-provided override
    safe_ds = re.sub(r"[^\w\-]", "_", req.dataset_name)[:40]
    model_tag = req.model.replace(".pt", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if req.run_name_override and req.run_name_override.strip():
        safe_override = re.sub(r"[^\w\-]", "_", req.run_name_override.strip())[:40]
        run_name = f"{safe_override}_{ts}"
    else:
        run_name = f"{safe_ds}_{model_tag}_{ts}"

    worker_config = {
        "model":             req.model,
        "data":              str(yaml_path.as_posix()),
        "dataset_name":      safe_ds,
        "epochs":            req.epochs,
        "batch":             req.batch,
        "imgsz":             req.imgsz,
        "patience":          req.patience,
        "lr0":               req.lr0,
        "optimizer":         req.optimizer,
        "workers":           req.workers,
        "device":            req.device,
        "cos_lr":            req.cos_lr,
        "warmup_epochs":     req.warmup_epochs,
        "weight_decay":      req.weight_decay,
        "mosaic":            req.mosaic,
        "memory_budget_gb":  req.memory_budget_gb,
        "project":           str(TRAINING_RUNS_DIR.as_posix()),
        "name":              run_name,
    }

    # Reset session
    _session.reset()
    _session.session_id   = str(uuid.uuid4())[:8]
    _session.start_time   = time.time()
    _session.config       = worker_config
    _session.status       = "running"
    _session.total_epochs = req.epochs

    # Launch subprocess
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(WORKER_SCRIPT),
        json.dumps(worker_config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,   # merge stderr into stdout
        creationflags=flags,
    )
    _session.process = proc

    # Background tasks: stream reader + hardware monitor
    _session._stream_task  = asyncio.create_task(_stream_training())
    _session._monitor_task = asyncio.create_task(_hardware_monitor())

    return {
        "session_id":   _session.session_id,
        "run_name":     run_name,
        "total_epochs": req.epochs,
        "status":       "running",
    }


@router.post("/api/train/stop")
async def stop_training(_admin: dict = Depends(get_admin_user)):
    """Gracefully stop the running training subprocess."""
    global _session

    if _session.status != "running":
        raise HTTPException(status_code=400, detail="No active training session to stop")

    _session.status = "stopping"

    proc = _session.process
    if proc and proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

    # Cancel background tasks
    for task in (_session._stream_task, _session._monitor_task):
        if task and not task.done():
            task.cancel()

    _session.status = "stopped"
    await emit_train_status({"status": "stopped"})
    await emit_train_log("[INFO] Training stopped by user.")
    return {"status": "stopped"}


@router.get("/api/train/status")
async def get_training_status(_admin: dict = Depends(get_admin_user)):
    """Return current session state (used on reconnect)."""
    elapsed = None
    if _session.start_time and _session.status in ("running", "stopping"):
        elapsed = round(time.time() - _session.start_time)

    return {
        "status":       _session.status,
        "session_id":   _session.session_id,
        "epoch":        _session.epoch,
        "total_epochs": _session.total_epochs,
        "elapsed_sec":  elapsed,
        "config":       _session.config,
        "final_result": _session.final_result,
    }


@router.get("/api/train/models")
async def list_models(_admin: dict = Depends(get_admin_user)):
    """List all saved .pt files in the models/ directory."""
    models = []
    all_pt_files = list(MODELS_DIR.rglob("*.pt"))
    for f in sorted(all_pt_files, key=lambda x: x.stat().st_mtime, reverse=True):
        stat = f.stat()
        rel_path = f.relative_to(MODELS_DIR)
        models.append({
            "name":     str(rel_path).replace("\\", "/"),
            "size_mb":  round(stat.st_size / (1024 ** 2), 1),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "path":     str(f),
        })
    return {"models": models}


@router.get("/api/train/models/{model_path:path}")
async def download_model(model_path: str, _admin: dict = Depends(get_admin_user)):
    """Download a .pt model file."""
    # Prevent path traversal
    if ".." in model_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    
    model_file = MODELS_DIR / model_path
    
    try:
        model_file.resolve().relative_to(MODELS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not model_file.exists() or model_file.suffix != ".pt":
        raise HTTPException(status_code=404, detail="Model not found")
        
    return FileResponse(
        path=str(model_file),
        filename=model_file.name,
        media_type="application/octet-stream",
    )


async def cleanup_session():
    """Clean up active training subprocess and background tasks on application shutdown."""
    global _session
    if _session.status == "running":
        _session.status = "stopping"
        proc = _session.process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    for task in (_session._stream_task, _session._monitor_task):
        if task and not task.done():
            task.cancel()

