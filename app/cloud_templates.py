"""
Files placed inside a cloud training bundle: train.py, train.ipynb and README.md.

train.py is self-contained (only needs Python + internet for `pip install ultralytics`) and
runs unchanged on Kaggle, Google Colab or any PC with a GPU. The notebook holds the same
code split into cells. Both pin Ultralytics to the version this app uses, so the trained
weights load here without surprises.
"""
import json
import pprint

TRAIN_SCRIPT = r'''#!/usr/bin/env python3
"""
Invictus Annotate — cloud training for bundle __BUNDLE_ID__ (project "__PROJECT__").

Runs on Kaggle, Google Colab, or any PC with Python; an NVIDIA GPU is used when present.
    python train.py
Result: invictus_result___BUNDLE_ID__.zip  →  Invictus Annotate › Train model › Import cloud result.
Change the settings in TRAIN below if you like (epochs, model size, image size).
"""
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

BUNDLE = __BUNDLE_JSON__

TRAIN = __TRAIN_JSON__

USE_GOOGLE_DRIVE = True    # Colab: keep checkpoints on Drive, so a disconnect can resume instead of restarting
RESUME_IF_POSSIBLE = True  # pick up from last.pt when this run was interrupted before
EXPORT_ONNX = False        # also export model.onnx (for deployment outside Python)


def environment() -> str:
    if os.path.isdir("/kaggle/working"):
        return "kaggle"
    try:
        import google.colab  # noqa: F401
        return "colab"
    except ImportError:
        return "local"


ENV = environment()
WORK = Path({"kaggle": "/kaggle/working", "colab": "/content"}.get(ENV, os.getcwd())).resolve()


def find_bundle() -> Path:
    """The folder holding invictus_bundle.json: a Kaggle dataset, an unzipped upload, or a zip we unpack."""
    here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
    roots = [here, Path.cwd(), Path("/kaggle/input"), Path("/content"), Path("/content/drive/MyDrive"),
             Path("/content/drive/MyDrive/invictus")]
    patterns = ["", "*/", "*/*/", "*/*/*/"]
    candidates = []
    for root in roots:
        if root.is_dir():
            for pat in patterns:
                candidates += sorted(root.glob(pat + "invictus_bundle.json"))
    for c in candidates:
        try:
            if json.loads(c.read_text(encoding="utf-8")).get("id") == BUNDLE["id"]:
                return c.parent
        except (OSError, ValueError):
            pass
    for root in roots:
        if root.is_dir():
            for pat in patterns:
                for z in sorted(root.glob(pat + f"invictus_bundle_*{BUNDLE['id']}*.zip")):
                    dest = WORK / "invictus_bundle" / BUNDLE["id"]
                    print(f"Unpacking {z} …")
                    with zipfile.ZipFile(z) as zf:
                        zf.extractall(dest)
                    return dest
    if candidates:
        return candidates[0].parent
    raise SystemExit("Could not find the bundle. Kaggle: add it as a dataset (Add Input). "
                     "Colab: upload the bundle .zip to the Files panel or to My Drive/invictus/.")


def ensure_ultralytics() -> None:
    from importlib import metadata
    want = BUNDLE["ultralytics_version"]
    try:
        have = metadata.version("ultralytics")
    except metadata.PackageNotFoundError:
        have = None
    if have != want:
        print(f"Installing ultralytics=={want} (the version your Invictus Annotate uses) …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", f"ultralytics=={want}"])


def prepare_dataset(bundle_dir: Path) -> Path:
    """Copy the dataset somewhere writable (Kaggle inputs are read-only) and point data.yaml at it."""
    import yaml
    dst = WORK / "invictus_data" / BUNDLE["id"]
    if not (dst / "data.yaml").exists():
        print(f"Copying the dataset to {dst} …")
        shutil.copytree(bundle_dir / "dataset", dst, dirs_exist_ok=True)
    data = yaml.safe_load((dst / "data.yaml").read_text(encoding="utf-8"))
    data["path"] = str(dst)
    out = dst / "data_abs.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return out


def runs_dir() -> Path:
    if ENV == "colab" and USE_GOOGLE_DRIVE:
        try:
            from google.colab import drive
            drive.mount("/content/drive")
            d = Path("/content/drive/MyDrive/invictus_runs")
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception as exc:                      # no Drive permission → train in the session
            print(f"Google Drive not mounted ({exc}); checkpoints stay in this session.")
    d = WORK / "invictus_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def train(data_yaml: Path):
    import torch
    from ultralytics import YOLO
    if torch.cuda.is_available():
        device = 0
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    else:
        device = "cpu"
        print("WARNING: no GPU. Kaggle: Settings → Accelerator → GPU. Colab: Runtime → Change runtime type → GPU.")
    project, name = runs_dir(), TRAIN["run_name"]
    last = project / name / "weights" / "last.pt"
    t0 = time.time()
    if RESUME_IF_POSSIBLE and last.exists():
        print(f"Resuming from {last}")
        YOLO(str(last)).train(resume=True, device=device)
    else:
        model = YOLO(TRAIN["model"])
        model.train(
            data=str(data_yaml), epochs=TRAIN["epochs"], imgsz=TRAIN["imgsz"], batch=TRAIN["batch"],
            patience=TRAIN["patience"], optimizer=TRAIN["optimizer"], lr0=TRAIN["lr0"], cos_lr=TRAIN["cos_lr"],
            project=str(project), name=name, exist_ok=True, device=device, plots=True,
            save_period=TRAIN.get("save_period", -1), seed=TRAIN.get("seed", 0), workers=TRAIN.get("workers", 4),
        )
    return project / name, round(time.time() - t0), device


def evaluate(run_dir: Path, data_yaml: Path) -> dict:
    import yaml
    from ultralytics import YOLO
    out = {}
    best = run_dir / "weights" / "best.pt"
    model = YOLO(str(best))
    data = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
    test_dir = Path(data.get("path", "")) / data["test"] if data.get("test") else None
    has_test = bool(test_dir and test_dir.is_dir() and any(test_dir.iterdir()))   # a small dataset may have no test images
    splits = ["val"] + (["test"] if has_test else [])
    for split in splits:
        m = model.val(data=str(data_yaml), split=split, plots=split == "val", verbose=False,
                      project=str(run_dir), name=f"eval_{split}", exist_ok=True)
        names = m.names if isinstance(m.names, dict) else dict(enumerate(m.names))
        per_class = {}
        for i, c in enumerate(m.box.ap_class_index):
            p, r, ap50, ap = m.box.class_result(i)
            per_class[names[int(c)]] = {"precision": float(p), "recall": float(r), "mAP50": float(ap50), "mAP50-95": float(ap)}
        out[split] = {
            "metrics/precision(B)": float(m.box.mp), "metrics/recall(B)": float(m.box.mr),
            "metrics/mAP50(B)": float(m.box.map50), "metrics/mAP50-95(B)": float(m.box.map),
            "per_class": per_class,
        }
    if EXPORT_ONNX:
        model.export(format="onnx")
    return out


def package(run_dir: Path, evaluation: dict, seconds: int, device) -> Path:
    import torch
    import ultralytics
    csv = run_dir / "results.csv"
    epochs_done = max(0, len(csv.read_text().splitlines()) - 1) if csv.exists() else None
    result = {
        "bundle_id": BUNDLE["id"], "project": BUNDLE["project"], "project_id": BUNDLE["project_id"],
        "run_name": TRAIN["run_name"], "model": TRAIN["model"], "classes": BUNDLE["classes"],
        "ultralytics_version": ultralytics.__version__, "environment": ENV,
        "device": torch.cuda.get_device_name(0) if device == 0 else "cpu",
        "train_seconds": seconds, "epochs_done": epochs_done, "train_config": TRAIN,
        "metrics": {k: v for k, v in evaluation.get("val", {}).items() if k != "per_class"},
        "per_class": evaluation.get("val", {}).get("per_class", {}),
        "test": evaluation.get("test"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = WORK / f"invictus_result_{BUNDLE['id']}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("invictus_result.json", json.dumps(result, indent=2))
        for rel in ("weights/best.pt", "weights/last.pt", "results.csv", "args.yaml"):
            if (run_dir / rel).exists():
                zf.write(run_dir / rel, rel)
        for img in sorted(run_dir.glob("*.png")):
            zf.write(img, f"plots/{img.name}")
        for img in sorted((run_dir / "eval_val").glob("*.png")):      # same names as the training plots
            zf.write(img, f"plots/eval_{img.name}")
        for onnx in run_dir.glob("weights/*.onnx"):
            zf.write(onnx, f"weights/{onnx.name}")
    print(f"\nDone. Result: {out}  ({out.stat().st_size / 1024 ** 2:.1f} MB)")
    if ENV == "kaggle":
        print("Download it from the notebook's Output panel (right side → /kaggle/working).")
    elif ENV == "colab":
        try:
            from google.colab import files
            files.download(str(out))
        except Exception:
            print("Download it from the Files panel on the left.")
    print("Then in Invictus Annotate: Train model → Import cloud result.")
    return out


def main() -> None:
    print(f"Invictus Annotate cloud training · {BUNDLE['project']} · bundle {BUNDLE['id']} · {ENV}")
    bundle_dir = find_bundle()
    ensure_ultralytics()
    data_yaml = prepare_dataset(bundle_dir)
    run_dir, seconds, device = train(data_yaml)
    evaluation = evaluate(run_dir, data_yaml)
    package(run_dir, evaluation, seconds, device)


def _in_notebook() -> bool:
    return "ipykernel" in sys.modules


if __name__ == "__main__" and not _in_notebook():
    main()
'''


def render_script(bundle: dict, train: dict) -> str:
    # Python literals (True / None), not JSON (true / null)
    return (TRAIN_SCRIPT
            .replace("__BUNDLE_JSON__", pprint.pformat(bundle, indent=4, width=110, sort_dicts=False))
            .replace("__TRAIN_JSON__", pprint.pformat(train, indent=4, width=110, sort_dicts=False))
            .replace("__BUNDLE_ID__", bundle["id"])
            .replace("__PROJECT__", bundle["project"].replace('"', "'")))


def render_notebook(bundle: dict, train: dict) -> str:
    script = render_script(bundle, train)
    def code(src: str) -> dict:
        return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                "source": src.splitlines(keepends=True)}
    def md(src: str) -> dict:
        return {"cell_type": "markdown", "metadata": {}, "source": src.splitlines(keepends=True)}
    cells = [
        md(f"# Train “{bundle['project']}” on a free cloud GPU\n\n"
           f"Bundle `{bundle['id']}` · {bundle['images']} images · {len(bundle['classes'])} classes · "
           f"model `{train['model']}` · {train['epochs']} epochs at {train['imgsz']} px\n\n"
           "1. **Turn on a GPU** — Kaggle: *Settings → Accelerator → GPU T4 x2 / P100* (and *Internet → On*). "
           "Colab: *Runtime → Change runtime type → T4 GPU*.\n"
           "2. **Add the bundle** — Kaggle: *Add Input → Datasets → your uploaded bundle*. "
           "Colab: upload the bundle `.zip` to the Files panel, or to *My Drive/invictus/*.\n"
           "3. **Run all cells.** When it finishes, download `invictus_result_….zip` and import it in "
           "Invictus Annotate (*Train model → Import cloud result*).\n\n"
           "Tip (Kaggle): *Save Version → Save & Run All* trains in the background (up to 12 h) — you can close the tab."),
        md("## Settings and code\nEdit `TRAIN` here if you want (e.g. `epochs`, `model`: `yolo11s.pt` / `yolo11m.pt`, `imgsz`)."),
        code(script),
        md("## Train"),
        code("main()"),
    ]
    nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                                       "language_info": {"name": "python"}, "accelerator": "GPU",
                                       "colab": {"provenance": [], "gpuType": "T4"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1)


def render_readme(bundle: dict, train: dict) -> str:
    return f"""# Invictus Annotate — cloud training bundle

Project **{bundle['project']}** · bundle `{bundle['id']}` · {bundle['images']} images
(train {bundle['counts'].get('train', 0)} · valid {bundle['counts'].get('valid', 0)} · test {bundle['counts'].get('test', 0)})
· classes: {', '.join(bundle['classes'])}

Training settings: `{train['model']}`, {train['epochs']} epochs, {train['imgsz']} px, batch {train['batch']}
(`-1` = pick the largest batch that fits the GPU). Change them in `TRAIN` at the top of `train.py` / the notebook.

## Kaggle (free GPU, about 30 h per week)
1. kaggle.com → **Datasets → New Dataset** → upload this `.zip` (Kaggle unpacks it) → Create.
2. **Code → New Notebook** → File → Import Notebook → `train.ipynb` (from this bundle).
3. Right panel: **Add Input** → your dataset. **Settings → Accelerator → GPU**, **Internet → On**
   (both need a phone-verified Kaggle account).
4. **Run All** — or **Save Version → Save & Run All** to train in the background with the tab closed.
5. Download `invictus_result_{bundle['id']}.zip` from the **Output** panel.

## Google Colab (free GPU, sessions up to a few hours)
1. colab.research.google.com → **File → Upload notebook** → `train.ipynb`.
2. **Runtime → Change runtime type → T4 GPU**.
3. Upload this `.zip` to the **Files** panel (left), or to **My Drive/invictus/** (better for big datasets).
4. **Runtime → Run all.** Allow Google Drive when asked: checkpoints are kept on Drive, so if Colab
   disconnects just run all again and it resumes.
5. The result zip downloads automatically when training ends.

## Any PC with a GPU (e.g. your next laptop)
```
pip install ultralytics=={bundle['ultralytics_version']}
python train.py
```

## Bring the model back
Invictus Annotate → **Train model → Import cloud result** → pick `invictus_result_{bundle['id']}.zip`.
The run then shows up in the run history with its accuracy per class, and the model can pre-label
new frames under **Model assist**.
"""
