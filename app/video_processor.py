import os
import json
import asyncio
import subprocess
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
import static_ffmpeg

static_ffmpeg.add_paths()

router = APIRouter(tags=["Video Processor"])

def resolve_executable(name):
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"The system cannot find '{name}'. Please ensure it is installed and added to the system PATH.")
    return path

def run_command_sync(command):
    """Runs a process synchronously in a safe way, particularly on Windows."""
    executable = command[0]
    resolved_path = shutil.which(executable)
    
    use_shell = False
    if resolved_path:
        command[0] = resolved_path
    else:
        use_shell = True

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        res = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=use_shell,
            creationflags=flags
        )
        return res.returncode, res.stdout, res.stderr
    except FileNotFoundError as e:
        return 2, "", f"The system cannot find '{executable}'. Please ensure it is installed and added to the system PATH. Details: {str(e)}"

class VideoProcessRequest(BaseModel):
    filepath: str

class VideoExtractRequest(BaseModel):
    filepath: str
    fps: float
    project_id: str

class VideoCutRequest(BaseModel):
    filepath: str
    save_path: str
    start_sec: float
    end_sec: float

@router.get("/api/video/stream")
async def stream_video(request: Request, path: str):
    """
    Streams a local video file, supporting HTTP Range requests for HTML5 <video> seeking.
    """
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get('range')

    if range_header:
        # Expected format: "bytes=start-" or "bytes=start-end"
        try:
            byte_range = range_header.replace('bytes=', '').split('-')
            start = int(byte_range[0])
            end = int(byte_range[1]) if byte_range[1] else file_size - 1
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid Range header")

        if start >= file_size or end >= file_size or start > end:
            raise HTTPException(status_code=416, detail="Requested Range Not Satisfiable")

        chunk_size = (end - start) + 1
        
        def iterfile():
            with open(file_path, mode="rb") as file_like:
                file_like.seek(start)
                yield file_like.read(chunk_size)
                
        headers = {
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(chunk_size),
            'Content-Type': 'video/mp4', # Mime can be dynamic but browsers handle video/mp4 well
        }
        return Response(content=next(iterfile()), status_code=206, headers=headers, media_type='video/mp4')
    else:
        # Full file
        def iterfile():
            with open(file_path, mode="rb") as file_like:
                yield from file_like

        headers = {
            'Accept-Ranges': 'bytes',
            'Content-Length': str(file_size),
            'Content-Type': 'video/mp4',
        }
        return Response(content=next(iterfile()), status_code=200, headers=headers, media_type='video/mp4')

@router.post("/api/video/stats")
async def get_video_stats(req: VideoProcessRequest):
    """
    Uses ffprobe to get video telemetry: FPS, frames, avg frame size.
    """
    filepath = req.filepath
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Video file not found on server")

    stats = {'fps': 0.0, 'frames': 0, 'avg_size_kb': 0.0, 'duration': 0.0, 'codec': 'N/A', 'resolution': 'N/A'}

    # 1. Get info (FPS, frames, duration, codec, width, height)
    cmd_info = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,nb_frames,duration,codec_name,width,height",
        "-of", "json", filepath
    ]

    try:
        returncode, stdout, stderr = await asyncio.to_thread(run_command_sync, cmd_info)
        if returncode == 0:
            info = json.loads(stdout)
            stream = info.get('streams', [{}])[0]
            
            fps_str = stream.get('r_frame_rate', '0/1')
            if '/' in fps_str:
                num, den = fps_str.split('/')
                if float(den) != 0:
                    stats['fps'] = round(float(num) / float(den), 2)
            
            if 'width' in stream and 'height' in stream:
                stats['resolution'] = f"{stream['width']}x{stream['height']}"
                
            if 'codec_name' in stream:
                stats['codec'] = stream['codec_name'].upper()

            if 'duration' in stream:
                stats['duration'] = float(stream['duration'])

            if 'nb_frames' in stream:
                stats['frames'] = int(stream['nb_frames'])
            elif 'duration' in stream and stats['fps'] > 0:
                stats['frames'] = int(float(stream['duration']) * stats['fps'])
    except Exception as e:
        print(f"Error fetching stats info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # 2. Get avg frame size (read first 5 frames intervals for estimation)
    cmd_frames = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=pkt_size", 
        "-read_intervals", "%+#5",  
        "-of", "json", filepath
    ]

    try:
        returncode, stdout_frames, stderr = await asyncio.to_thread(run_command_sync, cmd_frames)
        if returncode == 0:
            frames_data = json.loads(stdout_frames)
            frame_sizes = [int(f.get('pkt_size', 0)) for f in frames_data.get('frames', []) if 'pkt_size' in f]
            if frame_sizes:
                avg_bytes = sum(frame_sizes) / len(frame_sizes)
                stats['avg_size_kb'] = round(avg_bytes / 1024, 2)
    except Exception as e:
        print(f"Error fetching frame sizes: {e}")

    return stats

@router.post("/api/video/cut")
async def cut_video(req: VideoCutRequest):
    """
    Uses ffmpeg to losslessly extract a video clip.
    """
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="Source video not found")

    if req.start_sec >= req.end_sec:
        raise HTTPException(status_code=400, detail="Start time must be before end time")

    command = [
        "ffmpeg",
        "-y",
        "-i", req.filepath,
        "-ss", str(req.start_sec),
        "-to", str(req.end_sec),
        "-c", "copy",
        req.save_path
    ]

    try:
        returncode, stdout, stderr = await asyncio.to_thread(run_command_sync, command)
        if returncode != 0:
            raise Exception(f"FFmpeg failed: {stderr}")
    except Exception as e:
        print(f"FFmpeg error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    return {"message": "Export complete!", "save_path": req.save_path}

@router.get("/api/video/pick_import")
async def pick_import_video():
    import sys
    script_path = Path(__file__).parent / "pick_file.py"
    returncode, stdout, stderr = await asyncio.to_thread(
        run_command_sync, [sys.executable, str(script_path)]
    )
    if returncode != 0:
        raise HTTPException(status_code=400, detail="No file selected")
    return {"filepath": stdout.strip()}

@router.get("/api/video/pick_export")
async def pick_export_video(default_name: str = ""):
    import sys
    script_path = Path(__file__).parent / "save_file.py"
    cmd = [sys.executable, str(script_path)]
    if default_name:
        cmd.append(default_name)
        
    returncode, stdout, stderr = await asyncio.to_thread(run_command_sync, cmd)
    if returncode != 0:
        raise HTTPException(status_code=400, detail="No save location selected")
    return {"filepath": stdout.strip()}

@router.post("/api/video/extract_frames")
async def extract_frames(req: VideoExtractRequest):
    import uuid
    from datetime import datetime

    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="Source video not found")
        
    if req.fps <= 0:
        raise HTTPException(status_code=400, detail="FPS must be greater than 0")

    # Create the destination directory: dataset/extracted_frames/{project_id}/{video_basename}_{timestamp}
    video_basename = Path(req.filepath).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_folder = Path(f"dataset/extracted_frames/{req.project_id}/{video_basename}_{timestamp}_{uuid.uuid4().hex[:6]}")
    dest_folder.mkdir(parents=True, exist_ok=True)

    # ffmpeg command to extract frames losslessly
    # We use .png for lossless extraction
    command = [
        "ffmpeg",
        "-y",
        "-i", req.filepath,
        "-vf", f"fps={req.fps}",
        str(dest_folder / "frame_%06d.png")
    ]

    try:
        returncode, stdout, stderr = await asyncio.to_thread(run_command_sync, command)
        if returncode != 0:
            # Clean up empty folder on failure
            try:
                if not any(dest_folder.iterdir()):
                    dest_folder.rmdir()
            except Exception:
                pass
            raise Exception(f"FFmpeg failed: {stderr}")
    except Exception as e:
        print(f"FFmpeg error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    return {"folder_path": str(dest_folder.absolute())}
