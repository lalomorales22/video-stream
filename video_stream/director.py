"""Auto-director: rule-driven scene switching from motion AND audio signals.

The decision engine is february11's damping machine (ported, with its two
protocol bugs fixed — see obs.AudioMeterListener), generalized to two signal
kinds that are never mixed into one scalar:

* ``motion:<camIndex>`` — this rig's per-camera motion score in [0, 1]
* ``audio:<inputName>`` — live OBS input loudness in dBFS (meters events)

Each rule maps one signal to one scene, with a threshold, a priority, and an
optional per-rule hold. The pipeline in order: cooldown freeze → fresh+loud
candidates → priority sort → hysteresis vs the active rule (+3 dB audio /
+margin motion; only within the same signal kind — cross-kind steals are
damped by priority and hold instead) → pending/hold confirmation →
adopt-without-call when the scene is already live → commit. Every pass leaves
a human-readable ``last_decision`` so the operator can always see WHY.

Without a rules file the engine synthesizes one motion rule per camera from
``--obs-scene-map``, which reproduces the original motion-only behavior.

``update()`` stays pure and clock-injected for deterministic tests; the
background thread feeds it live signals. House rule: every actuator that
touches OBS on its own asks ``safety.guard_action(...)`` first — the loop
probes ``safety.check_action`` each tick so blocked periods freeze the whole
engine instead of desyncing its state.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_FRESH_WINDOW = 1.8  # seconds a signal sample stays usable (feb11: 1800ms)


@dataclass(frozen=True)
class Rule:
    source: str            # "motion:<camIndex>" | "audio:<inputName>"
    scene: str
    threshold: float       # motion score [0,1] or dBFS [-90,0]
    priority: float = 50.0
    hold: float | None = None  # seconds; None = config default
    id: str = ""

    @property
    def kind(self) -> str:
        return "audio" if self.source.startswith("audio:") else "motion"

    @property
    def key(self) -> str:
        """Signal-map key: normalized (audio names are case/space-insensitive)."""
        kind, _, name = self.source.partition(":")
        return f"{kind}:{name.strip().lower()}"

    @property
    def cam_index(self) -> int | None:
        if self.kind != "motion":
            return None
        try:
            return int(self.source.partition(":")[2])
        except ValueError:
            return None


def _clamp(value, fallback: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if v != v or v in (float("inf"), float("-inf")):
        return fallback
    return min(hi, max(lo, v))


def _rule_id(source: str, scene: str, index: int) -> str:
    slug = lambda s: re.sub(r"\s+", "_", s.strip().lower())
    return f"{slug(source)}__{slug(scene)}__{index + 1}"


def parse_rules(raw: dict) -> tuple[list[Rule], dict]:
    """Parse a rules JSON object → (rules, engine overrides). Bad rules are
    skipped with a loud print naming the index — never silently."""
    overrides = {}
    if "cooldown" in raw:
        overrides["cooldown"] = _clamp(raw["cooldown"], 2.5, 0.25, 15.0)
    if "default_hold" in raw:
        overrides["hold"] = _clamp(raw["default_hold"], 0.9, 0.0, 8.0)
    if "hysteresis_db" in raw:
        overrides["hysteresis_db"] = _clamp(raw["hysteresis_db"], 3.0, 0.0, 24.0)

    rules: list[Rule] = []
    for i, item in enumerate(raw.get("rules") or []):
        if not isinstance(item, dict):
            print(f"[director] rules[{i}]: not an object — skipped")
            continue
        source = str(item.get("source") or "").strip()
        scene = str(item.get("scene") or "").strip()
        kind = source.partition(":")[0]
        if kind not in ("motion", "audio") or not source.partition(":")[2].strip() or not scene:
            print(f"[director] rules[{i}]: need source 'motion:<cam>'/'audio:<input>' and scene — skipped")
            continue
        threshold = (
            _clamp(item.get("threshold"), -32.0, -90.0, 0.0)
            if kind == "audio"
            else _clamp(item.get("threshold"), 0.02, 0.0, 1.0)
        )
        hold = item.get("hold")
        hold = _clamp(hold, 0.0, 0.0, 10.0) if isinstance(hold, (int, float)) else None
        rule_id = str(item.get("id") or "").strip() or _rule_id(source, scene, i)
        rules.append(
            Rule(
                source=source,
                scene=scene,
                threshold=threshold,
                priority=_clamp(item.get("priority"), 50.0, 0.0, 1000.0),
                hold=hold,
                id=rule_id,
            )
        )
    return rules, overrides


def load_rules_file(path: str) -> tuple[list[Rule], dict]:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        print(f"[director] could not read rules file {path}: {exc}")
        return [], {}
    if not isinstance(raw, dict):
        print(f"[director] rules file {path} must be a JSON object")
        return [], {}
    rules, overrides = parse_rules(raw)
    print(f"[director] rules: {len(rules)} loaded from {path}")
    return rules, overrides


@dataclass
class DirectorConfig:
    scene_map: dict[int, str] = field(default_factory=dict)
    min_score: float = 0.02
    margin: float = 0.01          # motion hysteresis
    hysteresis_db: float = 3.0    # audio hysteresis (feb11 default)
    hold: float = 1.5             # default seconds a challenger must persist
    cooldown: float = 3.0         # minimum seconds between cuts
    interval: float = 0.25        # polling period
    dry_run: bool = False
    rules: list[Rule] = field(default_factory=list)


class Director:
    def __init__(self, manager, obs_client=None, config: DirectorConfig | None = None,
                 clock=time.monotonic, safety=None, on_switch=None, audio=None) -> None:
        self.manager = manager
        self.obs = obs_client
        self.audio = audio  # AudioMeterListener | None
        self.cfg = config or DirectorConfig()
        self._clock = clock
        self.safety = safety
        self.on_switch = on_switch  # callback(cam_index|None, scene, entry); any thread

        self.active_rule: Rule | None = None
        self._pending: tuple[str, float] | None = None  # (rule_id, since)
        self._last_switch: float = -1e9
        self.last_decision: str | None = None
        self.signals: dict[str, tuple[float, float]] = {}  # key -> (value, seen_at)
        self.current_scene: str | None = None  # last known OBS program scene
        self.log: list[dict] = []          # recent switches, newest last (capped)
        self.obs_connected: bool = False

        self._thread: threading.Thread | None = None
        self._running = False

    # ---- decision engine (pure, testable) --------------------------------
    def _rules(self) -> list[Rule]:
        if self.cfg.rules:
            return self.cfg.rules
        # Legacy mode: one motion rule per camera currently reporting a signal.
        rules = []
        for key in self.signals:
            kind, _, cam = key.partition(":")
            if kind != "motion":
                continue
            idx = int(cam)
            rules.append(
                Rule(
                    source=key,
                    scene=self.cfg.scene_map.get(idx, f"camera {idx}"),
                    threshold=self.cfg.min_score,
                    id=f"motion_cam{idx}",
                )
            )
        return rules

    def update(self, now: float, signals: dict[str, tuple[float, float]]) -> tuple[Rule, str] | None:
        """One evaluation pass. ``signals`` maps rule keys to (value, seen_at)."""
        self.signals = signals
        rules = self._rules()
        if not rules:
            return None

        # Cooldown freezes the whole pipeline (feb11 order): pending survives,
        # decision untouched.
        if now - self._last_switch < self.cfg.cooldown:
            return None

        candidates = []
        for rule in rules:
            value, seen_at = signals.get(rule.key, (float("-inf"), 0.0))
            if now - seen_at <= _FRESH_WINDOW and value >= rule.threshold:
                candidates.append((rule, value))
        if not candidates:
            self._pending = None
            self.last_decision = "no-candidate"
            return None
        candidates.sort(key=lambda c: (-c[0].priority, -c[1]))
        top_rule, top_value = candidates[0]

        active = self.active_rule
        if active is not None and active.id == top_rule.id:
            self._pending = None
            self.last_decision = f"holding:{active.id}"
            return None

        # Hysteresis only compares like with like; a cross-kind steal is
        # damped by priority + hold instead (dB vs [0,1] can't be compared).
        if active is not None and active.kind == top_rule.kind:
            active_value, active_seen = signals.get(active.key, (float("-inf"), 0.0))
            if now - active_seen > _FRESH_WINDOW:
                # A stale active signal (e.g. a muted mic whose last loud
                # level the meters listener keeps frozen) cannot defend the
                # shot — same freshness rule the candidates already obey.
                active_value = float("-inf")
            margin = self.cfg.hysteresis_db if active.kind == "audio" else self.cfg.margin
            if top_value < active_value + margin:
                self._pending = None
                self.last_decision = f"hysteresis-hold:{active.id}"
                return None

        if self._pending is None or self._pending[0] != top_rule.id:
            self._pending = (top_rule.id, now)
            self.last_decision = f"pending:{top_rule.id}"
            return None
        hold = top_rule.hold if top_rule.hold is not None else self.cfg.hold
        if now - self._pending[1] < hold:
            return None

        # Adopt without a call when OBS is already showing the target scene.
        if self.current_scene is not None and self.current_scene == top_rule.scene:
            self.active_rule = top_rule
            self._pending = None
            self.last_decision = f"scene-already-live:{top_rule.scene}"
            return None

        # Commit the switch.
        self.active_rule = top_rule
        self._pending = None
        self._last_switch = now
        self.last_decision = f"switch:{top_rule.scene}"
        return top_rule, top_rule.scene

    # ---- signal collection ------------------------------------------------
    def _collect_signals(self) -> dict[str, tuple[float, float]]:
        now = self._clock()
        signals: dict[str, tuple[float, float]] = {}
        for cam in self.manager.list_cameras():
            stream = self.manager.get(cam.index)
            if stream is not None and stream.active:
                score = round(float(getattr(stream, "motion_score", 0.0)), 4)
                signals[f"motion:{cam.index}"] = (score, now)
        if self.audio is not None:
            for norm, (_name, db, seen_at) in self.audio.levels().items():
                signals[f"audio:{norm}"] = (round(db, 1), seen_at)
        return signals

    # ---- actuation --------------------------------------------------------
    def _actuate(self, rule: Rule, scene: str) -> None:
        if self.safety is not None:
            ok, reason = self.safety.guard_action("director:switch")
            if not ok:
                print(f"[director] switch to '{scene}' skipped — {reason}")
                self.last_decision = f"blocked:{reason}"
                return
        acted = False
        if self.obs is not None and not self.cfg.dry_run:
            if not self.obs.connected:
                self.obs_connected = self.obs.connect()
            if self.obs.connected:
                acted = self.obs.set_scene(scene)
                self.obs_connected = self.obs.connected
        if acted or self.cfg.dry_run:
            self.current_scene = scene
        entry = {
            "t": round(self._clock(), 2),
            "rule": rule.id,
            "camera": rule.cam_index,
            "scene": scene,
            "acted": acted,
            "mode": "obs" if acted else ("dry-run" if self.cfg.dry_run else "no-obs"),
        }
        self.log.append(entry)
        del self.log[:-20]  # keep last 20
        tag = "→ OBS" if acted else "(dry-run)" if self.cfg.dry_run else "(OBS not connected)"
        print(f"[director] {rule.id} → scene '{scene}' {tag}")
        if self.on_switch is not None:
            try:
                self.on_switch(rule.cam_index, scene, entry)
            except Exception:
                pass  # a broken listener must never stop the show

    def _loop(self) -> None:
        while self._running:
            # Probe (non-consuming) BEFORE the decision engine runs: while the
            # kill switch or rate limiter would block a cut, the whole engine
            # freezes instead of committing state for a cut that never
            # happens — releasing the guard resumes from consistent state.
            if self.safety is not None:
                ok, reason = self.safety.check_action("director:switch")
                if not ok:
                    self.last_decision = f"blocked:{reason}"
                    time.sleep(self.cfg.interval)
                    continue
            switch = self.update(self._clock(), self._collect_signals())
            if switch is not None:
                self._actuate(*switch)
            time.sleep(self.cfg.interval)

    def start(self) -> None:
        if self._running:
            return
        if self.obs is not None and not self.cfg.dry_run:
            self.obs_connected = self.obs.connect()
            if self.obs_connected:
                self.current_scene = self.obs.current_scene()
        if self.audio is not None:
            self.audio.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="director", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        # Close OBS first: close() waits out any in-flight cut on the client
        # lock and drops the socket, so the loop thread exits within one
        # interval instead of being abandoned by a timed-out join mid-cut.
        if self.obs is not None:
            self.obs.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self.audio is not None:
            self.audio.stop()

    def status(self) -> dict:
        motion = {
            key.partition(":")[2]: value
            for key, (value, _seen) in self.signals.items()
            if key.startswith("motion:")
        }
        audio_levels = []
        if self.audio is not None:
            now = self._clock()
            audio_levels = sorted(
                (
                    {"input": name, "db": round(db, 1), "fresh": now - seen <= _FRESH_WINDOW}
                    for name, db, seen in self.audio.levels().values()
                ),
                key=lambda e: -e["db"],
            )[:6]
        active = self.active_rule
        return {
            "active": active.cam_index if active else None,
            "active_rule": active.id if active else None,
            "last_decision": self.last_decision,
            "scores": motion,
            "audio_levels": audio_levels,
            "rules": len(self.cfg.rules) or None,
            "scene_map": self.cfg.scene_map,
            "dry_run": self.cfg.dry_run,
            "obs_connected": self.obs_connected,
            "audio_connected": bool(self.audio and self.audio.connected),
            "recent": self.log[-10:],
        }
