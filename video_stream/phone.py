"""Phone-as-camera: scan a QR and a phone becomes a wireless camera source.

Flow: the dashboard mints a session → the phone scans a QR pointing at
``https://<lan-ip>:<https-port>/phone?session=<id>`` → both sides meet in a
signaling room at ``/phone-signal`` (a dumb JSON relay, ported from
chroma-canvas's vite plugin) → WebRTC connects them directly → the receiver
page ``/phone-view?session=<id>`` shows the live camera full-bleed, ready to
be an OBS Browser Source.

The phone is always the offerer; the receiver is a PERSISTENT answerer that
re-answers every offer (flip-camera and reconnects tear the whole peer down
and re-offer — the source app's receiver died after the first track, which is
exactly the bug we don't port). One phone and one receiver per session; run
several sessions for several phones.

HTTPS matters: phones only expose ``getUserMedia`` in secure contexts, so the
phone page must load over https (see install-phone.sh + the second uvicorn
server in app.main). The receiver and dashboard stay on plain http — the
signaling rooms live in this process's memory, so both schemes meet in the
same room. Every client WS URL is protocol-relative (the chroma lesson:
mixed-content silently kills ``ws://`` on https pages).
"""

from __future__ import annotations

import io
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

from video_stream.network import primary_ip

_ctx: dict[str, Any] = {}  # templates, asset_version, config


def init(**kwargs: Any) -> None:
    _ctx.update(kwargs)


# session id -> connected sockets. Process-global on purpose: the phone joins
# over https and the receiver over http, and they must share a room.
rooms: dict[str, set[WebSocket]] = {}

router = APIRouter()


@router.websocket("/phone-signal")
async def phone_signal(ws: WebSocket):
    """Dumb relay: every frame goes verbatim to the other members of the room.

    The server never parses signaling. JSON text frames only; a missing
    session id is rejected (the source's shared "default" room invited
    cross-user collisions)."""
    session = ws.query_params.get("session") or ""
    if not session:
        await ws.accept()
        await ws.close(code=4400, reason="session required")
        return
    if session not in rooms and len(rooms) >= 64:
        await ws.accept()
        await ws.close(code=4409, reason="too many sessions")  # LAN sanity bound
        return
    await ws.accept()
    room = rooms.setdefault(session, set())
    room.add(ws)
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("text")
            if data is None:
                continue  # protocol is JSON text; ignore stray binary
            for peer in list(room):
                if peer is not ws:
                    try:
                        await peer.send_text(data)
                    except Exception:
                        room.discard(peer)
    except WebSocketDisconnect:
        pass
    finally:
        room.discard(ws)
        if not room:
            rooms.pop(session, None)


def _phone_url(session: str) -> str | None:
    config = _ctx["config"]
    if not config.get("phone_https"):
        return None
    return f"https://{primary_ip()}:{config['phone_https_port']}/phone?session={session}"


@router.get("/api/phone/session")
async def api_phone_session():
    """Mint a fresh session: the QR/phone URL (https) + the OBS view URL."""
    config = _ctx["config"]
    session = secrets.token_urlsafe(6)
    return {
        "session": session,
        "https": bool(config.get("phone_https")),
        "https_port": config.get("phone_https_port"),
        "phone_url": _phone_url(session),
        "view_url": f"http://{primary_ip()}:{config['port']}/phone-view?session={session}",
        "qr_url": f"/phone/qr?session={session}",
    }


@router.get("/api/phone/status")
async def api_phone_status():
    return {"sessions": {sid: len(members) for sid, members in rooms.items()}}


@router.get("/phone/qr")
async def phone_qr(session: str):
    url = _phone_url(session)
    if url is None:
        raise HTTPException(
            status_code=409,
            detail="HTTPS is not set up — run ./install-phone.sh, then restart",
        )
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2, dark="#0f0f11")
    return Response(
        buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/phone", response_class=HTMLResponse)
async def phone_page(request: Request):
    resp = _ctx["templates"].TemplateResponse(
        request, "phone.html", {"asset_v": _ctx["asset_version"]()}
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@router.get("/phone-view", response_class=HTMLResponse)
async def phone_view_page(request: Request):
    resp = _ctx["templates"].TemplateResponse(
        request, "phone-view.html", {"asset_v": _ctx["asset_version"]()}
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp
