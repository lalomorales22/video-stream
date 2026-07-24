"""FastAPI application: dashboard UI + camera MJPEG streams."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Some platforms' mimetypes DB doesn't know .mjs, so StaticFiles would serve it as
# text/plain and browsers refuse to execute the ES module. Register it explicitly.
mimetypes.add_type("text/javascript", ".mjs")

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from video_stream import __version__
from video_stream.camera import CameraManager
from video_stream.director import Director, DirectorConfig, load_rules_file
from video_stream.hub import hub
from video_stream.obs import AudioMeterListener, OBSClient
from video_stream.peers import PeerManager, parse_peers
from video_stream.network import get_local_ips, primary_ip
from video_stream.replay import ReplayConfig, ReplayDirector, ReplayError
from video_stream.safety import SafetyBlocked, SafetyManager
from video_stream import chaos as chaos_mod
from video_stream import chat as chat_mod
from video_stream import overlays
from video_stream import phone as phone_mod
from video_stream import settings as settings_mod
from video_stream import setup_wizard
from video_stream.settings import settings

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"

# Avatar Studio gallery: saved avatar presets + their uploaded VRMs. Under static/
# so the browser and OBS can load the VRMs directly; gitignored.
GALLERY_DIR = STATIC_DIR / "gallery"
GALLERY_VRM = GALLERY_DIR / "vrm"
PRESETS_FILE = GALLERY_DIR / "presets.json"


class Toggle(BaseModel):
    enabled: bool


class Preset(BaseModel):
    name: str
    vrm: str | None = None
    settings: dict[str, Any] = {}


def _load_presets() -> list[dict[str, Any]]:
    try:
        return json.loads(PRESETS_FILE.read_text())
    except Exception:
        return []


def _save_presets(items: list[dict[str, Any]]) -> None:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(items, indent=2))


manager = CameraManager()
director: Director | None = None
# Reentrant so the settings bounce can hold it across stop+start while those
# helpers take it again; serializes the lifecycle across threadpool workers.
_director_lock = threading.RLock()
_shutting_down = threading.Event()  # teardown seal: nothing may (re)start past it
safety = SafetyManager(on_change=lambda status: hub.emit("safety", status))
replay: ReplayDirector | None = None
config: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8765,
    "width": 1280,
    "height": 720,
    "quality": 80,
    "fps": 30.0,
    "pose": False,
    "pose_model": "lite",
    "pose_stride": 2,
    "director": False,
    "director_dry_run": False,
    "director_hold": 1.5,
    "director_cooldown": 3.0,
    "director_min_score": 0.02,
    "director_auto_punch": False,
    "obs_host": "127.0.0.1",
    "obs_port": 4455,
    "obs_password": "",
    "obs_scene_map": {},
    "replay_media_source": "",
    "replay_lower_third": "",
    "replay_lower_third_scene": "",
    "safety_fallback_scene": "",
    "safety_max_actions": 40,
    "director_rules": "",
    "peers": "",
    "phone_https": False,
    "phone_https_port": 8766,
}

# Rig Link export: monotonic time of the last peer poll with ?enable=1.
# While a remote director is actively watching, motion scoring stays on even
# if every local consumer (director, auto-replay) turns off.
_last_signals_export = 0.0
_SIGNALS_EXPORT_GRACE = 10.0


def _signals_export_active() -> bool:
    return time.monotonic() - _last_signals_export < _SIGNALS_EXPORT_GRACE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The Studio Bus needs the server loop so threads can emit safely.
    hub.set_loop(asyncio.get_running_loop())
    # Under --reload, uvicorn serves from a spawned subprocess that re-imports
    # this module fresh — main() (and its settings.load()) never ran there, so
    # the auth gate would be open and a dashboard Save would rewrite
    # settings.json from an empty dict. Hydrate saved settings here; no-op in
    # a normal boot where main() already loaded them.
    if not settings.loaded:
        saved = settings.load()
        if saved:
            _apply_settings(dict(saved))
    manager.width = config["width"]
    manager.height = config["height"]
    manager.jpeg_quality = config["quality"]
    manager.target_fps = config["fps"]
    manager.pose_enabled = config["pose"]
    manager.pose_variant = config["pose_model"]
    manager.pose_stride = config["pose_stride"]
    manager.motion_enabled = config["director"]  # scoring only when directing

    # Open cameras (and start the director) in the background so the server
    # serves the dashboard/avatar immediately instead of blocking for ~15s
    # while every camera warms up.
    def _startup() -> None:
        try:
            manager.discover(auto_start=True)
            hub.emit("cameras", _camera_payload())
            if config["director"]:
                start_director()
        except Exception as exc:  # never let startup kill the server
            print(f"[startup] {exc}")

    threading.Thread(target=_startup, name="startup", daemon=True).start()

    yield

    def _teardown() -> None:
        _shutting_down.set()  # startup/rescan threads must not (re)start anything
        chat_mod.chat.shutdown()
        if replay is not None:
            replay.shutdown()
        stop_director()
        manager.shutdown()

    # Teardown joins threads and closes OBS sockets — seconds of blocking when
    # OBS or a camera is wedged. Keep it off the event loop so shutdown (and a
    # second Ctrl-C) stays responsive.
    await asyncio.to_thread(_teardown)


def start_director() -> None:
    """Build and start the auto-director from current config. Idempotent.

    Runs off the event loop (threadpool / startup thread), so the lock makes
    the check-and-start atomic: two near-simultaneous toggles can otherwise
    both pass the None check and leak an unstoppable ghost director thread.
    """
    global director
    with _director_lock:
        if director is not None or _shutting_down.is_set():
            return
        manager.set_motion_all(True)
        rules, overrides = [], {}
        if config["director_rules"]:
            rules, overrides = load_rules_file(config["director_rules"])
        obs = None
        if not config["director_dry_run"]:
            obs = OBSClient(
                host=config["obs_host"],
                port=config["obs_port"],
                password=config["obs_password"],
            )
        audio = None
        if any(r.kind == "audio" for r in rules):
            # Live loudness is event-only; it gets its own OBS connection.
            audio = AudioMeterListener(
                host=config["obs_host"],
                port=config["obs_port"],
                password=config["obs_password"],
            )
        peers = None
        peer_list = parse_peers(config["peers"]) if config["peers"] else []
        if peer_list:
            peers = PeerManager(peer_list)
            print(
                "  [director] rig link: "
                + ", ".join(f"{n} → {u}" for n, u in peer_list)
            )
        director = Director(
            manager,
            obs_client=obs,
            config=DirectorConfig(
                scene_map=config["obs_scene_map"],
                min_score=config["director_min_score"],
                hold=overrides.get("hold", config["director_hold"]),
                cooldown=overrides.get("cooldown", config["director_cooldown"]),
                hysteresis_db=overrides.get("hysteresis_db", 3.0),
                dry_run=config["director_dry_run"],
                rules=rules,
            ),
            safety=safety,
            on_switch=_director_switched,
            audio=audio,
            peers=peers,
        )
        director.start()


def _director_switched(cam_index: int | None, scene: str, entry: dict) -> None:
    """Runs on the director thread after every committed cut."""
    if config["director_auto_punch"] and cam_index is not None:
        stream = manager.get(cam_index)
        if stream is not None and stream.active:
            stream.punch_in()
    hub.emit("scene_switch", {"scene": scene, "camera": cam_index}, retain=False)
    d = director
    hub.emit(
        "director",
        {"enabled": True, **d.status()} if d is not None else {"enabled": False},
    )


def stop_director() -> None:
    global director
    with _director_lock:
        if director is None:
            return
        director.stop()
        director = None
        # Motion scoring is shared: replay auto-capture and remote directors
        # (Rig Link) watch it too — only shut it off when nobody is listening.
        if (replay is None or not replay.auto_enabled) and not _signals_export_active():
            manager.set_motion_all(False)


def get_replay() -> ReplayDirector:
    """Build the replay director on first use (its own OBS connection)."""
    global replay
    if replay is None:
        replay = ReplayDirector(
            obs=OBSClient(
                host=config["obs_host"],
                port=config["obs_port"],
                password=config["obs_password"],
            ),
            config=ReplayConfig(
                media_input=config["replay_media_source"],
                lower_third_input=config["replay_lower_third"],
                lower_third_scene=config["replay_lower_third_scene"],
            ),
            manager=manager,
            safety=safety,
            on_event=hub.emit,
        )
    return replay


# Settings that only bind when the director is constructed (OBS connection
# params + engine tuning) — the only ones worth bouncing a live show for.
_DIRECTOR_KEYS = frozenset(
    {
        "obs_host",
        "obs_port",
        "obs_password",
        "obs_scene_map",
        "director_hold",
        "director_cooldown",
        "director_min_score",
        "peers",
    }
)


def _apply_settings(values: dict[str, Any]) -> None:
    """Push saved/posted settings onto the live subsystems. Runs on the event
    loop (settings POST) or at boot — director restart goes to a thread."""
    incoming = {k: v for k, v in values.items() if k in config}
    if "obs_scene_map" in values:
        incoming["obs_scene_map"] = _parse_scene_map(values["obs_scene_map"])
    director_dirty = any(
        k in _DIRECTOR_KEYS and config[k] != v for k, v in incoming.items()
    )
    config.update(incoming)
    safety.fallback_scene = config["safety_fallback_scene"] or None
    safety.max_actions = max(1, int(config["safety_max_actions"]))
    if replay is not None:
        replay.cfg.media_input = config["replay_media_source"]
        replay.cfg.lower_third_input = config["replay_lower_third"]
        replay.cfg.lower_third_scene = config["replay_lower_third_scene"]
    if director is not None and director_dirty:
        # New OBS/tuning values only bind at construction — bounce it.
        def _restart() -> None:
            # Hold the lock across the check so a toggle-off that lands
            # between the save and this thread can't be silently undone:
            # either it ran first (director is None — honor it) or it runs
            # after and stops the freshly built director.
            with _director_lock:
                if director is None:
                    return
                stop_director()
                start_director()

        threading.Thread(target=_restart, name="director-restart", daemon=True).start()


def _live_setting(key: str) -> Any:
    """Effective runtime value for the settings form (live config wins).
    Touches only `config` — never settings.get(), which would deadlock on the
    non-reentrant lock public() holds while calling this."""
    if key == "obs_scene_map":  # stored as dict, edited as "0=Cam A,1=Cam B"
        return ",".join(f"{i}={s}" for i, s in sorted(config["obs_scene_map"].items()))
    return config.get(key, "")  # auth_token lives only in the settings store


app = FastAPI(
    title="video-stream",
    description="Broadcast local cameras over Wi‑Fi for OBS and browsers",
    version=__version__,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

settings_mod.init(apply=_apply_settings, live=_live_setting)
setup_wizard.init(
    manager=manager,
    config=config,
    settings=settings,
    obs_factory=lambda: OBSClient(
        host=config["obs_host"], port=config["obs_port"], password=config["obs_password"]
    ),
)
overlays.init(templates=templates, asset_version=lambda: _asset_version())
phone_mod.init(templates=templates, asset_version=lambda: _asset_version(), config=config)
chaos_mod.init(
    safety=safety,
    # Repo presets first, user presets (~/.config/video-stream/chaos/) second —
    # later wins on id collisions, and installs without the repo tree still
    # get a preset dir that exists.
    presets_dirs=[
        ROOT.parent / "presets" / "chaos",
        Path(os.environ.get("VIDEO_STREAM_CONFIG", Path.home() / ".config" / "video-stream"))
        / "chaos",
    ],
    obs_factory=lambda: OBSClient(
        host=config["obs_host"], port=config["obs_port"], password=config["obs_password"]
    ),
)
chaos_mod.engine.reload()
app.include_router(settings_mod.router)
app.include_router(setup_wizard.router)
app.include_router(overlays.router)
app.include_router(chat_mod.router)
app.include_router(phone_mod.router)
app.include_router(chaos_mod.router)


@app.exception_handler(SafetyBlocked)
async def _safety_blocked(_request: Request, exc: SafetyBlocked):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})


@app.exception_handler(ReplayError)
async def _replay_error(_request: Request, exc: ReplayError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


# Events browser pages may publish INTO the bus. Kept to a tight allowlist so
# the bus stays a server-owned surface; avatar_rig carries Avatar Sync poses
# (one tracking page drives every OBS browser-source instance).
_PUBLISHABLE_EVENTS = {"avatar_rig"}


@app.websocket("/ws")
async def studio_bus(ws: WebSocket):
    """The Studio Bus: pushes retained state on connect, then live events."""
    queue = await hub.connect(ws)
    pump = asyncio.create_task(hub.pump(ws, queue))
    try:
        while True:
            # receive() not the typed helpers — those raise KeyError on a
            # frame of the other type.
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if not text or len(text) > 16384:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            publish = frame.get("publish")
            if (
                isinstance(publish, dict)
                and publish.get("event") in _PUBLISHABLE_EVENTS
            ):
                hub.emit(publish["event"], publish.get("payload"))
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):
            pass


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
                "pose": cam.pose,
                "zoom": cam.zoom,
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


def _asset_version() -> str:
    """Cache-busting token from static file mtimes, so browsers pick up updates
    (e.g. after a git pull) instead of serving a stale cached bundle."""
    try:
        latest = max(
            f.stat().st_mtime for f in STATIC_DIR.rglob("*") if f.is_file()
        )
        return str(int(latest))
    except ValueError:
        return __version__


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    resp = templates.TemplateResponse(
        request,
        "index.html",
        {
            "version": __version__,
            "asset_v": _asset_version(),
            "port": config["port"],
            "primary_ip": primary_ip(),
            "bases": _base_urls(request),
        },
    )
    # Always revalidate the HTML so the cache-busted JS/CSS tokens are never stale.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


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
            "pose": config["pose"],
            "pose_model": config["pose_model"],
        },
    }


@app.get("/avatar", response_class=HTMLResponse)
async def avatar(request: Request):
    # Assets are opt-in (fetched by ./install-avatar.sh). If they're missing, show
    # setup instructions instead of a page that fails to import its libraries.
    if not (STATIC_DIR / "vendor" / "three-vrm.module.js").exists():
        return HTMLResponse(
            "<body style='font:16px system-ui;background:#0c0c0e;color:#ececf1;"
            "max-width:640px;margin:12vh auto;padding:0 24px;line-height:1.6'>"
            "<h1 style='font-weight:800'>Avatar assets not installed</h1>"
            "<p>The VTuber avatar needs browser libraries and a face model that aren't "
            "bundled with the app. Install them once:</p>"
            "<pre style='background:#18181d;padding:14px 16px;border-radius:10px;"
            "overflow:auto'>cd " + str(ROOT.parent) + "\n./install-avatar.sh</pre>"
            "<p>Then reload this page. See <code>path_b.md</code> for details.</p></body>",
            status_code=503,
        )
    resp = templates.TemplateResponse(
        request, "avatar.html", {"asset_v": _asset_version()}
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/director")
async def api_director():
    d = director  # snapshot: a concurrent stop must not None us mid-read
    if d is None:
        return {"enabled": False}
    return {"enabled": True, **d.status()}


# ── Safety: kill switch + automation budget ───────────────────────────
class KillSwitch(BaseModel):
    on: bool
    reason: str | None = None


@app.get("/api/safety")
async def api_safety():
    return safety.status()


@app.post("/api/safety/kill")
async def api_safety_kill(body: KillSwitch):
    return safety.set_kill_switch(body.on, body.reason)


@app.post("/api/safety/fallback-scene")
async def api_safety_fallback():
    """Panic cut: switch OBS to the configured safe scene. Human-initiated,
    so it deliberately bypasses the kill switch and the rate limiter."""
    scene = safety.fallback_scene
    if not scene:
        raise HTTPException(
            status_code=400,
            detail="No fallback scene configured (--safety-fallback-scene)",
        )

    def _cut() -> bool:
        obs = OBSClient(
            host=config["obs_host"],
            port=config["obs_port"],
            password=config["obs_password"],
        )
        try:
            return obs.connect() and obs.set_scene(scene)
        finally:
            obs.close()

    ok = await asyncio.to_thread(_cut)
    if not ok:
        raise HTTPException(
            status_code=502, detail="Could not switch OBS to the fallback scene"
        )
    return {"ok": True, "scene": scene}


# ── Replay highlights ─────────────────────────────────────────────────
class ReplayRequest(BaseModel):
    label: str | None = None


@app.get("/api/replay")
async def api_replay_status():
    return get_replay().status()


@app.post("/api/replay")
async def api_replay_capture(body: ReplayRequest | None = None):
    # OBS calls block; keep them off the event loop.
    return await asyncio.to_thread(
        get_replay().capture, body.label if body else None
    )


@app.post("/api/replay/auto")
async def api_replay_auto(body: Toggle):
    r = get_replay()
    if body.enabled:
        manager.set_motion_all(True)  # spikes need motion scores flowing
    elif director is None and not _signals_export_active():
        manager.set_motion_all(False)  # only the watcher was using them
    r.set_auto(body.enabled)
    return r.status()


# ── Rig Link: export motion signals to a remote director ──────────────
@app.get("/api/signals")
async def api_signals(enable: bool = False):
    """Motion scores + camera states for a peer instance's director.

    ``?enable=1`` turns motion scoring on (and marks the export active so
    local toggles don't shut it off underneath the remote director)."""
    global _last_signals_export
    if enable:
        _last_signals_export = time.monotonic()
        if not manager.motion_enabled:
            manager.set_motion_all(True)
    motion: dict[str, float] = {}
    cameras = []
    for cam in manager.list_cameras():
        stream = manager.get(cam.index)
        if stream is not None and stream.active:
            motion[str(cam.index)] = round(float(stream.motion_score), 4)
        cameras.append({"index": cam.index, "name": cam.name, "active": cam.active})
    return {"motion": motion, "cameras": cameras, "ts": time.time()}


# ── Smart Zoom: punch-ins baked into the stream ───────────────────────
class ZoomTarget(BaseModel):
    x: float = 0.5
    y: float = 0.5
    level: float = 2.0


@app.post("/api/cameras/{index}/zoom")
async def api_zoom(index: int, body: ZoomTarget):
    stream = manager.get(index)
    if stream is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    # set_manual_zoom maps view→frame coords and cancels any pending
    # auto-punch ease-back, so the director can't revert an operator's shot.
    stream.set_manual_zoom(body.x, body.y, body.level)
    hub.emit("cameras", _camera_payload())  # keep every dashboard's badges honest
    return {"index": index, "zoom": round(stream.zoom.tzoom, 2)}


# ── Avatar Studio: gallery of saved presets ───────────────────────────
@app.get("/api/avatar/presets")
async def list_presets():
    return {"presets": _load_presets()}


@app.post("/api/avatar/presets")
async def add_preset(preset: Preset):
    items = _load_presets()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "name": (preset.name or "Avatar")[:60],
        "vrm": preset.vrm,
        "settings": preset.settings,
    }
    items.append(entry)
    _save_presets(items)
    return {"preset": entry}


@app.delete("/api/avatar/presets/{pid}")
async def delete_preset(pid: str):
    items = _load_presets()
    kept = [p for p in items if p.get("id") != pid]
    removed = [p for p in items if p.get("id") == pid]
    _save_presets(kept)
    # Remove an uploaded VRM that's no longer referenced by any preset.
    for r in removed:
        vrm = r.get("vrm") or ""
        if "/static/gallery/vrm/" in vrm and not any(k.get("vrm") == vrm for k in kept):
            (GALLERY_VRM / vrm.split("/")[-1]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/avatar/vrm")
async def upload_vrm(request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > 60_000_000:
        raise HTTPException(status_code=400, detail="VRM too large (max 60 MB)")
    GALLERY_VRM.mkdir(parents=True, exist_ok=True)
    fid = uuid.uuid4().hex[:8]
    (GALLERY_VRM / f"{fid}.vrm").write_bytes(data)
    return {"url": f"/static/gallery/vrm/{fid}.vrm"}


@app.post("/api/cameras/{index}/pose")
async def api_toggle_pose(index: int, body: Toggle, request: Request):
    stream = manager.get(index)
    if stream is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        # Loading the pose model can take a second — run it off the event loop
        # so the toggle responds and other requests aren't frozen.
        await asyncio.to_thread(stream.set_pose, body.enabled)
    except Exception as exc:
        # Most likely MediaPipe not installed — surface the install hint.
        raise HTTPException(status_code=400, detail=str(exc))
    hub.emit("cameras", _camera_payload())
    return {"camera": next(c for c in _camera_payload(request) if c["index"] == index)}


@app.post("/api/director")
async def api_toggle_director():
    if director is None:
        await asyncio.to_thread(start_director)  # OBS connect can block briefly
    else:
        await asyncio.to_thread(stop_director)
    d = director  # snapshot: a concurrent toggle must not None us mid-read
    state = {"enabled": d is not None} | (d.status() if d is not None else {})
    hub.emit("director", state)
    return state


@app.post("/api/discover")
async def api_discover(request: Request):
    # Probing camera indices is slow; run it in the background and return the
    # cameras we already know. The dashboard polls and picks up any new ones.
    def _rescan() -> None:
        manager.discover(auto_start=True)
        hub.emit("cameras", _camera_payload())

    threading.Thread(target=_rescan, name="rescan", daemon=True).start()
    return {"cameras": _camera_payload(request)}


@app.post("/api/cameras/{index}/start")
async def api_start(index: int, request: Request):
    info = await asyncio.to_thread(manager.start, index)
    if info is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not info.active:
        raise HTTPException(status_code=500, detail=info.error or "Failed to start camera")
    hub.emit("cameras", _camera_payload())
    return {"camera": next(c for c in _camera_payload(request) if c["index"] == index)}


@app.post("/api/cameras/{index}/stop")
async def api_stop(index: int, request: Request):
    info = await asyncio.to_thread(manager.stop, index)
    if info is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    hub.emit("cameras", _camera_payload())
    return {"camera": next(c for c in _camera_payload(request) if c["index"] == index)}


@app.get("/stream/{index}")
async def mjpeg_stream(index: int):
    stream = manager.get(index)
    if stream is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not stream.active:
        started = await asyncio.to_thread(stream.start)  # opening a camera blocks
        if not started:
            raise HTTPException(
                status_code=503,
                detail=stream.error or "Camera is not available",
            )

    return StreamingResponse(
        stream.mjpeg_async(),
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
        await asyncio.to_thread(stream.start)
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
    p.add_argument(
        "--pose",
        action="store_true",
        help="Overlay a live pose skeleton on every camera (needs MediaPipe)",
    )
    p.add_argument(
        "--pose-model",
        choices=("lite", "full", "heavy"),
        default="lite",
        help="Pose model: lite (fast, default), full, or heavy (most accurate)",
    )
    p.add_argument(
        "--pose-stride",
        type=int,
        default=2,
        help="Run pose inference every Nth frame (default 2); higher = lighter CPU",
    )
    p.add_argument(
        "--director",
        action="store_true",
        help="Auto-switch OBS to the most active camera (see --obs-* flags)",
    )
    p.add_argument(
        "--director-dry-run",
        action="store_true",
        help="Log intended camera switches without touching OBS (great for tuning)",
    )
    p.add_argument(
        "--obs-scene-map",
        default="",
        help='Map cameras to OBS scenes, e.g. "0=Cam A,1=Cam B,2=Cam C"',
    )
    p.add_argument("--obs-host", default="127.0.0.1", help="OBS WebSocket host")
    p.add_argument("--obs-port", type=int, default=4455, help="OBS WebSocket port")
    p.add_argument("--obs-password", default="", help="OBS WebSocket password")
    p.add_argument(
        "--director-hold",
        type=float,
        default=1.5,
        help="Seconds a camera must stay most-active before cutting to it",
    )
    p.add_argument(
        "--director-cooldown",
        type=float,
        default=3.0,
        help="Minimum seconds between camera switches",
    )
    p.add_argument(
        "--director-min-score",
        type=float,
        default=0.02,
        help="Motion score a camera must clear to be considered active (0–1)",
    )
    p.add_argument(
        "--director-rules",
        default="",
        help="Path to a JSON rules file mixing motion and audio triggers "
        "(see presets/director-rules.example.json)",
    )
    p.add_argument(
        "--peers",
        default="",
        help="Rig Link: remote video-stream instances whose motion feeds this "
        'director, e.g. "studio=192.168.1.42:8765,den=10.0.0.9:8765"',
    )
    p.add_argument(
        "--phone-https-port",
        type=int,
        default=8766,
        help="HTTPS port for phone-as-camera pages (needs certs: ./install-phone.sh)",
    )
    p.add_argument("--ssl-certfile", default="", help="TLS cert for the phone HTTPS port")
    p.add_argument("--ssl-keyfile", default="", help="TLS key for the phone HTTPS port")
    p.add_argument(
        "--director-auto-punch",
        action="store_true",
        help="After each director cut, punch in ~1.6x on the subject "
        "(aims at the tracked face when the skeleton overlay is on)",
    )
    p.add_argument(
        "--safety-fallback-scene",
        default="",
        help="OBS scene the dashboard panic button cuts to",
    )
    p.add_argument(
        "--safety-max-actions",
        type=int,
        default=40,
        help="Max automated OBS actions per rolling minute (default 40)",
    )
    p.add_argument(
        "--replay-media-source",
        default="",
        help="OBS media source that instantly plays each saved replay (optional)",
    )
    p.add_argument(
        "--replay-lower-third",
        default="",
        help="OBS text source updated with each replay's label (optional)",
    )
    p.add_argument(
        "--replay-lower-third-scene",
        default="",
        help="Scene holding the lower-third source; shown then auto-hidden (optional)",
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


def _https_cert_paths(args) -> tuple[Path, Path] | None:
    """Certs for the phone HTTPS port: explicit flags, else the config dir
    (where ./install-phone.sh puts them). None = phone pages stay http-only."""
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        print("  [phone] --ssl-certfile and --ssl-keyfile must BOTH be given — ignoring")
    if args.ssl_certfile and args.ssl_keyfile:
        cert, key = Path(args.ssl_certfile), Path(args.ssl_keyfile)
    else:
        base = (
            Path(os.environ.get("VIDEO_STREAM_CONFIG", Path.home() / ".config" / "video-stream"))
            / "certs"
        )
        cert, key = base / "cert.pem", base / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    return None


def _parse_scene_map(raw: str) -> dict[int, str]:
    """Parse "0=Cam A,1=Cam B" into {0: 'Cam A', 1: 'Cam B'}."""
    mapping: dict[int, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        idx, _, name = pair.partition("=")
        try:
            mapping[int(idx.strip())] = name.strip()
        except ValueError:
            continue
    return mapping


def _preflight_pose(variant: str) -> None:
    """Check pose deps once at startup so failures are loud, not silent.

    If MediaPipe is missing we print how to install it and turn pose off, so the
    rig still comes up as a normal stream instead of crashing.
    """
    try:
        from video_stream.pose import _INSTALL_HINT  # noqa: F401

        import mediapipe  # noqa: F401
    except ImportError:
        from video_stream.pose import _INSTALL_HINT

        print("\n  [pose] requested but unavailable —")
        for line in _INSTALL_HINT.splitlines():
            print(f"  {line}")
        print("  [pose] continuing WITHOUT pose overlay.\n")
        config["pose"] = False
        return
    print(f"  [pose] enabled · model={variant} · overlay flows through to OBS")


def _preflight_director() -> None:
    dry = config["director_dry_run"]
    smap = config["obs_scene_map"]
    print("  [director] auto-switching enabled")
    if dry:
        print("  [director] DRY RUN — will log intended switches, won't touch OBS")
    else:
        print(f"  [director] OBS target ws://{config['obs_host']}:{config['obs_port']}")
        print("  [director] enable it in OBS: Tools → WebSocket Server Settings")
    if smap:
        pairs = ", ".join(f"{i}→'{s}'" for i, s in sorted(smap.items()))
        print(f"  [director] scene map: {pairs}")
    elif not dry:
        print("  [director] no --obs-scene-map given; switches have no scene target")
    print("  [director] watch live: GET /api/director")


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
            "pose": args.pose,
            "pose_model": args.pose_model,
            "pose_stride": args.pose_stride,
            "director": args.director,
            "director_dry_run": args.director_dry_run,
            "director_hold": args.director_hold,
            "director_cooldown": args.director_cooldown,
            "director_min_score": args.director_min_score,
            "director_auto_punch": args.director_auto_punch,
            "obs_host": args.obs_host,
            "obs_port": args.obs_port,
            "obs_password": args.obs_password,
            "obs_scene_map": _parse_scene_map(args.obs_scene_map),
            "replay_media_source": args.replay_media_source,
            "replay_lower_third": args.replay_lower_third,
            "replay_lower_third_scene": args.replay_lower_third_scene,
            "safety_fallback_scene": args.safety_fallback_scene,
            "safety_max_actions": args.safety_max_actions,
            "director_rules": args.director_rules,
            "peers": args.peers,
        }
    )

    # Saved settings fill in wherever the CLI flag was left at its default
    # (explicit CLI beats settings.json beats defaults).
    parser = build_parser()
    saved = settings.load()
    fills = {
        key: value
        for key, value in saved.items()
        if hasattr(args, key) and getattr(args, key) == parser.get_default(key)
    }
    if fills:
        _apply_settings(dict(fills))
        print(f"  [settings] applied from {settings.path}: {', '.join(sorted(fills))}")

    safety.fallback_scene = config["safety_fallback_scene"] or None
    safety.max_actions = max(1, int(config["safety_max_actions"]))

    if args.pose:
        _preflight_pose(args.pose_model)
    if args.director:
        _preflight_director()

    https = None if args.reload else _https_cert_paths(args)
    if args.reload and _https_cert_paths(args) is not None:
        print("  [phone] --reload runs a single plain-http server — phone HTTPS is off in dev mode")
    config["phone_https"] = https is not None
    config["phone_https_port"] = args.phone_https_port

    lan = primary_ip()
    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │           video-stream  ·  live             │")
    print("  └─────────────────────────────────────────────┘")
    print(f"  Dashboard  http://127.0.0.1:{args.port}")
    print(f"  Network    http://{lan}:{args.port}")
    print(f"  Streams    http://{lan}:{args.port}/stream/{{id}}")
    print(f"  OBS view   http://{lan}:{args.port}/view/{{id}}")
    if https is not None:
        print(f"  Phone      https://{lan}:{args.phone_https_port}/phone  (QR on the dashboard)")
    else:
        print("  Phone      off — run ./install-phone.sh once to enable phone cameras")
    if args.open_browser:
        print("  Opening dashboard in your browser…")
    print()

    if args.open_browser:
        _open_dashboard(args.port)

    if https is None:
        uvicorn.run(
            "video_stream.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
        )
        return

    # Phone pages need HTTPS (secure-context getUserMedia); everything else
    # stays http. BOTH servers share this process so the phone (https) and the
    # receiver (http) meet in the same in-memory signaling room. The second
    # server runs with lifespan off — startup/teardown must fire exactly once.
    cert, key = https

    async def _serve_both() -> None:
        http_server = uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        )
        https_server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.phone_https_port,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                log_level="warning",
                lifespan="off",
            )
        )
        await asyncio.gather(http_server.serve(), https_server.serve())

    asyncio.run(_serve_both())


if __name__ == "__main__":
    main()
