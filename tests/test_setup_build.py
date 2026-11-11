"""“Set up OBS for me”: the plan is add-only and safe to run twice."""

from __future__ import annotations

from video_stream import setup_wizard as sw

CAMS = [{"index": 0, "name": "FaceTime HD", "active": True}, {"index": 100, "name": "Galaxy", "active": True}]
BASE = "http://127.0.0.1:8765"


class FakeOBS:
    """Just enough of OBS to apply a plan against: scenes, inputs, items."""

    def __init__(self, scenes=(), inputs=()):
        self.scenes = {s: [] for s in scenes}
        self.inputs = list(inputs)
        self.last_error = None
        self.calls: list[tuple] = []

    def connect(self): return True
    def close(self): pass
    def scene_list(self): return list(self.scenes)
    def input_list(self): return list(self.inputs)
    def video_size(self): return (1920, 1080)
    def scene_item_id(self, scene, source):
        items = self.scenes.get(scene, [])
        return items.index(source) + 1 if source in items else None

    def create_scene(self, scene):
        self.calls.append(("scene", scene))
        if scene in self.scenes:
            return False
        self.scenes[scene] = []
        return True

    def create_browser_input(self, scene, name, url, width, height):
        self.calls.append(("browser", scene, name, url, width, height))
        if name in self.inputs or scene not in self.scenes:
            return False
        self.inputs.append(name)
        self.scenes[scene].append(name)
        return True

    def create_media_input(self, scene, name, url):
        self.calls.append(("media", scene, name, url))
        if name in self.inputs or scene not in self.scenes:
            return False
        self.inputs.append(name)
        self.scenes[scene].append(name)
        return True

    def set_scene_item_index(self, scene, item_id, index):
        self.calls.append(("index", scene, item_id, index))
        return True

    def set_scene_item_transform(self, scene, item_id, transform):
        self.calls.append(("fit", scene, item_id, transform["boundsType"], transform["boundsWidth"], transform["boundsHeight"]))
        return True

    def set_scene_item_enabled(self, scene, item_id, enabled):
        self.calls.append(("enabled", scene, self.scenes[scene][item_id - 1], enabled))
        return True

    def add_to_scene(self, scene, source):
        self.calls.append(("place", scene, source))
        if scene not in self.scenes:
            return False
        self.scenes[scene].append(source)
        return True


def _plan(obs, scene_map=None, overlays=True, cameras=CAMS):
    return sw.plan_obs_build(
        cameras, obs.scene_list(), obs.input_list(), scene_map or {},
        lambda scene, source: obs.scene_item_id(scene, source) is not None, BASE, overlays=overlays,
    )


def test_fresh_obs_gets_a_scene_and_source_per_camera_plus_overlays():
    steps, scene_map = _plan(FakeOBS())
    assert scene_map == {0: "Cam 0", 100: "Cam 100"}
    assert all(s["new"] for s in steps)
    cam0 = [s for s in steps if s.get("scene") == "Cam 0"]
    assert [s["do"] for s in cam0] == ["scene", "media", "place"]
    assert cam0[1]["url"] == f"{BASE}/stream/0"  # a Media Source of the raw stream
    assert cam0[2]["name"] == sw.OVERLAY_SCENE
    overlay_urls = [s["url"] for s in steps if s.get("scene") == sw.OVERLAY_SCENE and s["do"] == "browser"]
    assert overlay_urls == [f"{BASE}/overlay/{slug}" for _, slug in sw.OVERLAYS]


def test_without_overlays_only_camera_scenes_are_planned():
    steps, _ = _plan(FakeOBS(), overlays=False)
    assert [s["do"] for s in steps] == ["scene", "media", "scene", "media"]


def test_a_camera_already_mapped_to_your_own_scene_is_left_alone():
    obs = FakeOBS(scenes=["My Desk Shot"])
    steps, scene_map = _plan(obs, scene_map={0: "My Desk Shot"}, overlays=False)
    assert scene_map == {0: "My Desk Shot", 100: "Cam 100"}
    keep = [s for s in steps if s["do"] == "keep"]
    assert len(keep) == 1 and keep[0]["scene"] == "My Desk Shot" and not keep[0]["new"]
    assert not any(s.get("scene") == "My Desk Shot" and s["new"] for s in steps)


def test_a_stale_mapping_to_a_deleted_scene_is_rebuilt():
    steps, scene_map = _plan(FakeOBS(), scene_map={0: "Gone"}, overlays=False, cameras=CAMS[:1])
    assert scene_map == {0: "Cam 0"}
    assert [s["do"] for s in steps] == ["scene", "media"]


def test_a_scene_from_the_browser_source_era_gets_a_media_feed_and_the_old_one_switched_off(monkeypatch):
    old = sw.legacy_camera_input_name(0)
    obs = FakeOBS(scenes=["Cam 0"], inputs=[old])
    obs.scenes["Cam 0"] = [old]
    _wire(monkeypatch, obs, {"port": 8765, "obs_host": "127.0.0.1", "obs_scene_map": {0: "Cam 0", 100: "Cam 100"}})
    result = sw._obs_build(apply=True, overlays=False)
    assert result["failed"] == 0
    assert obs.scenes["Cam 0"] == [old, "Cam 0 feed (video-stream)"]
    assert ("index", "Cam 0", 2, 0) in obs.calls  # the feed goes to the bottom of the stack
    assert ("fit", "Cam 0", 2, "OBS_BOUNDS_SCALE_INNER", 1920, 1080) in obs.calls
    assert ("enabled", "Cam 0", old, False) in obs.calls
    calls = len(obs.calls)
    assert sw._obs_build(apply=True, overlays=False)["to_do"] == 0 and len(obs.calls) == calls


def _wire(monkeypatch, obs, config):
    class Manager:
        def list_cameras(self):
            return [type("C", (), c)() for c in CAMS]

    monkeypatch.setitem(sw._ctx, "obs_factory", lambda: obs)
    monkeypatch.setitem(sw._ctx, "manager", Manager())
    monkeypatch.setitem(sw._ctx, "config", config)


def test_applying_twice_adds_everything_once(monkeypatch):
    obs = FakeOBS(scenes=["Scene"])
    config = {"port": 8765, "obs_host": "127.0.0.1", "obs_scene_map": {}}
    _wire(monkeypatch, obs, config)

    preview = sw._obs_build(apply=False, overlays=True)
    assert preview["reachable"] and not preview["applied"] and preview["to_do"] == len(preview["steps"])
    assert obs.calls == []  # a preview never touches OBS

    first = sw._obs_build(apply=True, overlays=True)
    assert first["applied"] and first["failed"] == 0
    assert first["scene_map_string"] == "0=Cam 0,100=Cam 100"
    assert obs.scenes["Cam 0"] == ["Cam 0 feed (video-stream)", sw.OVERLAY_SCENE]
    assert obs.scenes["Scene"] == []  # the user's own scene is untouched
    assert ("media", "Cam 100", "Cam 100 feed (video-stream)", f"{BASE}/stream/100") in obs.calls

    calls_before = len(obs.calls)
    config["obs_scene_map"] = {0: "Cam 0", 100: "Cam 100"}
    second = sw._obs_build(apply=True, overlays=True)
    assert second["to_do"] == 0 and len(obs.calls) == calls_before


def test_unreachable_obs_reports_instead_of_planning(monkeypatch):
    obs = FakeOBS()
    obs.connect = lambda: False
    obs.last_error = "refused"
    _wire(monkeypatch, obs, {"port": 8765, "obs_host": "127.0.0.1", "obs_scene_map": {}})
    assert sw._obs_build(apply=True, overlays=True) == {"reachable": False, "error": "refused", "steps": []}


def test_obs_on_another_machine_gets_the_lan_address(monkeypatch):
    monkeypatch.setitem(sw._ctx, "config", {"port": 9000, "obs_host": "127.0.0.1"})
    assert sw._obs_base_url() == "http://127.0.0.1:9000"
    monkeypatch.setitem(sw._ctx, "config", {"port": 9000, "obs_host": "192.168.1.50"})
    assert sw._obs_base_url().startswith("http://") and "127.0.0.1" not in sw._obs_base_url()
