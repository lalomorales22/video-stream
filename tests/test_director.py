"""Hybrid director engine: rules, damping pipeline, and the safety guard.

Includes the ported february11 auto-director test cases (adapted: seconds not
ms, mul→dB conversion happens in obs.AudioMeterListener, so the engine is fed
dB directly).
"""

from video_stream.director import (
    Director,
    DirectorConfig,
    Rule,
    parse_rules,
)
from video_stream.obs import mul_to_db, peak_db
from video_stream.safety import SafetyManager


def sig(now, **values):
    """Build a fresh signal map: sig(1.0, m0=0.5, aMic=-15.0). Audio keys are
    lowercased exactly like obs.AudioMeterListener normalizes input names."""
    out = {}
    for key, value in values.items():
        if key.startswith("m"):
            out[f"motion:{key[1:]}"] = (value, now)
        else:
            out[f"audio:{key[1:].lower()}"] = (value, now)
    return out


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


# ---- legacy motion mode (rules synthesized from the scene map) -----------

def test_motion_needs_pending_then_hold_before_cutting():
    d = make_director()
    assert d.update(0.0, sig(0.0, m0=0.5, m1=0.0)) is None  # pending registered
    assert "pending" in d.last_decision
    assert d.update(0.5, sig(0.5, m0=0.5, m1=0.0)) is None  # inside hold
    switch = d.update(1.0, sig(1.0, m0=0.5, m1=0.0))        # hold satisfied
    assert switch is not None
    rule, scene = switch
    assert scene == "Cam A"
    assert rule.cam_index == 0
    assert d.last_decision == "switch:Cam A"


def test_cooldown_freezes_the_pipeline_then_challenger_restarts():
    d = make_director()
    d.update(0.0, sig(0.0, m0=0.5, m1=0.0))
    assert d.update(1.0, sig(1.0, m0=0.5, m1=0.0)) is not None  # cut at t=1

    # Inside cooldown NOTHING moves — not even pending accrual (feb11 order).
    assert d.update(1.2, sig(1.2, m0=0.0, m1=0.5)) is None
    assert d.update(2.5, sig(2.5, m0=0.0, m1=0.5)) is None
    # Cooldown over: the challenger registers pending...
    assert d.update(4.1, sig(4.1, m0=0.0, m1=0.5)) is None
    assert "pending" in d.last_decision
    # ...and cuts after its hold.
    switch = d.update(5.2, sig(5.2, m0=0.0, m1=0.5))
    assert switch is not None
    assert switch[1] == "Cam B"


def test_scores_below_threshold_yield_no_candidate():
    d = make_director()
    assert d.update(0.0, sig(0.0, m0=0.05, m1=0.02)) is None
    assert d.last_decision == "no-candidate"
    assert d.active_rule is None


# ---- audio rules (ported feb11 cases) ------------------------------------

MIC_CAM = Rule(source="audio:Mic", scene="Camera", threshold=-45.0, priority=100, hold=0.0, id="mic-cam")


def test_audio_rule_switches_after_hold_confirmation():
    # feb11 test 1: hold=0 still needs two passes (pending, then confirm).
    d = make_director(rules=[MIC_CAM], cooldown=0.25, hold=0.0)
    assert d.update(0.0, sig(0.0, aMic=-15.0)) is None
    assert d.last_decision == "pending:mic-cam"
    switch = d.update(0.05, sig(0.05, aMic=-15.0))
    assert switch is not None
    assert switch[1] == "Camera"
    assert d.active_rule.id == "mic-cam"


def test_audio_rule_key_is_case_insensitive():
    d = make_director(rules=[MIC_CAM], cooldown=0.25, hold=0.0)
    # The meters listener normalizes names to lowercase; "audio:mic" matches "Mic".
    assert MIC_CAM.key == "audio:mic"
    d.update(0.0, {"audio:mic": (-15.0, 0.0)})
    assert d.last_decision == "pending:mic-cam"


def test_hysteresis_holds_until_challenger_is_3db_louder():
    quiet = Rule(source="audio:Desk", scene="Desk", threshold=-45.0, priority=100, hold=0.0, id="desk")
    d = make_director(rules=[MIC_CAM, quiet], cooldown=0.1, hold=0.0)
    d.update(0.0, sig(0.0, aMic=-15.0))
    assert d.update(0.05, sig(0.05, aMic=-15.0)) is not None  # mic-cam active

    # Desk at -14: louder, but not by the 3 dB hysteresis — hold the shot.
    d.update(0.3, sig(0.3, aMic=-15.0, aDesk=-14.0))
    assert d.last_decision == "hysteresis-hold:mic-cam"
    # Desk at -11: clears active (-15) + 3 dB → pending begins.
    d.update(0.4, sig(0.4, aMic=-15.0, aDesk=-11.0))
    assert d.last_decision == "pending:desk"


def test_stale_active_signal_cannot_defend_via_hysteresis():
    """A muted mic keeps its last loud level frozen in the meters map; once
    stale it must not block challengers forever (regression: review Tier 2)."""
    quiet = Rule(source="audio:Desk", scene="Desk", threshold=-45.0, priority=100, hold=0.0, id="desk")
    d = make_director(rules=[MIC_CAM, quiet], cooldown=0.1, hold=0.0)
    d.update(0.0, sig(0.0, aMic=-10.0))
    assert d.update(0.05, sig(0.05, aMic=-10.0)) is not None  # mic-cam active

    # Mic goes mute: its -10 dB sample goes stale, Desk speaks at a modest
    # -30 dB (which would LOSE to -10+3 hysteresis if staleness were ignored).
    signals = {"audio:mic": (-10.0, 0.05), "audio:desk": (-30.0, 5.0)}
    d.update(5.0, signals)
    assert d.last_decision == "pending:desk"  # stale active cannot hold the shot


def test_priority_beats_loudness():
    boss = Rule(source="audio:Host", scene="Host", threshold=-45.0, priority=200, hold=0.0, id="host")
    d = make_director(rules=[MIC_CAM, boss], cooldown=0.1, hold=0.0)
    # Mic is much louder, but Host has higher priority.
    d.update(0.0, sig(0.0, aMic=-5.0, aHost=-30.0))
    assert d.last_decision == "pending:host"


def test_stale_audio_signal_is_not_a_candidate():
    d = make_director(rules=[MIC_CAM], cooldown=0.1, hold=0.0)
    d.update(5.0, {"audio:mic": (-15.0, 1.0)})  # sampled 4s ago — stale
    assert d.last_decision == "no-candidate"


def test_scene_already_live_adopts_without_switch():
    d = make_director(rules=[MIC_CAM], cooldown=0.25, hold=0.0)
    d.current_scene = "Camera"
    d.update(0.0, sig(0.0, aMic=-15.0))
    assert d.update(0.05, sig(0.05, aMic=-15.0)) is None  # no cut returned
    assert d.last_decision == "scene-already-live:Camera"
    assert d.active_rule.id == "mic-cam"


def test_cross_kind_steal_skips_hysteresis_but_still_holds():
    cam_rule = Rule(source="motion:0", scene="Cam A", threshold=0.1, priority=50, hold=0.0, id="m0")
    d = make_director(rules=[MIC_CAM, cam_rule], cooldown=0.1, hold=0.0)
    d.update(0.0, sig(0.0, aMic=-15.0))
    assert d.update(0.05, sig(0.05, aMic=-15.0)) is not None  # audio active

    # Motion challenger: different kind → no hysteresis comparison, but the
    # pending/hold machine still applies (audio has higher priority here, so
    # drop the mic signal to let motion lead).
    d.update(0.3, sig(0.3, m0=0.9))
    assert d.last_decision == "pending:m0"


# ---- rules parsing --------------------------------------------------------

def test_parse_rules_clamps_and_skips_bad_entries():
    rules, overrides = parse_rules(
        {
            "cooldown": 1.6,
            "hysteresis_db": 99,       # clamped to 24
            "rules": [
                {"source": "audio:Mic/Aux", "scene": "Camera", "threshold": -55, "priority": 5000},
                {"source": "motion:1", "scene": "Cam B"},          # defaults fill in
                {"source": "teleport:x", "scene": "Nope"},         # bad kind — skipped
                {"source": "audio:Mic", "scene": ""},              # no scene — skipped
                "not-an-object",                                    # skipped
            ],
        }
    )
    assert overrides["cooldown"] == 1.6
    assert overrides["hysteresis_db"] == 24.0
    assert len(rules) == 2
    assert rules[0].priority == 1000.0          # clamped
    assert rules[0].key == "audio:mic/aux"
    assert rules[1].threshold == 0.02           # motion default
    assert rules[1].id                          # generated, non-empty


# ---- mul→dB conversion (the bug feb11 shipped) ----------------------------

def test_mul_to_db_and_peak_pick():
    assert mul_to_db(1.0) == 0.0
    assert abs(mul_to_db(0.178) - (-15.0)) < 0.1
    assert mul_to_db(0.0) is None               # silence, not -inf
    assert mul_to_db("nope") is None
    # Per-channel [magnitude, peak, inputPeak]: use index 1, loudest channel.
    assert abs(peak_db([[0.02, 0.1, 0.9], [0.02, 0.5, 0.9]]) - mul_to_db(0.5)) < 1e-9
    assert peak_db([]) is None


# ---- actuation guards -----------------------------------------------------

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
    d = Director(manager=None, obs_client=obs, config=DirectorConfig(), safety=safety)
    rule = Rule(source="motion:0", scene="Cam A", threshold=0.1, id="m0")

    safety.set_kill_switch(True, "test")
    d._actuate(rule, "Cam A")
    assert obs.scenes == []
    assert d.log == []
    assert "blocked" in d.last_decision

    safety.set_kill_switch(False)
    d._actuate(rule, "Cam A")
    assert obs.scenes == ["Cam A"]
    assert len(d.log) == 1
    assert d.current_scene == "Cam A"


def test_actuate_reports_switch_to_listener():
    events = []
    d = Director(
        manager=None,
        obs_client=None,
        config=DirectorConfig(dry_run=True),
        on_switch=lambda cam, scene, entry: events.append((cam, scene)),
    )
    d._actuate(Rule(source="motion:0", scene="Cam A", threshold=0.1, id="m0"), "Cam A")
    assert events == [(0, "Cam A")]
    audio_rule = Rule(source="audio:Mic", scene="Camera", threshold=-45, id="a0")
    d._actuate(audio_rule, "Camera")
    assert events[-1] == (None, "Camera")  # audio rules carry no camera index
