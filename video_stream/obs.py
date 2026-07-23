"""Minimal synchronous OBS WebSocket v5 client.

Just enough of the obs-websocket v5 protocol to switch the current program scene,
built on the `websockets` library that uvicorn[standard] already installs — so the
auto-director needs no extra dependency.

Protocol (https://github.com/obsproject/obs-websocket): connect, receive Hello
(op 0), reply with Identify (op 1) including an auth string when the server asks
for one, receive Identified (op 2), then send Requests (op 6) and read Responses
(op 7). Everything here is best-effort and non-fatal: if OBS isn't running or the
WebSocket server is off, calls just return False and the director stays in dry-run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import time

try:
    from websockets.sync.client import connect as _ws_connect
except Exception:  # pragma: no cover - websockets always present via uvicorn[standard]
    _ws_connect = None


def _parse_replay_path(data: dict) -> str | None:
    """Pull the saved replay's file path out of a GetLastReplayBufferReplay
    response, probing every key obs-websocket has used across versions."""
    for key in (
        "savedReplayPath",
        "lastReplayPath",
        "lastReplayBufferReplayPath",
        "outputPath",
        "path",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode()


class OBSClient:
    """Thread-safe, reconnecting client for switching OBS scenes."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4455,
        password: str = "",
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._ws = None
        self._req_id = 0
        self._lock = threading.Lock()
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def connect(self) -> bool:
        if _ws_connect is None:
            self.last_error = "websockets library unavailable"
            return False
        with self._lock:
            if self._ws is not None:
                return True
            try:
                url = f"ws://{self.host}:{self.port}"
                ws = _ws_connect(url, open_timeout=self.timeout, close_timeout=self.timeout)
                hello = json.loads(ws.recv(timeout=self.timeout))
                # eventSubscriptions=0: this client is request/response only.
                # Without it OBS pushes every event (scene/media/input churn
                # during a live show), which could starve the response scan
                # in _request and poison later requests with stale replies.
                identify = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
                auth = hello.get("d", {}).get("authentication")
                if auth:
                    identify["d"]["authentication"] = _auth_string(
                        self.password, auth["salt"], auth["challenge"]
                    )
                ws.send(json.dumps(identify))
                identified = json.loads(ws.recv(timeout=self.timeout))
                if identified.get("op") != 2:
                    ws.close()
                    self.last_error = f"identify failed: {identified}"
                    return False
                self._ws = ws
                self.last_error = None
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self._ws = None
                return False

    def _request(self, request_type: str, data: dict | None = None) -> dict | None:
        with self._lock:
            if self._ws is None:
                return None
            self._req_id += 1
            rid = str(self._req_id)
            msg = {
                "op": 6,
                "d": {"requestType": request_type, "requestId": rid, "requestData": data or {}},
            }
            try:
                self._ws.send(json.dumps(msg))
                # Read until we see our response (skip unrelated events).
                for _ in range(10):
                    reply = json.loads(self._ws.recv(timeout=self.timeout))
                    if reply.get("op") == 7 and reply["d"].get("requestId") == rid:
                        return reply["d"]
                return None
            except Exception as exc:
                self.last_error = str(exc)
                self._drop()
                return None

    @staticmethod
    def _ok(resp: dict | None) -> bool:
        return bool(resp and resp.get("requestStatus", {}).get("result"))

    def scene_list(self) -> list[str]:
        resp = self._request("GetSceneList")
        if not self._ok(resp):
            return []
        return [s["sceneName"] for s in resp["responseData"].get("scenes", [])]

    def set_scene(self, scene_name: str) -> bool:
        return self._ok(
            self._request("SetCurrentProgramScene", {"sceneName": scene_name})
        )

    def current_scene(self) -> str | None:
        resp = self._request("GetCurrentProgramScene")
        if not self._ok(resp):
            return None
        data = resp["responseData"]
        return data.get("currentProgramSceneName") or data.get("sceneName")

    def input_list(self) -> list[str]:
        resp = self._request("GetInputList")
        if not self._ok(resp):
            return []
        return [i["inputName"] for i in resp["responseData"].get("inputs", [])]

    # ── Replay buffer / sources (used by video_stream.replay) ─────────────

    def replay_buffer_active(self) -> bool | None:
        """True/False from OBS, or None when the request itself failed."""
        resp = self._request("GetReplayBufferStatus")
        if not self._ok(resp):
            return None
        return bool(resp["responseData"].get("outputActive"))

    def start_replay_buffer(self) -> bool:
        return self._ok(self._request("StartReplayBuffer"))

    def save_replay_buffer(self) -> bool:
        return self._ok(self._request("SaveReplayBuffer"))

    def last_replay_path(self) -> str | None:
        resp = self._request("GetLastReplayBufferReplay")
        if not self._ok(resp):
            return None
        return _parse_replay_path(resp.get("responseData") or {})

    def set_input_settings(self, name: str, settings: dict, overlay: bool = True) -> bool:
        return self._ok(
            self._request(
                "SetInputSettings",
                {"inputName": name, "inputSettings": settings, "overlay": overlay},
            )
        )

    def trigger_media_restart(self, name: str) -> bool:
        return self._ok(
            self._request(
                "TriggerMediaInputAction",
                {
                    "inputName": name,
                    "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
                },
            )
        )

    def record_active(self) -> bool:
        resp = self._request("GetRecordStatus")
        return self._ok(resp) and bool(resp["responseData"].get("outputActive"))

    def create_record_chapter(self, name: str) -> bool:
        return self._ok(self._request("CreateRecordChapter", {"chapterName": name}))

    def scene_item_id(self, scene: str, source: str) -> int | None:
        resp = self._request(
            "GetSceneItemId", {"sceneName": scene, "sourceName": source}
        )
        if not self._ok(resp):
            return None
        item_id = resp["responseData"].get("sceneItemId")
        return int(item_id) if isinstance(item_id, (int, float)) else None

    def set_scene_item_enabled(self, scene: str, item_id: int, enabled: bool) -> bool:
        return self._ok(
            self._request(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene,
                    "sceneItemId": item_id,
                    "sceneItemEnabled": enabled,
                },
            )
        )

    def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._drop()


def mul_to_db(mul: object) -> float | None:
    """Linear amplitude multiplier → dBFS. mul <= 0 is silence → None."""
    if isinstance(mul, (int, float)) and math.isfinite(mul) and mul > 0:
        return 20 * math.log10(mul)
    return None


def peak_db(input_levels_mul: list) -> float | None:
    """Loudest channel's post-fader peak (index 1 of each [mag, peak, inputPeak]
    triple) in dB; falls back to any numeric value for unexpected shapes."""
    values = [ch[1] for ch in input_levels_mul if isinstance(ch, list) and len(ch) > 1]
    if not values:
        values = [v for ch in input_levels_mul if isinstance(ch, list) for v in ch]
    dbs = [d for d in (mul_to_db(v) for v in values) if d is not None]
    return max(dbs) if dbs else None


class AudioMeterListener:
    """Dedicated OBS connection subscribed ONLY to InputVolumeMeters events.

    Live loudness is event-only in obs-websocket v5 — there is no request for
    it (GetInputVolume returns the *fader*, which is why february11's audio
    director never actually worked). This runs its own socket + reader thread
    with eventSubscriptions=65536 so the request/response client (above, with
    eventSubscriptions=0) stays clean, and keeps the latest per-input peak:
    ``levels()`` → {normalized name: (name, dB, monotonic seen_at)}.

    OBS emits meters ~every 50ms while audio flows; staleness is the reader's
    problem (the director applies a freshness window), not ours.
    """

    def __init__(
        self, host: str = "127.0.0.1", port: int = 4455, password: str = "", timeout: float = 3.0
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._lock = threading.Lock()
        self._levels: dict[str, tuple[str, float, float]] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._ws = None  # live meters socket; stop() closes it to unblock recv
        self.connected = False
        self.last_error: str | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="obs-meters", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        # The reader can be parked in ws.recv(timeout=30.0) during quiet
        # audio; closing the socket makes recv raise immediately so a
        # director bounce never leaves a zombie meters connection stacked
        # against OBS for up to 30s.
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def levels(self) -> dict[str, tuple[str, float, float]]:
        with self._lock:
            return dict(self._levels)

    def _loop(self) -> None:
        while self._running:
            try:
                self._listen_once()
            except Exception as exc:
                self.last_error = str(exc)
            self._ws = None
            self.connected = False
            if self._running:
                time.sleep(3.0)  # reconnect backoff

    def _listen_once(self) -> None:
        if _ws_connect is None:
            raise RuntimeError("websockets library unavailable")
        with _ws_connect(
            f"ws://{self.host}:{self.port}",
            open_timeout=self.timeout,
            close_timeout=self.timeout,
        ) as ws:
            self._ws = ws
            hello = json.loads(ws.recv(timeout=self.timeout))
            identify = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 65536}}
            auth = hello.get("d", {}).get("authentication")
            if auth:
                identify["d"]["authentication"] = _auth_string(
                    self.password, auth["salt"], auth["challenge"]
                )
            ws.send(json.dumps(identify))
            identified = json.loads(ws.recv(timeout=self.timeout))
            if identified.get("op") != 2:
                raise RuntimeError(f"identify failed: {identified}")
            self.connected = True
            self.last_error = None

            while self._running:
                # Long timeout: with no audio flowing OBS sends nothing, and
                # that must read as "quiet", not as a broken connection.
                try:
                    frame = json.loads(ws.recv(timeout=30.0))
                except TimeoutError:
                    continue
                d = frame.get("d", {})
                if frame.get("op") != 5 or d.get("eventType") != "InputVolumeMeters":
                    continue
                now = time.monotonic()
                with self._lock:
                    for entry in d.get("eventData", {}).get("inputs", []):
                        name = str(entry.get("inputName") or "").strip()
                        if not name:
                            continue
                        db = peak_db(entry.get("inputLevelsMul") or [])
                        if db is None:
                            continue  # silence: keep the last level, let it go stale
                        self._levels[name.lower()] = (name, db, now)
