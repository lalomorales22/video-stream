"""Runtime settings: a declarative field registry persisted outside the web root.

The dashboard can finally configure the rig (OBS password, scene map, director
tuning, replay sources, the auth token) without CLI flags. Persistence lives at
``~/.config/video-stream/settings.json`` (override dir with
``VIDEO_STREAM_CONFIG``) — deliberately NOT under ``static/``, which is served
over HTTP.

Precedence at boot: explicit CLI flags > saved settings > defaults. (A CLI flag
set to exactly its default value is indistinguishable from "not passed" and
lets the saved setting win — documented tradeoff, kept for simplicity.)

Secrets are write-only through the API: ``public()`` masks them and POST
accepts a new value, but the actual value never leaves the server. The
:func:`require_token` dependency gates the settings routes (and any future
sensitive route) with one shared header token once ``auth_token`` is set.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

_MASK = "••••••"


@dataclass(frozen=True)
class Field:
    kind: type  # str | int | float | bool
    default: Any
    secret: bool = False
    help: str = ""


# Keys double as config[] keys and argparse dests, so one name works everywhere.
FIELDS: dict[str, Field] = {
    "obs_host": Field(str, "127.0.0.1", help="OBS WebSocket host"),
    "obs_port": Field(int, 4455, help="OBS WebSocket port"),
    "obs_password": Field(str, "", secret=True, help="OBS WebSocket password"),
    "obs_scene_map": Field(str, "", help='Camera→scene map, e.g. "0=Cam A,1=Cam B"'),
    "director_hold": Field(float, 1.5, help="Seconds a challenger must lead before a cut"),
    "director_cooldown": Field(float, 3.0, help="Minimum seconds between cuts"),
    "director_min_score": Field(float, 0.02, help="Motion score needed to be a candidate"),
    "director_auto_punch": Field(bool, False, help="Punch in on the subject after each cut"),
    "peers": Field(str, "", help='Rig Link peers, e.g. "studio=192.168.1.42:8765"'),
    "safety_fallback_scene": Field(str, "", help="OBS scene for the panic cut"),
    "safety_max_actions": Field(int, 40, help="Max automated OBS actions per minute"),
    "replay_media_source": Field(str, "", help="OBS media source for instant replay playback"),
    "replay_lower_third": Field(str, "", help="OBS text source for the replay lower-third"),
    "replay_lower_third_scene": Field(str, "", help="Scene holding the lower-third source"),
    "auth_token": Field(str, "", secret=True, help="Shared token required for settings changes"),
}


def _coerce(key: str, value: Any) -> Any:
    field = FIELDS[key]
    try:
        if field.kind is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if field.kind is str:
            return str(value).strip()
        return field.kind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: expected {field.kind.__name__}, got {value!r}")


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        config_dir = Path(
            os.environ.get("VIDEO_STREAM_CONFIG", Path.home() / ".config" / "video-stream")
        )
        self.path = path or (config_dir / "settings.json")
        self._lock = threading.Lock()
        self.loaded = False  # set by load(); False in a fresh --reload subprocess
        self._values: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        """Read saved values from disk (unknown keys dropped, all coerced)."""
        with self._lock:
            try:
                raw = json.loads(self.path.read_text())
            except (OSError, ValueError):
                raw = {}
            values: dict[str, Any] = {}
            for key, value in raw.items():
                if key in FIELDS:
                    try:
                        values[key] = _coerce(key, value)
                    except ValueError:
                        continue  # a hand-edited bad value must not kill boot
            self._values = values
            self.loaded = True
            return dict(values)

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values.get(key, FIELDS[key].default)

    def save(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Validate, merge, and atomically persist. Returns just the values
        this call actually changed, so callers apply only the delta."""
        clean: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in FIELDS:
                raise ValueError(f"unknown setting: {key}")
            if FIELDS[key].secret and value == _MASK:
                continue  # the UI echoed the mask back — keep the stored secret
            clean[key] = _coerce(key, value)
        with self._lock:
            self._values.update(clean)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._values, indent=2))
            os.chmod(tmp, 0o600)  # holds secrets — owner-only, like ssh keys
            tmp.replace(self.path)
            return dict(clean)

    def public(self) -> list[dict[str, Any]]:
        """Field metadata + current values for the dashboard; secrets masked.
        Prefers the live runtime value over the saved one, so the form always
        edits what the rig is actually doing."""
        with self._lock:
            out = []
            for key, field in FIELDS.items():
                stored = self._values.get(key, field.default)
                if field.secret:
                    live = _live(key) if _live is not None else ""
                    value = _MASK if (stored or live) else ""
                else:
                    value = _live(key) if _live is not None else stored
                out.append(
                    {
                        "key": key,
                        "kind": field.kind.__name__,
                        "value": value,
                        "default": field.default,
                        "secret": field.secret,
                        "help": field.help,
                    }
                )
            return out


settings = Settings()

# Wired by app.py before the router is included: `_apply` pushes posted values
# onto the live config dict / subsystems; `_live` reads the EFFECTIVE runtime
# value so the dashboard form shows reality (live config), not just what was
# previously saved — otherwise one full-form Save would silently revert every
# CLI-set value to its saved-or-default state mid-show.
_apply: Callable[[dict[str, Any]], None] | None = None
_live: Callable[[str], Any] | None = None


def init(
    apply: Callable[[dict[str, Any]], None],
    live: Callable[[str], Any] | None = None,
) -> None:
    global _apply, _live
    _apply = apply
    _live = live


def require_token(request: Request) -> None:
    """Shared-token gate. Open until an auth_token is configured; after that,
    callers must send it as the X-Auth-Token header. (Deliberately NOT a query
    param — uvicorn's access log would print the URL, secret and all.)"""
    token = settings.get("auth_token")
    if not token:
        return
    supplied = request.headers.get("x-auth-token") or ""
    if not hmac.compare_digest(supplied.encode(), str(token).encode()):
        raise HTTPException(status_code=401, detail="Missing or bad X-Auth-Token")


router = APIRouter()


@router.get("/api/settings")
async def api_settings_get(request: Request):
    require_token(request)
    return {"fields": settings.public(), "path": str(settings.path)}


@router.post("/api/settings")
async def api_settings_post(request: Request):
    require_token(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object of settings")
    try:
        # Atomic disk write — off the loop so a slow $HOME can't stall streams.
        saved = await asyncio.to_thread(settings.save, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if _apply is not None:
        _apply(saved)
    return {"fields": settings.public()}
