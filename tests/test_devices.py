"""Device cameras: pairing, the ingest socket, and the remote stream."""

from __future__ import annotations

import json
import time

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from video_stream import devices
from video_stream.camera import (
    REMOTE_INDEX_BASE,
    CameraManager,
    RemoteCameraStream,
    jpeg_size,
)


def _jpeg(width: int = 64, height: int = 48, value: int = 120) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((height, width, 3), value, np.uint8))
    assert ok
    return buf.tobytes()


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_jpeg_size_reads_the_header_and_rejects_non_jpegs():
    assert jpeg_size(_jpeg(64, 48)) == (64, 48)
    assert jpeg_size(_jpeg(320, 240)) == (320, 240)
    assert jpeg_size(b"") is None
    assert jpeg_size(b"GIF89a" + b"\x00" * 40) is None
    assert jpeg_size(b"\xff\xd8\xff") is None  # truncated


def test_remote_stream_passes_frames_through_and_goes_stale():
    stream = RemoteCameraStream(index=REMOTE_INDEX_BASE, name="Tab", key="dev")
    assert stream.start()
    try:
        assert not stream.active  # nothing received yet
        frame = _jpeg(64, 48)
        assert stream.push_jpeg(frame)
        assert _wait(lambda: stream.get_jpeg() is not None)
        assert stream.get_jpeg() == frame  # untouched: no decode/re-encode needed
        info = stream.info()
        assert (info.width, info.height, info.kind, info.active) == (64, 48, "remote", True)
        assert not stream.push_jpeg(b"not a jpeg")
    finally:
        stream.stop()
    assert not stream.active
    assert not stream.push_jpeg(_jpeg())  # stopped streams take nothing


def test_remote_stream_runs_the_cv_pipeline_when_zoomed():
    stream = RemoteCameraStream(index=REMOTE_INDEX_BASE, name="Tab", key="dev")
    stream.start()
    try:
        stream.set_manual_zoom(0.5, 0.5, 2.0)
        frame = _jpeg(64, 48)
        stream.push_jpeg(frame)
        assert _wait(lambda: stream.get_jpeg() is not None)
        out = stream.get_jpeg()
        assert out != frame  # re-encoded after the crop
        assert jpeg_size(out) == (64, 48)  # zoom keeps the frame size
    finally:
        stream.stop()


def test_manager_keeps_remote_cameras_out_of_the_local_range():
    manager = CameraManager()
    try:
        a = manager.add_remote("aaaaaaaa", "A")
        b = manager.add_remote("bbbbbbbb", "B")
        assert (a.index, b.index) == (REMOTE_INDEX_BASE, REMOTE_INDEX_BASE + 1)
        assert manager.add_remote("aaaaaaaa", "A renamed") is a
        assert a.name == "A renamed"
        pinned = manager.add_remote("cccccccc", "C", index=REMOTE_INDEX_BASE + 7)
        assert pinned.index == REMOTE_INDEX_BASE + 7
        assert manager.remove_remote("aaaaaaaa")
        assert not manager.remove_remote("aaaaaaaa")
        assert manager.get(REMOTE_INDEX_BASE) is None
    finally:
        manager.shutdown()


def test_store_pairs_verifies_and_persists_only_a_hash(tmp_path):
    path = tmp_path / "devices.json"
    store = devices.DeviceStore(path)
    secret, entry = store.pair("device-one", "Old Galaxy")
    assert entry["index"] == REMOTE_INDEX_BASE
    assert store.verify("device-one", secret)["name"] == "Old Galaxy"
    assert store.verify("device-one", "wrong") is None
    assert store.verify("device-one", "") is None
    assert store.verify("someone-else", secret) is None
    assert secret not in path.read_text()  # only the SHA-256 is on disk
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    # a second process (restart) sees the same device at the same camera number
    again = devices.DeviceStore(path)
    assert again.verify("device-one", secret)["index"] == REMOTE_INDEX_BASE
    _, second = again.pair("device-two", "Tablet")
    assert second["index"] == REMOTE_INDEX_BASE + 1
    assert again.remove("device-one") and not again.remove("device-one")


def test_join_tokens_expire(monkeypatch):
    store = devices.DeviceStore()
    token = store.mint_join()
    assert store.join_valid(token)
    assert not store.join_valid("nope") and not store.join_valid("")
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + devices.JOIN_TTL + 1)
    assert not store.join_valid(token)


def test_clean_name_strips_control_chars_and_caps_length():
    assert devices.clean_name("  Kitchen\x00 cam\n") == "Kitchen cam"
    assert devices.clean_name("") == "Phone"
    assert len(devices.clean_name("x" * 200)) == 40


def test_parse_adb_devices():
    text = (
        "List of devices attached\n"
        "RFCW40X1K9Z            unauthorized usb:34668544X transport_id:1\n"
        "R58M123ABC             device usb:1-2 product:a52q model:SM_A525F device:a52q transport_id:2\n"
        "\n"
    )
    assert devices.parse_adb_devices(text) == [
        {"serial": "RFCW40X1K9Z", "state": "unauthorized", "model": "Android device"},
        {"serial": "R58M123ABC", "state": "device", "model": "SM A525F"},
    ]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    manager = CameraManager()
    monkeypatch.setattr(devices, "store", devices.DeviceStore(tmp_path / "devices.json"))
    devices._live.clear()
    changes: list[int] = []
    devices.init(
        templates=None,
        asset_version=lambda: "t",
        config={"port": 8765, "phone_https": False, "phone_https_port": 8766},
        manager=manager,
        on_change=lambda: changes.append(1),
    )
    app = FastAPI()
    app.include_router(devices.router)
    with TestClient(app) as client:
        yield client, manager
    manager.shutdown()


def test_ingest_refuses_an_unpaired_device(rig):
    client, manager = rig
    with client.websocket_connect("/cam-ingest") as ws:
        ws.send_text(json.dumps({"id": "device-one", "name": "Phone", "join": "made-up"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 4401
    assert manager.list_cameras() == []


def test_ingest_pairs_with_a_join_token_then_reconnects_by_secret(rig):
    client, manager = rig
    join = client.get("/api/devices/join").json()
    assert join["usb_url"] == f"http://localhost:8765/cam?join={join['join']}"
    assert join["wifi_url"] is None  # https is off in this rig

    with client.websocket_connect("/cam-ingest") as ws:
        ws.send_text(json.dumps({"id": "device-one", "name": "Old Galaxy", "join": join["join"]}))
        paired = json.loads(ws.receive_text())
        assert paired["type"] == "paired" and paired["secret"]
        ready = json.loads(ws.receive_text())
        assert ready == {"type": "ready", "index": REMOTE_INDEX_BASE, "name": "Old Galaxy"}
        frame = _jpeg()
        ws.send_bytes(frame)
        ws.send_bytes(b"junk")  # ignored, connection stays up
        stream = manager.get(REMOTE_INDEX_BASE)
        assert _wait(lambda: stream.get_jpeg() == frame)
        listed = client.get("/api/devices").json()["devices"]
        assert listed == [
            {"id": "device-one", "name": "Old Galaxy", "index": REMOTE_INDEX_BASE, "connected": True, "usb": False, "live": True}
        ]
        ws.send_text(json.dumps({"type": "name", "name": "Desk cam"}))
        assert _wait(lambda: stream.name == "Desk cam")

    # no join token this time — the stored secret is enough, same camera number
    with client.websocket_connect("/cam-ingest") as ws:
        ws.send_text(json.dumps({"id": "device-one", "name": "Desk cam", "secret": paired["secret"]}))
        assert json.loads(ws.receive_text())["index"] == REMOTE_INDEX_BASE

    # a wrong secret with no join token is refused
    with client.websocket_connect("/cam-ingest") as ws:
        ws.send_text(json.dumps({"id": "device-one", "name": "x", "secret": "guess"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 4401

    assert client.delete("/api/devices/device-one").status_code == 200
    assert manager.get(REMOTE_INDEX_BASE) is None
    assert client.delete("/api/devices/device-one").status_code == 404


def test_usb_open_rejects_bad_serials_and_unknown_devices(rig, monkeypatch):
    client, _ = rig

    async def fake_adb(*args, timeout=8.0):
        return 0, "List of devices attached\nGOOD1 device model:Tab_A\nLOCKED1 unauthorized\n"

    monkeypatch.setattr(devices, "_run_adb", fake_adb)
    monkeypatch.setattr(devices, "_adb", lambda: "/usr/bin/adb")
    assert client.post("/api/devices/usb/bad;serial/open").status_code == 400
    assert client.post("/api/devices/usb/NOPE/open").status_code == 404
    assert client.post("/api/devices/usb/LOCKED1/open").status_code == 409
    usb = client.get("/api/devices/usb").json()
    assert [d["state"] for d in usb["devices"]] == ["device", "unauthorized"]


def test_wake_reopens_a_usb_device_and_explains_wifi_ones(rig, monkeypatch):
    client, _ = rig
    calls: list[tuple] = []

    async def fake_adb(*args, timeout=8.0):
        calls.append(args)
        if args[0] == "devices":
            return 0, "List of devices attached\nGOOD1 device model:Tab_A\n"
        return 0, "Starting: Intent"

    monkeypatch.setattr(devices, "_run_adb", fake_adb)
    monkeypatch.setattr(devices, "_adb", lambda: "/usr/bin/adb")

    # USB add: the join code carries the cable's serial to the pairing
    assert client.post("/api/devices/usb/GOOD1/open").status_code == 200
    url = next(a for a in calls[-1] if a.startswith("'http"))
    assert url.startswith("'http://localhost:8765/cam?join=") and url.endswith("&name=Tab-A'")
    token = url.split("join=")[1].split("&")[0]
    with client.websocket_connect("/cam-ingest") as ws:
        ws.send_text(json.dumps({"id": "usb-device-1", "name": "Tab A", "join": token}))
        ws.receive_text(), ws.receive_text()
    assert devices.store.all()["usb-device-1"]["serial"] == "GOOD1"

    calls.clear()
    assert client.post("/api/devices/usb-device-1/wake").status_code == 200
    assert ("-s", "GOOD1", "shell", "input", "keyevent", "224") in calls
    assert calls[-1][:4] == ("-s", "GOOD1", "shell", "am") and "com.android.browser.application_id" in calls[-1]

    # a Wi-Fi device has no cable to reach it through
    join = client.get("/api/devices/join").json()["join"]
    with client.websocket_connect("/cam-ingest") as ws:
        ws.send_text(json.dumps({"id": "wifi-device-1", "name": "iPad", "join": join}))
        ws.receive_text(), ws.receive_text()
    res = client.post("/api/devices/wifi-device-1/wake")
    assert res.status_code == 409 and "Wi-Fi" in res.json()["detail"]
    assert client.post("/api/devices/nobody-here/wake").status_code == 404
