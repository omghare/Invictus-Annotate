"""
FastAPI app factory — wires all routers, mounts static files,
defines page routes, and wraps with Socket.IO ASGI.
Multi-project routing: /home, /projects/{id}, /projects/{id}/view, /projects/{id}/annotate/{image_id}
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import API_DOCS, ASSET_VERSION, BACKUP_KEEP, BASE_DIR, STATIC_DIR
from app.database import backup_db, init_db
from app.socket_manager import sio
import app.auth        as _auth
import app.projects    as _projects
import app.images      as _images
import app.annotations as _annotations
import app.sequence    as _sequence
import app.export      as _export
import app.video_processor as _video_processor
import app.trainer     as _trainer
import app.jobs        as _jobs
import app.assist      as _assist          # registers the "predict" job
import app.cloud       as _cloud           # registers the "cloud_bundle" job
import app.compute     as _compute

log = logging.getLogger("invictus")


async def _daily_backups():
    """One consistent DB copy per day in data/backups/ (the newest BACKUP_KEEP are kept)."""
    while True:
        try:
            made = await asyncio.to_thread(backup_db, BACKUP_KEEP)
            if made:
                log.info("Database backup written: %s", made)
        except Exception as exc:
            log.warning("Database backup failed: %s", exc)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    try:
        await asyncio.to_thread(_trainer.recover_unlinked_runs)
        await _trainer.import_legacy_runs()
    except Exception as exc:
        log.warning("Run history import skipped: %s", exc)
    backups = asyncio.create_task(_daily_backups())
    yield
    backups.cancel()
    await _jobs.shutdown()
    await _trainer.cleanup_session()


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Invictus Annotate",
    description="LAN-based multi-project bounding box annotation tool for YOLO dataset creation",
    version="2.2.0",
    docs_url="/api/docs" if API_DOCS else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if API_DOCS else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response

# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Templates ─────────────────────────────────────────────────────────────────

templates = Jinja2Templates(directory=BASE_DIR / "templates")
# {{ asset('css/app.css') }} → /static/css/app.css?v=<mtime> so browsers never run stale JS/CSS
templates.env.globals["asset"] = lambda path: f"/static/{path}?v={ASSET_VERSION}"

# ── API Routers ───────────────────────────────────────────────────────────────

app.include_router(_auth.router,        prefix="/api/auth")
app.include_router(_projects.router,    prefix="/api")
app.include_router(_images.router,      prefix="/api")
app.include_router(_annotations.router, prefix="/api")
app.include_router(_sequence.router,    prefix="/api")
app.include_router(_export.router,      prefix="/api")
app.include_router(_video_processor.router)
app.include_router(_trainer.router)
app.include_router(_jobs.router)
app.include_router(_assist.router)
app.include_router(_cloud.router)
app.include_router(_compute.router)

# ── Page Routes ───────────────────────────────────────────────────────────────

def _page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/home")

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def page_login(request: Request):
    return _page(request, "login.html")

@app.get("/home", response_class=HTMLResponse, include_in_schema=False)
async def page_home(request: Request):
    return _page(request, "home.html", nav="projects")

@app.get("/users", response_class=HTMLResponse, include_in_schema=False)
async def page_users(request: Request):
    return _page(request, "users.html", nav="users")

@app.get("/account", response_class=HTMLResponse, include_in_schema=False)
async def page_account(request: Request):
    return _page(request, "account.html", nav="account")

@app.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
async def page_dashboard(request: Request, project_id: str):
    """Admin dashboard for a specific project."""
    return _page(request, "dashboard.html", project_id=project_id, nav="project")

@app.get("/projects/{project_id}/view", response_class=HTMLResponse, include_in_schema=False)
async def page_project_view(request: Request, project_id: str):
    """Annotator project view — their assigned images with filters and pagination."""
    return _page(request, "project_view.html", project_id=project_id, nav="project")

@app.get("/projects/{project_id}/annotate/{image_id}", response_class=HTMLResponse, include_in_schema=False)
async def page_annotate(request: Request, project_id: str, image_id: str):
    return _page(request, "annotate.html", image_id=image_id, project_id=project_id)

@app.get("/video_processor", response_class=HTMLResponse, include_in_schema=False)
async def page_video_processor(request: Request):
    """Dedicated page for cutting clips out of long videos."""
    return _page(request, "video_processor.html", nav="video")

@app.get("/train", response_class=HTMLResponse, include_in_schema=False)
async def page_train(request: Request):
    """Standalone training page — YOLO model training with live monitoring."""
    return _page(request, "training.html", nav="train")

@app.get("/system", response_class=HTMLResponse, include_in_schema=False)
async def page_system(request: Request):
    """Compute profile (CPU / GPU), what is installed, and disk usage."""
    return _page(request, "system.html", nav="system")

# ── Backward compatibility redirects ─────────────────────────────────────────

@app.get("/dashboard", include_in_schema=False)
@app.get("/project", include_in_schema=False)
@app.get("/export", include_in_schema=False)
async def compat_redirect():
    return RedirectResponse(url="/home")

@app.get("/annotate/{image_id}", include_in_schema=False)
async def compat_annotate(image_id: str):
    return RedirectResponse(url="/home")

# ── ASGI app (Socket.IO wrapped around FastAPI) ───────────────────────────────

socket_app = socketio.ASGIApp(sio, app)
