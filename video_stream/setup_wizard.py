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

from video_stream.settings import require_token

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


# ── "Set up OBS for me" ──────────────────────────────────
# Builds what the director needs instead of asking the operator to: one scene
# per camera holding a Media Source of that camera's stream, plus a shared
# overlay scene nested on top. (Cameras are Media Sources, not Browser sources:
# OBS's embedded browser fails to start on some installs, and a camera must
# not depend on it. Overlays are web pages, so they have to be Browser sources.) It only ever ADDS — a scene the camera is already
# mapped to is left exactly as it is, and nothing is renamed or removed.

OVERLAY_SCENE = "Overlays (video-stream)"
OVERLAYS = [
    ("Chat", "chat"),
    ("Captions", "subtitles"),
    ("Alerts", "alerts"),
    ("Effects", "fx"),
    ("Stinger", "stinger"),
]


def camera_scene_name(index: int) -> str:
    return f"Cam {index}"


def camera_input_name(index: int) -> str:
    return f"Cam {index} feed (video-stream)"


def legacy_camera_input_name(index: int) -> str:
    """The Browser-source camera an earlier version created."""
    return f"Camera {index} (video-stream)"


def plan_obs_build(
    cameras: list[dict],
    scenes: list[str],
    inputs: list[str],
    scene_map: dict[int, str],
    has_item: Callable[[str, str], bool],
    base_url: str,
    overlays: bool = True,
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Work out the steps without touching OBS. Returns (steps, new scene map).

    Each step: ``{"do", "label", "new": bool, ...args}`` — ``new`` False means
    it already exists and will be skipped, which is what makes a second run a
    no-op. ``has_item(scene, source)`` answers whether a scene already
    contains a source."""
    steps: list[dict[str, Any]] = []
    scene_set, input_set = set(scenes), set(inputs)
    result_map = dict(scene_map)

    def browser(scene: str, name: str, url: str, scene_is_new: bool, kind: str = "browser") -> None:
        placed = not scene_is_new and name in input_set and has_item(scene, name)
        steps.append(
            {
                "do": kind if name not in input_set else "place",
                "camera": kind == "media",
                "scene": scene,
                "name": name,
                "url": url,
                "new": not placed,
                "label": f"Add “{name}” to scene “{scene}”",
            }
        )

    if overlays:
        overlay_new = OVERLAY_SCENE not in scene_set
        steps.append(
            {"do": "scene", "scene": OVERLAY_SCENE, "new": overlay_new,
             "label": f"Create scene “{OVERLAY_SCENE}” (captions, chat, alerts, effects in one place)"}
        )
        for title, slug in OVERLAYS:
            browser(OVERLAY_SCENE, f"{title} (video-stream)", f"{base_url}/overlay/{slug}", overlay_new)

    for cam in cameras:
        index = cam["index"]
        mapped = scene_map.get(index)
        # A scene with our own name is ours to complete (it may predate the
        # switch to Media Sources); any other mapped scene is the operator's.
        if mapped and mapped in scene_set and mapped != camera_scene_name(index):
            steps.append(
                {"do": "keep", "scene": mapped, "new": False,
                 "label": f"Camera {index} ({cam['name']}) already uses your scene “{mapped}” — left as it is"}
            )
            continue
        scene = camera_scene_name(index)
        scene_new = scene not in scene_set
        steps.append(
            {"do": "scene", "scene": scene, "new": scene_new,
             "label": f"Create scene “{scene}” for {cam['name']}"}
        )
        browser(scene, camera_input_name(index), f"{base_url}/stream/{index}", scene_new, kind="media")
        if overlays:
            nested = not scene_new and has_item(scene, OVERLAY_SCENE)
            steps.append(
                {"do": "place", "scene": scene, "name": OVERLAY_SCENE, "new": not nested,
                 "label": f"Put the overlays on top of “{scene}”"}
            )
        result_map[index] = scene
    return steps, result_map


def scene_map_string(scene_map: dict[int, str]) -> str:
    return ",".join(f"{i}={s}" for i, s in sorted(scene_map.items()))


def _obs_base_url() -> str:
    """The address OBS should load pages from: loopback when OBS is on this
    machine, this machine's LAN address when OBS is somewhere else."""
    config = _ctx["config"]
    host = str(config.get("obs_host") or "127.0.0.1")
    if host in ("127.0.0.1", "localhost", "::1"):
        return f"http://127.0.0.1:{config['port']}"
    from video_stream.network import primary_ip

    return f"http://{primary_ip()}:{config['port']}"


def _seat_camera(obs: Any, scene: str, name: str, width: int, height: int) -> None:
    """Put a camera feed at the bottom of its scene, scaled to fit the canvas
    whatever its shape (a phone turned upright is 720x1280), and switch off
    the dead Browser-source camera an earlier version left in the same scene.
    Best effort: the feed is already in the scene if any of this fails."""
    item = obs.scene_item_id(scene, name)
    if item is None:
        return
    obs.set_scene_item_index(scene, item, 0)
    obs.set_scene_item_transform(
        scene,
        item,
        {
            "positionX": 0,
            "positionY": 0,
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsAlignment": 0,
            "boundsWidth": width,
            "boundsHeight": height,
        },
    )
    index = name.split(" ")[1] if name.startswith("Cam ") else ""
    if index.isdigit():
        legacy = obs.scene_item_id(scene, legacy_camera_input_name(int(index)))
        if legacy is not None:
            obs.set_scene_item_enabled(scene, legacy, False)


def _obs_build(apply: bool, overlays: bool) -> dict[str, Any]:
    """Plan (and optionally run) the build on one OBS connection. Blocking."""
    obs = _ctx["obs_factory"]()
    try:
        if not obs.connect():
            return {"reachable": False, "error": obs.last_error, "steps": []}
        cameras = [c for c in _cameras() if c["active"]] or _cameras()
        steps, new_map = plan_obs_build(
            cameras,
            obs.scene_list(),
            obs.input_list(),
            dict(_ctx["config"].get("obs_scene_map") or {}),
            lambda scene, source: obs.scene_item_id(scene, source) is not None,
            _obs_base_url(),
            overlays=overlays,
        )
        result = {
            "reachable": True,
            "error": None,
            "applied": False,
            "steps": steps,
            "to_do": sum(1 for s in steps if s["new"]),
            "scene_map_string": scene_map_string(new_map),
            "no_cameras": not cameras,
        }
        if not apply:
            return result

        width, height = obs.video_size() or (1920, 1080)
        failed = 0
        for step in steps:
            if not step["new"]:
                step["state"] = "kept"
                continue
            if step["do"] == "scene":
                ok = obs.create_scene(step["scene"])
            elif step["do"] == "browser":
                ok = obs.create_browser_input(step["scene"], step["name"], step["url"], width, height)
            elif step["do"] == "media":
                ok = obs.create_media_input(step["scene"], step["name"], step["url"])
            else:  # place an existing input / nest a scene
                ok = obs.add_to_scene(step["scene"], step["name"])
            if ok and step.get("camera"):
                _seat_camera(obs, step["scene"], step["name"], width, height)
            step["state"] = "done" if ok else "failed"
            failed += 0 if ok else 1
        result.update(applied=True, failed=failed)
        return result
    finally:
        obs.close()


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


@router.post("/api/setup/obs-build")
async def api_setup_obs_build(request: Request):
    """Preview (``apply: false``) or run (``apply: true``) the OBS build."""
    require_token(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    apply = bool(body.get("apply"))
    result = await asyncio.to_thread(_obs_build, apply, bool(body.get("overlays", True)))
    if apply and result.get("reachable") and result.get("scene_map_string"):
        # Persist + apply live through the normal settings path, so the
        # director picks the new scenes up without a restart.
        from video_stream import settings as settings_mod

        saved = await asyncio.to_thread(
            _ctx["settings"].save, {"obs_scene_map": result["scene_map_string"]}
        )
        if settings_mod._apply is not None:
            settings_mod._apply(saved)
    return result


@router.get("/api/setup/verify")
async def api_setup_verify(request: Request):
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, details: str, fix: str | None = None) -> None:
        # ``fix`` names the dashboard step that resolves a failure:
        # "obs" (connect), "build" (make the scenes), "device" (add a camera).
        checks.append(
            {"name": name, "ok": bool(ok), "details": details, "fix": None if ok else fix}
        )

    cameras = _cameras()
    active = [c for c in cameras if c["active"]]
    check(
        "Cameras streaming",
        len(active) > 0,
        f"{len(active)} live of {len(cameras)} discovered"
        if cameras
        else "No cameras found — plug one in and hit Rescan, or add a phone",
        fix="device",
    )

    obs = await asyncio.to_thread(_obs_snapshot)
    check(
        "OBS connected",
        obs["reachable"],
        "Connected"
        if obs["reachable"]
        else "OBS isn't answering — open OBS and turn on Tools → WebSocket Server Settings"
        + (" (or the password is wrong)" if "auth" in str(obs["error"] or "").lower() else ""),
        fix="obs",
    )

    scene_map: dict[int, str] = _ctx["config"].get("obs_scene_map") or {}
    uncovered = [c["index"] for c in active if c["index"] not in scene_map]
    check(
        "Every live camera has a scene",
        len(active) > 0 and not uncovered,
        "All linked"
        if scene_map and not uncovered
        else f"Cameras without a scene: {', '.join(str(i) for i in uncovered)}" if scene_map
        else "No scenes are linked to your cameras yet",
        fix="build" if obs["reachable"] else "obs",
    )

    if obs["reachable"] and scene_map:
        missing = [s for s in scene_map.values() if s not in obs["scenes"]]
        check(
            "Linked scenes still exist in OBS",
            not missing,
            "All found" if not missing else f"Not in OBS: {missing}",
            fix="build",
        )

    if obs["reachable"]:
        buffer = obs.get("replay_buffer")
        check(
            "Replay buffer on (for highlights)",
            bool(buffer),
            "Running"
            if buffer
            else "Off — in OBS: Settings → Output → Replay Buffer → Enable, then Start Replay Buffer",
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
        check("Skeleton tracking", True, "Installed")
    else:
        check("Skeleton tracking", False, "Optional add-on, not installed — run ./install-pose.sh")

    settings_path = _ctx["settings"].path
    writable = os.access(settings_path.parent, os.W_OK) or not settings_path.parent.exists()
    check("Settings can be saved", writable, str(settings_path))

    passed = sum(1 for c in checks if c["ok"])
    return {"checks": checks, "passed": passed, "total": len(checks)}
