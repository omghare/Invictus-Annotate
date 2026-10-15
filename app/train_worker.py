"""
Training worker — runs in its own process (spawned by app/trainer.py).
Prints one JSON event per line on stdout: {"__event__": "<type>", ...}.
Stop protocol: the parent creates config["stop_file"]; the worker checks it after every
batch and exits cleanly between batches (never while weights are being written).
"""
import json
import math
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# CPU Performance Optimization: limit OpenMP/BLAS threads to physical CPU cores
try:
    import psutil
    physical_cpus = str(psutil.cpu_count(logical=False) or 2)
    os.environ.setdefault("OMP_NUM_THREADS", physical_cpus)
    os.environ.setdefault("MKL_NUM_THREADS", physical_cpus)
    os.environ.setdefault("OPENBLAS_NUM_THREADS", physical_cpus)
except Exception:
    psutil = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _finite(obj):
    """Replace NaN/inf (invalid JSON — breaks the browser's socket parser) with None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def emit(event_type: str, data: dict):
    """Print a JSON-encoded event line that the parent process can detect."""
    print(json.dumps(_finite({"__event__": event_type, **data}), allow_nan=False), flush=True)


class StopRequested(KeyboardInterrupt):
    pass


def _watch_parent(parent_pid: int):
    """Exit if the web server dies, so a crashed server never leaves the GPU busy."""
    if not psutil or not parent_pid:
        return
    while True:
        time.sleep(5)
        if not psutil.pid_exists(parent_pid):
            os._exit(3)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        emit("error", {"message": "train_worker: missing config argument"})
        sys.exit(1)

    try:
        config = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        emit("error", {"message": f"train_worker: invalid JSON config — {exc}"})
        sys.exit(1)

    threading.Thread(target=_watch_parent, args=(int(config.get("parent_pid") or 0),), daemon=True).start()
    stop_file = Path(config["stop_file"]) if config.get("stop_file") else None

    try:
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        emit("error", {"message": f"ultralytics or torch is not installed in this environment. "
                                  f"Run: pip install ultralytics — Details: {exc}"})
        sys.exit(1)

    # ── Parse config ──────────────────────────────────────────────────────────
    model_name    = config["model"]
    data_yaml     = config["data"]
    epochs        = int(config.get("epochs", 100))
    batch         = int(config.get("batch", 16))
    imgsz         = int(config.get("imgsz", 640))
    patience      = int(config.get("patience", 50))
    lr0           = float(config.get("lr0", 0.01))
    optimizer     = config.get("optimizer", "SGD")
    workers       = int(config.get("workers", 2))
    project_dir   = config.get("project", "training_runs")
    run_name      = config.get("name", "train")
    device        = str(config.get("device", "cpu")).strip() or "cpu"
    cos_lr        = bool(config.get("cos_lr", False))
    warmup_epochs = float(config.get("warmup_epochs", 3.0))
    weight_decay  = float(config.get("weight_decay", 0.0005))
    mosaic        = float(config.get("mosaic", 1.0))
    memory_budget_gb = config.get("memory_budget_gb")

    if not torch.cuda.is_available() and device != "cpu":
        emit("log", {"line": f"[INFO] CUDA is not available — switching training device from '{device}' to 'cpu'."})
        device = "cpu"
    config["device"] = device

    # ── Memory budget: a hard cap on this process's share of VRAM ─────────────
    if memory_budget_gb and device != "cpu":
        try:
            dev_index = int(device.split(",")[0])
            total_gb = torch.cuda.get_device_properties(dev_index).total_memory / 1024 ** 3
            fraction = max(0.05, min(1.0, float(memory_budget_gb) / total_gb))
            torch.cuda.set_per_process_memory_fraction(fraction, dev_index)
            emit("log", {"line": f"[INFO] VRAM capped at {memory_budget_gb:.1f} GB ({fraction:.0%} of {total_gb:.1f} GB)."})
        except Exception as exc:
            emit("log", {"line": f"[WARN] Could not apply VRAM budget: {exc}"})
    elif memory_budget_gb:
        emit("log", {"line": "[INFO] Memory budget only applies to GPU training; ignored in CPU mode."})

    emit("started", {"model": model_name, "config": config})

    epoch_times: list[float] = []
    epoch_start = [0.0]
    best = {"fitness": -1.0, "epoch": 0}
    weights_dir = Path(project_dir) / run_name / "weights"

    # ── Callbacks ─────────────────────────────────────────────────────────────
    def on_train_epoch_start(trainer):
        epoch_start[0] = time.monotonic()

    def on_train_batch_end(trainer):
        if stop_file and stop_file.exists():
            raise StopRequested()

    def on_fit_epoch_end(trainer):
        """Runs after validation and checkpoint saving, so metrics belong to this epoch."""
        # Ultralytics fires this once more after the final best.pt validation — report each epoch once
        if int(trainer.epoch) + 1 == best.get("last_emitted"):
            return
        best["last_emitted"] = int(trainer.epoch) + 1
        epoch_ms = round((time.monotonic() - epoch_start[0]) * 1000)
        epoch_times.append(epoch_ms)
        del epoch_times[:-5]

        metrics = {k: float(v) for k, v in (getattr(trainer, "metrics", None) or {}).items()
                   if isinstance(v, (int, float))}

        loss_items, loss_names = [], []
        tloss = getattr(trainer, "tloss", None)          # epoch-average training losses
        if tloss is not None:
            try:
                loss_items = [round(float(x), 6) for x in (tloss if tloss.ndim else [tloss])]
            except Exception:
                pass
        try:
            loss_names = list(trainer.loss_names or [])
        except Exception:
            pass

        try:
            lr = float(trainer.optimizer.param_groups[0]["lr"])
        except Exception:
            lr = 0.0

        fitness = getattr(trainer, "fitness", None)
        if not fitness and "metrics/mAP50(B)" in metrics and "metrics/mAP50-95(B)" in metrics:
            fitness = 0.1 * metrics["metrics/mAP50(B)"] + 0.9 * metrics["metrics/mAP50-95(B)"]
        fitness = float(fitness or 0.0)
        if fitness > best["fitness"]:
            best["fitness"], best["epoch"] = fitness, int(trainer.epoch) + 1

        best_pt, last_pt = weights_dir / "best.pt", weights_dir / "last.pt"
        emit("epoch", {
            "epoch":        int(trainer.epoch) + 1,
            "epochs":       int(trainer.epochs),
            "metrics":      metrics,
            "loss_items":   loss_items,
            "loss_names":   loss_names,
            "lr":           lr,
            "best_epoch":   best["epoch"],
            "best_pt":      str(best_pt) if best_pt.exists() else None,
            "last_pt":      str(last_pt) if last_pt.exists() else None,
            "epoch_ms":     epoch_ms,
            "avg_epoch_ms": round(sum(epoch_times) / len(epoch_times)) if epoch_times else 0,
        })

    def on_train_end(trainer):
        best_pt, last_pt = weights_dir / "best.pt", weights_dir / "last.pt"
        final_metrics = {k: float(v) for k, v in (getattr(trainer, "metrics", None) or {}).items()
                         if isinstance(v, (int, float))}
        emit("complete", {
            "best_pt":       str(best_pt) if best_pt.exists() else None,
            "last_pt":       str(last_pt) if last_pt.exists() else None,
            "final_metrics": final_metrics,
            "run_dir":       str(Path(project_dir) / run_name),
        })

    # ── Train ─────────────────────────────────────────────────────────────────
    try:
        model = YOLO(model_name)
        model.add_callback("on_train_epoch_start", on_train_epoch_start)
        model.add_callback("on_train_batch_end", on_train_batch_end)
        model.add_callback("on_fit_epoch_end", on_fit_epoch_end)
        model.add_callback("on_train_end", on_train_end)

        if config.get("resume"):
            # last.pt carries its own settings (data, epochs, optimizer state); only the device may change
            emit("log", {"line": f"[INFO] Resuming from {model_name}"})
            model.train(resume=True, device=device, workers=workers)
        else:
            model.train(
                data=data_yaml, epochs=epochs, batch=batch, imgsz=imgsz, patience=patience,
                lr0=lr0, optimizer=optimizer, workers=workers, project=project_dir, name=run_name,
                device=device, cos_lr=cos_lr, warmup_epochs=warmup_epochs, weight_decay=weight_decay,
                mosaic=mosaic, verbose=True, exist_ok=True,
            )
    except KeyboardInterrupt:
        last_pt = weights_dir / "last.pt"
        emit("stopped", {"message": "Training stopped by user request.",
                         "last_pt": str(last_pt) if last_pt.exists() else None})
        sys.exit(0)
    except Exception as exc:
        emit("error", {"message": str(exc), "traceback": traceback.format_exc()})
        sys.exit(1)


if __name__ == "__main__":
    main()
