"""Overlay pack: live captions, alerts, the rig HUD, and cut stingers.

Every page under ``/overlay/*`` is a transparent OBS Browser Source riding the
Studio Bus (``/ws``) — no CDN, no external fonts, fully offline like the rest
of the rig. Retained hub events mean a browser source reloaded mid-show
re-renders its current state instantly.

Subtitles are the one overlay with server state: the dashboard's Web Speech
capture (or anything else) POSTs lines to ``/api/subtitles/push`` and the
server relays them — no server-side ML, the STT runs in the operator's Chrome
tab for free. Sanitizers ported verbatim from february11's OBS-Overlays
sidecar: hex colors, a font-stack character allowlist (not a name whitelist),
numeric clamps, a 220-char line cap. Invalid setting values silently keep the
old value — partial updates are the contract.

The HUD and stinger need nothing from this module but their page routes: they
are pure consumers of events other subsystems already emit (``cameras``,
``director``, ``safety``, ``scene_switch``, ``replay_saved``).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from video_stream.hub import hub

_ctx: dict[str, Any] = {}  # templates, asset_version (callable)


def init(**kwargs: Any) -> None:
    _ctx.update(kwargs)


# ---- sanitizers (ported verbatim semantics) ------------------------------
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sanitize_hex_color(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if HEX_COLOR_RE.fullmatch(candidate):
            return candidate.lower()
    return fallback


def sanitize_font_family(value: Any, fallback: str) -> str:
    """Character allowlist, NOT a font-name whitelist — arbitrary comma stacks
    stay usable while CSS-hostile characters are stripped."""
    if not isinstance(value, str):
        return fallback
    trimmed = value.strip()
    if not trimmed:
        return fallback
    safe = "".join(ch for ch in trimmed if ch.isalnum() or ch in " ,-'\"")
    safe = " ".join(safe.split())
    return safe[:96] if safe else fallback


def sanitize_font_size(value: Any, fallback: int) -> int:
    try:
        return int(_clamp(int(value), 18, 140))
    except Exception:
        return fallback


def sanitize_opacity(value: Any, fallback: float) -> float:
    try:
        return round(_clamp(float(value), 0.0, 1.0), 2)
    except Exception:
        return fallback


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- subtitle state -------------------------------------------------------
_subtitle_lock = threading.Lock()
subtitle_state: dict[str, Any] = {"text": "", "final": True, "updated_at": None}
subtitle_settings: dict[str, Any] = {
    "font_family": "-apple-system, Inter, Segoe UI, sans-serif",
    "font_size_px": 56,
    "text_color": "#ffffff",
    "background_color": "#000000",
    "background_opacity": 0.45,
    "updated_at": None,
}

_SETTING_SANITIZERS = {
    "font_family": sanitize_font_family,
    "font_size_px": sanitize_font_size,
    "text_color": sanitize_hex_color,
    "background_color": sanitize_hex_color,
    "background_opacity": sanitize_opacity,
}


def set_subtitle_text(text: Any, final: bool = True) -> dict[str, Any]:
    if not isinstance(text, str):
        text = str(text)
    normalized = " ".join(text.split()).strip()
    if len(normalized) > 220:
        normalized = normalized[:220].rstrip()
    with _subtitle_lock:
        subtitle_state.update(text=normalized, final=bool(final), updated_at=_now_iso())
        snapshot = dict(subtitle_state)
    hub.emit("subtitle_update", snapshot)
    return snapshot


# ---- overlay pages --------------------------------------------------------
_OVERLAYS = ("subtitles", "alerts", "hud", "stinger", "chat", "fx")

router = APIRouter()


@router.get("/overlay/{name}", response_class=HTMLResponse)
async def overlay_page(name: str, request: Request):
    if name not in _OVERLAYS:
        raise HTTPException(status_code=404, detail="No such overlay")
    resp = _ctx["templates"].TemplateResponse(
        request, f"overlay/{name}.html", {"asset_v": _ctx["asset_version"]()}
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---- subtitles API --------------------------------------------------------
@router.get("/api/subtitles/state")
async def api_subtitle_state():
    with _subtitle_lock:
        return dict(subtitle_state)


@router.get("/api/subtitles/settings")
async def api_subtitle_settings():
    with _subtitle_lock:
        return dict(subtitle_settings)


@router.post("/api/subtitles/settings")
async def api_subtitle_settings_post(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    with _subtitle_lock:
        for key, sanitize in _SETTING_SANITIZERS.items():
            if key in data:
                subtitle_settings[key] = sanitize(data[key], subtitle_settings[key])
        subtitle_settings["updated_at"] = _now_iso()
        snapshot = dict(subtitle_settings)
    hub.emit("subtitle_settings", snapshot)
    return {"status": "ok", "settings": snapshot}


class SubtitlePush(BaseModel):
    text: str
    final: bool = True


@router.post("/api/subtitles/push")
async def api_subtitle_push(body: SubtitlePush):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    return {"status": "ok", "subtitle": set_subtitle_text(body.text, body.final)}


@router.post("/api/subtitles/clear")
async def api_subtitle_clear():
    return {"status": "ok", "subtitle": set_subtitle_text("", final=True)}


# ---- alerts ---------------------------------------------------------------
class TestAlert(BaseModel):
    type: Literal["follow", "sub", "raid", "bits", "donation"] = "follow"
    username: str = "TestUser"
    amount: int | str | None = None
    viewers: int = 50


@router.post("/api/alerts/test")
async def api_alert_test(body: TestAlert):
    messages = {
        "follow": f"{body.username} just followed!",
        "sub": f"{body.username} just subscribed!",
        "raid": f"{body.username} is raiding with {body.viewers} viewers!",
        "bits": f"{body.username} cheered {body.amount or 100} bits!",
        "donation": f"{body.username} donated {body.amount or '$5.00'}!",
    }
    payload: dict[str, Any] = {
        "type": body.type,
        "username": body.username[:64],
        "message": messages[body.type],
        "sound": "",  # no sound files vendored; overlays skip empty sounds
        "duration": 5000,
    }
    if body.amount is not None:
        payload["amount"] = body.amount
    hub.emit("alert", payload, retain=False)  # never replay old alerts
    return {"status": "ok"}
