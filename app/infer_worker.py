"""
Inference worker — runs model inference in its own process (spawned by app/assist.py), so
torch never loads into the web server and a crash can't take it down.

Usage: python infer_worker.py <config.json>
Config: {provider, model, images: [{id, path, w, h}], conf, iou, imgsz, device, max_det, stop_file}
Prints one JSON event per line on stdout:
  {"__event__": "ready", "device": "cpu", "names": [...]}
  {"__event__": "pred", "image_id": ..., "boxes": [{class_name, conf, x, y, w, h}]}   (image pixels)
  {"__event__": "skip", "image_id": ..., "reason": ...}
  {"__event__": "error", "message": ...}
  {"__event__": "done", "count": n}

Providers live in PROVIDERS. "yolo" (any Ultralytics detection .pt) works on CPU and GPU.
GPU-class providers (SAM 2 masks, Grounding DINO text prompts, …) are added as another
function with the same contract; see app/compute.py.
"""
import json
import math
import os
import sys
import time
from pathlib import Path

try:
    import psutil
    _cores = str(psutil.cpu_count(logical=False) or 2)
    for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(_var, _cores)
except Exception:
    psutil = None


def emit(event: str, **data) -> None:
    def fin(v):
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, dict):
            return {k: fin(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [fin(x) for x in v]
        return v
    print(json.dumps(fin({"__event__": event, **data})), flush=True)


def _stop_requested(cfg: dict) -> bool:
    sf = cfg.get("stop_file")
    return bool(sf) and Path(sf).exists()


def _resolve_device(requested: str) -> str:
    import torch
    requested = (requested or "auto").strip().lower()
    if requested in ("auto", ""):
        return "0" if torch.cuda.is_available() else "cpu"
    if requested != "cpu" and not torch.cuda.is_available():
        return "cpu"
    return requested


# ── Providers ─────────────────────────────────────────────────────────────────

def run_yolo(cfg: dict) -> int:
    from ultralytics import YOLO

    model = YOLO(cfg["model"])
    device = _resolve_device(cfg.get("device", "auto"))
    names = model.names if isinstance(model.names, dict) else dict(enumerate(model.names))
    emit("ready", device=device, names=[names[k] for k in sorted(names)])

    count = 0
    for item in cfg["images"]:
        if _stop_requested(cfg):
            break
        try:
            r = model.predict(item["path"], conf=cfg.get("conf", 0.25), iou=cfg.get("iou", 0.7),
                              imgsz=cfg.get("imgsz", 640), max_det=cfg.get("max_det", 300),
                              device=device, verbose=False)[0]
        except Exception as exc:
            emit("skip", image_id=item["id"], reason=str(exc)[:300])
            continue
        oh, ow = r.orig_shape
        # The DB size is what the annotator draws on; scale if the decoder saw a different size
        sx, sy = (item.get("w") or ow) / ow, (item.get("h") or oh) / oh
        boxes = []
        if r.boxes is not None and len(r.boxes):
            for (x1, y1, x2, y2), conf, cls in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
                boxes.append({"class_name": names[int(cls)], "conf": round(float(conf), 4),
                              "x": x1 * sx, "y": y1 * sy, "w": (x2 - x1) * sx, "h": (y2 - y1) * sy})
        emit("pred", image_id=item["id"], boxes=boxes)
        count += 1
    return count


def run_fixture(cfg: dict) -> int:
    """Test-only provider (no torch): returns the boxes given in the config for every image."""
    emit("ready", device="cpu", names=cfg.get("names", []))
    count = 0
    for item in cfg["images"]:
        if _stop_requested(cfg):
            break
        emit("pred", image_id=item["id"], boxes=cfg.get("fixture_boxes", []))
        count += 1
        time.sleep(cfg.get("fixture_delay", 0))
    return count


PROVIDERS = {"yolo": run_yolo}
if os.getenv("INVICTUS_TEST_PROVIDERS") == "1":
    PROVIDERS["fixture"] = run_fixture


def main() -> None:
    try:
        cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        provider = PROVIDERS.get(cfg.get("provider", "yolo"))
        if provider is None:
            raise ValueError(f"Unknown provider '{cfg.get('provider')}'")
        n = provider(cfg)
        emit("done", count=n)
    except Exception as exc:
        emit("error", message=str(exc)[:2000])
        sys.exit(1)


if __name__ == "__main__":
    main()
