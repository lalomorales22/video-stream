"""One-keypress replay highlights, ported from february11's ReplayDirector.

``capture()`` drives the OBS replay buffer end to end: make sure the buffer is
running, save it, fetch the saved file's path, then run the optional garnish —
instant playback into a media source, a templated lower-third that auto-hides,
and a chapter marker if a recording is rolling. Every garnish step degrades to
a warning instead of failing the capture; the replay file is saved either way.

Also home to the auto-capture watcher: a small thread that watches per-camera
motion scores and calls ``capture()`` by itself when the room clearly pops off
— several distinct spikes inside a short window — with its own cooldown so one
hype moment can't machine-gun the replay buffer. It runs independently of the
director, so auto-highlights work even when auto-switching is off.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import PurePath
from typing import Callable


@dataclass
class ReplayConfig:
    media_input: str = ""        # OBS media source for instant playback ("" = skip)
    lower_third_input: str = ""  # OBS text source updated per capture ("" = skip)
    lower_third_scene: str = ""  # scene holding that text source ("" = text only)
    lower_third_duration: float = 6.0
    lower_third_template: str = "REPLAY · {label}"
    capture_wait: float = 0.7    # seconds OBS needs to finish writing the file
    auto_start_buffer: bool = True
    create_chapter: bool = True
    chapter_prefix: str = "Replay"
    # Auto-capture: N motion spikes inside the window trigger one capture.
    auto_threshold: float = 0.30
    auto_spikes: int = 3
    auto_window: float = 10.0
    auto_cooldown: float = 30.0


class ReplayError(RuntimeError):
    """Capture failed in a way the operator must hear about (HTTP-ish code)."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class ReplayDirector:
    def __init__(
        self,
        obs,
        config: ReplayConfig | None = None,
        manager=None,
        safety=None,
        on_event: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.obs = obs
        self.cfg = config or ReplayConfig()
        self.manager = manager  # CameraManager, for auto-capture motion scores
        self.safety = safety
        self.on_event = on_event  # hub.emit-compatible: (event, payload, retain=)
        self._clock = clock

        self._lock = threading.Lock()
        self._item_cache: tuple[str, str, int] | None = None  # scene, source, id
        self._timer_lock = threading.Lock()
        self._hide_timer: threading.Timer | None = None
        self._hide_generation = 0

        self._auto_running = False
        self._auto_thread: threading.Thread | None = None
        self._last_auto = -1e9

        self._status: dict = {
            "replay_buffer_active": None,
            "last_capture_at": None,
            "last_label": None,
            "last_path": None,
            "playback_triggered": False,
            "chapter_created": False,
            "lower_third_visible": False,
            "last_error": None,
            "auto_enabled": False,
        }

    # ---- status ----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _update(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)
            snap = dict(self._status)
        if self.on_event is not None:
            try:
                self.on_event("replay", snap)
            except Exception:
                pass

    # ---- the capture pipeline -------------------------------------------
    def capture(self, label: str | None = None, *, action: str = "replay:capture") -> dict:
        if self.safety is not None:
            self.safety.assert_action(action)  # raises SafetyBlocked

        label = (label or "").strip()[:64] or time.strftime("Highlight %H:%M:%S")

        if not self.obs.connected and not self.obs.connect():
            hint = self.obs.last_error or "is the WebSocket server on?"
            self._update(last_error=f"OBS unreachable: {hint}")
            raise ReplayError(f"OBS is not reachable — {hint}", 503)

        try:
            self._ensure_buffer()

            if not self.obs.save_replay_buffer():
                raise ReplayError("OBS refused to save the replay buffer", 500)
            time.sleep(self.cfg.capture_wait)  # let OBS finish writing the file

            path = self.obs.last_replay_path()
            playback = self._trigger_playback(path)
            lower_third = self._show_lower_third(label, path)
            chapter = self._create_chapter(label)
        except ReplayError as exc:
            self._update(last_error=str(exc))
            raise

        result = {
            "label": label,
            "path": path,
            "playback_triggered": playback,
            "chapter_created": chapter,
            "lower_third_shown": lower_third,
        }
        self._update(
            replay_buffer_active=True,
            last_capture_at=time.strftime("%H:%M:%S"),
            last_label=label,
            last_path=path,
            playback_triggered=playback,
            chapter_created=chapter,
            last_error=None,
        )
        if self.on_event is not None:
            try:
                self.on_event("replay_saved", result, retain=False)
            except Exception:
                pass
        print(f"[replay] saved · '{label}'" + (f" · {path}" if path else ""))
        return result

    def _ensure_buffer(self) -> None:
        active = self.obs.replay_buffer_active()
        if active is None and not self.obs.connected:
            # The socket went stale mid-request (OBS restarted since the last
            # capture). One reconnect + retry, so the first click still works.
            if self.obs.connect():
                active = self.obs.replay_buffer_active()
        if active:
            return
        # `active is None` here means OBS answered the connection but errored
        # the status request — which is exactly what happens when no replay
        # buffer output is configured, so give the setup hint, not a 502.
        if active is not None and self.cfg.auto_start_buffer and self.obs.start_replay_buffer():
            time.sleep(0.45)  # buffer needs a beat before it can save
            return
        raise ReplayError(
            "The OBS replay buffer is off. Enable it in OBS: "
            "Settings → Output → Replay Buffer, then try again.",
            409,
        )

    def _trigger_playback(self, path: str | None) -> bool:
        if not path or not self.cfg.media_input:
            return False
        ok = self.obs.set_input_settings(
            self.cfg.media_input, {"local_file": path}
        ) and self.obs.trigger_media_restart(self.cfg.media_input)
        if not ok:
            print(f"[replay] instant playback failed on '{self.cfg.media_input}'")
        return ok

    def _show_lower_third(self, label: str, path: str | None) -> bool:
        if not self.cfg.lower_third_input:
            return False
        # Tokens first, the label last — so a label that itself contains
        # "{time}" or "{file}" is shown verbatim, never substituted.
        text = (
            self.cfg.lower_third_template.replace("{time}", time.strftime("%H:%M:%S"))
            .replace("{file}", PurePath(path).name if path else "")
            .replace("{label}", label)
        )
        if not self.obs.set_input_settings(self.cfg.lower_third_input, {"text": text}):
            print(f"[replay] lower-third update failed on '{self.cfg.lower_third_input}'")
            return False
        if not self.cfg.lower_third_scene:
            return True  # text-only mode: the source is always visible

        if not self._set_lower_third_enabled(True):
            print(
                f"[replay] could not show '{self.cfg.lower_third_input}' in scene "
                f"'{self.cfg.lower_third_scene}'"
            )
            return False
        self._update(lower_third_visible=True)
        self._schedule_hide()
        return True

    def _set_lower_third_enabled(self, enabled: bool) -> bool:
        """Toggle the lower-third scene item, refreshing the cached item id
        once if OBS rejects it (the source may have been recreated)."""
        for attempt in (1, 2):
            item_id = self._lower_third_item_id()
            if item_id is not None and self.obs.set_scene_item_enabled(
                self.cfg.lower_third_scene, item_id, enabled
            ):
                return True
            self._item_cache = None  # stale id — resolve fresh and retry once
        return False

    def _create_chapter(self, label: str) -> bool:
        if not self.cfg.create_chapter:
            return False
        try:
            if not self.obs.record_active():
                return False
            return self.obs.create_record_chapter(
                f"{self.cfg.chapter_prefix} {label}".strip()
            )
        except Exception:
            return False

    def _lower_third_item_id(self) -> int | None:
        cached = self._item_cache
        if (
            cached is not None
            and cached[0] == self.cfg.lower_third_scene
            and cached[1] == self.cfg.lower_third_input
        ):
            return cached[2]
        item_id = self.obs.scene_item_id(
            self.cfg.lower_third_scene, self.cfg.lower_third_input
        )
        if item_id is not None:
            self._item_cache = (
                self.cfg.lower_third_scene,
                self.cfg.lower_third_input,
                item_id,
            )
        return item_id

    def _schedule_hide(self) -> None:
        # Generation-tagged so a timer from an earlier capture that fires mid-
        # swap can never hide the lower-third a newer capture just showed.
        with self._timer_lock:
            self._hide_generation += 1
            generation = self._hide_generation
            if self._hide_timer is not None:
                self._hide_timer.cancel()
            self._hide_timer = threading.Timer(
                self.cfg.lower_third_duration, self._hide_lower_third, args=(generation,)
            )
            self._hide_timer.daemon = True
            self._hide_timer.start()

    def _hide_lower_third(self, generation: int | None = None) -> None:
        with self._timer_lock:
            if generation is not None and generation != self._hide_generation:
                return  # superseded by a newer capture's timer
        try:
            self._set_lower_third_enabled(False)
        except Exception:
            pass
        self._update(lower_third_visible=False)

    # ---- auto-capture on motion spikes ----------------------------------
    @property
    def auto_enabled(self) -> bool:
        return self._auto_running

    def set_auto(self, enabled: bool) -> None:
        if enabled and not self._auto_running:
            self._auto_running = True
            # A fresh thread supersedes any older one still finishing its tick:
            # the loop checks it is still `self._auto_thread`, so a fast
            # off→on flip can never leave two watchers running.
            self._auto_thread = threading.Thread(
                target=self._auto_loop, name="replay-auto", daemon=True
            )
            self._auto_thread.start()
            print("[replay] auto-capture on — watching for motion spikes")
        elif not enabled and self._auto_running:
            self._auto_running = False
            print("[replay] auto-capture off")
        self._update(auto_enabled=self._auto_running)

    def _auto_loop(self) -> None:
        me = threading.current_thread()
        spikes: deque[float] = deque()
        was_above = False
        while self._auto_running and self._auto_thread is me:
            time.sleep(0.25)
            if self.manager is None:
                continue
            now = self._clock()
            top = 0.0
            for cam in self.manager.list_cameras():
                stream = self.manager.get(cam.index)
                if stream is not None and stream.active:
                    top = max(top, float(getattr(stream, "motion_score", 0.0)))

            # Rising-edge detection: one sustained wave = one spike, not many.
            above = top >= self.cfg.auto_threshold
            if above and not was_above:
                spikes.append(now)
            was_above = above

            while spikes and now - spikes[0] > self.cfg.auto_window:
                spikes.popleft()
            if (
                len(spikes) < self.cfg.auto_spikes
                or now - self._last_auto < self.cfg.auto_cooldown
            ):
                continue

            spikes.clear()
            self._last_auto = now
            try:
                self.capture("Auto highlight", action="replay:auto")
            except Exception as exc:  # SafetyBlocked, ReplayError, anything
                print(f"[replay] auto-capture skipped — {exc}")

    def shutdown(self) -> None:
        self._auto_running = False
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None
        try:
            self.obs.close()
        except Exception:
            pass
