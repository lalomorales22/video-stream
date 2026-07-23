"""Settings registry + setup-wizard scene-matching heuristics."""

import pytest

from video_stream.settings import _MASK, Settings
from video_stream.setup_wizard import propose_scene_map


def make_settings(tmp_path):
    return Settings(path=tmp_path / "settings.json")


def test_save_load_round_trip(tmp_path):
    s = make_settings(tmp_path)
    s.save({"obs_host": " 10.0.0.5 ", "obs_port": "4460", "director_auto_punch": "true"})

    fresh = make_settings(tmp_path)
    loaded = fresh.load()
    assert loaded["obs_host"] == "10.0.0.5"  # stripped
    assert loaded["obs_port"] == 4460        # coerced to int
    assert loaded["director_auto_punch"] is True


def test_unknown_key_rejected(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(ValueError, match="unknown setting"):
        s.save({"rm_rf": "yes"})


def test_bad_type_rejected_with_field_name(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(ValueError, match="obs_port"):
        s.save({"obs_port": "not-a-port"})


def test_secrets_masked_in_public_and_mask_write_ignored(tmp_path):
    s = make_settings(tmp_path)
    s.save({"obs_password": "hunter2"})

    fields = {f["key"]: f for f in s.public()}
    assert fields["obs_password"]["value"] == _MASK  # never the real value

    # The UI posts the whole form back, mask included — secret must survive.
    s.save({"obs_password": _MASK, "obs_host": "1.2.3.4"})
    assert s.get("obs_password") == "hunter2"
    assert s.get("obs_host") == "1.2.3.4"


def test_empty_secret_shows_empty_not_mask(tmp_path):
    s = make_settings(tmp_path)
    fields = {f["key"]: f for f in s.public()}
    assert fields["auth_token"]["value"] == ""


def test_corrupt_file_never_kills_boot(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    s = Settings(path=path)
    assert s.load() == {}


def test_save_returns_only_the_delta(tmp_path):
    """The route applies save()'s return value to live config — it must be
    just this call's changes, never the whole store (regression: a one-field
    Save used to clobber every CLI-set value mid-show)."""
    s = make_settings(tmp_path)
    s.save({"obs_host": "10.0.0.5", "obs_port": 4460})
    delta = s.save({"director_hold": 2.5})
    assert delta == {"director_hold": 2.5}
    # But the store still holds everything, and it all persists.
    fresh = make_settings(tmp_path)
    assert fresh.load()["obs_host"] == "10.0.0.5"


def test_save_delta_excludes_echoed_mask(tmp_path):
    from video_stream.settings import _MASK

    s = make_settings(tmp_path)
    s.save({"obs_password": "hunter2"})
    delta = s.save({"obs_password": _MASK, "obs_host": "1.2.3.4"})
    assert "obs_password" not in delta  # mask echo must not re-apply the secret
    assert delta == {"obs_host": "1.2.3.4"}


def test_propose_scene_map_prefers_index_names():
    cameras = [
        {"index": 0, "name": "FaceTime HD", "active": True},
        {"index": 1, "name": "Logitech BRIO", "active": True},
    ]
    scenes = ["Intro", "Cam 1 Wide", "Camera 0", "BRB"]
    proposal = propose_scene_map(cameras, scenes)
    assert proposal == {0: "Camera 0", 1: "Cam 1 Wide"}


def test_propose_scene_map_matches_device_words_then_cammy_scenes():
    cameras = [
        {"index": 2, "name": "Logitech BRIO", "active": True},
        {"index": 5, "name": "USB Video", "active": True},
    ]
    scenes = ["Brio Close", "Main Cam", "Chat"]
    proposal = propose_scene_map(cameras, scenes)
    assert proposal[2] == "Brio Close"   # shared word wins
    assert proposal[5] == "Main Cam"     # leftover cam-ish scene
    # Each scene used at most once, nothing invented.
    assert len(set(proposal.values())) == len(proposal)
