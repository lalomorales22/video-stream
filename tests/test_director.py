"""Director decision engine + the safety guard on its actuator."""

from video_stream.director import Director, DirectorConfig
from video_stream.safety import SafetyManager


def make_director(**cfg):
    defaults = dict(
        scene_map={0: "Cam A", 1: "Cam B"},
        min_score=0.1,
        margin=0.01,
        hold=1.0,
        cooldown=3.0,
    )
    defaults.update(cfg)
    return Director(manager=None, config=DirectorConfig(**defaults))


def test_update_needs_hold_before_cutting():
    d = make_director()
    # Leader appears: becomes the candidate, no cut yet.
    assert d.update(0.0, {0: 0.5, 1: 0.0}) is None
    # Still inside the hold window.
    assert d.update(0.5, {0: 0.5, 1: 0.0}) is None
    # Hold satisfied: commit.
    assert d.update(1.0, {0: 0.5, 1: 0.0}) == (0, "Cam A")
    assert d.active == 0


def test_update_respects_cooldown_between_cuts():
    d = make_director()
    d.update(0.0, {0: 0.5, 1: 0.0})
    assert d.update(1.0, {0: 0.5, 1: 0.0}) == (0, "Cam A")

    # A new leader holds long enough, but the cooldown blocks the cut...
    d.update(1.2, {0: 0.0, 1: 0.5})
    assert d.update(2.5, {0: 0.0, 1: 0.5}) is None
    # ...until cooldown has passed since the last switch.
    assert d.update(4.1, {0: 0.0, 1: 0.5}) == (1, "Cam B")


def test_update_ignores_scores_below_min():
    d = make_director()
    assert d.update(0.0, {0: 0.05, 1: 0.02}) is None
    assert d.update(5.0, {0: 0.05, 1: 0.02}) is None
    assert d.active is None


class FakeOBS:
    def __init__(self):
        self.connected = True
        self.scenes = []
        self.last_error = None

    def connect(self):
        return True

    def set_scene(self, scene):
        self.scenes.append(scene)
        return True


def test_actuate_respects_kill_switch():
    obs = FakeOBS()
    safety = SafetyManager(max_actions=10, window=60.0)
    d = Director(
        manager=None,
        obs_client=obs,
        config=DirectorConfig(scene_map={0: "Cam A"}),
        safety=safety,
    )

    safety.set_kill_switch(True, "test")
    d._actuate(0, "Cam A")
    assert obs.scenes == []  # never reached OBS
    assert d.log == []       # and never logged as a real switch

    safety.set_kill_switch(False)
    d._actuate(0, "Cam A")
    assert obs.scenes == ["Cam A"]
    assert len(d.log) == 1


def test_actuate_reports_switch_to_listener():
    events = []
    d = Director(
        manager=None,
        obs_client=None,
        config=DirectorConfig(scene_map={0: "Cam A"}, dry_run=True),
        on_switch=lambda cam, scene, entry: events.append((cam, scene)),
    )
    d._actuate(0, "Cam A")
    assert events == [(0, "Cam A")]
