"""Recorded avatar moves: validation, storage, the play trigger, and the
.vrma writer (run through Node when it is installed)."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_stream import animations

ROOT = Path(__file__).resolve().parent.parent


def _frame(t: float, body: bool = False) -> dict:
    frame = {
        "t": t,
        "f": {
            "head": {"x": 0.1, "y": -0.2, "z": 0.05},
            "eye": {"l": 1, "r": 0.5},
            "mouth": {"shape": {"A": 0.7, "I": 0, "U": 0, "E": 0.2, "O": 0}},
            "brow": 0.3,
            "pupil": {"x": 0.1, "y": -0.1},
        },
        "p": None,
    }
    if body:
        frame["p"] = {"Hips": {"rotation": {"x": 0, "y": 0.1, "z": 0}}, "LeftUpperArm": {"x": 0, "y": 0, "z": 1.2}}
    return frame


def _clip(n: int = 30, body: bool = False, start: float = 0.0) -> dict:
    return {"name": "Wave", "frames": [_frame(start + i / 30, body) for i in range(n)]}


@pytest.fixture
def rig(tmp_path):
    events: list[tuple] = []
    animations.init(dir=tmp_path / "animations", emit=lambda e, p, retain=True: events.append((e, p, retain)))
    app = FastAPI()
    app.include_router(animations.router)
    with TestClient(app) as client:
        yield client, events, tmp_path / "animations"


def test_clean_frame_rebuilds_the_frame_and_drops_everything_else():
    raw = _frame(1.5, body=True)
    raw["f"]["head"]["x"] = "nan"
    raw["f"]["eye"]["l"] = 7
    raw["f"]["evil"] = "<script>"
    raw["extra"] = {"anything": 1}
    raw["p"]["Tail"] = {"x": 1}
    frame = animations.clean_frame(raw)
    assert frame["f"]["head"] == {"x": 0.0, "y": -0.2, "z": 0.05}  # NaN → 0
    assert frame["f"]["eye"]["l"] == 1  # clamped to 0..1
    assert set(frame) == {"t", "f", "p"} and "evil" not in frame["f"] and "Tail" not in frame["p"]
    assert frame["p"]["LeftUpperArm"]["z"] == 1.2
    assert frame["p"]["RightLowerLeg"] == {"x": 0.0, "y": 0.0, "z": 0.0}  # missing parts filled in
    assert animations.clean_frame({"t": 0}) is None and animations.clean_frame("nope") is None


def test_clean_clip_rebases_time_and_rejects_bad_recordings():
    clip = animations.clean_clip(_clip(30, start=4.0))
    assert clip["frames"][0]["t"] == 0 and clip["duration"] == pytest.approx(29 / 30, abs=1e-3)
    assert clip["body"] is False and animations.clean_clip(_clip(30, body=True))["body"] is True
    assert animations.clean_clip({"name": "\x00 ", "frames": _clip()["frames"]})["name"] == "Untitled move"
    with pytest.raises(ValueError, match="Too short"):
        animations.clean_clip(_clip(3))
    with pytest.raises(ValueError, match="Too long"):
        animations.clean_clip({"frames": [_frame(0)] * (animations.MAX_FRAMES + 1)})
    with pytest.raises(ValueError):
        animations.clean_clip({"frames": "all of them"})


def test_save_list_get_rename_delete(rig):
    client, events, folder = rig
    res = client.post("/api/avatar/animations", json=_clip(45, body=True))
    assert res.status_code == 200
    entry = res.json()["animation"]
    assert entry["name"] == "Wave" and entry["frames"] == 45 and entry["body"] is True
    assert (folder / f"{entry['id']}.json").exists()
    assert events[-1] == ("avatar_animations", {"changed": entry["id"]}, False)

    assert [a["id"] for a in client.get("/api/avatar/animations").json()["animations"]] == [entry["id"]]
    full = client.get(f"/api/avatar/animations/{entry['id']}").json()
    assert len(full["frames"]) == 45 and full["frames"][0]["t"] == 0

    assert client.patch(f"/api/avatar/animations/{entry['id']}", json={"name": "Big wave"}).json()["animation"]["name"] == "Big wave"
    assert client.get(f"/api/avatar/animations/{entry['id']}").json()["name"] == "Big wave"
    assert client.patch(f"/api/avatar/animations/{entry['id']}", json={"name": "  "}).status_code == 400

    assert client.delete(f"/api/avatar/animations/{entry['id']}").status_code == 200
    assert client.get("/api/avatar/animations").json()["animations"] == []
    assert client.get(f"/api/avatar/animations/{entry['id']}").status_code == 404
    assert client.delete(f"/api/avatar/animations/{entry['id']}").status_code == 404


def test_bad_uploads_are_refused_with_a_readable_reason(rig):
    client, _, _ = rig
    res = client.post("/api/avatar/animations", json=_clip(2))
    assert res.status_code == 400 and "Too short" in res.json()["detail"]
    assert client.post("/api/avatar/animations", content=b"not json").status_code == 400
    # ids are fixed-shape hex, so a path can't be smuggled through one
    assert client.get("/api/avatar/animations/..%2Findex").status_code == 404
    assert animations.load_clip("../../etc") is None and not animations.delete_clip("../index")


def test_play_is_a_one_shot_event_never_retained(rig):
    client, events, _ = rig
    clip_id = client.post("/api/avatar/animations", json=_clip()).json()["animation"]["id"]
    events.clear()
    assert client.post(f"/api/avatar/animations/{clip_id}/play", json={"loop": True}).status_code == 200
    event, payload, retain = events[-1]
    assert event == "avatar_play" and payload["id"] == clip_id and payload["loop"] is True and retain is False
    assert client.post("/api/avatar/animations/00000000/play").status_code == 404
    assert client.post("/api/avatar/animations/stop").status_code == 200
    assert events[-1][0] == "avatar_stop" and events[-1][2] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_vrma_writer_produces_a_valid_animation_file(tmp_path):
    module = tmp_path / "vrma.mjs"
    shutil.copy(ROOT / "video_stream" / "static" / "js" / "vrma.js", module)
    script = tmp_path / "run.mjs"
    script.write_text(
        """
import { buildVrma, HUMANOID_ORDER, nearestParent } from "./vrma.mjs";
import { writeFileSync } from "node:fs";
const have = new Set(["hips", "spine", "chest", "neck", "head", "leftUpperArm", "leftLowerArm"]);
const bones = HUMANOID_ORDER.filter((n) => have.has(n)).map((n) => ({
  name: n, parent: nearestParent(n, (x) => have.has(x)), translation: n === "hips" ? [0, 0.9, 0] : [0, 0.1, 0],
}));
const N = 31;
const times = Float32Array.from({ length: N }, (_, i) => i / 30);
const head = new Float32Array(N * 4);
for (let i = 0; i < N; i++) { const a = i / 60; head.set([Math.sin(a), 0, 0, Math.cos(a)], i * 4); }
const blink = Float32Array.from({ length: N }, (_, i) => (i === 15 ? 1.7 : 0));
writeFileSync(process.argv[2], Buffer.from(buildVrma({ bones, times, rotations: { head, tail: head }, expressions: { blink, wink: blink }, name: "nod" })));
let threw = false;
try { buildVrma({ bones, times: new Float32Array(0) }); } catch { threw = true; }
if (!threw) process.exit(3);
"""
    )
    out = tmp_path / "nod.vrma"
    subprocess.run(["node", str(script), str(out)], check=True, timeout=30)

    data = out.read_bytes()
    magic, version, total = struct.unpack("<4sII", data[:12])
    assert (magic, version, total) == (b"glTF", 2, len(data)) and total % 4 == 0
    json_len, json_type = struct.unpack("<I4s", data[12:20])
    assert json_type == b"JSON" and json_len % 4 == 0
    gltf = json.loads(data[20 : 20 + json_len])
    bin_len, bin_type = struct.unpack("<I4s", data[20 + json_len : 28 + json_len])
    binary = data[28 + json_len :]
    assert bin_type == b"BIN\x00" and bin_len == len(binary) == gltf["buffers"][0]["byteLength"]

    ext = gltf["extensions"]["VRMC_vrm_animation"]
    assert ext["specVersion"] == "1.0" and gltf["extensionsUsed"] == ["VRMC_vrm_animation"]
    bones = ext["humanoid"]["humanBones"]
    nodes = gltf["nodes"]
    assert nodes[bones["hips"]["node"]]["translation"] == [0, 0.9, 0]
    # chest has no upperChest above it here, so the neck and the arm hang off the chest
    chest = nodes[bones["chest"]["node"]]
    assert bones["neck"]["node"] in chest["children"] and bones["leftUpperArm"]["node"] in chest["children"]
    assert not any("rotation" in n for n in nodes)  # a rest pose with no rotations = retargetable
    assert ext["expressions"] == {
        "preset": {"blink": {"node": len(bones)}},
        "custom": {"wink": {"node": len(bones) + 1}},
    }

    anim = gltf["animations"][0]
    assert anim["name"] == "nod" and len(anim["channels"]) == 3  # head + 2 expressions; "tail" is not a bone
    targets = {(c["target"]["node"], c["target"]["path"]) for c in anim["channels"]}
    assert (bones["head"]["node"], "rotation") in targets

    def read(accessor_index):
        acc = gltf["accessors"][accessor_index]
        view = gltf["bufferViews"][acc["bufferView"]]
        width = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}[acc["type"]]
        assert acc["componentType"] == 5126 and view["byteOffset"] % 4 == 0
        return struct.unpack_from(f"<{acc['count'] * width}f", binary, view["byteOffset"]), acc

    times, time_acc = read(anim["samplers"][0]["input"])
    assert len(times) == 31 and time_acc["min"] == [0] and time_acc["max"] == [pytest.approx(1.0)]
    head_channel = next(c for c in anim["channels"] if c["target"]["path"] == "rotation")
    quats, _ = read(anim["samplers"][head_channel["sampler"]]["output"])
    assert len(quats) == 31 * 4
    assert all(abs(sum(v * v for v in quats[i : i + 4]) - 1) < 1e-4 for i in range(0, len(quats), 4))
    blink_channel = next(c for c in anim["channels"] if c["target"]["node"] == len(bones))
    weights, _ = read(anim["samplers"][blink_channel["sampler"]]["output"])
    assert weights[15 * 3] == 1.0 and max(weights) == 1.0  # 1.7 clamped; weight rides on translation.x
