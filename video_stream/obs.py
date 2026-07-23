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
import threading

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
