"""Setup wizard: scan the rig, propose a config, verify before going live.

Multi-machine setups fail silently today — a scene map that doesn't match OBS,
a replay buffer that was never enabled, MediaPipe missing on one box. The
wizard turns those into a checklist the operator can read *before* the show:

* ``scan``     — what exists right now (cameras, OBS scenes, audio inputs)
* ``generate`` — a PROPOSED camera→scene mapping (never auto-applied; the
                 dashboard shows it for one-click apply through settings)
* ``verify``   — pass/fail rig checks with actionable details

Scene-matching heuristics (the pickByHint idea from february11's onboarding
service): prefer scenes literally naming the camera index, then scenes sharing
words with the device name, then any "cam"-ish scene not already taken.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from typing import Any, Callable

from fastapi import APIRouter, Request

# Wired by app.py before the router is included.
_ctx: dict[str, Any] = {}


def init(**kwargs: Any) -> None:
    """Expects: manager, config (dict), obs_factory (callable), settings."""
    _ctx.update(kwargs)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def propose_scene_map(cameras: list[dict], scenes: list[str]) -> dict[int, str]:
    """Best-guess camera→scene assignment. Each scene used at most once."""
    taken: set[str] = set()
    proposal: dict[int, str] = {}

    def claim(index: int, scene: str) -> None:
        proposal[index] = scene
        taken.add(scene)

    # Pass 1: scenes that literally name the camera index ("Cam 0", "camera2").
    for cam in cameras:
        idx = cam["index"]
        pattern = re.compile(rf"\bcam(?:era)?\s*{idx}\b", re.IGNORECASE)
        for scene in scenes:
            if scene not in taken and pattern.search(scene):
                claim(idx, scene)
                break

    # Pass 2: scenes sharing a word with the device name ("BRIO" → "Brio Close").
    for cam in cameras:
        idx = cam["index"]
        if idx in proposal:
            continue
        name_tokens = _tokens(cam["name"])
        best = None
        best_score = 0
        for scene in scenes:
            if scene in taken:
                continue
            score = len(name_tokens & _tokens(scene))
            if score > best_score:
                best, best_score = scene, score
        if best is not None:
            claim(idx, best)

    # Pass 3: any remaining "cam"-ish scene, in order.
    cammy = [s for s in scenes if "cam" in s.lower() and s not in taken]
    for cam in cameras:
        idx = cam["index"]
        if idx not in proposal and cammy:
            claim(idx, cammy.pop(0))

    return proposal


def _obs_snapshot() -> dict[str, Any]:
    """Connect to OBS once and gather everything the wizard needs. Blocking."""
    obs = _ctx["obs_factory"]()
    try:
        if not obs.connect():
            return {"reachable": False, "error": obs.last_error, "scenes": [], "inputs": []}
        return {
            "reachable": True,
            "error": None,
            "scenes": obs.scene_list(),
            "inputs": obs.input_list(),
            "replay_buffer": obs.replay_buffer_active(),
        }
    finally:
        obs.close()


def _cameras() -> list[dict[str, Any]]:
    return [
        {"index": c.index, "name": c.name, "active": c.active}
        for c in _ctx["manager"].list_cameras()
    ]


router = APIRouter()


@router.get("/api/setup/scan")
async def api_setup_scan():
    obs = await asyncio.to_thread(_obs_snapshot)
    return {"cameras": _cameras(), "obs": obs}


@router.post("/api/setup/generate")
async def api_setup_generate():
    obs = await asyncio.to_thread(_obs_snapshot)
    cameras = [c for c in _cameras() if c["active"]] or _cameras()
    proposal = propose_scene_map(cameras, obs.get("scenes", []))
    unmatched = [c["index"] for c in cameras if c["index"] not in proposal]
    return {
        "proposal": {str(i): s for i, s in sorted(proposal.items())},
        "scene_map_string": ",".join(f"{i}={s}" for i, s in sorted(proposal.items())),
        "unmatched_cameras": unmatched,
        "obs_reachable": obs["reachable"],
        "note": "Proposal only — review it, then apply via settings.",
    }


@router.get("/api/setup/verify")
async def api_setup_verify(request: Request):
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, details: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "details": details})

    cameras = _cameras()
    active = [c for c in cameras if c["active"]]
    check(
        "Cameras streaming",
        len(active) > 0,
        f"{len(active)} live of {len(cameras)} discovered"
        if cameras
        else "No cameras discovered — hit Rescan",
    )

    obs = await asyncio.to_thread(_obs_snapshot)
    check(
        "OBS reachable",
        obs["reachable"],
        "Connected" if obs["reachable"] else f"{obs['error'] or 'unreachable'} — "
        "enable Tools → WebSocket Server Settings in OBS",
    )

    scene_map: dict[int, str] = _ctx["config"].get("obs_scene_map") or {}
    uncovered = [c["index"] for c in active if c["index"] not in scene_map]
    check(
        "Scene map covers live cameras",
        len(active) > 0 and not uncovered,
        "All mapped"
        if scene_map and not uncovered
        else f"Cameras without a scene: {uncovered}" if scene_map
        else "No scene map — run Generate, or set obs_scene_map in settings",
    )

    if obs["reachable"] and scene_map:
        missing = [s for s in scene_map.values() if s not in obs["scenes"]]
        check(
            "Mapped scenes exist in OBS",
            not missing,
            "All found" if not missing else f"Not in OBS: {missing}",
        )

    if obs["reachable"]:
        buffer = obs.get("replay_buffer")
        check(
            "Replay buffer enabled",
            bool(buffer),
            "Active"
            if buffer
            else "Off — enable in OBS: Settings → Output → Replay Buffer",
        )

    from video_stream.peers import parse_peers

    peer_list = parse_peers(_ctx["config"].get("peers") or "")
    if peer_list:

        def _check_peers() -> list[tuple[str, bool, str]]:
            import httpx

            results = []
            with httpx.Client(timeout=2.0) as client:
                for name, url in peer_list:
                    try:
                        resp = client.get(f"{url}/api/signals")
                        if resp.status_code == 200:
                            cams = resp.json().get("cameras") or []
                            live = sum(1 for c in cams if c.get("active"))
                            results.append((name, True, f"{url} — {live} live cameras"))
                        else:
                            results.append((name, False, f"{url} — HTTP {resp.status_code}"))
                    except Exception as exc:
                        results.append((name, False, f"{url} — {exc}"))
            return results

        for name, ok, detail in await asyncio.to_thread(_check_peers):
            check(f"Rig Link peer '{name}'", ok, detail)

    # find_spec answers "installed?" without executing mediapipe's heavy
    # native import (~1s), which would block the event loop mid-show.
    if importlib.util.find_spec("mediapipe") is not None:
        check("Pose (MediaPipe)", True, "Installed")
    else:
        check("Pose (MediaPipe)", False, "Not installed — ./install-pose.sh (optional)")

    settings_path = _ctx["settings"].path
    writable = os.access(settings_path.parent, os.W_OK) or not settings_path.parent.exists()
    check("Settings writable", writable, str(settings_path))

    passed = sum(1 for c in checks if c["ok"])
    return {"checks": checks, "passed": passed, "total": len(checks)}
