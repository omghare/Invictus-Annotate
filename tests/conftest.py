"""
Test harness: every test session runs against a throwaway data/dataset directory,
so the real data/invictus.db is never touched.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="invictus_test_"))
os.environ["INVICTUS_DATA_DIR"] = str(_TMP / "data")
os.environ["INVICTUS_DATASET_DIR"] = str(_TMP / "dataset")
os.environ["INVICTUS_MODELS_DIR"] = str(_TMP / "models")
os.environ["INVICTUS_RUNS_DIR"] = str(_TMP / "training_runs")
os.environ["INVICTUS_MODEL_CACHE"] = str(_TMP / "model_cache")
os.environ["INVICTUS_TEST_PROVIDERS"] = "1"          # enables the torch-free "fixture" inference provider
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from PIL import Image  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402
import app.socket_manager as sm  # noqa: E402


@pytest.fixture(autouse=True)
def no_socket_emits(monkeypatch):
    """Record socket events instead of sending them."""
    sent = []

    async def fake_emit(event, data=None, room=None, **kw):
        sent.append((event, data, room))

    monkeypatch.setattr(sm.sio, "emit", fake_emit)
    return sent


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest_asyncio.fixture
async def client():
    db_path = database.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    await database.init_db()
    # The seeded admin must change admin123 first (enforced by the API); tests skip that step
    # unless they test it (see force_password_change).
    async with database.connect() as db:
        await db.execute("UPDATE users SET must_change_password = 0")
        await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def force_password_change(username="admin"):
    async with database.connect() as db:
        await db.execute("UPDATE users SET must_change_password = 1 WHERE username = ?", (username,))
        await db.commit()


async def login(client, username="admin", password="admin123") -> dict:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "_token": data["access_token"], **data}


def hdr(auth: dict) -> dict:
    return {"Authorization": auth["Authorization"]}


def make_images(folder: Path, names: list[str], size=(64, 48)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        Image.new("RGB", size, (120, 80, 40)).save(folder / n)
    return folder


def tmp_dir(name: str) -> Path:
    p = _TMP / "work" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


async def make_project(client, admin, classes=("car", "person"), name="Test Project") -> str:
    r = await client.post("/api/projects", json={"name": name, "classes": list(classes)}, headers=hdr(admin))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_user(client, admin, username="ann1", password="pass1234") -> dict:
    r = await client.post("/api/users", json={"username": username, "password": password}, headers=hdr(admin))
    assert r.status_code == 201, r.text
    return {"id": r.json()["id"], **(await login(client, username, password))}
