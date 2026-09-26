"""
Configuration — paths, secrets, constants.
All values can be overridden via .env
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Directories ──────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent          # d:\Invictus_Annotate
DATA_DIR    = BASE_DIR / "data"
DATASET_DIR       = BASE_DIR / "dataset"
DB_PATH           = DATA_DIR / "invictus.db"
MODELS_DIR        = BASE_DIR / "models"
TRAINING_RUNS_DIR = BASE_DIR / "training_runs"

# Auto-create dirs
for _d in [MODELS_DIR, TRAINING_RUNS_DIR]:
    _d.mkdir(exist_ok=True)

# ── Auth ─────────────────────────────────────────────────────
SECRET_KEY                 = os.getenv("SECRET_KEY", "invictus-default-secret")
ALGORITHM                  = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS  = 24

# ── Server ───────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8000"))

# ── Annotation ───────────────────────────────────────────────
MIN_BOX_PX   = 5          # minimum bounding box dimension in pixels
MAX_UNDO     = 50         # maximum undo history depth

# ── Export ───────────────────────────────────────────────────────────────────
DATASET_SPLITS = ["train", "valid", "test"]

# ── Training ──────────────────────────────────────────────────────────────────
TRAINING_VENV_PYTHON = str(BASE_DIR / "venv" / "Scripts" / "python.exe")  # same venv as app
