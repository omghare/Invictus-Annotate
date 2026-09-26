"""
Database — SQLite with WAL mode.
Provides async connection dependency and schema initialization.
Supports multiple projects simultaneously.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import aiosqlite

from app.config import DB_PATH, DATA_DIR

# ── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    classes             TEXT NOT NULL,
    source_folder       TEXT,
    thumbnail_image_id  TEXT,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS images (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    filename      TEXT NOT NULL,
    filepath      TEXT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    state         TEXT NOT NULL DEFAULT 'unassigned',
    assigned_to   TEXT,
    assigned_at   TEXT,
    opened_at     TEXT,
    annotated_at  TEXT,
    submitted_at  TEXT,
    FOREIGN KEY (project_id) REFERENCES project(id),
    FOREIGN KEY (assigned_to) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_images_state         ON images(state);
CREATE INDEX IF NOT EXISTS idx_images_assigned      ON images(assigned_to);
CREATE INDEX IF NOT EXISTS idx_images_project       ON images(project_id);
CREATE INDEX IF NOT EXISTS idx_images_project_state ON images(project_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_images_project_filepath ON images(project_id, filepath);

CREATE TABLE IF NOT EXISTS annotations (
    id          TEXT PRIMARY KEY,
    image_id    TEXT NOT NULL,
    class_id    INTEGER NOT NULL,
    class_name  TEXT NOT NULL,
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL,
    created_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (image_id)    REFERENCES images(id),
    FOREIGN KEY (created_by)  REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_ann_image ON annotations(image_id);

CREATE TABLE IF NOT EXISTS project_history (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    name           TEXT NOT NULL,
    date_created   TEXT NOT NULL,
    dataset_size   INTEGER NOT NULL DEFAULT 0,
    date_deleted   TEXT
);
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

# ── Dependency ───────────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """FastAPI dependency — yields an aiosqlite connection, closes on exit."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # Re-apply pragmas (they persist per-connection, not globally)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA synchronous=NORMAL")
    try:
        yield db
    finally:
        await db.close()

# ── Initialisation ───────────────────────────────────────────────────────────

async def init_db() -> None:
    """Called at startup — creates tables, runs migrations, and seeds the default admin user."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await db.commit()

        # ── Run safe migrations for existing installs ────────────────────────
        await _migrate(db)

        # Seed default admin if no users exist
        cur = await db.execute("SELECT COUNT(*) AS cnt FROM users")
        row = await cur.fetchone()
        if row["cnt"] == 0:
            import bcrypt as _bcrypt
            hashed = _bcrypt.hashpw(b"admin123", _bcrypt.gensalt()).decode()
            await db.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (new_id(), "admin", hashed, now_iso()),
            )
            await db.commit()
            print("[DB] Default admin created: admin / admin123")


async def _migrate(db: aiosqlite.Connection) -> None:
    """
    Safe, additive migrations for existing databases.
    Each migration is idempotent — safe to run on every startup.
    """
    # Migration 1: Add thumbnail_image_id to project table (multi-project update)
    cur = await db.execute("PRAGMA table_info(project)")
    cols = {row["name"] for row in await cur.fetchall()}
    if "thumbnail_image_id" not in cols:
        await db.execute("ALTER TABLE project ADD COLUMN thumbnail_image_id TEXT")
        await db.commit()
        print("[DB] Migration: added project.thumbnail_image_id")

    # Migration 2: Ensure the composite index exists (safe — IF NOT EXISTS in schema)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_project_state ON images(project_id, state)"
    )
    await db.commit()

    # Migration 3: Backfill thumbnail_image_id for projects that have images but no thumbnail
    cur = await db.execute(
        "SELECT id FROM project WHERE thumbnail_image_id IS NULL"
    )
    projects_without_thumb = await cur.fetchall()
    for proj_row in projects_without_thumb:
        pid = proj_row["id"]
        img_cur = await db.execute(
            "SELECT id FROM images WHERE project_id = ? ORDER BY filename LIMIT 1",
            (pid,),
        )
        img = await img_cur.fetchone()
        if img:
            await db.execute(
                "UPDATE project SET thumbnail_image_id = ? WHERE id = ?",
                (img["id"], pid),
            )
    await db.commit()

    # Migration 4: Ensure project_history is populated for existing projects
    cur = await db.execute("SELECT * FROM project")
    all_projects = await cur.fetchall()
    for proj in all_projects:
        pid = proj["id"]
        h_cur = await db.execute("SELECT COUNT(*) AS cnt FROM project_history WHERE project_id = ?", (pid,))
        in_history = (await h_cur.fetchone())["cnt"] > 0
        if not in_history:
            img_cur = await db.execute("SELECT COUNT(*) AS cnt FROM images WHERE project_id = ?", (pid,))
            img_count = (await img_cur.fetchone())["cnt"]
            await db.execute(
                "INSERT INTO project_history (id, project_id, name, date_created, dataset_size, date_deleted) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (new_id(), pid, proj["name"], proj["created_at"], img_count)
            )
    await db.commit()

