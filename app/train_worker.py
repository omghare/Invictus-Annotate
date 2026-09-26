import os
import sys
import json
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
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def emit(event_type: str, data: dict):
    """Print a JSON-encoded event line that the parent process can detect."""
    print(json.dumps({"__event__": event_type, **data}), flush=True)


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

    # ── Attempt to import ultralytics and torch ──────────────────────────────────
    try:
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        emit("error", {
            "message": (
                f"ultralytics or torch is not installed in this environment. "
                f"Run: pip install ultralytics  —  Details: {exc}"
            )
        })
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
    device        = str(config.get("device", "cpu")).strip()
    cos_lr        = bool(config.get("cos_lr", False))
    warmup_epochs = float(config.get("warmup_epochs", 3.0))
    weight_decay  = float(config.get("weight_decay", 0.0005))
    mosaic        = float(config.get("mosaic", 1.0))
    memory_budget_gb = config.get("memory_budget_gb")

    # Automatic Hardware Device Check & Fallback
    if not torch.cuda.is_available() and device != "cpu":
        emit("log", {"line": f"[INFO] CUDA is not available on this system. Automatically switching training device from '{device}' to 'cpu'."})
        device = "cpu"

    config["device"] = device

    # ── Apply memory budget ───────────────────────────────────────────────────
    if memory_budget_gb is not None:
        budget_mb = int(memory_budget_gb * 1024)
        if torch.cuda.is_available() and device != "cpu":
            os.environ["PYTORCH_CUDA_ALLOC_CONF"] = f"max_split_size_mb:{budget_mb}"
            emit("log", {"line": f"[INFO] VRAM budget set to {memory_budget_gb:.1f} GB."})
        else:
            emit("log", {"line": f"[INFO] Memory budget {memory_budget_gb:.1f} GB noted (CPU mode)."})

    emit("started", {"model": model_name, "config": config})

    # Epoch wall-clock timing for ETA
    _epoch_times: list[float] = []
    _epoch_start: list[float] = [0.0]

    # ── Register callbacks ────────────────────────────────────────────────────
    def on_train_epoch_start(trainer):
        """Record epoch start time."""
        _epoch_start[0] = time.monotonic()

    def on_train_epoch_end(trainer):
        """Called after each training epoch — emit parsed metrics + timing."""
        epoch_ms = round((time.monotonic() - _epoch_start[0]) * 1000)
        _epoch_times.append(epoch_ms)
        # Keep rolling window of last 5 epochs for ETA
        if len(_epoch_times) > 5:
            _epoch_times.pop(0)
        metrics = {}
        if hasattr(trainer, "metrics") and trainer.metrics:
            metrics = {
                k: float(v)
                for k, v in trainer.metrics.items()
                if isinstance(v, (int, float))
            }

        loss_items = []
        loss_names = []
        if hasattr(trainer, "loss_items") and trainer.loss_items is not None:
            try:
                loss_items = [round(float(x), 6) for x in trainer.loss_items]
            except Exception:
                pass
        if hasattr(trainer, "loss_names") and trainer.loss_names:
            try:
                loss_names = list(trainer.loss_names)
            except Exception:
                pass

        # LR from optimizer param groups
        lr = 0.0
        try:
            lr = float(trainer.optimizer.param_groups[0]["lr"])
        except Exception:
            pass

        if not hasattr(on_train_epoch_end, "best_fitness"):
            on_train_epoch_end.best_fitness = -1.0
            on_train_epoch_end.best_epoch = 0

        # Compute fitness to track best epoch
        fitness = getattr(trainer, "fitness", 0.0)
        if fitness is None or fitness == 0.0:
            if "metrics/mAP50(B)" in metrics and "metrics/mAP50-95(B)" in metrics:
                fitness = 0.1 * metrics["metrics/mAP50(B)"] + 0.9 * metrics["metrics/mAP50-95(B)"]
            else:
                fitness = 0.0
        
        if fitness > on_train_epoch_end.best_fitness:
            on_train_epoch_end.best_fitness = fitness
            on_train_epoch_end.best_epoch = int(trainer.epoch) + 1

        weights_dir = Path(project_dir) / run_name / "weights"
        best_pt = weights_dir / "best.pt"
        last_pt = weights_dir / "last.pt"

        avg_ms = round(sum(_epoch_times) / len(_epoch_times)) if _epoch_times else 0
        emit("epoch", {
            "epoch":        int(trainer.epoch) + 1,
            "epochs":       int(trainer.epochs),
            "metrics":      metrics,
            "loss_items":   loss_items,
            "loss_names":   loss_names,
            "lr":           lr,
            "best_epoch":   on_train_epoch_end.best_epoch,
            "best_pt":      str(best_pt) if best_pt.exists() else None,
            "last_pt":      str(last_pt) if last_pt.exists() else None,
            "epoch_ms":     epoch_ms,
            "avg_epoch_ms": avg_ms,
        })

    def on_train_end(trainer):
        """Called after training finishes — emit result with best.pt path."""
        weights_dir = Path(project_dir) / run_name / "weights"
        best_pt = weights_dir / "best.pt"
        last_pt = weights_dir / "last.pt"

        final_metrics = {}
        if hasattr(trainer, "metrics") and trainer.metrics:
            final_metrics = {
                k: float(v)
                for k, v in trainer.metrics.items()
                if isinstance(v, (int, float))
            }

        emit("complete", {
            "best_pt":       str(best_pt) if best_pt.exists() else None,
            "last_pt":       str(last_pt) if last_pt.exists() else None,
            "final_metrics": final_metrics,
            "run_dir":       str(Path(project_dir) / run_name),
        })

    # ── Load model & attach callbacks ───────────────────────────────────────────────
    try:
        model = YOLO(model_name)
        model.add_callback("on_train_epoch_start", on_train_epoch_start)
        model.add_callback("on_train_epoch_end", on_train_epoch_end)
        model.add_callback("on_train_end", on_train_end)

        # ── Start training ────────────────────────────────────────────────────
        model.train(
            data=data_yaml,
            epochs=epochs,
            batch=batch,
            imgsz=imgsz,
            patience=patience,
            lr0=lr0,
            optimizer=optimizer,
            workers=workers,
            project=project_dir,
            name=run_name,
            device=device,
            cos_lr=cos_lr,
            warmup_epochs=warmup_epochs,
            weight_decay=weight_decay,
            mosaic=mosaic,
            verbose=True,
            exist_ok=True,   # allow re-running same run name
        )

    except KeyboardInterrupt:
        emit("stopped", {"message": "Training stopped by user request."})
        sys.exit(0)

    except Exception as exc:
        emit("error", {
            "message":   str(exc),
            "traceback": traceback.format_exc(),
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
