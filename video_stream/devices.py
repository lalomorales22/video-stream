"""Device cameras: a phone or tablet becomes a real camera on the rig.

The older phone flow (``phone.py``) connects a phone straight to one OBS
browser source over WebRTC — the server never sees a frame, so that feed
can't be a director input, wear a skeleton, punch in, or drive an avatar.

Here the device opens ``/cam`` in its browser (no app to install) and sends
JPEG frames up a WebSocket. Each paired device becomes a ``RemoteCameraStream``
with its own card, ``/stream/N`` and ``/view/N`` URLs, like a USB webcam.

Two ways for the device to reach the page, both a *secure context* (browsers
only hand out the camera on https or localhost):

* **USB** (Android): ``adb reverse`` makes the device's own ``localhost:<port>``
  tunnel to this server over the cable — no Wi-Fi, no certificate.
* **Wi-Fi**: the https port from ``./install-phone.sh``, via a QR code.

Pairing: the dashboard mints a short-lived join token (in the QR / the USB
link). A device presenting a valid one is issued its own secret, kept in the
browser's storage and stored here only as a SHA-256 — after that it reconnects
by itself, and keeps the same camera number, until it is removed. Nothing can
push frames into the rig without one or the other.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

from video_stream.camera import REMOTE_INDEX_BASE, REMOTE_MAX_FRAME_BYTES
from video_stream.network import primary_ip
from video_stream.settings import require_token

JOIN_TTL = 15 * 60  # seconds a QR / USB link stays valid for a first pairing
MAX_DEVICES = 16
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_ctx: dict[str, Any] = {}  # templates, asset_version, config, manager, on_change

router = APIRouter()


def init(**kwargs: Any) -> None:
    _ctx.update(kwargs)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def clean_name(raw: Any, fallback: str = "Phone") -> str:
    """Device names are typed on the device and shown on the dashboard —
    keep them short and free of control characters."""
    name = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()
    return name[:40] or fallback


class DeviceStore:
    """Paired devices, persisted owner-only next to settings.json."""

    def __init__(self, path: Path | None = None) -> None:
        config_dir = Path(
            os.environ.get("VIDEO_STREAM_CONFIG", Path.home() / ".config" / "video-stream")
        )
        self.path = path or (config_dir / "devices.json")
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._joins: dict[str, float] = {}  # token -> monotonic expiry
        self._join_serial: dict[str, str] = {}  # token -> adb serial it was sent to
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            for dev_id, entry in raw.items():
                if _ID_RE.match(str(dev_id)) and isinstance(entry, dict) and entry.get("secret"):
                    self._devices[dev_id] = entry

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._devices, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ── join tokens ──────────────────────────────────────
    def mint_join(self, serial: str = "") -> str:
        with self._lock:
            now = time.monotonic()
            self._joins = {t: exp for t, exp in self._joins.items() if exp > now}
            self._join_serial = {t: s for t, s in self._join_serial.items() if t in self._joins}
            token = secrets.token_urlsafe(9)
            self._joins[token] = now + JOIN_TTL
            if serial:
                self._join_serial[token] = serial
            return token

    def join_serial(self, token: str) -> str:
        """The USB serial a (still valid) join code was handed to, if any."""
        with self._lock:
            expiry = self._joins.get(token or "")
            if expiry is None or expiry <= time.monotonic():
                return ""
            return self._join_serial.get(token, "")

    def set_serial(self, dev_id: str, serial: str) -> None:
        with self._lock:
            self._load()
            entry = self._devices.get(dev_id)
            if entry is not None and entry.get("serial") != serial:
                entry["serial"] = serial
                self._save()

    def join_valid(self, token: str) -> bool:
        with self._lock:
            expiry = self._joins.get(token or "")
            return expiry is not None and expiry > time.monotonic()

    # ── devices ──────────────────────────────────────────
    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            self._load()
            return {k: dict(v) for k, v in self._devices.items()}

    def verify(self, dev_id: str, secret: str) -> dict[str, Any] | None:
        with self._lock:
            self._load()
            entry = self._devices.get(dev_id)
            if entry is None or not secret:
                return None
            if not hmac.compare_digest(_hash(secret), str(entry.get("secret", ""))):
                return None
            return dict(entry)

    def pair(self, dev_id: str, name: str) -> tuple[str, dict[str, Any]]:
        """Create (or re-key) a device. Returns its fresh secret + entry."""
        with self._lock:
            self._load()
            existing = self._devices.get(dev_id)
            if existing is None and len(self._devices) >= MAX_DEVICES:
                raise ValueError("too many paired devices — remove one first")
            secret = secrets.token_urlsafe(24)
            if existing is not None:
                index = existing["index"]
            else:
                used = {int(d.get("index", 0)) for d in self._devices.values()}
                index = REMOTE_INDEX_BASE
                while index in used:
                    index += 1
            entry = {
                "name": name,
                "index": index,
                "secret": _hash(secret),
                "paired_at": int(time.time()),
            }
            if existing is not None and existing.get("serial"):
                entry["serial"] = existing["serial"]
            self._devices[dev_id] = entry
            self._save()
            return secret, dict(entry)

    def rename(self, dev_id: str, name: str) -> None:
        with self._lock:
            self._load()
            if dev_id in self._devices:
                self._devices[dev_id]["name"] = name
                self._save()

    def remove(self, dev_id: str) -> bool:
        with self._lock:
            self._load()
            if self._devices.pop(dev_id, None) is None:
                return False
            self._save()
            return True


store = DeviceStore()

# device id -> the socket currently feeding it (newest connection wins)
_live: dict[str, WebSocket] = {}


def _changed() -> None:
    callback: Callable[[], None] | None = _ctx.get("on_change")
    if callback is not None:
        try:
            callback()
        except Exception:
            pass


def restore_cameras() -> None:
    """Re-create a (offline) camera card for every paired device at boot, so
    OBS sources pointed at /view/10x resolve before the device reconnects."""
    manager = _ctx["manager"]
    for dev_id, entry in store.all().items():
        manager.add_remote(dev_id, clean_name(entry.get("name")), int(entry.get("index", 0)))


# ── pairing + pages ──────────────────────────────────────
def _wifi_url(token: str) -> str | None:
    config = _ctx["config"]
    if not config.get("phone_https"):
        return None
    return f"https://{primary_ip()}:{config['phone_https_port']}/cam?join={token}"


def _usb_url(token: str) -> str:
    return f"http://localhost:{_ctx['config']['port']}/cam?join={token}"


@router.get("/api/devices/join")
async def api_devices_join(request: Request):
    """Mint a join token: the Wi-Fi (QR) link and the USB link for it."""
    require_token(request)
    token = store.mint_join()
    return {
        "join": token,
        "expires_in": JOIN_TTL,
        "https": bool(_ctx["config"].get("phone_https")),
        "wifi_url": _wifi_url(token),
        "usb_url": _usb_url(token),
        "qr_url": f"/devices/qr?join={token}",
    }


@router.get("/devices/qr")
async def devices_qr(join: str):
    if not store.join_valid(join):
        raise HTTPException(status_code=404, detail="That link expired — open Add device again")
    url = _wifi_url(join)
    if url is None:
        raise HTTPException(
            status_code=409, detail="HTTPS is not set up — run ./install-phone.sh, then restart"
        )
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2, dark="#0f0f11")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/cam", response_class=HTMLResponse)
async def cam_page(request: Request):
    resp = _ctx["templates"].TemplateResponse(
        request, "cam.html", {"asset_v": _ctx["asset_version"]()}
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"  # the URL carries the join token
    return resp


@router.get("/api/devices")
async def api_devices():
    manager = _ctx["manager"]
    out = []
    for dev_id, entry in store.all().items():
        stream = manager.get(int(entry.get("index", -1)))
        out.append(
            {
                "id": dev_id,
                "name": entry.get("name"),
                "index": entry.get("index"),
                "connected": dev_id in _live,
                "usb": bool(entry.get("serial")),
                "live": bool(stream and stream.active),
            }
        )
    return {"devices": sorted(out, key=lambda d: d["index"])}


@router.delete("/api/devices/{dev_id}")
async def api_devices_remove(dev_id: str, request: Request):
    require_token(request)
    if not _ID_RE.match(dev_id) or not store.remove(dev_id):
        raise HTTPException(status_code=404, detail="Unknown device")
    ws = _live.pop(dev_id, None)
    if ws is not None:
        try:
            await ws.close(code=4403, reason="removed")
        except Exception:
            pass
    await asyncio.to_thread(_ctx["manager"].remove_remote, dev_id)
    _changed()
    return {"ok": True}


# ── ingest ───────────────────────────────────────────────
@router.websocket("/cam-ingest")
async def cam_ingest(ws: WebSocket):
    """One device's camera feed. First message is a JSON hello —
    ``{id, name, secret?, join?}`` — then every binary message is one JPEG."""
    await ws.accept()
    manager = _ctx["manager"]
    dev_id = ""
    try:
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:
            await ws.close(code=4400, reason="expected a hello")
            return
        if not isinstance(hello, dict) or not _ID_RE.match(str(hello.get("id", ""))):
            await ws.close(code=4400, reason="bad device id")
            return
        dev_id = str(hello["id"])
        name = clean_name(hello.get("name"))

        entry = store.verify(dev_id, str(hello.get("secret") or ""))
        if entry is None:
            if not store.join_valid(str(hello.get("join") or "")):
                await ws.close(code=4401, reason="not paired")
                dev_id = ""
                return
            try:
                secret, entry = await asyncio.to_thread(store.pair, dev_id, name)
            except ValueError as exc:
                await ws.close(code=4409, reason=str(exc)[:100])
                dev_id = ""
                return
            await ws.send_text(json.dumps({"type": "paired", "secret": secret}))
        elif name != entry.get("name"):
            await asyncio.to_thread(store.rename, dev_id, name)

        # Opened from the dashboard's USB button? Remember which cable that
        # was, so the card can re-open the page on the device later.
        serial = store.join_serial(str(hello.get("join") or ""))
        if serial:
            await asyncio.to_thread(store.set_serial, dev_id, serial)

        previous = _live.get(dev_id)
        _live[dev_id] = ws
        if previous is not None:
            try:
                await previous.close(code=4408, reason="replaced by a newer connection")
            except Exception:
                pass

        stream = await asyncio.to_thread(manager.add_remote, dev_id, name, int(entry["index"]))
        await ws.send_text(json.dumps({"type": "ready", "index": stream.index, "name": name}))
        _changed()

        got_first = False
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                if len(data) > REMOTE_MAX_FRAME_BYTES or not stream.push_jpeg(data):
                    continue
                if not got_first:
                    got_first = True
                    # let the worker thread publish it, then light the card up
                    asyncio.get_running_loop().call_later(0.4, _changed)
                continue
            text = message.get("text")
            if text:
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "name":
                    name = clean_name(msg.get("name"))
                    stream.name = name
                    await asyncio.to_thread(store.rename, dev_id, name)
                    _changed()
    except WebSocketDisconnect:
        pass
    finally:
        if dev_id and _live.get(dev_id) is ws:
            _live.pop(dev_id, None)
            # the card flips to offline once frames go stale; tell dashboards then
            try:
                asyncio.get_running_loop().call_later(3.2, _changed)
            except RuntimeError:
                pass


# ── USB (Android, via adb) ───────────────────────────────
def _adb() -> str | None:
    return os.environ.get("VIDEO_STREAM_ADB") or shutil.which("adb")


async def _run_adb(*args: str, timeout: float = 8.0) -> tuple[int, str]:
    adb = _adb()
    if adb is None:
        return 127, "adb not found"
    proc = await asyncio.create_subprocess_exec(
        adb, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "adb timed out"
    return proc.returncode or 0, out.decode(errors="replace")


def parse_adb_devices(text: str) -> list[dict[str, str]]:
    devices = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or not _SERIAL_RE.match(parts[0]):
            continue
        fields = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(
            {
                "serial": parts[0],
                "state": parts[1],  # device | unauthorized | offline
                "model": fields.get("model", "").replace("_", " ") or "Android device",
            }
        )
    return devices


@router.get("/api/devices/usb")
async def api_devices_usb():
    """Android devices on a cable. ``state`` tells the dashboard what to ask
    the user for: ``unauthorized`` = tap Allow on the phone."""
    if _adb() is None:
        return {"adb": False, "devices": []}
    code, out = await _run_adb("devices", "-l")
    if code != 0:
        return {"adb": True, "devices": [], "error": out.strip()[:200]}
    return {"adb": True, "devices": parse_adb_devices(out)}


async def _open_on_usb(serial: str) -> dict[str, str]:
    """Tunnel this server to the device's localhost over the cable and bring
    the camera page to the front on it. Raises HTTPException with a message
    the dashboard can show as-is."""
    if not _SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Bad device serial")
    if _adb() is None:
        raise HTTPException(status_code=409, detail="adb is not installed on this computer")
    code, out = await _run_adb("devices", "-l")
    known = {d["serial"]: d for d in parse_adb_devices(out)} if code == 0 else {}
    device = known.get(serial)
    if device is None:
        raise HTTPException(status_code=404, detail="That device is not plugged in")
    if device["state"] != "device":
        raise HTTPException(
            status_code=409,
            detail="Unlock the device and tap Allow on the “Allow USB debugging?” prompt",
        )
    port = int(_ctx["config"]["port"])
    code, out = await _run_adb("-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}")
    if code != 0:
        raise HTTPException(status_code=502, detail=f"Could not set up the USB tunnel: {out.strip()[:160]}")
    # Wake the screen first — a sleeping phone opens the page behind the lock
    # screen, where Chrome won't run the camera. (224 = KEYCODE_WAKEUP: it
    # only ever wakes, never toggles the screen off.)
    await _run_adb("-s", serial, "shell", "input", "keyevent", "224")
    # Chrome hides the real model from web pages ("K"), so hand the page the
    # name adb reports. `adb shell` re-parses its arguments on the device, so
    # the URL goes in single quotes and everything in it is restricted to
    # characters that cannot end them: token_urlsafe is [A-Za-z0-9_-], and the
    # model is filtered to the same set here.
    model = re.sub(r"[^A-Za-z0-9_-]", "-", device["model"]).strip("-")[:40]
    url = _usb_url(store.mint_join(serial)) + (f"&name={model}" if model else "")
    code, out = await _run_adb(
        "-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{url}'",
        # Chrome reuses one tab per application id, so pressing the button
        # again replaces the camera page instead of stacking up tabs.
        "--es", "com.android.browser.application_id", "video-stream",
    )
    if code != 0 or "Error" in out:
        raise HTTPException(status_code=502, detail=f"Could not open the page on the device: {out.strip()[:160]}")
    return device


@router.post("/api/devices/usb/{serial}/open")
async def api_devices_usb_open(serial: str, request: Request):
    """Add a plugged-in Android device as a camera."""
    require_token(request)
    device = await _open_on_usb(serial)
    return {"ok": True, "model": device["model"]}


@router.post("/api/devices/{dev_id}/wake")
async def api_devices_wake(dev_id: str, request: Request):
    """Bring a paired device's camera page back to the front. Only possible
    over USB — a Wi-Fi device has to reopen the page itself."""
    require_token(request)
    entry = store.all().get(dev_id) if _ID_RE.match(dev_id) else None
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    serial = str(entry.get("serial") or "")
    if not serial:
        raise HTTPException(
            status_code=409,
            detail="This device joined over Wi-Fi — open the camera page on it again and keep it on screen",
        )
    device = await _open_on_usb(serial)
    return {"ok": True, "model": device["model"]}
