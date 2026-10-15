# Installing and moving Invictus Annotate

## On a laptop without an NVIDIA GPU (like the current one)

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements-cpu.txt
python run.py            (or double-click start.bat)
```

Open http://localhost:8000. First sign-in: `admin` / `admin123` — you are asked to choose a new password straight away.
Everything works on the CPU: labelling, model assist (~0.3 s per frame with a nano model) and small trainings.
For long trainings use **cloud training**: a project's *Export & cloud training* section builds a Kaggle / Colab bundle.

## On a machine with an NVIDIA GPU (e.g. an RTX 4050 laptop)

1. Install a recent NVIDIA driver.
2. Create the venv as above, but install `requirements-gpu.txt` instead (about 3 GB download):
   ```
   pip install --no-cache-dir -r requirements-gpu.txt
   ```
3. Start the app. **System** (left menu) should show *GPU mode* and the GPU's name. Training ("Device: auto")
   and model assist now use the GPU — nothing else to change.

## Moving the project to another computer

Copy the whole folder, or at least: `app/ static/ templates/ run.py requirements*.txt` plus your data:

| Folder | What it holds |
|---|---|
| `data/` | database (all projects, users, labels), thumbnails, daily backups, logs |
| `dataset/` | exports, frames extracted from videos, cloud bundles |
| `models/` | trained and imported models |
| `training_runs/` | Ultralytics run folders (plots, `last.pt` for resuming) |
| `model_cache/` | downloaded base weights (`yolo11n.pt` …) — optional, re-downloaded when missing |

Images you imported from other folders are referenced where they are (not copied). If those folders end up
somewhere else on the new machine, use *Import images → Moved your images? → Relink*.

Don't copy `venv/` — create a fresh one on the new machine (it contains machine-specific builds).

## Settings

Copy `.env.example` to `.env`. The most useful ones:

- `HOST=127.0.0.1` — only this computer can open the app (default `0.0.0.0` = the whole LAN).
- `INVICTUS_MODEL_CACHE` — put downloaded model weights on a drive with more space.
- `INVICTUS_DATA_DIR`, `INVICTUS_DATASET_DIR`, `INVICTUS_MODELS_DIR`, `INVICTUS_RUNS_DIR` — move the big folders.

## Backups

A consistent copy of the database is written to `data/backups/` once a day while the app runs (the last 14
are kept). To restore: stop the app, copy a backup over `data/invictus.db`, start the app.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```
