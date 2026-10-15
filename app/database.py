"""
Database — SQLite with WAL mode.
Provides async connection dependency and schema initialization.
Supports multiple projects simultaneously.
"""
import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import aiosqlite

from app.config import BACKUPS_DIR, DB_PATH, DATA_DIR

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

-- Undo log for bulk operations (propagate / interpolate)
CREATE TABLE IF NOT EXISTS annotation_batches (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    undone_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_project ON annotation_batches(project_id, created_at);

CREATE TABLE IF NOT EXISTS annotation_batch_items (
    batch_id          TEXT NOT NULL,
    image_id          TEXT NOT NULL,
    prev_state        TEXT NOT NULL,
    prev_assigned_to  TEXT,
    prev_background   INTEGER NOT NULL DEFAULT 0,
    prev_annotations  TEXT NOT NULL,
    new_hash          TEXT NOT NULL,
    PRIMARY KEY (batch_id, image_id)
);

-- Background jobs (model pre-labelling, label checks, cloud bundles)
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    project_id   TEXT,
    status       TEXT NOT NULL,          -- queued | running | done | failed | cancelled | interrupted
    progress     REAL NOT NULL DEFAULT 0,
    message      TEXT,
    params       TEXT NOT NULL DEFAULT '{}',
    result       TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at);

-- Raw model output per image (pre-labelling and the label-error finder read from here)
CREATE TABLE IF NOT EXISTS model_predictions (
    job_id      TEXT NOT NULL,
    image_id    TEXT NOT NULL,
    model       TEXT NOT NULL,
    boxes       TEXT NOT NULL,           -- [{class_name, conf, x, y, w, h}] in image pixels
    created_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, image_id)
);
CREATE INDEX IF NOT EXISTS idx_pred_image ON model_predictions(image_id);

-- Every training run, local or trained in the cloud and imported
CREATE TABLE IF NOT EXISTS training_runs (
    id            TEXT PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    source        TEXT NOT NULL DEFAULT 'local',   -- local | cloud
    status        TEXT NOT NULL,                   -- running | complete | stopped | error | interrupted | imported
    model         TEXT,
    dataset       TEXT,
    config        TEXT NOT NULL DEFAULT '{}',
    metrics       TEXT NOT NULL DEFAULT '{}',
    best_epoch    INTEGER,
    epochs_done   INTEGER NOT NULL DEFAULT 0,
    total_epochs  INTEGER,
    error         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

# ── Dependency ───────────────────────────────────────────────────────────────

async def _open() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # Re-apply pragmas (they persist per-connection, not globally)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA synchronous=NORMAL")
    # Wait for a competing writer instead of failing with "database is locked"
    await db.execute("PRAGMA busy_timeout=8000")
    return db


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """FastAPI dependency — yields an aiosqlite connection, closes on exit."""
    db = await _open()
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def connect() -> AsyncGenerator[aiosqlite.Connection, None]:
    """The same connection outside a request (background jobs, startup tasks)."""
    db = await _open()
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
        await db.execute("PRAGMA busy_timeout=8000")
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
                "INSERT INTO users (id, username, password_hash, is_admin, created_at, must_change_password) "
                "VALUES (?, ?, ?, 1, ?, 1)",
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
    cols = await _columns(db, "project")
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

    # Migration 5: users — soft delete + forced password change
    user_cols = await _columns(db, "users")
    if "is_active" not in user_cols:
        await db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "must_change_password" not in user_cols:
        await db.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        # Force a change if the seeded admin still uses the default password
        import bcrypt as _bcrypt
        cur = await db.execute("SELECT id, password_hash FROM users WHERE username = 'admin'")
        row = await cur.fetchone()
        if row:
            try:
                if _bcrypt.checkpw(b"admin123", row["password_hash"].encode()):
                    await db.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (row["id"],))
            except ValueError:
                pass
    await db.commit()

    # Migration 6: frame sequence ordering + background (no-object) frames
    img_cols = await _columns(db, "images")
    if "seq" not in img_cols:
        await db.execute("ALTER TABLE images ADD COLUMN seq INTEGER")
    if "seq_group" not in img_cols:
        await db.execute("ALTER TABLE images ADD COLUMN seq_group TEXT")
    if "is_background" not in img_cols:
        await db.execute("ALTER TABLE images ADD COLUMN is_background INTEGER NOT NULL DEFAULT 0")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_images_project_seq ON images(project_id, seq)")
    await db.commit()
    await _backfill_seq(db)

    # Migration 7: annotation provenance (manual / carried / propagated / interpolated) + track ids
    ann_cols = await _columns(db, "annotations")
    if "source" not in ann_cols:
        await db.execute("ALTER TABLE annotations ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if "track_id" not in ann_cols:
        await db.execute("ALTER TABLE annotations ADD COLUMN track_id TEXT")
    await db.commit()

    # Migration 8: token revocation, edit conflicts, review, model confidence
    if "token_version" not in await _columns(db, "users"):
        await db.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
    img_cols = await _columns(db, "images")
    for col, ddl in (("version", "INTEGER NOT NULL DEFAULT 0"), ("review_status", "TEXT"),
                     ("review_note", "TEXT"), ("reviewed_by", "TEXT"), ("reviewed_at", "TEXT")):
        if col not in img_cols:
            await db.execute(f"ALTER TABLE images ADD COLUMN {col} {ddl}")
    if "confidence" not in await _columns(db, "annotations"):
        await db.execute("ALTER TABLE annotations ADD COLUMN confidence REAL")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_images_project_assigned ON images(project_id, assigned_to)")
    # Leftovers of images deleted by older versions
    await db.execute("DELETE FROM annotation_batch_items WHERE image_id NOT IN (SELECT id FROM images)")
    # Jobs / runs that were running when the server stopped can never finish
    await db.execute("UPDATE jobs SET status='interrupted', finished_at=? WHERE status IN ('queued', 'running')",
                     (now_iso(),))
    await db.execute("UPDATE training_runs SET status='interrupted', finished_at=? WHERE status='running'",
                     (now_iso(),))
    await db.commit()

    # Repair: project thumbnails that point at deleted images (older versions left these behind)
    await db.execute("""
        UPDATE project SET thumbnail_image_id = (
            SELECT id FROM images WHERE images.project_id = project.id
            ORDER BY COALESCE(seq, 0), filename LIMIT 1)
        WHERE thumbnail_image_id IS NULL
           OR thumbnail_image_id NOT IN (SELECT id FROM images WHERE images.project_id = project.id)
    """)
    await db.commit()


def backup_db(keep: int) -> Optional[Path]:
    """
    Consistent copy of the live database (SQLite online backup, safe while the app runs)
    into data/backups/, at most one per day; keeps the newest `keep`. Blocking — run in a thread.
    """
    if not DB_PATH.exists():
        return None
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUPS_DIR / f"invictus_{datetime.now().strftime('%Y%m%d')}.db"
    if dst.exists():
        return None
    tmp = dst.with_suffix(".tmp")
    src = sqlite3.connect(DB_PATH)
    try:
        out = sqlite3.connect(tmp)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    tmp.replace(dst)
    dailies = sorted(BACKUPS_DIR.glob("invictus_????????.db"))
    for old in dailies[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dst


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in await cur.fetchall()}


async def _backfill_seq(db: aiosqlite.Connection) -> None:
    """
    Give every image without a seq a stable position: groups (source folders) in
    import order, natural filename order inside each group. Idempotent.
    """
    from pathlib import Path
    from app.seq_utils import natural_key, seq_group_for

    cur = await db.execute("SELECT DISTINCT project_id FROM images WHERE seq IS NULL")
    project_ids = [r["project_id"] for r in await cur.fetchall()]
    for pid in project_ids:
        cur = await db.execute(
            "SELECT id, filename, filepath, seq FROM images WHERE project_id = ? ORDER BY rowid",
            (pid,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        start = max((r["seq"] for r in rows if r["seq"] is not None), default=0)
        pending = [r for r in rows if r["seq"] is None]

        groups: dict[str, list[dict]] = {}
        for r in pending:  # dict keeps first-seen (import) order
            groups.setdefault(str(Path(r["filepath"]).parent), []).append(r)

        updates = []
        seq = start
        for folder, items in groups.items():
            group = seq_group_for(Path(folder))
            for r in sorted(items, key=lambda r: natural_key(r["filename"])):
                seq += 1
                updates.append((seq, group, r["id"]))
        await db.executemany("UPDATE images SET seq = ?, seq_group = ? WHERE id = ?", updates)
    await db.commit()

