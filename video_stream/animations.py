"""Recorded avatar moves: save a performance once, replay it on any avatar.

The avatar page already solves each camera frame into a small "rig" — head
turn, eye openness, mouth shape, and (in full-body mode) arm/leg/torso angles.
That is what Avatar Sync broadcasts to OBS. A recorded move is simply that
stream written down: a list of timed rig frames.

Storing the *rig* rather than baked bone rotations is what makes a move
portable — playback runs the same retargeting the live tracker uses, so one
recording drives a VRoid girl, a penguin and a 3 m mech alike. (The avatar
page can also bake a move onto the loaded avatar and export it as a standard
``.vrma`` file for other VRM software.)

Playback is a Studio Bus event: ``POST …/play`` emits ``avatar_play`` and
every open avatar page — the studio tab and each OBS browser source — fetches
the clip and performs it, then returns to live tracking.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from video_stream.settings import require_token

MAX_SECONDS = 180.0
MAX_FRAMES = int(MAX_SECONDS * 30) + 30
MAX_BODY_BYTES = 6 * 1024 * 1024
MAX_CLIPS = 500
_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# The exact shape of a rig frame (see slimFace / slimPose in avatar.js).
_POSE_PARTS = (
    "Spine",
    "RightUpperArm",
    "RightLowerArm",
    "LeftUpperArm",
    "LeftLowerArm",
    "RightUpperLeg",
    "RightLowerLeg",
    "LeftUpperLeg",
    "LeftLowerLeg",
)

_ctx: dict[str, Any] = {}  # dir (Path), emit (callable)

router = APIRouter()
_lock = threading.Lock()


def init(**kwargs: Any) -> None:
    _ctx.update(kwargs)


def _dir() -> Path:
    return Path(_ctx["dir"])


def _num(value: Any, lo: float = -10.0, hi: float = 10.0) -> float:
    """A finite float, clamped and rounded — never NaN/inf, never a string."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return round(min(hi, max(lo, f)), 4)


def _xyz(value: Any) -> dict[str, float]:
    value = value if isinstance(value, dict) else {}
    return {"x": _num(value.get("x")), "y": _num(value.get("y")), "z": _num(value.get("z"))}


def clean_frame(raw: Any) -> dict[str, Any] | None:
    """Rebuild one frame from scratch, keeping only the fields the player
    reads. Anything else a client sent is dropped, so a saved clip can never
    carry surprises to the pages that later load it."""
    if not isinstance(raw, dict) or not isinstance(raw.get("f"), dict):
        return None
    f = raw["f"]
    eye = f.get("eye") if isinstance(f.get("eye"), dict) else {}
    mouth = f.get("mouth") if isinstance(f.get("mouth"), dict) else {}
    shape = mouth.get("shape") if isinstance(mouth.get("shape"), dict) else {}
    pupil = f.get("pupil") if isinstance(f.get("pupil"), dict) else {}
    frame: dict[str, Any] = {
        "t": _num(raw.get("t"), 0.0, MAX_SECONDS),
        "f": {
            "head": _xyz(f.get("head")),
            "eye": {"l": _num(eye.get("l", 1), 0, 1), "r": _num(eye.get("r", 1), 0, 1)},
            "mouth": {"shape": {k: _num(shape.get(k), 0, 1) for k in "AIUEO"}},
            "brow": _num(f.get("brow"), -1, 1),
            "pupil": {"x": _num(pupil.get("x"), -1, 1), "y": _num(pupil.get("y"), -1, 1)},
        },
        "p": None,
    }
    p = raw.get("p")
    if isinstance(p, dict):
        hips = p.get("Hips") if isinstance(p.get("Hips"), dict) else {}
        pose: dict[str, Any] = {"Hips": {"rotation": _xyz(hips.get("rotation"))}}
        for part in _POSE_PARTS:
            pose[part] = _xyz(p.get(part))
        frame["p"] = pose
    return frame


def clean_clip(raw: Any) -> dict[str, Any]:
    """Validate an uploaded recording. Raises ValueError with a message fit
    to show the person who pressed Save."""
    if not isinstance(raw, dict) or not isinstance(raw.get("frames"), list):
        raise ValueError("That isn't a recording")
    if len(raw["frames"]) > MAX_FRAMES:
        raise ValueError(f"Too long — moves can be up to {int(MAX_SECONDS)} seconds")
    frames = [f for f in (clean_frame(x) for x in raw["frames"]) if f is not None]
    if len(frames) < 5:
        raise ValueError("Too short — nothing was captured. Is the tracker seeing your face?")
    frames.sort(key=lambda f: f["t"])
    start = frames[0]["t"]
    for frame in frames:
        frame["t"] = round(frame["t"] - start, 4)
    name = "".join(ch for ch in str(raw.get("name") or "") if ch.isprintable()).strip()[:60]
    return {
        "name": name or "Untitled move",
        "duration": frames[-1]["t"],
        "body": any(f["p"] is not None for f in frames),
        "frames": frames,
    }


# ── storage: one JSON file per clip + a small index ──────
def _index_path() -> Path:
    return _dir() / "index.json"


def _read_index() -> list[dict[str, Any]]:
    try:
        data = json.loads(_index_path().read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and _ID_RE.match(str(e.get("id", "")))]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def list_clips() -> list[dict[str, Any]]:
    with _lock:
        return _read_index()


def save_clip(raw: Any) -> dict[str, Any]:
    clip = clean_clip(raw)
    with _lock:
        index = _read_index()
        if len(index) >= MAX_CLIPS:
            raise ValueError("The moves gallery is full — delete a few first")
        clip_id = uuid.uuid4().hex[:8]
        entry = {
            "id": clip_id,
            "name": clip["name"],
            "duration": clip["duration"],
            "frames": len(clip["frames"]),
            "body": clip["body"],
            "created": int(time.time()),
        }
        _write_json(_dir() / f"{clip_id}.json", {**entry, "frames": clip["frames"]})
        index.append(entry)
        _write_json(_index_path(), index)
        return entry


def load_clip(clip_id: str) -> dict[str, Any] | None:
    if not _ID_RE.match(clip_id):
        return None
    try:
        return json.loads((_dir() / f"{clip_id}.json").read_text())
    except (OSError, ValueError):
        return None


def rename_clip(clip_id: str, name: str) -> dict[str, Any] | None:
    name = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()[:60]
    if not name:
        raise ValueError("Give it a name")
    with _lock:
        index = _read_index()
        entry = next((e for e in index if e["id"] == clip_id), None)
        if entry is None:
            return None
        entry["name"] = name
        clip = load_clip(clip_id)
        if clip is not None:
            clip["name"] = name
            _write_json(_dir() / f"{clip_id}.json", clip)
        _write_json(_index_path(), index)
        return entry


def delete_clip(clip_id: str) -> bool:
    if not _ID_RE.match(clip_id):
        return False
    with _lock:
        index = _read_index()
        kept = [e for e in index if e["id"] != clip_id]
        if len(kept) == len(index):
            return False
        (_dir() / f"{clip_id}.json").unlink(missing_ok=True)
        _write_json(_index_path(), kept)
        return True


def _emit(event: str, payload: dict[str, Any]) -> None:
    emit: Callable[..., None] | None = _ctx.get("emit")
    if emit is not None:
        # Never retained: a move is a moment, and a page that connects later
        # must not replay the last one that happened to be triggered.
        emit(event, payload, retain=False)


def play(clip_id: str, loop: bool = False) -> bool:
    """Tell every open avatar page to perform a clip. Used by the API and
    available to other subsystems (chat commands, chaos presets)."""
    if not any(e["id"] == clip_id for e in list_clips()):
        return False
    _emit("avatar_play", {"id": clip_id, "loop": bool(loop), "at": time.time()})
    return True


# ── API ──────────────────────────────────────────────────
@router.get("/api/avatar/animations")
async def api_list():
    return {"animations": await asyncio.to_thread(list_clips)}


@router.post("/api/avatar/animations")
async def api_save(request: Request):
    require_token(request)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too big to save")
    try:
        raw = json.loads(body)
        entry = await asyncio.to_thread(save_clip, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "That isn't a recording")
    _emit("avatar_animations", {"changed": entry["id"]})
    return {"animation": entry}


@router.post("/api/avatar/animations/stop")
async def api_stop():
    _emit("avatar_stop", {"at": time.time()})
    return {"ok": True}


@router.get("/api/avatar/animations/{clip_id}")
async def api_get(clip_id: str):
    clip = await asyncio.to_thread(load_clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="That move no longer exists")
    return clip


@router.patch("/api/avatar/animations/{clip_id}")
async def api_rename(clip_id: str, request: Request):
    require_token(request)
    try:
        body = await request.json()
        entry = await asyncio.to_thread(
            rename_clip, clip_id, body.get("name") if isinstance(body, dict) else ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Bad request")
    if entry is None:
        raise HTTPException(status_code=404, detail="That move no longer exists")
    _emit("avatar_animations", {"changed": clip_id})
    return {"animation": entry}


@router.delete("/api/avatar/animations/{clip_id}")
async def api_delete(clip_id: str, request: Request):
    require_token(request)
    if not await asyncio.to_thread(delete_clip, clip_id):
        raise HTTPException(status_code=404, detail="That move no longer exists")
    _emit("avatar_animations", {"changed": clip_id})
    return {"ok": True}


@router.post("/api/avatar/animations/{clip_id}/play")
async def api_play(clip_id: str, request: Request):
    loop = False
    try:
        body = await request.json()
        loop = bool(body.get("loop")) if isinstance(body, dict) else False
    except Exception:
        pass
    if not await asyncio.to_thread(play, clip_id, loop):
        raise HTTPException(status_code=404, detail="That move no longer exists")
    return {"ok": True}
