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
                identify = {"op": 1, "d": {"rpcVersion": 1}}
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

    def scene_list(self) -> list[str]:
        resp = self._request("GetSceneList")
        if not resp or not resp.get("requestStatus", {}).get("result"):
            return []
        return [s["sceneName"] for s in resp["responseData"].get("scenes", [])]

    def set_scene(self, scene_name: str) -> bool:
        resp = self._request("SetCurrentProgramScene", {"sceneName": scene_name})
        return bool(resp and resp.get("requestStatus", {}).get("result"))

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
