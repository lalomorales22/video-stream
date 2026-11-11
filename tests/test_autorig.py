"""Auto-rig: upload validation, job lifecycle, Blender discovery, recipes.

Everything here runs without Blender — the fake runner stands in for it. The
parts that genuinely need Blender (the geometry fitting, the VRM export) are
verified by rigging real models, not by mocks that would only prove the mock
works.
"""

import json

import pytest
from fastapi.testclient import TestClient

from video_stream import autorig
from video_stream.app import app

client = TestClient(app)

GLB = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 64  # header magic + filler


@pytest.fixture
def rig_dir(tmp_path, monkeypatch):
    """Point the gallery at a temp dir so tests never touch a real install."""
    monkeypatch.setitem(autorig._ctx, "gallery_dir", tmp_path)
    monkeypatch.setitem(autorig._ctx, "vrm_dir", tmp_path / "vrm")
    return tmp_path / "rig"


@pytest.fixture
def available(monkeypatch):
    """Pretend Blender is installed and working."""
    monkeypatch.setattr(
        autorig,
        "probe",
        lambda refresh=False: {
            "blender": "/fake/blender",
            "version": "5.1.2",
            "vrm_addon": True,
            "vrm_addon_version": "4.4.0",
            "available": True,
            "error": "",
        },
    )


# ── recipes ───────────────────────────────────────────────────────────
def test_archetypes_load_with_everything_the_fitter_reads():
    kinds = autorig.load_archetypes()
    assert {k["id"] for k in kinds} >= {"humanoid", "bird_biped", "quadruped", "bust"}
    raw = json.loads(autorig.ARCHETYPES_FILE.read_text())["archetypes"]
    for name, spec in raw.items():
        for section in ("spine", "arms", "legs", "face"):
            assert section in spec, f"{name} is missing {section}"
        assert set(spec["spine"]) == {"hips", "spine", "chest", "neck", "head_top"}
        assert spec["mode"] in ("upright", "horizontal", "static")
        assert spec["face"]["mouth_kind"] in ("lips", "muzzle", "beak")
        assert spec["height_m"] > 0


def test_upright_recipes_stack_hips_to_head_in_order():
    """A recipe whose neck sits below its hips would fold the skeleton."""
    raw = json.loads(autorig.ARCHETYPES_FILE.read_text())["archetypes"]
    for name, spec in raw.items():
        if spec["mode"] != "upright":
            continue
        s = spec["spine"]
        order = [s["hips"], s["spine"], s["chest"], s["neck"], s["head_top"]]
        assert order == sorted(order), f"{name} spine is out of order: {order}"
        assert spec["legs"]["ankle_z"] < s["hips"], f"{name} has ankles above its hips"


# ── Blender discovery ─────────────────────────────────────────────────
def test_override_wins_and_a_bad_override_is_not_silently_ignored(tmp_path, monkeypatch):
    fake = tmp_path / "blender"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("VIDEO_STREAM_BLENDER", str(fake))
    assert autorig.find_blender() == str(fake)
    # A pointed-at binary that isn't there means "broken config", not "go look
    # elsewhere" — falling back would hide the typo.
    monkeypatch.setenv("VIDEO_STREAM_BLENDER", str(tmp_path / "nope"))
    assert autorig.find_blender() is None


def test_status_reports_unavailable_without_blender(monkeypatch):
    monkeypatch.setattr(autorig, "find_blender", lambda: None)
    monkeypatch.setattr(autorig, "_probe_cache", None)
    body = autorig.runner.status()
    assert body["available"] is False
    assert "Blender" in body["error"] or "blender" in body["error"]
    assert body["archetypes"], "the UI still needs the list of kinds to show"


# ── uploads ───────────────────────────────────────────────────────────
def test_upload_rejects_wrong_extension(rig_dir):
    r = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "model.png"})
    assert r.status_code == 400
    assert ".glb" in r.json()["detail"]


def test_upload_rejects_a_glb_that_isnt_one(rig_dir):
    r = client.post("/api/rig/sources", content=b"<html>nope</html>", headers={"x-file-name": "a.glb"})
    assert r.status_code == 400


def test_upload_rejects_empty_body(rig_dir):
    assert client.post("/api/rig/sources", content=b"", headers={"x-file-name": "a.glb"}).status_code == 400


def test_upload_stores_the_model_and_returns_an_id(rig_dir):
    r = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "penguin.glb"})
    assert r.status_code == 200
    meta = r.json()
    assert (rig_dir / meta["id"] / "source.glb").read_bytes() == GLB
    assert meta["name"] == "penguin.glb"
    assert client.get("/api/rig/sources").json()["sources"][0]["id"] == meta["id"]


def test_upload_accepts_a_name_in_any_alphabet(rig_dir):
    """Header values are Latin-1, so the browser percent-encodes the name."""
    r = client.post(
        "/api/rig/sources", content=GLB, headers={"x-file-name": "%E3%83%9A%E3%83%B3%E3%82%AE%E3%83%B3.glb"}
    )
    assert r.status_code == 200
    assert r.json()["name"] == "ペンギン.glb"
    assert (rig_dir / r.json()["id"] / "source.glb").exists()  # stored name never comes from theirs


# ── refusing models that read the host's disk ─────────────────────────
def test_gltf_pointing_at_a_host_file_is_refused():
    """Blender resolves a .gltf's URIs against the local filesystem and packs
    whatever it finds, so an external reference is a file-read primitive."""
    for uri in ("/etc/passwd", "../../../../etc/passwd", "secret.txt", "http://evil/x.png"):
        doc = json.dumps({"asset": {"version": "2.0"}, "images": [{"uri": uri}]}).encode()
        assert autorig.reject_external_references(doc), f"{uri} should have been refused"
    buffers = json.dumps({"asset": {"version": "2.0"}, "buffers": [{"uri": "/etc/hosts"}]}).encode()
    assert autorig.reject_external_references(buffers)


def test_self_contained_gltf_is_accepted():
    doc = json.dumps(
        {
            "asset": {"version": "2.0"},
            "buffers": [{"uri": "data:application/octet-stream;base64,AAAA"}],
            "images": [{"uri": "data:image/png;base64,AAAA"}],
            "meshes": [{"primitives": []}],  # no uri at all is fine too
        }
    ).encode()
    assert autorig.reject_external_references(doc) == ""


def test_unparseable_gltf_is_refused_not_crashed():
    assert autorig.reject_external_references(b"\x00\x01 not json")
    assert autorig.reject_external_references(b"[1,2,3]")


def test_upload_refuses_a_gltf_that_reaches_off_the_box(rig_dir):
    evil = json.dumps({"asset": {"version": "2.0"}, "images": [{"uri": "/etc/passwd"}]}).encode()
    r = client.post("/api/rig/sources", content=evil, headers={"x-file-name": "evil.gltf"})
    assert r.status_code == 400
    assert "separate file" in r.json()["detail"]
    assert not any(rig_dir.iterdir()) if rig_dir.exists() else True  # nothing left behind


def test_oversized_upload_is_refused_by_its_declared_length(rig_dir, monkeypatch):
    monkeypatch.setattr(autorig, "MAX_SOURCE_BYTES", 100)
    r = client.post("/api/rig/sources", content=GLB * 10, headers={"x-file-name": "big.glb"})
    assert r.status_code == 413


def test_old_uploads_are_pruned(rig_dir, monkeypatch):
    monkeypatch.setattr(autorig, "_KEEP_SOURCES", 2)
    ids = [
        client.post("/api/rig/sources", content=GLB, headers={"x-file-name": f"m{i}.glb"}).json()["id"]
        for i in range(4)
    ]
    kept = {d.name for d in rig_dir.iterdir() if d.is_dir()}
    assert len(kept) == 2 and set(ids[-2:]) == kept


def test_deleting_a_source_removes_its_directory(rig_dir):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    assert client.delete(f"/api/rig/sources/{sid}").status_code == 200
    assert not (rig_dir / sid).exists()


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "..", "x" * 64])
def test_source_ids_off_the_wire_cannot_escape_the_gallery(bad, rig_dir):
    """These index directories, so a traversal here would be a file-delete bug.

    405 counts: the client collapses ``/sources/..`` to ``/sources``, which only
    accepts POST — either way nothing outside the gallery gets removed."""
    assert client.delete(f"/api/rig/sources/{bad}").status_code in (400, 404, 405)
    assert rig_dir.parent.exists()


# ── jobs ──────────────────────────────────────────────────────────────
def test_rig_refuses_when_blender_is_missing(rig_dir, monkeypatch):
    monkeypatch.setattr(
        autorig, "probe", lambda refresh=False: {"available": False, "error": "no Blender found"}
    )
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    r = client.post("/api/rig/jobs", json={"source_id": sid})
    assert r.status_code == 503
    assert "Blender" in r.json()["detail"]


def test_rig_on_an_unknown_source_is_a_404(rig_dir, available):
    assert client.post("/api/rig/jobs", json={"source_id": "deadbeef"}).status_code == 404


def test_job_runs_and_reports_its_result(rig_dir, available, monkeypatch):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]

    def fake_run(self, job):
        job.state = "done"
        job.progress = 1.0
        job.step = "done"
        job.result = {"ok": True, "vrm": "/static/gallery/vrm/x.vrm", "archetype": "bird_biped"}

    monkeypatch.setattr(autorig.RigRunner, "_run", fake_run)
    job = client.post("/api/rig/jobs", json={"source_id": sid, "name": "Penguin"}).json()
    for _ in range(200):
        snapshot = client.get(f"/api/rig/jobs/{job['id']}").json()
        if snapshot["state"] == "done":
            break
    assert snapshot["state"] == "done"
    assert snapshot["result"]["archetype"] == "bird_biped"
    assert snapshot["label"] == "Penguin"


def test_a_failing_job_reports_why_and_does_not_kill_the_worker(rig_dir, available, monkeypatch):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]

    def explode(self, job):
        raise RuntimeError("Blender fell over")

    monkeypatch.setattr(autorig.RigRunner, "_run", explode)
    job = client.post("/api/rig/jobs", json={"source_id": sid}).json()
    for _ in range(200):
        snapshot = client.get(f"/api/rig/jobs/{job['id']}").json()
        if snapshot["state"] == "failed":
            break
    assert snapshot["state"] == "failed"
    assert "fell over" in snapshot["error"]

    # The worker must survive it: the next job still runs.
    monkeypatch.setattr(
        autorig.RigRunner,
        "_run",
        lambda self, job: setattr(job, "state", "done") or setattr(job, "result", {"ok": True}),
    )
    second = client.post("/api/rig/jobs", json={"source_id": sid}).json()
    for _ in range(200):
        snapshot = client.get(f"/api/rig/jobs/{second['id']}").json()
        if snapshot["state"] == "done":
            break
    assert snapshot["state"] == "done"


def test_cancelling_a_queued_job_marks_it_cancelled(rig_dir, available, monkeypatch):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    job = autorig.Job(id="jjj", kind="rig", source_id=sid, label="x")
    with autorig.runner._lock:
        autorig.runner._jobs[job.id] = job
        autorig.runner._order.append(job.id)
    assert client.post(f"/api/rig/jobs/{job.id}/cancel").json() == {"ok": True}
    assert job.state == "cancelled"
    # Cancelling something already finished is a 404, not a second cancel.
    assert client.post(f"/api/rig/jobs/{job.id}/cancel").status_code == 404


def test_history_trimming_never_evicts_a_job_still_running(rig_dir, available, monkeypatch):
    """Evicting the running job would make it untrackable AND uncancellable."""
    monkeypatch.setattr(autorig, "_KEEP_JOBS", 3)
    runner = autorig.RigRunner()
    live = autorig.Job(id="live", kind="rig", source_id="s", label="live", state="running")
    with runner._lock:
        runner._jobs["live"] = live
        runner._order.append("live")
    for i in range(6):
        done = autorig.Job(id=f"d{i}", kind="rig", source_id="s", label="d", state="done")
        with runner._lock:
            runner._jobs[done.id] = done
            runner._order.append(done.id)
            for stale in runner._order[:-autorig._KEEP_JOBS]:
                if runner._jobs.get(stale) and runner._jobs[stale].state in autorig._TERMINAL:
                    runner._jobs.pop(stale, None)
            runner._order = [i for i in runner._order if i in runner._jobs][-autorig._KEEP_JOBS:]
    assert runner.get("live") is not None, "the running job was pruned out from under itself"


def test_a_failed_rig_does_not_leave_a_half_written_vrm(rig_dir, available, monkeypatch, tmp_path):
    """A killed Blender can leave a truncated file, and the gallery serves
    whatever is in that directory."""
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    partial = tmp_path / "vrm" / "half.vrm"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"glTF truncated")

    real_run = autorig.RigRunner._run

    def fake_run(self, job):
        job.params["vrm_out"] = str(partial)
        job.state = "failed"
        job.error = "Blender fell over"
        job.finished = 1.0
        if job.state != "done":
            from pathlib import Path as _P
            _P(job.params["vrm_out"]).unlink(missing_ok=True)

    monkeypatch.setattr(autorig.RigRunner, "_run", fake_run)
    job = client.post("/api/rig/jobs", json={"source_id": sid}).json()
    for _ in range(200):
        snap = client.get(f"/api/rig/jobs/{job['id']}").json()
        if snap["state"] == "failed":
            break
    assert snap["state"] == "failed"
    assert not partial.exists(), "the truncated VRM was left in the gallery"


def test_unknown_job_is_a_404():
    assert client.get("/api/rig/jobs/nosuchjob").status_code == 404


# ── the Blender-side protocol ─────────────────────────────────────────
def test_result_line_is_read_even_when_blender_is_chatty():
    noisy = "\n".join(
        [
            "Blender quit unexpectedly? no",
            'RIGRESULT>{"ok": false, "error": "first attempt"}',
            "INFO: some later chatter",
            'RIGRESULT>{"ok": true, "version": "5.1.2"}',
            "Blender quit",
        ]
    )
    # The LAST result line wins: Blender prints plenty after a run starts.
    assert autorig._last_result_line(noisy) == {"ok": True, "version": "5.1.2"}
    assert autorig._last_result_line("nothing useful here") is None


def test_progress_events_update_the_job_and_bound_the_log():
    job = autorig.Job(id="j", kind="rig", source_id="s", label="l")
    autorig.runner._on_event(job, json.dumps({"step": "binding", "progress": 0.5}))
    assert job.step == "binding" and job.progress == 0.5
    autorig.runner._on_event(job, json.dumps({"progress": 9.0}))
    assert job.progress == 1.0  # clamped, never past the end of the bar
    for i in range(autorig._LOG_LINES + 50):
        autorig.runner._on_event(job, json.dumps({"log": f"line {i}"}))
    assert len(job.log) == autorig._LOG_LINES
    autorig.runner._on_event(job, "not json at all")  # must not raise


def test_the_blender_script_and_recipes_ship_together():
    assert autorig.BLENDER_SCRIPT.exists()
    assert autorig.ARCHETYPES_FILE.exists()


# ── what a rig is for: target options ─────────────────────────────────
def test_rig_passes_the_target_and_its_overrides_to_blender(rig_dir, available, monkeypatch):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    seen = {}

    def fake_run(self, job):
        seen.update(job.params)
        job.state = "done"
        job.result = {"ok": True}

    monkeypatch.setattr(autorig.RigRunner, "_run", fake_run)
    job = client.post(
        "/api/rig/jobs", json={"source_id": sid, "target": "code27", "fingers": False}
    ).json()
    for _ in range(200):
        if client.get(f"/api/rig/jobs/{job['id']}").json()["state"] == "done":
            break
    assert seen["target"] == "code27"
    assert seen["fingers"] is False       # an explicit choice survives
    assert seen["mtoon"] is None          # "whatever the target wants" stays undecided


def test_rig_defaults_to_the_studio_avatar_target(rig_dir, available, monkeypatch):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    seen = {}
    monkeypatch.setattr(autorig.RigRunner, "_run", lambda self, job: seen.update(job.params))
    client.post("/api/rig/jobs", json={"source_id": sid})
    for _ in range(200):
        if seen:
            break
    assert seen["target"] == "avatar"


def test_rig_refuses_a_target_it_has_never_heard_of(rig_dir, available):
    sid = client.post("/api/rig/sources", content=GLB, headers={"x-file-name": "a.glb"}).json()["id"]
    r = client.post("/api/rig/jobs", json={"source_id": sid, "target": "toaster"})
    assert r.status_code == 400
    assert "toaster" in r.json()["detail"]


# ── the Code27 checklist ──────────────────────────────────────────────
import struct  # noqa: E402

import numpy as np  # noqa: E402

from video_stream import code27_check  # noqa: E402
from video_stream.code27_check import check_code27  # noqa: E402

_ALL_PRESETS = ("aa", "ih", "ou", "ee", "oh", "blink", "happy", "angry", "sad", "relaxed", "surprised")


def make_vrm(
    path,
    *,
    drop_bones=(),
    presets=_ALL_PRESETS,
    mtoon=True,
    statue=False,
    arms_down=False,
    springs=False,
    fingers=False,
    vrm0=False,
    plain_gltf=False,
):
    """A tiny but structurally real VRM: a skeleton, one skinned triangle strip
    with a vertex per bone, expressions, one material. Small enough to read in
    a test, complete enough that the checker can't tell it from Blender's."""
    bones = [b for b in code27_check.REQUIRED_BONES if b not in drop_bones]
    if fingers:
        bones += ["leftIndexProximal", "rightIndexProximal"]
    where = {
        "hips": (0, 1.0, 0), "spine": (0, 1.1, 0), "head": (0, 1.6, 0),
        "leftUpperArm": (0.2, 1.4, 0), "rightUpperArm": (-0.2, 1.4, 0),
        "leftLowerArm": (0.45, 1.4, 0), "rightLowerArm": (-0.45, 1.4, 0),
        "leftHand": (0.7, 1.4, 0), "rightHand": (-0.7, 1.4, 0),
        "leftUpperLeg": (0.1, 0.9, 0), "rightUpperLeg": (-0.1, 0.9, 0),
        "leftLowerLeg": (0.1, 0.5, 0), "rightLowerLeg": (-0.1, 0.5, 0),
        "leftFoot": (0.1, 0.1, 0), "rightFoot": (-0.1, 0.1, 0),
        "leftIndexProximal": (0.75, 1.4, 0), "rightIndexProximal": (-0.75, 1.4, 0),
    }
    if arms_down:
        where.update({"leftLowerArm": (0.22, 1.15, 0), "rightLowerArm": (-0.22, 1.15, 0)})
    # Flat hierarchy: every bone is a root, so translation == world position.
    nodes = [{"name": b, "translation": list(where[b])} for b in bones]
    nodes.append({"name": "neutral_bone", "translation": [0, 0, 0]})
    nodes.append({"name": "Body", "mesh": 0, "skin": 0})
    joints = list(range(len(bones) + 1))

    count = len(bones)
    positions = np.array([where[b] for b in bones], dtype=np.float32)
    lead = np.full(count, len(bones), dtype=np.uint16) if statue else np.arange(count, dtype=np.uint16)
    joint_data = np.zeros((count, 4), dtype=np.uint16)
    joint_data[:, 0] = lead
    weight_data = np.zeros((count, 4), dtype=np.float32)
    weight_data[:, 0] = 1.0
    indices = np.arange(3 * (count // 3), dtype=np.uint16)
    chunks = [positions.tobytes(), joint_data.tobytes(), weight_data.tobytes(), indices.tobytes()]
    views, offset = [], 0
    for chunk in chunks:
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(chunk)})
        offset += len(chunk) + (-len(chunk) % 4)
    blob = b"".join(c + b"\x00" * (-len(c) % 4) for c in chunks)

    material = {"name": "Skin"}
    if mtoon and not vrm0:
        material["extensions"] = {"VRMC_materials_mtoon": {"specVersion": "1.0"}}
    doc = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "skins": [{"joints": joints}],
        "materials": [material],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "JOINTS_0": 1, "WEIGHTS_0": 2},
            "indices": 3, "material": 0,
        }]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": count, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": count, "type": "VEC4"},
            {"bufferView": 2, "componentType": 5126, "count": count, "type": "VEC4"},
            {"bufferView": 3, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
        "extensions": {},
    }
    if plain_gltf:
        pass
    elif vrm0:
        names = {"aa": "a", "ih": "i", "ou": "u", "ee": "e", "oh": "o", "happy": "joy",
                 "sad": "sorrow", "relaxed": "fun", "angry": "angry", "blink": "blink"}
        doc["extensions"]["VRM"] = {
            "humanoid": {"humanBones": [{"bone": b, "node": i} for i, b in enumerate(bones)]},
            "blendShapeMaster": {"blendShapeGroups": [
                {"presetName": names[p], "binds": [{"mesh": 0, "index": 0, "weight": 100}]}
                for p in presets if p in names
            ]},
            "materialProperties": [{"shader": "VRM/MToon" if mtoon else "Standard"}],
        }
    else:
        doc["extensions"]["VRMC_vrm"] = {
            "specVersion": "1.0",
            "humanoid": {"humanBones": {b: {"node": i} for i, b in enumerate(bones)}},
            "expressions": {"preset": {
                p: {"morphTargetBinds": [{"node": len(nodes) - 1, "index": 0, "weight": 1.0}]}
                for p in presets
            } | {"neutral": {}}},
        }
        if springs:
            doc["extensions"]["VRMC_springBone"] = {"springs": [{"joints": [{"node": 0}]}]}
    text = json.dumps(doc).encode()
    text += b" " * (-len(text) % 4)
    body = struct.pack("<I4s", len(text), b"JSON") + text + struct.pack("<I4s", len(blob), b"BIN\x00") + blob
    path.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body)
    return path


def _levels(report):
    return {item["id"]: item["level"] for item in report["items"]}


def test_a_complete_avatar_only_draws_the_optional_warnings(tmp_path):
    report = check_code27(make_vrm(tmp_path / "ok.vrm"))
    levels = _levels(report)
    assert report["verdict"] == "warn"                  # springs + fingers are optional extras
    assert {k for k, v in levels.items() if v == "warn"} == {"springs", "fingers"}
    assert levels["bones"] == levels["skin"] == levels["lipsync"] == "pass"
    assert levels["emotions"] == levels["materials"] == levels["pose"] == "pass"
    assert "not Code27's own import check" in report["note"]


def test_an_avatar_with_every_extra_passes_outright(tmp_path):
    report = check_code27(make_vrm(tmp_path / "full.vrm", springs=True, fingers=True))
    assert report["verdict"] == "pass"
    assert report["counts"]["warn"] == 0 and report["counts"]["fail"] == 0


def test_a_statue_fails_even_though_the_file_is_valid(tmp_path):
    """The bug this checker exists for: a rig that 'succeeded' with every
    vertex tied to one neutral bone. The JSON looks perfect; nothing moves."""
    report = check_code27(make_vrm(tmp_path / "statue.vrm", statue=True))
    assert report["verdict"] == "fail"
    assert _levels(report)["skin"] == "fail"
    assert _levels(report)["bones"] == "pass"           # the skeleton itself is fine


def test_a_missing_required_bone_fails_and_names_it(tmp_path):
    report = check_code27(make_vrm(tmp_path / "armless.vrm", drop_bones=("leftHand",)))
    bones = next(i for i in report["items"] if i["id"] == "bones")
    assert bones["level"] == "fail" and "leftHand" in bones["detail"]
    assert report["verdict"] == "fail"


def test_missing_emotions_and_plain_materials_are_warnings_not_failures(tmp_path):
    report = check_code27(
        make_vrm(tmp_path / "old.vrm", presets=("aa", "ih", "ou", "ee", "oh", "blink"), mtoon=False)
    )
    levels = _levels(report)
    assert levels["emotions"] == "warn" and levels["materials"] == "warn"
    assert levels["lipsync"] == "pass"
    assert report["verdict"] == "warn"
    emotions = next(i for i in report["items"] if i["id"] == "emotions")
    assert "surprised" in emotions["detail"]


def test_no_mouth_shapes_is_a_failure(tmp_path):
    report = check_code27(make_vrm(tmp_path / "mute.vrm", presets=("blink",)))
    assert _levels(report)["lipsync"] == "fail"


def test_arms_at_the_sides_are_flagged_as_not_a_t_pose(tmp_path):
    report = check_code27(make_vrm(tmp_path / "apose.vrm", arms_down=True))
    assert _levels(report)["pose"] == "warn"
    assert _levels(check_code27(make_vrm(tmp_path / "tpose.vrm")))["pose"] == "pass"


def test_vrm0_files_are_read_with_their_older_names(tmp_path):
    report = check_code27(make_vrm(tmp_path / "zero.vrm", vrm0=True))
    levels = _levels(report)
    assert report["vrm_version"] == "0.x"
    assert levels["bones"] == "pass" and levels["skin"] == "pass"
    assert levels["lipsync"] == "pass" and levels["materials"] == "pass"
    # VRM 0.x has no "surprised" preset at all, so that one is honestly missing.
    assert levels["emotions"] == "warn"


@pytest.mark.parametrize("payload", [b"", b"not a model", b"glTF\x02\x00\x00\x00", GLB])
def test_junk_is_reported_not_raised(tmp_path, payload):
    path = tmp_path / "junk.vrm"
    path.write_bytes(payload)
    report = check_code27(path)
    assert report["verdict"] == "fail"
    assert report["items"][0]["id"] == "format"


def test_a_plain_gltf_is_told_it_isnt_a_vrm(tmp_path):
    report = check_code27(make_vrm(tmp_path / "plain.vrm", plain_gltf=True))
    assert report["verdict"] == "fail"
    assert "not a VRM" in report["items"][0]["detail"]


def test_a_missing_file_is_a_failed_check_not_a_crash(tmp_path):
    assert check_code27(tmp_path / "nope.vrm")["verdict"] == "fail"


def test_check_endpoint_reads_gallery_avatars_only(rig_dir, tmp_path):
    vrm_dir = tmp_path / "vrm"
    vrm_dir.mkdir()
    make_vrm(vrm_dir / "abc123.vrm")
    ok = client.get("/api/rig/check", params={"vrm": "/static/gallery/vrm/abc123.vrm"})
    assert ok.status_code == 200
    assert ok.json()["target"] == "code27"
    assert client.get("/api/rig/check", params={"vrm": "abc123.vrm"}).status_code == 200
    assert client.get("/api/rig/check", params={"vrm": "missing.vrm"}).status_code == 404


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..%2f..%2fsecrets.vrm", "a b.vrm", "x.glb", ".vrm", ""])
def test_check_endpoint_cannot_be_pointed_outside_the_gallery(bad, rig_dir, tmp_path):
    (tmp_path / "secrets.vrm").write_bytes(b"glTF")
    r = client.get("/api/rig/check", params={"vrm": bad})
    assert r.status_code in (400, 404, 422)
    assert r.status_code != 200


def test_every_finished_rig_carries_its_checklist(tmp_path):
    vrm = make_vrm(tmp_path / "done.vrm", statue=True)
    check = autorig._check_result({"vrm_path": str(vrm)})
    assert check["verdict"] == "fail"                    # and the statue is caught at the source
    assert autorig._check_result({}) is None


def test_a_checker_that_trips_does_not_take_the_rig_down_with_it(tmp_path, monkeypatch):
    def boom(_path):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(autorig, "check_code27", boom)
    check = autorig._check_result({"vrm_path": str(tmp_path / "x.vrm")})
    assert check["verdict"] == "unknown" and "parser exploded" in check["note"]
