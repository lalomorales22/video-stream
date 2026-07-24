"""Auto-rig: a raw .glb goes in, a drivable VRM avatar comes out.

The Avatar Studio has always been able to *load* a VRM. Making one was the
whole project: rig a skeleton, author blink/mouth blendshapes, convert to VRM —
hours of Blender per model, which is why the README used to say a Tripo export
"isn't drivable as-is". This module does that part on the box, headless.

Two passes, one subprocess script with a different verb each time:

* ``analyze`` imports the mesh, measures it (bounding box, horizontal band
  slices, left/right symmetry, how many leg lobes the bottom has), guesses which
  ARCHETYPE it is — a person, a penguin, a boar, a floating head — and renders
  front/side previews so the guess is something you can look at rather than
  take on faith.
* ``rig`` builds the skeleton that archetype describes, fitted to the landmarks
  the analysis measured, binds the mesh to it, generates the blink/mouth shape
  keys the face tracker drives, and exports a VRM into the avatar gallery.

Blender is the one heavy dependency and it stays strictly optional: with none
installed every route still answers, ``available`` is false, and the UI says
where to get one. Jobs run one at a time on a worker thread — a rig pass eats a
core and a gigabyte, and two at once would just fight over both — and stream
progress over the Studio Bus, so the page shows a live log instead of a
spinner that might mean anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from video_stream import settings as settings_mod
from video_stream.hub import hub

ROOT = Path(__file__).resolve().parent
BLENDER_SCRIPT = ROOT / "blender" / "autorig_blender.py"
ARCHETYPES_FILE = ROOT / "blender" / "archetypes.json"

# Source meshes are the raw AI-generator exports — 30 MB is typical, and a
# 4K-texture model can be far bigger than the 60 MB cap on finished VRMs.
MAX_SOURCE_BYTES = 250_000_000
SOURCE_SUFFIXES = (".glb", ".gltf", ".fbx")
MAX_GLTF_BYTES = 64_000_000   # a .gltf gets JSON-parsed for validation; bound it
ANALYZE_TIMEOUT = 420.0    # import + slice + two renders
RIG_TIMEOUT = 2400.0       # decimate + bind + shape keys + export, on one core
_KEEP_JOBS = 24            # job history kept in memory (the UI shows recent ones)
_KEEP_SOURCES = 12         # uploaded models kept on disk (30 MB each adds up)
_LOG_LINES = 400           # per job, tail-kept

_ctx: dict[str, Any] = {}  # gallery_dir, vrm_dir — wired by app.py


def init(**kwargs: Any) -> None:
    _ctx.update(kwargs)


def _rig_dir() -> Path:
    """Where uploaded sources, analyses and previews live. Under static/ (like
    the rest of the gallery) so the browser can show previews by URL."""
    return Path(_ctx.get("gallery_dir") or (ROOT / "static" / "gallery")) / "rig"


def _vrm_dir() -> Path:
    return Path(_ctx.get("vrm_dir") or (ROOT / "static" / "gallery" / "vrm"))


def _job_dir() -> Path:
    """Job arguments and raw Blender logs. Deliberately NOT under static/:
    they carry absolute paths and whatever Blender felt like printing, and
    everything under static/ is served to anyone who can reach the port."""
    base = Path(
        os.environ.get("VIDEO_STREAM_CONFIG", Path.home() / ".config" / "video-stream")
    ) / "rig-jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── finding Blender ───────────────────────────────────────────────────
# Blender is a desktop app, not a pip dependency: look where each platform's
# installer actually puts it, and let VIDEO_STREAM_BLENDER override everything
# for the "I keep it somewhere weird" case.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Darwin": (
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "~/Applications/Blender.app/Contents/MacOS/Blender",
    ),
    "Linux": (
        "/usr/bin/blender",
        "/usr/local/bin/blender",
        "/var/lib/flatpak/exports/bin/org.blender.Blender",
        "~/.local/share/flatpak/exports/bin/org.blender.Blender",
        "/snap/bin/blender",
    ),
    "Windows": (
        r"C:\Program Files\Blender Foundation\Blender\blender.exe",
    ),
}


def find_blender() -> str | None:
    override = os.environ.get("VIDEO_STREAM_BLENDER", "").strip()
    if override:
        path = Path(override).expanduser()
        return str(path) if path.exists() and os.access(path, os.X_OK) else None
    found = shutil.which("blender")
    if found:
        return found
    for raw in _CANDIDATES.get(platform.system(), ()):
        path = Path(raw).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    # Windows ships a version-numbered folder per release; take the newest.
    if platform.system() == "Windows":
        base = Path(r"C:\Program Files\Blender Foundation")
        hits = sorted(base.glob("Blender*/blender.exe"), reverse=True)
        if hits:
            return str(hits[0])
    return None


_probe_lock = threading.Lock()
_probe_cache: dict[str, Any] | None = None


def probe(refresh: bool = False) -> dict[str, Any]:
    """Ask Blender itself what it is and whether the VRM add-on is there.

    Cached: it costs a process launch (~1s), and the answer only changes when
    the user installs something, which is exactly when they'd hit Rescan."""
    global _probe_cache
    with _probe_lock:
        if _probe_cache is not None and not refresh:
            return dict(_probe_cache)
    blender = find_blender()
    result: dict[str, Any] = {
        "blender": blender,
        "version": "",
        "vrm_addon": False,
        "vrm_addon_version": "",
        "available": False,
        "error": "",
    }
    if not blender:
        result["error"] = "no Blender found"
    elif not BLENDER_SCRIPT.exists():
        result["error"] = f"rig script missing at {BLENDER_SCRIPT}"
    else:
        try:
            out = _run_blender(blender, ["--verb", "probe"], timeout=120.0)
            result.update(out)
            result["available"] = bool(out.get("vrm_addon"))
            if not out.get("vrm_addon"):
                result["error"] = "Blender's VRM add-on isn't installed"
        except Exception as exc:  # a broken Blender must not 500 the dashboard
            result["error"] = str(exc)[:400]
    with _probe_lock:
        _probe_cache = result
    return dict(result)


def _run_blender(blender: str, args: list[str], timeout: float) -> dict[str, Any]:
    """Run the rig script once and return the JSON object it printed.

    ``--factory-startup`` is deliberately NOT used: it resets preferences, and
    the VRM add-on is a preference-registered extension — the one thing this
    whole module needs."""
    proc = subprocess.run(
        [blender, "--background", "--python", str(BLENDER_SCRIPT), "--"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = _last_result_line(proc.stdout)
    if payload is None:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-12:])
        raise RuntimeError(f"Blender produced no result (exit {proc.returncode}):\n{tail}")
    return payload


def reject_external_references(data: bytes) -> str:
    """Refuse a .gltf that points at files on this machine.

    A .gltf is JSON that names its buffers and textures by URI, and Blender's
    importer will happily resolve a relative or absolute path against the local
    filesystem — and *pack the bytes it finds* into whatever we export next.
    Upload one that says ``"uri": "/etc/passwd"`` and the VRM that comes back
    has it inside. Only ``data:`` URIs are self-contained, and a .gltf that
    isn't self-contained is useless to us anyway: its sidecar files were never
    uploaded. Returns "" when the file is fine, or the reason to refuse it.

    (.glb has one embedded buffer and no URIs to abuse. .fbx can name external
    textures, which the Blender side scrubs after import.)"""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "that isn't a readable .gltf file"
    if not isinstance(doc, dict):
        return "that isn't a glTF document"
    for section in ("buffers", "images"):
        for item in doc.get(section) or []:
            uri = isinstance(item, dict) and item.get("uri")
            if uri and not str(uri).startswith("data:"):
                return (
                    f"this .gltf points at a separate file ({str(uri)[:40]!r}), which this "
                    "server won't read. Re-export it as .glb — one self-contained file."
                )
    return ""


_RESULT_PREFIX = "RIGRESULT>"
_EVENT_PREFIX = "RIGEVENT>"


def _last_result_line(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            try:
                return json.loads(line[len(_RESULT_PREFIX):])
            except ValueError:
                return None
    return None


# ── archetypes ────────────────────────────────────────────────────────
def load_archetypes() -> list[dict[str, Any]]:
    """The rig recipes, as the UI needs to show them. The same file is read by
    the Blender script — one source of truth for "what is a penguin"."""
    try:
        raw = json.loads(ARCHETYPES_FILE.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for key, spec in raw.get("archetypes", {}).items():
        out.append(
            {
                "id": key,
                "label": spec.get("label", key),
                "description": spec.get("description", ""),
                "examples": spec.get("examples", ""),
                "tracking": spec.get("tracking", "face"),
            }
        )
    return out


# ── jobs ──────────────────────────────────────────────────────────────
@dataclass
class Job:
    id: str
    kind: str                       # "analyze" | "rig"
    source_id: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)
    state: str = "queued"           # queued | running | done | failed | cancelled
    step: str = "waiting"
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_id": self.source_id,
            "label": self.label,
            "archetype": self.params.get("archetype", ""),
            "state": self.state,
            "step": self.step,
            "progress": round(self.progress, 3),
            "result": self.result,
            "error": self.error,
            "created": self.created,
            "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
            "log": self.log[-40:],
        }


_TERMINAL = ("done", "failed", "cancelled")


class RigError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class RigRunner:
    """One worker thread, one job at a time, progress on the Studio Bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._current: str = ""
        self._cancelled: set[str] = set()

    # -- public API ---------------------------------------------------
    def submit(self, kind: str, source_id: str, label: str, params: dict[str, Any]) -> Job:
        if not probe().get("available"):
            raise RigError(probe().get("error") or "Blender isn't available", 503)
        job = Job(id=uuid.uuid4().hex[:8], kind=kind, source_id=source_id, label=label, params=params)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            # Trim history, but never drop a job that hasn't finished: evicting
            # the running one makes it untrackable AND uncancellable.
            for stale in self._order[:-_KEEP_JOBS]:
                if self._jobs.get(stale) and self._jobs[stale].state in _TERMINAL:
                    self._jobs.pop(stale, None)
            self._order = [i for i in self._order if i in self._jobs][-_KEEP_JOBS:]
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="autorig", daemon=True)
                self._worker.start()
        self._queue.put(job.id)
        self._publish()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in ("done", "failed", "cancelled"):
                return False
            self._cancelled.add(job_id)
            proc = self._proc if self._current == job_id else None
            if job.state == "queued":
                job.state = "cancelled"
                job.step = "cancelled"
                job.finished = time.time()
        if proc is not None:
            proc.kill()  # the worker turns the non-zero exit into "cancelled"
        self._publish()
        return True

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._jobs[i].snapshot() for i in self._order if i in self._jobs]

    def status(self) -> dict[str, Any]:
        info = probe()
        return {
            "available": info.get("available", False),
            "blender": info.get("blender") or "",
            "version": info.get("version", ""),
            "vrm_addon": info.get("vrm_addon", False),
            "error": info.get("error", ""),
            "archetypes": load_archetypes(),
            "jobs": self.jobs(),
            "busy": bool(self._current),
        }

    # -- worker -------------------------------------------------------
    def _loop(self) -> None:
        # Parks forever on the queue rather than retiring when idle. A retiring
        # worker still reads as alive to submit(), which would then hand a job
        # to a thread on its way out and leave it queued for good. A parked
        # daemon thread costs nothing.
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None or job.state == "cancelled":
                continue
            try:
                self._run(job)
            except Exception as exc:  # never let one bad job kill the worker
                job.state = "failed"
                job.error = str(exc)[:600]
                job.finished = time.time()
                self._publish()

    def _run(self, job: Job) -> None:
        blender = find_blender()
        if not blender:
            raise RigError("Blender disappeared between submit and run", 503)
        job.state = "running"
        job.started = time.time()
        job.step = "starting Blender"
        self._publish()

        work = _rig_dir() / job.source_id
        payload = dict(job.params)
        payload["source"] = str(_source_path(job.source_id))
        payload["work_dir"] = str(work)
        args_file = _job_dir() / f"{job.kind}-{job.id}.json"
        args_file.write_text(json.dumps(payload))
        timeout = ANALYZE_TIMEOUT if job.kind == "analyze" else RIG_TIMEOUT

        if job.id in self._cancelled:
            # Cancelled in the window between submit and launch — don't spend a
            # minute of CPU producing something nobody wants.
            job.state = "cancelled"
            job.step = "cancelled"
            job.finished = time.time()
            self._publish()
            return
        cmd = [
            blender,
            "--background",
            "--python", str(BLENDER_SCRIPT),
            "--",
            "--verb", job.kind,
            "--args", str(args_file),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with self._lock:
            self._proc = proc
            self._current = job.id
        # A plain Timer(timeout, proc.kill) can't tell us afterwards whether it
        # fired: Timer.finished is set by cancel() too, so it always reads as
        # "fired" by the time we look. Flag it explicitly.
        timed_out = threading.Event()

        def _give_up():
            timed_out.set()
            proc.kill()

        killer = threading.Timer(timeout, _give_up)
        killer.daemon = True
        killer.start()
        result: dict[str, Any] | None = None
        try:
            for line in proc.stdout or ():
                line = line.rstrip("\n")
                if line.startswith(_EVENT_PREFIX):
                    self._on_event(job, line[len(_EVENT_PREFIX):])
                elif line.startswith(_RESULT_PREFIX):
                    try:
                        result = json.loads(line[len(_RESULT_PREFIX):])
                    except ValueError:
                        result = None
                elif line.strip():
                    job.log.append(line[:500])
                    del job.log[:-_LOG_LINES]
            code = proc.wait()
        finally:
            killer.cancel()
            with self._lock:
                self._proc = None
                self._current = ""
        try:
            (_job_dir() / f"{job.kind}-{job.id}.log").write_text("\n".join(job.log))
        except OSError:
            pass  # a log we couldn't write must never fail a rig that worked

        job.finished = time.time()
        if job.id in self._cancelled:
            job.state = "cancelled"
            job.step = "cancelled"
        elif code == 0 and result and result.get("ok"):
            job.state = "done"
            job.step = "done"
            job.progress = 1.0
            job.result = result
        else:
            job.state = "failed"
            job.progress = 0.0
            detail = (result or {}).get("error") or ""
            if not detail:
                if timed_out.is_set():
                    detail = f"Blender gave up after {int(timeout)}s — the model may be too heavy"
                else:
                    detail = "\n".join(job.log[-6:]) or f"Blender exited with {code}"
            job.error = detail[:1200]
            job.step = "failed"
        if job.state != "done":
            # A killed Blender can leave a half-written .vrm behind, and the
            # gallery serves whatever is in that directory.
            leftover = payload.get("vrm_out")
            if leftover:
                Path(leftover).unlink(missing_ok=True)
        self._cancelled.discard(job.id)
        self._publish()

    def _on_event(self, job: Job, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if "step" in event:
            job.step = str(event["step"])[:120]
        if "progress" in event:
            try:
                job.progress = max(0.0, min(1.0, float(event["progress"])))
            except (TypeError, ValueError):
                pass
        if event.get("log"):
            job.log.append(str(event["log"])[:500])
            del job.log[:-_LOG_LINES]
        self._publish()

    def _publish(self) -> None:
        hub.emit("rig", {"jobs": self.jobs(), "busy": bool(self._current)})


runner = RigRunner()


# ── sources ───────────────────────────────────────────────────────────
def _source_path(source_id: str) -> Path:
    directory = _rig_dir() / source_id
    for suffix in SOURCE_SUFFIXES:
        candidate = directory / f"source{suffix}"
        if candidate.exists():
            return candidate
    return directory / "source.glb"


def _safe_id(source_id: str) -> str:
    """Path-segment ids only. These come off the wire and index directories."""
    clean = "".join(c for c in source_id if c.isalnum())[:32]
    if not clean or clean != source_id:
        raise RigError("bad source id")
    return clean


def list_sources() -> list[dict[str, Any]]:
    out = []
    base = _rig_dir()
    if not base.is_dir():
        return out
    def when(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0  # deleted underneath us mid-listing

    for directory in sorted(base.iterdir(), key=when, reverse=True):
        if not directory.is_dir():
            continue
        meta_file = directory / "meta.json"
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        analysis = directory / "analysis.json"
        if analysis.exists():
            try:
                meta["analysis"] = json.loads(analysis.read_text())
            except ValueError:
                pass
        out.append(meta)
    return out


def prune_sources(keep: int | None = None) -> None:
    """Drop the oldest uploads. They're 30 MB each and nothing else ever
    deletes them — a month of experimenting would quietly fill a disk.

    The limit is read at call time, not bound as a default, so tuning
    ``_KEEP_SOURCES`` takes effect without a restart."""
    keep = _KEEP_SOURCES if keep is None else keep
    base = _rig_dir()
    if not base.is_dir():
        return
    try:
        dirs = sorted(
            (d for d in base.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in dirs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


router = APIRouter()


class RigRequest(BaseModel):
    source_id: str
    archetype: str = "auto"
    name: str = ""
    height_m: float = 0.0            # 0 = keep the analysis' suggestion
    target_tris: int = 0             # 0 = the archetype default
    face_front: bool = True          # False when the user says "it's facing away"
    yaw_deg: float = 0.0             # extra spin, for models that face sideways
    eyes: list[dict[str, float]] | None = None   # optional hand-placed eye centers
    mouth: dict[str, float] | None = None        # optional hand-placed mouth point
    expressions: bool = True


@router.get("/api/rig")
async def api_rig_status():
    # status() may have to launch Blender to answer "is it installed" — a
    # second of subprocess on the event loop would stall every camera stream.
    return await asyncio.to_thread(runner.status)


@router.post("/api/rig/rescan")
async def api_rig_rescan(request: Request):
    settings_mod.require_token(request)
    await asyncio.to_thread(probe, True)
    return await asyncio.to_thread(runner.status)


@router.get("/api/rig/sources")
async def api_rig_sources():
    return {"sources": list_sources()}


@router.post("/api/rig/sources")
async def api_rig_upload(request: Request):
    """Raw-body upload of a .glb/.gltf/.fbx, matching /api/avatar/vrm.

    The filename rides in a header because the body is the file — multipart
    would mean buffering a 200 MB form just to read one string."""
    settings_mod.require_token(request)
    # The browser percent-encodes it: header values are Latin-1, and model
    # files are named in every alphabet there is. Read the extension BEFORE
    # truncating, or a long-but-valid name loses its suffix and gets refused.
    raw_name = unquote((request.headers.get("x-file-name") or "model.glb").strip())
    suffix = Path(raw_name).suffix.lower()
    name = raw_name[:120]
    if suffix not in SOURCE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix or name}' — use {', '.join(SOURCE_SUFFIXES)}",
        )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"model too large (max {MAX_SOURCE_BYTES // 1_000_000} MB)",
        )
    # Stream it to disk rather than await request.body(): the body arrives
    # before any size check could run, so buffering first means a single
    # request can decide how much of this machine's memory to occupy.
    source_id = uuid.uuid4().hex[:8]
    directory = _rig_dir() / source_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"source{suffix}"
    size = 0
    head = b""
    try:
        with target.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"model too large (max {MAX_SOURCE_BYTES // 1_000_000} MB)",
                    )
                if len(head) < 4:
                    head = (head + chunk)[:4]
                out.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="empty upload")
        if suffix == ".glb" and head != b"glTF":
            raise HTTPException(status_code=400, detail="that isn't a binary glTF (.glb) file")
        if suffix == ".gltf":
            # Validating means parsing the JSON, so cap what we'll parse. A
            # self-contained .gltf is base64 and ~33% fatter than the .glb of
            # the same model — past this, .glb is the right answer anyway.
            if size > MAX_GLTF_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f".gltf files are capped at {MAX_GLTF_BYTES // 1_000_000} MB — export it as .glb instead",
                )
            problem = reject_external_references(target.read_bytes())
            if problem:
                raise HTTPException(status_code=400, detail=problem)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)  # never leave a half-file behind
        raise
    meta = {
        "id": source_id,
        "name": name,
        "bytes": size,
        "uploaded": time.time(),
    }
    (directory / "meta.json").write_text(json.dumps(meta, indent=2))
    prune_sources()
    return meta


@router.delete("/api/rig/sources/{source_id}")
async def api_rig_delete_source(source_id: str, request: Request):
    settings_mod.require_token(request)
    try:
        directory = _rig_dir() / _safe_id(source_id)
    except RigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    return {"ok": True}


@router.post("/api/rig/analyze")
async def api_rig_analyze(body: RigRequest, request: Request):
    settings_mod.require_token(request)
    try:
        source_id = _safe_id(body.source_id)
        if not _source_path(source_id).exists():
            raise RigError("no such source model", 404)
        job = await asyncio.to_thread(
            runner.submit,
            "analyze",
            source_id,
            body.name or source_id,
            {"yaw_deg": body.yaw_deg, "face_front": body.face_front},
        )
    except RigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return job.snapshot()


@router.post("/api/rig/jobs")
async def api_rig_start(body: RigRequest, request: Request):
    settings_mod.require_token(request)
    try:
        source_id = _safe_id(body.source_id)
        if not _source_path(source_id).exists():
            raise RigError("no such source model", 404)
        params = {
            "archetype": body.archetype or "auto",
            "name": (body.name or "Avatar")[:60],
            "height_m": body.height_m,
            "target_tris": body.target_tris,
            "face_front": body.face_front,
            "yaw_deg": body.yaw_deg,
            "eyes": body.eyes,
            "mouth": body.mouth,
            "expressions": body.expressions,
            "vrm_out": str(_vrm_dir() / f"{uuid.uuid4().hex[:8]}.vrm"),
        }
        job = await asyncio.to_thread(runner.submit, "rig", source_id, params["name"], params)
    except RigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return job.snapshot()


@router.get("/api/rig/jobs/{job_id}")
async def api_rig_job(job_id: str):
    job = runner.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return job.snapshot()


@router.post("/api/rig/jobs/{job_id}/cancel")
async def api_rig_cancel(job_id: str, request: Request):
    settings_mod.require_token(request)
    if not runner.cancel(job_id):
        raise HTTPException(status_code=404, detail="no such job (or it already finished)")
    return {"ok": True}
