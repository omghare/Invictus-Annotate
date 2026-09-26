"""
FastAPI app factory — wires all routers, mounts static files,
defines page routes, and wraps with Socket.IO ASGI.
Multi-project routing: /home, /projects/{id}, /projects/{id}/view, /projects/{id}/annotate/{image_id}
"""
from pathlib import Path

import socketio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.socket_manager import sio
import app.auth        as _auth
import app.projects    as _projects
import app.images      as _images
import app.annotations as _annotations
import app.export      as _export
import app.video_processor as _video_processor
import app.trainer     as _trainer

# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Invictus Annotate",
    description="LAN-based multi-project bounding box annotation tool for YOLO dataset creation",
    version="2.0.0",
    docs_url="/api/docs",
)

# ── Static files ──────────────────────────────────────────────────────────────

BASE = Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# ── Templates ─────────────────────────────────────────────────────────────────

templates = Jinja2Templates(directory=BASE / "templates")

# ── API Routers ───────────────────────────────────────────────────────────────

app.include_router(_auth.router,        prefix="/api/auth")
app.include_router(_projects.router,    prefix="/api")
app.include_router(_images.router,      prefix="/api")
app.include_router(_annotations.router, prefix="/api")
app.include_router(_export.router,      prefix="/api")
app.include_router(_video_processor.router)
app.include_router(_trainer.router)

# ── Page Routes ───────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/home")

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html")

@app.get("/home", response_class=HTMLResponse, include_in_schema=False)
async def page_home(request: Request):
    return templates.TemplateResponse(request, "home.html")

@app.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
async def page_dashboard(request: Request, project_id: str):
    """Admin dashboard for a specific project."""
    return templates.TemplateResponse(request, "dashboard.html", {"project_id": project_id})

@app.get("/projects/{project_id}/view", response_class=HTMLResponse, include_in_schema=False)
async def page_project_view(request: Request, project_id: str):
    """Annotator project view — shows their assigned images with filters and pagination."""
    return templates.TemplateResponse(request, "project_view.html")

@app.get("/projects/{project_id}/annotate/{image_id}", response_class=HTMLResponse, include_in_schema=False)
async def page_annotate(request: Request, project_id: str, image_id: str):
    return templates.TemplateResponse(request, "annotate.html", {
        "image_id": image_id,
        "project_id": project_id,
    })

@app.get("/video_processor", response_class=HTMLResponse, include_in_schema=False)
async def page_video_processor(request: Request):
    """Dedicated page for video processing/cutting."""
    return templates.TemplateResponse(request, "video_processor.html")

@app.get("/train", response_class=HTMLResponse, include_in_schema=False)
async def page_train(request: Request):
    """Standalone training page — YOLO model training with live monitoring."""
    return templates.TemplateResponse(request, "training.html")

# ── Backward compatibility redirects ─────────────────────────────────────────

@app.get("/dashboard", include_in_schema=False)
async def compat_dashboard():
    return RedirectResponse(url="/home")

@app.get("/project", include_in_schema=False)
async def compat_project():
    return RedirectResponse(url="/home")

@app.get("/annotate/{image_id}", include_in_schema=False)
async def compat_annotate(image_id: str):
    # Can't know the project_id here — redirect to home
    return RedirectResponse(url="/home")

@app.get("/export", include_in_schema=False)
async def compat_export():
    return RedirectResponse(url="/home")

# ── Startup & Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await init_db()

@app.on_event("shutdown")
async def shutdown():
    await _trainer.cleanup_session()

# ── ASGI app (Socket.IO wrapped around FastAPI) ───────────────────────────────

socket_app = socketio.ASGIApp(sio, app)
