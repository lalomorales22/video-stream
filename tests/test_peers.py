"""Rig Link: peer parsing and remote motion signals in the director."""

from video_stream.director import Director, DirectorConfig, Rule
from video_stream.peers import parse_peers


def test_parse_peers_names_schemes_and_duplicates():
    peers = parse_peers("studio=192.168.1.42:8765, https://den.local:9000, ,")
    assert peers[0] == ("studio", "http://192.168.1.42:8765")
    assert peers[1] == ("den.local", "https://den.local:9000")
    assert len(peers) == 2

    # Bare IPs get named after the host; duplicate names get suffixed.
    peers = parse_peers("10.0.0.9:8765,10.0.0.9:8766")
    assert peers[0][0] == "10.0.0.9"
    assert peers[1][0] == "10.0.0.92"
    assert peers[0][1] != peers[1][1]


def test_parse_peers_sanitizes_names():
    peers = parse_peers("my rig!=192.168.1.5:8765")
    assert peers[0][0] == "my-rig-"
    assert peers == parse_peers("my rig!=http://192.168.1.5:8765/")


def test_parse_peers_lowercases_names_to_match_rule_keys():
    """Rule.key lowercases sources; peer names must match (regression)."""
    peers = parse_peers("Studio=192.168.1.42:8765")
    assert peers[0][0] == "studio"
    rule = Rule(source="motion:Studio:1", scene="Wide", threshold=0.05, id="r")
    assert rule.key == f"motion:{peers[0][0]}:1"


def test_legacy_scene_map_mode_survives_peer_signals():
    """Peer signals in scene-map mode must be ignored, not crash the loop
    (regression: critical review finding — int('studio:1') ValueError)."""
    d = Director(manager=None, config=DirectorConfig(scene_map={0: "Cam A"}))
    signals = {"motion:0": (0.5, 0.0), "motion:studio:1": (0.9, 0.0)}
    assert d.update(0.0, signals) is None  # pending for local cam, no crash
    assert "pending" in d.last_decision
    switch = d.update(1.5, {"motion:0": (0.5, 1.5), "motion:studio:1": (0.9, 1.5)})
    assert switch is not None
    assert switch[1] == "Cam A"  # the local rule won; peer signal ignored


class FakePeers:
    def __init__(self, signals):
        self._signals = signals

    def signals(self):
        return dict(self._signals)

    def status(self):
        return [{"name": "studio", "ok": True}]


def test_remote_motion_rule_cuts_on_peer_signal():
    rule = Rule(source="motion:studio:1", scene="Studio Wide", threshold=0.05, hold=0.0, id="remote")
    d = Director(
        manager=None,
        config=DirectorConfig(rules=[rule], cooldown=0.1, hold=0.0),
        peers=FakePeers({"motion:studio:1": (0.4, 0.0)}),
    )
    assert rule.key == "motion:studio:1"
    assert rule.cam_index is None  # remote rules never drive the local punch-in

    assert d.update(0.0, {"motion:studio:1": (0.4, 0.0)}) is None  # pending
    switch = d.update(0.05, {"motion:studio:1": (0.4, 0.05)})
    assert switch is not None
    assert switch[1] == "Studio Wide"
    assert d.status()["peers"] == [{"name": "studio", "ok": True}]


def test_stale_peer_signals_drop_out_of_candidacy():
    rule = Rule(source="motion:studio:1", scene="Studio Wide", threshold=0.05, hold=0.0, id="remote")
    d = Director(manager=None, config=DirectorConfig(rules=[rule], cooldown=0.1, hold=0.0))
    # Peer died 5s ago: its last signal is stale, so no candidate.
    assert d.update(5.0, {"motion:studio:1": (0.9, 0.0)}) is None
    assert d.last_decision == "no-candidate"
