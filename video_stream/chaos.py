"""Chaos engine: shareable JSON choreography for OBS + fullscreen effects.

A preset is a JSON file in ``presets/chaos/`` describing a timeline of steps —
scene cuts, source show/hide, transforms, filter toggles, sleeps, fullscreen
overlay effects — nested in ``serial``/``parallel`` containers. Think intros,
punch-in slams, BRB screens, endings: OBS choreography as data, validated
loudly at load with per-step path labels (``file.json:steps[2].steps[0]``),
never silently.

Execution runs on a worker thread (the OBS client is synchronous), one preset
at a time (409 when busy), with a per-preset cooldown and a
``safety.guard_action("chaos:<id>")`` check — the kill switch freezes chaos
like every other automation. Overlay-only ``fx`` steps ride the Studio Bus to
``/overlay/fx``, so they work even with OBS disconnected.

Ported in spirit from february11's chaos-engine.ts; the interpreter is
thread-based here because our OBS client is sync, and OBS "Sleep frames"
batching degrades to millisecond sleeps (documented tradeoff — revisit only
if frame-accurate choreography ever matters).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from video_stream.hub import hub

# "shake" was cut: it translated the fx page's own (transparent) body — an
# invisible effect on a browser-source overlay. Real screen-shake belongs to a
# chaos `transform` step wiggling the OBS scene item instead.
FX_EFFECTS = ("confetti", "glitch", "matrix", "flash", "blackout")
_MAX_STEPS = 200          # runaway-preset backstop (counted through containers)
_MAX_SLEEP_MS = 20_000

_ctx: dict[str, Any] = {}  # obs_factory, safety, presets_dir


def init(**kwargs: Any) -> None:
    _ctx.update(kwargs)


class ChaosError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---- validation -----------------------------------------------------------
def _validate_step(step: Any, path: str, counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > _MAX_STEPS:
        raise ChaosError(f"{path}: preset exceeds {_MAX_STEPS} steps")
    if not isinstance(step, dict):
        raise ChaosError(f"{path}: step must be an object")
    do = step.get("do")
    if do in ("serial", "parallel"):
        steps = step.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ChaosError(f"{path}: '{do}' needs a non-empty 'steps' array")
        for i, sub in enumerate(steps):
            _validate_step(sub, f"{path}.steps[{i}]", counter)
    elif do == "sleep":
        ms = step.get("ms")
        if not isinstance(ms, (int, float)) or not 0 <= ms <= _MAX_SLEEP_MS:
            raise ChaosError(f"{path}: 'sleep' needs ms in 0..{_MAX_SLEEP_MS}")
    elif do == "scene":
        if not isinstance(step.get("scene"), str) or not step["scene"].strip():
            raise ChaosError(f"{path}: 'scene' needs a scene name")
    elif do == "item":
        for key in ("scene", "source"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ChaosError(f"{path}: 'item' needs '{key}'")
        if not isinstance(step.get("enabled"), bool):
            raise ChaosError(f"{path}: 'item' needs boolean 'enabled'")
    elif do == "transform":
        for key in ("scene", "source"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ChaosError(f"{path}: 'transform' needs '{key}'")
        if not isinstance(step.get("transform"), dict) or not step["transform"]:
            raise ChaosError(f"{path}: 'transform' needs a non-empty transform object")
    elif do == "filter":
        for key in ("source", "filter"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ChaosError(f"{path}: 'filter' needs '{key}'")
        if not isinstance(step.get("enabled"), bool):
            raise ChaosError(f"{path}: 'filter' needs boolean 'enabled'")
    elif do == "fx":
        if step.get("effect") not in FX_EFFECTS:
            raise ChaosError(f"{path}: 'fx' effect must be one of {FX_EFFECTS}")
        if "ms" in step and (
            not isinstance(step["ms"], (int, float)) or not 0 <= step["ms"] <= _MAX_SLEEP_MS
        ):
            raise ChaosError(f"{path}: 'fx' ms must be 0..{_MAX_SLEEP_MS}")
    elif do == "request":
        if not isinstance(step.get("type"), str) or not step["type"].strip():
            raise ChaosError(f"{path}: 'request' needs an obs-websocket 'type'")
        if "data" in step and not isinstance(step["data"], dict):
            raise ChaosError(f"{path}: 'request' data must be an object")
    else:
        raise ChaosError(
            f"{path}: unknown step '{do}' — expected serial|parallel|sleep|"
            "scene|item|transform|filter|fx|request"
        )


def load_presets(*directories: Path) -> tuple[dict[str, dict], list[str]]:
    """Load every ``*.json`` preset from each directory (later dirs override
    earlier ones on id collisions, so user presets beat shipped ones).
    Invalid files are skipped LOUDLY."""
    presets: dict[str, dict] = {}
    errors: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(file.read_text())
                if not isinstance(raw, dict):
                    raise ChaosError(f"{file.name}: preset must be a JSON object")
                steps = raw.get("steps")
                if not isinstance(steps, list) or not steps:
                    raise ChaosError(f"{file.name}: needs a non-empty 'steps' array")
                counter = [0]
                for i, step in enumerate(steps):
                    _validate_step(step, f"{file.name}:steps[{i}]", counter)
                try:
                    # .get with a default so an explicit cooldown of 0 sticks.
                    cooldown = max(0.0, float(raw.get("cooldown", 5.0)))
                except (TypeError, ValueError):
                    raise ChaosError(f"{file.name}: cooldown must be a number")
                preset_id = file.stem
                presets[preset_id] = {
                    "id": preset_id,
                    "name": str(raw.get("name") or preset_id),
                    "cooldown": cooldown,
                    "confirm": bool(raw.get("confirm", False)),
                    "steps": steps,
                    "step_count": counter[0],
                }
            except (ValueError, ChaosError) as exc:
                message = f"[chaos] {exc}"
                print(message)
                errors.append(str(exc))
    return presets, errors


# ---- execution ------------------------------------------------------------
class ChaosEngine:
    def __init__(self) -> None:
        self._run_lock = threading.Lock()  # single-flight across all presets
        self._running: str | None = None
        self._last_run: dict[str, float] = {}
        self.presets: dict[str, dict] = {}
        self.load_errors: list[str] = []

    def reload(self) -> None:
        dirs = _ctx["presets_dirs"]
        self.presets, self.load_errors = load_presets(*dirs)

    def _kill_engaged(self) -> bool:
        safety = _ctx.get("safety")
        return bool(safety is not None and safety.status()["kill_switch"])

    def trigger(self, preset_id: str) -> dict:
        preset = self.presets.get(preset_id)
        if preset is None:
            raise ChaosError(f"no such preset: {preset_id}", 404)

        # Order matters: cheap rejections (cooldown, busy) must not consume
        # the shared safety budget — the guard runs only when we'd truly act.
        now = time.monotonic()
        last = self._last_run.get(preset_id, -1e9)
        if now - last < preset["cooldown"]:
            wait = preset["cooldown"] - (now - last)
            raise ChaosError(f"'{preset_id}' cooling down — {wait:.1f}s left", 429)
        if not self._run_lock.acquire(blocking=False):
            raise ChaosError(f"busy running '{self._running}' — one preset at a time", 409)

        try:
            safety = _ctx.get("safety")
            if safety is not None:
                safety.assert_action(f"chaos:{preset_id}")  # 423/429 via app handler
            self._running = preset_id
            self._last_run[preset_id] = now
            thread = threading.Thread(
                target=self._run_preset, args=(preset,), name=f"chaos-{preset_id}", daemon=True
            )
            thread.start()
        except BaseException:
            self._running = None
            self._run_lock.release()  # never leak the single-flight lock
            raise
        return {"status": "running", "id": preset_id, "steps": preset["step_count"]}

    def _run_preset(self, preset: dict) -> None:
        obs = _ctx["obs_factory"]()
        try:
            obs.connect()  # best-effort; fx/sleep steps work without OBS
            for i, step in enumerate(preset["steps"]):
                self._run_step(obs, step, f"{preset['id']}:steps[{i}]")
        except Exception as exc:
            print(f"[chaos] {preset['id']} aborted — {exc}")
        finally:
            try:
                obs.close()
            except Exception:
                pass
            self._running = None
            self._run_lock.release()
            hub.emit("chaos_done", {"id": preset["id"]}, retain=False)
            print(f"[chaos] {preset['id']} finished")

    def _run_step(self, obs, step: dict, path: str) -> None:
        if self._kill_engaged():
            return  # the kill switch aborts a RUNNING preset too, step by step
        do = step["do"]
        if do == "serial":
            for i, sub in enumerate(step["steps"]):
                self._run_step(obs, sub, f"{path}.steps[{i}]")
        elif do == "parallel":
            threads = [
                threading.Thread(
                    target=self._run_step,
                    args=(obs, sub, f"{path}.steps[{i}]"),
                    daemon=True,
                )
                for i, sub in enumerate(step["steps"])
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=_MAX_SLEEP_MS / 1000)
            for t in threads:
                if t.is_alive():
                    print(f"[chaos] {path}: a parallel branch is still running past its join window")
        elif do == "sleep":
            # Chunked so the kill switch interrupts long sleeps promptly.
            remaining = float(step["ms"]) / 1000.0
            while remaining > 0 and not self._kill_engaged():
                nap = min(0.25, remaining)
                time.sleep(nap)
                remaining -= nap
        elif do == "scene":
            if not obs.set_scene(step["scene"]):
                print(f"[chaos] {path}: scene '{step['scene']}' failed (missing?)")
        elif do == "item":
            item_id = obs.scene_item_id(step["scene"], step["source"])
            if item_id is None or not obs.set_scene_item_enabled(
                step["scene"], item_id, step["enabled"]
            ):
                print(f"[chaos] {path}: item '{step['source']}' in '{step['scene']}' failed")
        elif do == "transform":
            item_id = obs.scene_item_id(step["scene"], step["source"])
            if item_id is None or not obs.set_scene_item_transform(
                step["scene"], item_id, step["transform"]
            ):
                print(f"[chaos] {path}: transform on '{step['source']}' failed")
        elif do == "filter":
            if not obs.set_source_filter_enabled(
                step["source"], step["filter"], step["enabled"]
            ):
                print(f"[chaos] {path}: filter '{step['filter']}' on '{step['source']}' failed")
        elif do == "fx":
            hub.emit(
                "fx",
                {"effect": step["effect"], "ms": int(step.get("ms") or 1500)},
                retain=False,
            )
        elif do == "request":
            obs._request(step["type"], step.get("data") or {})

    def status(self) -> dict:
        return {
            "running": self._running,
            "presets": [
                {k: p[k] for k in ("id", "name", "cooldown", "confirm", "step_count")}
                for p in self.presets.values()
            ],
            "effects": list(FX_EFFECTS),
            "load_errors": self.load_errors,
        }


engine = ChaosEngine()

router = APIRouter()


class ChaosTrigger(BaseModel):
    id: str


class FxTrigger(BaseModel):
    effect: str
    ms: int = 1500


@router.get("/api/chaos")
async def api_chaos_list():
    return engine.status()


@router.post("/api/chaos/reload")
async def api_chaos_reload():
    engine.reload()
    return engine.status()


@router.post("/api/chaos/trigger")
async def api_chaos_trigger(body: ChaosTrigger):
    try:
        return engine.trigger(body.id)
    except ChaosError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/api/chaos/fx")
async def api_chaos_fx(body: FxTrigger):
    """Overlay-only effect: harmless by construction (never touches OBS)."""
    if body.effect not in FX_EFFECTS:
        raise HTTPException(status_code=400, detail=f"effect must be one of {FX_EFFECTS}")
    hub.emit("fx", {"effect": body.effect, "ms": max(200, min(20_000, body.ms))}, retain=False)
    return {"status": "ok"}
