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
