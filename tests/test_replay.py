"""ReplayDirector pipeline against a fake OBS client."""

import time

import pytest

from video_stream.replay import ReplayConfig, ReplayDirector, ReplayError


class FakeOBS:
    def __init__(self):
        self.connected = True
        self.buffer_active = True
        self.saved = 0
        self.inputs = {}
        self.record = False
        self.last_error = None

    def connect(self):
        self.connected = True
        return True

    def replay_buffer_active(self):
        return self.buffer_active

    def start_replay_buffer(self):
        self.buffer_active = True
        return True

    def save_replay_buffer(self):
        self.saved += 1
        return True

    def last_replay_path(self):
        return "/tmp/Replay 2026.mkv"

    def set_input_settings(self, name, settings, overlay=True):
        self.inputs[name] = settings
        return True

    def trigger_media_restart(self, name):
        return True

    def record_active(self):
        return self.record

    def create_record_chapter(self, name):
        return True

    def scene_item_id(self, scene, source):
        return 7

    def set_scene_item_enabled(self, scene, item_id, enabled):
        return True

    def close(self):
        pass


def make_replay(**cfg):
    obs = FakeOBS()
    defaults = dict(capture_wait=0.0)
    defaults.update(cfg)
    return ReplayDirector(obs, ReplayConfig(**defaults)), obs


def test_capture_saves_and_reports():
    r, obs = make_replay()
    result = r.capture("Big moment")
    assert obs.saved == 1
    assert result["label"] == "Big moment"
    assert result["path"] == "/tmp/Replay 2026.mkv"
    assert r.status()["last_label"] == "Big moment"


def test_capture_409_when_buffer_off_and_no_autostart():
    r, obs = make_replay(auto_start_buffer=False)
    obs.buffer_active = False
    with pytest.raises(ReplayError) as exc_info:
        r.capture()
    assert exc_info.value.status_code == 409
    assert obs.saved == 0


def test_capture_autostarts_the_buffer():
    r, obs = make_replay()
    obs.buffer_active = False
    r.capture()
    assert obs.buffer_active is True
    assert obs.saved == 1


def test_unqueryable_buffer_gets_the_setup_hint_not_a_502():
    r, obs = make_replay()
    obs.buffer_active = None  # OBS answered but errored: no buffer configured
    with pytest.raises(ReplayError) as exc_info:
        r.capture()
    assert exc_info.value.status_code == 409
    assert "Replay Buffer" in str(exc_info.value)


def test_label_tokens_are_shown_verbatim_never_substituted():
    r, obs = make_replay(
        lower_third_input="LT", lower_third_template="{label} · {time}"
    )
    r.capture("literal {time} label")
    text = obs.inputs["LT"]["text"]
    assert "literal {time} label" in text


def test_set_auto_off_on_never_leaves_two_watchers():
    r, _ = make_replay()
    r.set_auto(True)
    first = r._auto_thread
    r.set_auto(False)
    r.set_auto(True)  # faster than the old thread's 0.25s tick
    second = r._auto_thread
    assert first is not second
    time.sleep(0.6)
    assert not first.is_alive()  # superseded thread exits on its next tick
    assert second.is_alive()
    r.set_auto(False)
