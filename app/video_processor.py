"""
Video tools — probe, stream, lossless clip cutting and frame extraction (admin only).

Frame extraction checks free disk space first and can drop near-duplicate frames: each
frame is compared with the last kept one on a 64×64 grayscale copy, and kept only if enough
pixels changed noticeably. A small object moving still counts (it changes a cluster of
pixels); sensor / JPEG noise in a still scene does not.
"""
import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import aiosqlite
import static_ffmpeg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import get_admin_header_or_query, get_admin_user
from app.common import get_project_or_404
from app.config import EXTRACTED_FRAMES_DIR, VIDEO_EXTS
from app.database import get_db
from app.socket_manager import emit_video_progress

static_ffmpeg.add_paths()

router = APIRouter(tags=["Video Processor"])

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run_command_sync(command: list[str], timeout: Optional[float] = None) -> tuple[int, str, str]:
    """Run a tool (ffmpeg/ffprobe/python) without a console window."""
    resolved = shutil.which(command[0])
    if not resolved:
        return 2, "", f"The system cannot find '{command[0]}'. Please ensure it is installed and on PATH."
    try:
        res = subprocess.run(
            [resolved, *command[1:]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, timeout=timeout,
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return 3, "", "Timed out"
    except OSError as e:
        return 2, "", str(e)


# ── Frame size / disk estimates ───────────────────────────────────────────────

# Bytes per pixel actually seen for ffmpeg output (-q:v 2 JPEG, default PNG) on camera footage;
# deliberately on the high side so the disk check errs towards caution.
_BYTES_PER_PIXEL = {"jpg": 0.25, "png": 1.6}
DISK_RESERVE = 1024 ** 3        # always leave 1 GB free


def estimate_extraction(stats: dict, fps: float, image_format: str) -> dict:
    """Expected frame count and output size for an extraction."""
    src_fps = stats.get("fps") or fps
    eff_fps = min(fps, src_fps) if src_fps else fps
    frames = max(1, int(round((stats.get("duration") or 0) * eff_fps))) if stats.get("duration") else 0
    try:
        w, h = (int(v) for v in str(stats.get("resolution", "")).split("x"))
    except ValueError:
        w, h = 1920, 1080
    per_frame = w * h * _BYTES_PER_PIXEL[image_format]
    return {"frames": frames, "fps": eff_fps, "bytes": int(frames * per_frame), "bytes_per_frame": int(per_frame)}


# Fraction of pixels that must change (by more than PIXEL_DELTA grey levels) to keep a frame
DEDUPE_LEVELS = {1: 0.01, 2: 0.04}      # 1 = light, 2 = strong
PIXEL_DELTA = 24


def frame_signature(path: Path, size: int = 64) -> bytes:
    """Small grayscale copy of a frame (JPEG draft mode makes this a few ms)."""
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        im.draft("L", (size * 2, size * 2))
        return im.convert("L").resize((size, size)).tobytes()


def changed_fraction(a: bytes, b: bytes, delta: int = PIXEL_DELTA) -> float:
    return sum(abs(x - y) > delta for x, y in zip(a, b)) / max(1, len(a))


def drop_near_duplicates(frames: list[Path], min_change: float, on_progress=None) -> int:
    """Delete frames that differ from the last kept frame by less than min_change. Returns the count removed."""
    removed, last = 0, None
    for i, f in enumerate(frames):
        try:
            sig = frame_signature(f)
        except Exception:
            continue
        if last is not None and changed_fraction(sig, last) < min_change:
            f.unlink(missing_ok=True)
            removed += 1
        else:
            last = sig
        if on_progress and i % 25 == 0:
            on_progress(i / max(1, len(frames)) * 100)
    return removed


def _video_path(path: str) -> Path:
    p = Path(path.strip().strip('"'))
    if p.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"Not a supported video file ({', '.join(sorted(VIDEO_EXTS))})")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Video file not found on server")
    return p


class VideoProcessRequest(BaseModel):
    filepath: str

class VideoExtractRequest(BaseModel):
    filepath: str
    fps: float
    project_id: str
    image_format: Literal["jpg", "png"] = "jpg"
    # Skip frames that look the same as the last kept one: 0 = keep all, 1 = light, 2 = strong
    dedupe: Literal[0, 1, 2] = 0

class VideoCutRequest(BaseModel):
    filepath: str
    save_path: str
    start_sec: float
    end_sec: float


@router.get("/api/video/stream")
async def stream_video(path: str, _admin: dict = Depends(get_admin_header_or_query)):
    """Streams a local video with HTTP Range support (FileResponse handles ranges)."""
    p = _video_path(path)
    media_type = mimetypes.guess_type(p.name)[0] or "video/mp4"
    return FileResponse(p, media_type=media_type)


def _probe(filepath: str) -> dict:
    stats = {"fps": 0.0, "frames": 0, "avg_size_kb": 0.0, "duration": 0.0, "codec": "N/A", "resolution": "N/A"}
    rc, out, err = run_command_sync([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,nb_frames,duration,codec_name,width,height:format=duration",
        "-of", "json", filepath,
    ])
    if rc != 0:
        raise RuntimeError(err.strip() or "ffprobe failed")
    info = json.loads(out)
    stream = (info.get("streams") or [{}])[0]
    num, _, den = stream.get("r_frame_rate", "0/1").partition("/")
    try:
        if float(den or 1):
            stats["fps"] = round(float(num) / float(den or 1), 2)
    except ValueError:
        pass
    if "width" in stream and "height" in stream:
        stats["resolution"] = f"{stream['width']}x{stream['height']}"
    if "codec_name" in stream:
        stats["codec"] = stream["codec_name"].upper()
    duration = stream.get("duration") or (info.get("format") or {}).get("duration")
    if duration:
        stats["duration"] = float(duration)
    if str(stream.get("nb_frames", "")).isdigit():
        stats["frames"] = int(stream["nb_frames"])
    elif stats["duration"] and stats["fps"]:
        stats["frames"] = int(stats["duration"] * stats["fps"])

    rc, out, _ = run_command_sync([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pkt_size",
        "-read_intervals", "%+#5", "-of", "json", filepath,
    ])
    if rc == 0:
        sizes = [int(f["pkt_size"]) for f in json.loads(out).get("frames", []) if "pkt_size" in f]
        if sizes:
            stats["avg_size_kb"] = round(sum(sizes) / len(sizes) / 1024, 2)
    return stats


@router.post("/api/video/stats")
async def get_video_stats(req: VideoProcessRequest, _admin: dict = Depends(get_admin_user)):
    """ffprobe telemetry: FPS, frames, duration, codec, resolution, avg frame size, free disk."""
    p = _video_path(req.filepath)
    try:
        stats = await asyncio.to_thread(_probe, str(p))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read video: {e}")
    EXTRACTED_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    stats["free_bytes"] = shutil.disk_usage(EXTRACTED_FRAMES_DIR).free
    stats["bytes_per_pixel"] = _BYTES_PER_PIXEL
    return stats


@router.post("/api/video/cut")
async def cut_video(req: VideoCutRequest, _admin: dict = Depends(get_admin_user)):
    """Losslessly extract a clip with ffmpeg stream copy."""
    src = _video_path(req.filepath)
    dst = Path(req.save_path.strip().strip('"'))
    if dst.suffix.lower() not in {".mp4", ".mkv"}:
        raise HTTPException(status_code=400, detail="Save path must end in .mp4 or .mkv")
    if not dst.parent.is_dir():
        raise HTTPException(status_code=400, detail="Save folder does not exist")
    if dst.resolve() == src.resolve():
        raise HTTPException(status_code=400, detail="Save path must differ from the source video")
    if req.start_sec < 0 or req.start_sec >= req.end_sec:
        raise HTTPException(status_code=400, detail="Start time must be before end time")

    # -ss before -i seeks straight to the nearest keyframe (fast); with stream copy the cut
    # can only start on a keyframe anyway. -t keeps the requested length.
    command = ["ffmpeg", "-y", "-ss", str(req.start_sec), "-i", str(src), "-t", str(req.end_sec - req.start_sec),
               "-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)]
    rc, _, stderr = await asyncio.to_thread(run_command_sync, command)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {stderr[-800:]}")
    return {"message": "Export complete!", "save_path": str(dst)}


async def _pick(script: str, *args: str) -> str:
    script_path = Path(__file__).parent / script
    # The dialog opens on the server's own screen; give up if nobody answers it
    rc, stdout, _ = await asyncio.to_thread(run_command_sync, [sys.executable, str(script_path), *args], 300)
    if rc == 3:
        raise HTTPException(status_code=408, detail="Nothing was picked on the server within 5 minutes")
    if rc != 0 or not stdout.strip():
        raise HTTPException(status_code=400, detail="Nothing selected")
    return stdout.strip()


@router.get("/api/video/pick_import")
async def pick_import_video(_admin: dict = Depends(get_admin_user)):
    return {"filepath": await _pick("pick_file.py")}


@router.get("/api/video/pick_export")
async def pick_export_video(default_name: str = "", _admin: dict = Depends(get_admin_user)):
    return {"filepath": await _pick("save_file.py", *([default_name] if default_name else []))}


def _extract_with_progress(command: list[str], duration: float, on_progress) -> tuple[int, str]:
    """Run ffmpeg with -progress on stdout, reporting 0-100 %. Blocking (thread)."""
    resolved = shutil.which(command[0]) or command[0]
    proc = subprocess.Popen(
        [resolved, *command[1:]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
    )
    stderr_tail: list[str] = []

    def drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line)
            del stderr_tail[:-40]

    import threading
    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    for line in proc.stdout:
        key, _, val = line.strip().partition("=")
        if key == "out_time_us" and duration > 0 and val.isdigit():
            on_progress(min(99.0, int(val) / 1e6 / duration * 100))
    proc.wait()
    t.join(timeout=2)
    return proc.returncode, "".join(stderr_tail)


@router.post("/api/video/extract_frames")
async def extract_frames(
    req: VideoExtractRequest,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: dict = Depends(get_admin_user),
):
    """
    Extract frames into dataset/extracted_frames/{project_id}/{video}_{timestamp}_{tag}/.
    Frames are named {video}_{tag}_000001.jpg so frames of different videos never collide.
    Progress is streamed as `video_progress` socket events to the project room.
    """
    src = _video_path(req.filepath)
    await get_project_or_404(req.project_id, db)      # also rejects path tricks in project_id
    if req.fps <= 0:
        raise HTTPException(status_code=400, detail="FPS must be greater than 0")

    try:
        probe = await asyncio.to_thread(_probe, str(src))
    except Exception:
        probe = {"duration": 0.0, "fps": 0.0, "resolution": "N/A"}
    duration = probe["duration"]
    fps = min(req.fps, probe["fps"]) if probe.get("fps") else req.fps     # more than the source fps only duplicates frames

    EXTRACTED_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    est = estimate_extraction(probe, fps, req.image_format)
    free = shutil.disk_usage(EXTRACTED_FRAMES_DIR).free
    if est["bytes"] and est["bytes"] > free - DISK_RESERVE:
        gb = 1024 ** 3
        raise HTTPException(
            status_code=400,
            detail=(f"Not enough disk space: about {est['frames']:,} frames ≈ {est['bytes'] / gb:.1f} GB needed, "
                    f"{max(0, free - DISK_RESERVE) / gb:.1f} GB available (1 GB is kept free). "
                    f"Lower the frames per second, use JPG, or trim the video first."),
        )

    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", src.stem)[:40].strip("_") or "video"
    tag = uuid.uuid4().hex[:6]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = EXTRACTED_FRAMES_DIR / req.project_id / f"{stem}_{timestamp}_{tag}"
    dest.mkdir(parents=True, exist_ok=True)

    ext = req.image_format
    quality = ["-q:v", "2"] if ext == "jpg" else []
    command = ["ffmpeg", "-y", "-nostats", "-progress", "pipe:1", "-i", str(src),
               "-vf", f"fps={fps}", *quality, str(dest / f"{stem}_{tag}_%06d.{ext}")]

    loop = asyncio.get_running_loop()
    last = [-10.0]

    def on_progress(pct: float):
        if pct - last[0] >= 2:
            last[0] = pct
            asyncio.run_coroutine_threadsafe(emit_video_progress(req.project_id, pct, "extracting"), loop)

    rc, stderr = await asyncio.to_thread(_extract_with_progress, command, duration, on_progress)
    if rc != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {stderr[-800:]}")

    removed = 0
    if req.dedupe > 0:
        files = sorted(dest.glob(f"*.{ext}"))
        last_d = [-10.0]

        def on_dedupe(pct: float):
            if pct - last_d[0] >= 5:
                last_d[0] = pct
                asyncio.run_coroutine_threadsafe(emit_video_progress(req.project_id, pct, "deduplicating"), loop)

        removed = await asyncio.to_thread(drop_near_duplicates, files, DEDUPE_LEVELS[req.dedupe], on_dedupe)

    frames = sum(1 for _ in dest.glob(f"*.{ext}"))
    await emit_video_progress(req.project_id, 100, "done")
    return {"folder_path": str(dest.resolve()), "frames": frames, "removed_duplicates": removed, "fps": fps}
