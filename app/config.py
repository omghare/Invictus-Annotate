"""
Configuration — paths, secrets, constants.
All values can be overridden via .env
"""
import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Directories ──────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent          # repo root
# INVICTUS_DATA_DIR lets tests / sandboxes run against a separate database
DATA_DIR    = Path(os.getenv("INVICTUS_DATA_DIR", BASE_DIR / "data"))
DATASET_DIR       = Path(os.getenv("INVICTUS_DATASET_DIR", BASE_DIR / "dataset"))
DB_PATH           = DATA_DIR / "invictus.db"
THUMBS_DIR        = DATA_DIR / "thumbs"
BACKUPS_DIR       = DATA_DIR / "backups"
LOGS_DIR          = DATA_DIR / "logs"
EXTRACTED_FRAMES_DIR = DATASET_DIR / "extracted_frames"
BUNDLES_DIR       = DATASET_DIR / "_cloud_bundles"
MODELS_DIR        = Path(os.getenv("INVICTUS_MODELS_DIR", BASE_DIR / "models"))
TRAINING_RUNS_DIR = Path(os.getenv("INVICTUS_RUNS_DIR", BASE_DIR / "training_runs"))
# Downloaded base weights (yolo11n.pt, SAM, …). Point it at a roomier drive on another machine.
MODEL_CACHE_DIR   = Path(os.getenv("INVICTUS_MODEL_CACHE", BASE_DIR / "model_cache"))
STATIC_DIR        = BASE_DIR / "static"

# Auto-create dirs
for _d in [DATA_DIR, THUMBS_DIR, BACKUPS_DIR, LOGS_DIR, MODELS_DIR, TRAINING_RUNS_DIR, MODEL_CACHE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Auth ─────────────────────────────────────────────────────
_LEGACY_DEFAULT_SECRET = "invictus-default-secret"


def _load_secret_key() -> str:
    """Use SECRET_KEY from the environment, else a random key persisted on first run."""
    env_key = os.getenv("SECRET_KEY")
    if env_key and env_key != _LEGACY_DEFAULT_SECRET:
        return env_key
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    key = secrets.token_urlsafe(48)
    key_file.write_text(key, encoding="utf-8")
    return key


SECRET_KEY                 = _load_secret_key()
ALGORITHM                  = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS  = 24

MIN_PASSWORD_LEN           = 8
MEDIA_COOKIE               = "ia_media"     # HttpOnly copy of the token for <img>/<video>/download URLs

# ── Server ───────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")         # 127.0.0.1 = this machine only
API_DOCS = os.getenv("INVICTUS_API_DOCS", "0") == "1"
BACKUP_KEEP = int(os.getenv("INVICTUS_BACKUP_KEEP", "14"))


def _compute_asset_version() -> str:
    """Cache-buster for /static URLs — changes whenever any static file changes."""
    try:
        latest = max(p.stat().st_mtime for p in STATIC_DIR.rglob("*") if p.is_file())
        return str(int(latest))
    except ValueError:
        return "0"


ASSET_VERSION = _compute_asset_version()

# ── Annotation ───────────────────────────────────────────────
MIN_BOX_PX   = 5          # minimum bounding box dimension in pixels
MAX_UNDO     = 50         # maximum undo history depth
MAX_BULK_ITEMS = 5000     # max frames touched by one propagate / interpolate call
BATCH_HISTORY_KEEP = 50   # undoable bulk batches kept per project

# ── Images ───────────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
THUMB_SIZES = (160, 320, 640)

# ── Export ───────────────────────────────────────────────────────────────────
DATASET_SPLITS = ["train", "valid", "test"]
