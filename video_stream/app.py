"""FastAPI application: dashboard UI + camera MJPEG streams."""

from __future__ import annotations

import argparse
import os
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from video_stream import __version__
from video_stream.camera import CameraManager
from video_stream.network import get_local_ips, primary_ip

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"

manager = CameraManager()
config: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8765,
    "width": 1280,
    "height": 720,
    "quality": 80,
    "fps": 30.0,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    manager.width = config["width"]
    manager.height = config["height"]
    manager.jpeg_quality = config["quality"]
    manager.target_fps = config["fps"]
    manager.discover(auto_start=True)
    yield
    manager.stop_all()


app = FastAPI(
    title="video-stream",
    description="Broadcast local cameras over Wi‑Fi for OBS and browsers",
    version=__version__,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _base_urls(request: Request | None = None) -> list[dict[str, str]]:
    port = config["port"]
    urls: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(ip: str, base: str, label: str, front: bool = False) -> None:
        if base in seen:
            return
        seen.add(base)
        item = {"ip": ip, "base": base, "label": label}
        if front:
            urls.insert(0, item)
        else:
            urls.append(item)

    # Always include localhost for this machine
    add("127.0.0.1", f"http://127.0.0.1:{port}", "This machine")

    for ip in get_local_ips():
        label = "LAN" if ip != "127.0.0.1" else "Local"
        add(ip, f"http://{ip}:{port}", label)

    # Surface the host the browser actually used (if new)
    if request is not None:
        host = request.headers.get("host")
        if host:
            req_base = f"{request.url.scheme}://{host}"
            add(host.split(":")[0], req_base, "Current", front=True)

    return urls


def _camera_payload(request: Request | None = None) -> list[dict[str, Any]]:
    bases = _base_urls(request)
    # Prefer a real LAN address for shareable/OBS URLs
    primary = next(
        (b["base"] for b in bases if b["ip"] not in ("127.0.0.1", "localhost")),
        None,
    )
    if primary is None:
        primary = bases[0]["base"] if bases else f"http://127.0.0.1:{config['port']}"
    cameras = []
    for cam in manager.list_cameras():
        cameras.append(
            {
                "index": cam.index,
                "name": cam.name,
                "width": cam.width,
                "height": cam.height,
                "fps": round(cam.fps, 1),
                "active": cam.active,
                "error": cam.error,
                "stream_url": f"{primary}/stream/{cam.index}",
                "view_url": f"{primary}/view/{cam.index}",
                "preview_url": f"/stream/{cam.index}",
                "urls": [
                    {
                        "label": u["label"],
                        "ip": u["ip"],
                        "stream": f"{u['base']}/stream/{cam.index}",
                        "view": f"{u['base']}/view/{cam.index}",
                    }
                    for u in bases
                ],
            }
        )
    return cameras


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "version": __version__,
            "port": config["port"],
            "primary_ip": primary_ip(),
            "bases": _base_urls(request),
        },
    )


@app.get("/view/{index}", response_class=HTMLResponse)
async def viewer(request: Request, index: int):
    stream = manager.get(index)
    if stream is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "index": index,
            "name": stream.name,
            "stream_url": f"/stream/{index}",
        },
    )


@app.get("/api/status")
async def api_status(request: Request):
    return {
        "version": __version__,
        "port": config["port"],
        "primary_ip": primary_ip(),
        "bases": _base_urls(request),
        "cameras": _camera_payload(request),
        "settings": {
            "width": config["width"],
            "height": config["height"],
            "quality": config["quality"],
            "fps": config["fps"],
        },
    }


@app.post("/api/discover")
async def api_discover(request: Request):
    manager.discover(auto_start=True)
    return {"cameras": _camera_payload(request)}


@app.post("/api/cameras/{index}/start")
async def api_start(index: int, request: Request):
    info = manager.start(index)
    if info is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not info.active:
        raise HTTPException(status_code=500, detail=info.error or "Failed to start camera")
    return {"camera": next(c for c in _camera_payload(request) if c["index"] == index)}


@app.post("/api/cameras/{index}/stop")
async def api_stop(index: int, request: Request):
    info = manager.stop(index)
    if info is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"camera": next(c for c in _camera_payload(request) if c["index"] == index)}


@app.get("/stream/{index}")
async def mjpeg_stream(index: int):
    stream = manager.get(index)
    if stream is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not stream.active:
        started = stream.start()
        if not started:
            raise HTTPException(
                status_code=503,
                detail=stream.error or "Camera is not available",
            )

    return StreamingResponse(
        stream.mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/snapshot/{index}")
async def snapshot(index: int):
    stream = manager.get(index)
    if stream is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not stream.active:
        stream.start()
    jpeg = stream.get_jpeg()
    if jpeg is None:
        raise HTTPException(status_code=503, detail="No frame available")
    return StreamingResponse(
        iter([jpeg]),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video-stream",
        description="Broadcast local cameras over Wi‑Fi for OBS and browsers",
    )
    p.add_argument("--host", default=os.environ.get("VIDEO_STREAM_HOST", "0.0.0.0"))
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("VIDEO_STREAM_PORT", "8765")),
    )
    p.add_argument("--width", type=int, default=1280, help="Capture width")
    p.add_argument("--height", type=int, default=720, help="Capture height")
    p.add_argument("--fps", type=float, default=30.0, help="Target capture FPS")
    p.add_argument(
        "--quality",
        type=int,
        default=80,
        help="JPEG quality 40–95 (default 80)",
    )
    p.add_argument("--reload", action="store_true", help="Dev auto-reload")
    p.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        default=True,
        help="Open the dashboard in your browser (default)",
    )
    p.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Do not open a browser window",
    )
    return p


def _open_dashboard(port: int, delay: float = 1.0) -> None:
    url = f"http://127.0.0.1:{port}"

    def _run() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_run, name="open-browser", daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config.update(
        {
            "host": args.host,
            "port": args.port,
            "width": args.width,
            "height": args.height,
            "quality": args.quality,
            "fps": args.fps,
        }
    )

    lan = primary_ip()
    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │           video-stream  ·  live             │")
    print("  └─────────────────────────────────────────────┘")
    print(f"  Dashboard  http://127.0.0.1:{args.port}")
    print(f"  Network    http://{lan}:{args.port}")
    print(f"  Streams    http://{lan}:{args.port}/stream/{{id}}")
    print(f"  OBS view   http://{lan}:{args.port}/view/{{id}}")
    if args.open_browser:
        print("  Opening dashboard in your browser…")
    print()

    if args.open_browser:
        _open_dashboard(args.port)

    uvicorn.run(
        "video_stream.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
