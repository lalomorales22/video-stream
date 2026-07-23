"""Auto-director: pick the "active" camera from motion scores and switch OBS to it.

The hard part isn't picking the highest score — it's *not* switching on every little
flicker. The decision engine applies three guards:

* **min_score** — a camera must show real activity to be a candidate at all.
* **margin** — a challenger must beat the current camera by a clear margin, so two
  similar feeds don't ping-pong.
* **hold + cooldown** — a challenger must stay on top for `hold` seconds before we cut
  to it, and we won't cut again until `cooldown` seconds have passed.

`update()` is pure and clock-injected so it can be tested deterministically; the
background thread just feeds it live scores on an interval.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class DirectorConfig:
    scene_map: dict[int, str] = field(default_factory=dict)
    min_score: float = 0.02
    margin: float = 0.01
    hold: float = 1.5       # seconds a challenger must lead before cutting
    cooldown: float = 3.0   # minimum seconds between cuts
    interval: float = 0.25  # polling period
    dry_run: bool = False


class Director:
    """House rule: every actuator that touches OBS on its own must ask
    ``safety.guard_action("<subsystem>:<action>")`` first — the director does
    here, and any future automation (chaos, broadcast, …) does the same the
    day it's born, so the one kill switch really freezes everything."""

    def __init__(self, manager, obs_client=None, config: DirectorConfig | None = None,
                 clock=time.monotonic, safety=None, on_switch=None) -> None:
        self.manager = manager
        self.obs = obs_client
        self.cfg = config or DirectorConfig()
        self._clock = clock
        self.safety = safety
        self.on_switch = on_switch  # callback(cam_index, scene, entry); any thread

        self.active: int | None = None
        self._candidate: int | None = None
        self._candidate_since: float = 0.0
        self._last_switch: float = -1e9
        self.scores: dict[int, float] = {}
        self.log: list[dict] = []          # recent decisions, newest last (capped)
        self.obs_connected: bool = False

        self._thread: threading.Thread | None = None
        self._running = False

    # ---- decision engine (pure, testable) --------------------------------
    def update(self, now: float, scores: dict[int, float]) -> tuple[int, str] | None:
        self.scores = scores
        if not scores:
            return None

        leader = max(scores, key=scores.get)
        if scores[leader] < self.cfg.min_score:
            self._candidate = None
            return None

        if leader == self.active:
            self._candidate = None
            return None

        # Challenger must clear the current active by a margin.
        current = self.scores.get(self.active, 0.0) if self.active is not None else 0.0
        if scores[leader] < current + self.cfg.margin:
            self._candidate = None
            return None

        # Debounce: the same challenger has to persist for `hold` seconds.
        if self._candidate != leader:
            self._candidate = leader
            self._candidate_since = now
            return None
        if now - self._candidate_since < self.cfg.hold:
            return None
        if now - self._last_switch < self.cfg.cooldown:
            return None

        # Commit the switch.
        self.active = leader
        self._candidate = None
        self._last_switch = now
        scene = self.cfg.scene_map.get(leader, f"camera {leader}")
        return leader, scene

    # ---- background loop -------------------------------------------------
    def _collect_scores(self) -> dict[int, float]:
        scores: dict[int, float] = {}
        for cam in self.manager.list_cameras():
            stream = self.manager.get(cam.index)
            if stream is not None and stream.active:
                scores[cam.index] = round(float(getattr(stream, "motion_score", 0.0)), 4)
        return scores

    def _actuate(self, cam_index: int, scene: str) -> None:
        if self.safety is not None:
            ok, reason = self.safety.guard_action("director:switch")
            if not ok:
                print(f"[director] switch to camera {cam_index} skipped — {reason}")
                return
        acted = False
        if self.obs is not None and not self.cfg.dry_run:
            if not self.obs.connected:
                self.obs_connected = self.obs.connect()
            if self.obs.connected:
                acted = self.obs.set_scene(scene)
                self.obs_connected = self.obs.connected
        entry = {
            "t": round(self._clock(), 2),
            "camera": cam_index,
            "scene": scene,
            "acted": acted,
            "mode": "obs" if acted else ("dry-run" if self.cfg.dry_run else "no-obs"),
        }
        self.log.append(entry)
        del self.log[:-20]  # keep last 20
        tag = "→ OBS" if acted else "(dry-run)" if self.cfg.dry_run else "(OBS not connected)"
        print(f"[director] switch to camera {cam_index} · scene '{scene}' {tag}")
        if self.on_switch is not None:
            try:
                self.on_switch(cam_index, scene, entry)
            except Exception:
                pass  # a broken listener must never stop the show

    def _loop(self) -> None:
        while self._running:
            # Probe (non-consuming) BEFORE the decision engine runs: while the
            # kill switch or rate limiter would block a cut, the whole engine
            # freezes instead of committing `active` for a cut that never
            # happens — releasing the guard resumes from consistent state.
            if self.safety is not None and not self.safety.check_action("director:switch")[0]:
                time.sleep(self.cfg.interval)
                continue
            switch = self.update(self._clock(), self._collect_scores())
            if switch is not None:
                self._actuate(*switch)
            time.sleep(self.cfg.interval)

    def start(self) -> None:
        if self._running:
            return
        if self.obs is not None and not self.cfg.dry_run:
            self.obs_connected = self.obs.connect()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="director", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self.obs is not None:
            self.obs.close()

    def status(self) -> dict:
        return {
            "active": self.active,
            "scores": self.scores,
            "scene_map": self.cfg.scene_map,
            "dry_run": self.cfg.dry_run,
            "obs_connected": self.obs_connected,
            "obs_error": getattr(self.obs, "last_error", None) if self.obs else None,
            "recent": self.log[-10:],
        }
