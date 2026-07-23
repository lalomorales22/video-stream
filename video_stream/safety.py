"""Safety rails for every automation: a kill switch and a rate limiter.

Ported from february11's SafetyManager. One shared instance guards everything
that acts on OBS by itself — the auto-director today; replay auto-capture and
any future actuator (chaos, broadcast, …) must call ``guard_action`` the day
they are born. Two independent guards:

* **Kill switch** — one operator action freezes every automation instantly.
  Manual, human-initiated actions (camera start/stop, the fallback-scene cut)
  are deliberately NOT guarded: the panic path must always work.
* **Rate limiter** — at most ``max_actions`` automated actions per rolling
  ``window`` seconds, so a runaway loop can't machine-gun OBS.

Action names are namespaced ``"subsystem:action"`` — e.g. ``director:switch``,
``replay:capture`` — so status and logs say exactly who got blocked.

Framework-free and clock-injected so it unit-tests deterministically (like
``Director.update``); app.py maps :class:`SafetyBlocked` onto HTTP 423 (kill
switch) / 429 (rate limited).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class SafetyBlocked(Exception):
    """An automated action was refused. ``status_code`` follows HTTP semantics."""

    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class SafetyManager:
    def __init__(
        self,
        max_actions: int = 40,
        window: float = 60.0,
        fallback_scene: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[dict], None] | None = None,
    ) -> None:
        self.max_actions = max_actions
        self.window = window
        self.fallback_scene = fallback_scene
        self.on_change = on_change  # called with status() after every change
        self._clock = clock
        self._lock = threading.Lock()
        self._timestamps: deque[float] = deque()
        self._kill = False
        self._last_blocked: str | None = None

    # ---- queries ---------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            self._prune()
            used = len(self._timestamps)
            return {
                "kill_switch": self._kill,
                "fallback_scene": self.fallback_scene,
                "actions_in_window": used,
                "max_actions": self.max_actions,
                "window_seconds": self.window,
                "remaining": max(0, self.max_actions - used),
                "last_blocked": self._last_blocked,
            }

    # ---- controls --------------------------------------------------------
    def set_kill_switch(self, on: bool, reason: str | None = None) -> dict:
        with self._lock:
            self._kill = on
            self._last_blocked = (reason or "kill switch engaged") if on else None
        if on:
            print(f"[safety] KILL SWITCH ON — {reason or 'operator'}")
        else:
            print("[safety] kill switch off — automations may act again")
        return self._changed()

    def check_action(self, name: str) -> tuple[bool, str | None]:
        """Pure probe: would this action be allowed right now? Consumes nothing
        and mutates nothing — safe to call every tick of an automation loop."""
        with self._lock:
            self._prune()
            if self._kill:
                return False, f"blocked by kill switch ({name})"
            if len(self._timestamps) >= self.max_actions:
                return False, f"rate limited ({name})"
            return True, None

    def guard_action(
        self, name: str, *, bypass_kill: bool = False, bypass_rate: bool = False
    ) -> tuple[bool, str | None]:
        """Ask permission to act. Consumes one budget slot when allowed."""
        with self._lock:
            self._prune()
            if self._kill and not bypass_kill:
                reason = f"blocked by kill switch ({name})"
                self._last_blocked = reason
                result = (False, reason)
            elif not bypass_rate and len(self._timestamps) >= self.max_actions:
                reason = f"rate limited ({name})"
                self._last_blocked = reason
                result = (False, reason)
            else:
                if not bypass_rate:
                    self._timestamps.append(self._clock())
                result = (True, None)
        self._changed()
        return result

    def assert_action(
        self, name: str, *, bypass_kill: bool = False, bypass_rate: bool = False
    ) -> None:
        ok, reason = self.guard_action(
            name, bypass_kill=bypass_kill, bypass_rate=bypass_rate
        )
        if not ok:
            code = 429 if reason and reason.startswith("rate limited") else 423
            raise SafetyBlocked(reason or "action blocked", code)

    # ---- internals -------------------------------------------------------
    def _prune(self) -> None:
        cutoff = self._clock() - self.window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _changed(self) -> dict:
        snap = self.status()
        cb = self.on_change
        if cb is not None:
            try:
                cb(snap)
            except Exception:
                pass  # a broken listener must never block the automation path
        return snap
