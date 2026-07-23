"""Chaos engine: preset validation, guard/cooldown semantics, execution."""

import json
import time

import pytest

from video_stream import chaos
from video_stream.chaos import ChaosEngine, ChaosError, load_presets
from video_stream.safety import SafetyBlocked, SafetyManager


def write_preset(tmp_path, name, body):
    (tmp_path / f"{name}.json").write_text(json.dumps(body))


def test_load_presets_accepts_valid_and_counts_steps(tmp_path):
    write_preset(
        tmp_path,
        "good",
        {
            "name": "Good",
            "cooldown": 2,
            "steps": [
                {"do": "fx", "effect": "confetti"},
                {"do": "serial", "steps": [{"do": "sleep", "ms": 100}, {"do": "scene", "scene": "A"}]},
            ],
        },
    )
    presets, errors = load_presets(tmp_path)
    assert errors == []
    assert presets["good"]["step_count"] == 4  # containers count too
    assert presets["good"]["cooldown"] == 2.0


def test_load_presets_rejects_bad_steps_with_path_labels(tmp_path):
    write_preset(tmp_path, "bad", {"steps": [{"do": "teleport"}]})
    write_preset(tmp_path, "worse", {"steps": [{"do": "serial", "steps": [{"do": "sleep"}]}]})
    write_preset(tmp_path, "fine", {"steps": [{"do": "fx", "effect": "flash"}]})
    presets, errors = load_presets(tmp_path)
    assert list(presets) == ["fine"]  # bad files skipped, good ones survive
    assert any("bad.json:steps[0]" in e for e in errors)
    assert any("worse.json:steps[0].steps[0]" in e for e in errors)


def test_unknown_fx_effect_rejected(tmp_path):
    write_preset(tmp_path, "fx", {"steps": [{"do": "fx", "effect": "explode"}]})
    _, errors = load_presets(tmp_path)
    assert any("effect must be one of" in e for e in errors)


class FakeOBS:
    def __init__(self):
        self.calls = []
        self.connected = True

    def connect(self):
        return True

    def close(self):
        pass

    def set_scene(self, scene):
        self.calls.append(("scene", scene))
        return True

    def scene_item_id(self, scene, source):
        return 3

    def set_scene_item_enabled(self, scene, item_id, enabled):
        self.calls.append(("item", scene, item_id, enabled))
        return True

    def set_scene_item_transform(self, scene, item_id, transform):
        self.calls.append(("transform", scene, item_id, tuple(sorted(transform))))
        return True

    def set_source_filter_enabled(self, source, filter_name, enabled):
        self.calls.append(("filter", source, filter_name, enabled))
        return True

    def _request(self, request_type, data=None):
        self.calls.append(("raw", request_type))
        return {"requestStatus": {"result": True}, "responseData": {}}


def make_engine(tmp_path, safety=None):
    obs = FakeOBS()
    chaos.init(safety=safety, presets_dirs=[tmp_path], obs_factory=lambda: obs)
    engine = ChaosEngine()
    engine.reload()
    return engine, obs


def wait_done(engine, timeout=3.0):
    deadline = time.monotonic() + timeout
    while engine._running is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert engine._running is None, "preset did not finish"


def test_trigger_runs_steps_in_order(tmp_path):
    write_preset(
        tmp_path,
        "run",
        {
            "steps": [
                {"do": "scene", "scene": "Intro"},
                {"do": "item", "scene": "Intro", "source": "Logo", "enabled": False},
                {"do": "filter", "source": "Cam", "filter": "Blur", "enabled": True},
                {"do": "request", "type": "GetVersion"},
            ]
        },
    )
    engine, obs = make_engine(tmp_path)
    result = engine.trigger("run")
    assert result["status"] == "running"
    wait_done(engine)
    assert obs.calls == [
        ("scene", "Intro"),
        ("item", "Intro", 3, False),
        ("filter", "Cam", "Blur", True),
        ("raw", "GetVersion"),
    ]


def test_cooldown_and_unknown_preset(tmp_path):
    write_preset(tmp_path, "quick", {"cooldown": 60, "steps": [{"do": "sleep", "ms": 1}]})
    engine, _ = make_engine(tmp_path)
    engine.trigger("quick")
    wait_done(engine)
    with pytest.raises(ChaosError) as exc_info:
        engine.trigger("quick")
    assert exc_info.value.status_code == 429
    with pytest.raises(ChaosError) as exc_info:
        engine.trigger("nope")
    assert exc_info.value.status_code == 404


def test_kill_switch_blocks_chaos(tmp_path):
    write_preset(tmp_path, "guarded", {"steps": [{"do": "scene", "scene": "X"}]})
    safety = SafetyManager(max_actions=10, window=60.0)
    engine, obs = make_engine(tmp_path, safety=safety)
    safety.set_kill_switch(True, "test")
    with pytest.raises(SafetyBlocked):
        engine.trigger("guarded")
    assert obs.calls == []
    assert engine._run_lock.acquire(blocking=False)  # lock was never leaked
    engine._run_lock.release()


def test_busy_and_cooldown_rejections_never_consume_safety_budget(tmp_path):
    write_preset(tmp_path, "slow", {"cooldown": 0, "steps": [{"do": "sleep", "ms": 400}]})
    safety = SafetyManager(max_actions=5, window=60.0)
    engine, _ = make_engine(tmp_path, safety=safety)

    engine.trigger("slow")  # consumes exactly one budget slot
    with pytest.raises(ChaosError) as exc_info:
        engine.trigger("slow")  # busy → rejected BEFORE the guard
    assert exc_info.value.status_code == 409
    assert safety.status()["actions_in_window"] == 1
    wait_done(engine)


def test_explicit_zero_cooldown_sticks(tmp_path):
    write_preset(tmp_path, "rapid", {"cooldown": 0, "steps": [{"do": "sleep", "ms": 1}]})
    engine, _ = make_engine(tmp_path)
    assert engine.presets["rapid"]["cooldown"] == 0.0
    engine.trigger("rapid")
    wait_done(engine)
    engine.trigger("rapid")  # no cooldown → runs again immediately
    wait_done(engine)


def test_kill_switch_aborts_a_running_preset(tmp_path):
    write_preset(
        tmp_path,
        "long",
        {"cooldown": 0, "steps": [{"do": "sleep", "ms": 3000}, {"do": "scene", "scene": "Late"}]},
    )
    safety = SafetyManager(max_actions=10, window=60.0)
    engine, obs = make_engine(tmp_path, safety=safety)
    engine.trigger("long")
    time.sleep(0.1)
    safety.set_kill_switch(True, "abort mid-preset")
    wait_done(engine, timeout=2.0)  # chunked sleep notices the kill promptly
    assert ("scene", "Late") not in obs.calls  # the late step never ran
    safety.set_kill_switch(False)


def test_user_presets_dir_overrides_shipped(tmp_path):
    shipped = tmp_path / "shipped"
    user = tmp_path / "user"
    shipped.mkdir()
    user.mkdir()
    write_preset(shipped, "intro", {"name": "Shipped", "steps": [{"do": "sleep", "ms": 1}]})
    write_preset(user, "intro", {"name": "Mine", "steps": [{"do": "sleep", "ms": 1}]})
    presets, _ = load_presets(shipped, user)
    assert presets["intro"]["name"] == "Mine"
