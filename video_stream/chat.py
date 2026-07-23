"""Unified live chat: Twitch + Kick aggregated server-side, one merged feed.

Twitch needs ZERO credentials — the anonymous ``justinfan`` IRC login reads
any channel's chat. Kick rides its public Pusher websocket after one HTTP
lookup of the chatroom id (a browser User-Agent is load-bearing there).
Aggregating on the server (unlike ChromaCanvas's in-browser model) means one
merged feed shared by every dashboard, every OBS overlay, and any future
consumer (chat-spike replay triggers, chaos commands, …).

Ported from chroma-canvas/services/liveChat.ts with its known holes fixed:
Kick answers ``pusher:ping`` (the source didn't, so Pusher dropped it after
~2 min of quiet chat), Kick reconnects like Twitch does, channel input strips
trailing URL junk, and Twitch only reports "live" once the channel JOIN is
acknowledged (numeric 366), not the moment the socket opens.

Each platform connection is one daemon thread (house style); ``hub.emit`` is
thread-safe. Messages are NOT retained on the hub — late joiners backfill
from ``GET /api/chat/history`` instead; per-platform status events ARE
retained so pages render connection state instantly.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from video_stream.hub import hub

try:
    from websockets.sync.client import connect as _ws_connect
except Exception:  # pragma: no cover
    _ws_connect = None

PLATFORM_COLORS = {"twitch": "#a970ff", "kick": "#53fc18"}
MAX_MESSAGES = 200

TWITCH_IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
KICK_PUSHER_URL = (
    "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
    "?protocol=7&client=js&version=8.4.0&flash=false"
)
KICK_LOOKUP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

PRIVMSG_RE = re.compile(r"^(@[^ ]+ )?:([^!]+)![^ ]+ PRIVMSG #[^ ]+ :(.*)$")
COLOR_RE = re.compile(r"color=(#[0-9A-Fa-f]{6})")
NAME_RE = re.compile(r"display-name=([^;]+)")
KICK_EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")

RECONNECT_DELAY = 3.0


class _PermanentError(RuntimeError):
    """Deterministic failure — retrying would just repeat the same request."""


def _unescape_tag(value: str) -> str:
    """IRCv3 tag value unescaping (\\s space, \\: semicolon, \\\\ backslash)."""
    return (
        value.replace("\\\\", "\x00")
        .replace("\\s", " ")
        .replace("\\:", ";")
        .replace("\x00", "\\")
    )


def normalize_channel(platform: str, raw: str) -> str:
    clean = raw.strip().lower()
    clean = re.sub(r"^#", "", clean)
    if platform == "twitch":
        clean = re.sub(r"^.*twitch\.tv/", "", clean)
    else:
        clean = re.sub(r"^.*kick\.com/", "", clean)
    return re.sub(r"[/?#].*$", "", clean)  # URLs paste in with trailing junk


class ChatAggregator:
    def __init__(self) -> None:
        self.history: deque[dict] = deque(maxlen=MAX_MESSAGES)
        self._lock = threading.Lock()
        self._connections: dict[str, "_Connection"] = {}

    # ---- fan-out ---------------------------------------------------------
    def push_message(self, conn: "_Connection", author: str, text: str, color: str | None) -> None:
        message = {
            "id": uuid.uuid4().hex,
            "platform": conn.platform,
            "author": author[:80],
            "color": color,
            "text": text[:500],
            "ts": int(time.time() * 1000),
        }
        with self._lock:
            if self._connections.get(conn.platform) is not conn:
                return  # a superseded thread draining its final recv batch
            self.history.append(message)
            hub.emit("chat_message", message, retain=False)  # emit never blocks

    def set_status(
        self,
        platform: str,
        status: str,
        detail: str | None,
        channel: str,
        conn: "_Connection | None" = None,
    ) -> None:
        with self._lock:
            # A stopped thread can surface from a blocking lookup/connect up
            # to ~20s after disconnect() and would clobber the retained status
            # with a stale "live". Drop emits from a superseded connection;
            # conn=None marks aggregator-authored writes (the "off").
            if conn is not None and self._connections.get(platform) is not conn:
                return
            hub.emit(
                f"chat_status_{platform}",
                {"platform": platform, "status": status, "detail": detail, "channel": channel},
            )

    # ---- lifecycle -------------------------------------------------------
    def connect(self, platform: str, channel: str) -> None:
        self.disconnect(platform)
        conn = _Connection(self, platform, channel)
        with self._lock:
            self._connections[platform] = conn
        conn.start()

    def disconnect(self, platform: str) -> None:
        with self._lock:
            conn = self._connections.pop(platform, None)
        if conn is not None:
            conn.stop()
            self.set_status(platform, "off", None, conn.channel)

    def status(self) -> dict:
        with self._lock:
            return {
                "connections": {
                    p: {"channel": c.channel, "alive": c.is_alive()}
                    for p, c in self._connections.items()
                },
                "history_len": len(self.history),
            }

    def shutdown(self) -> None:
        for platform in list(self._connections):
            self.disconnect(platform)


class _Connection(threading.Thread):
    """One platform's chat feed. Runs until stop() — reconnects forever."""

    def __init__(self, aggregator: ChatAggregator, platform: str, channel: str) -> None:
        super().__init__(name=f"chat-{platform}", daemon=True)
        self.agg = aggregator
        self.platform = platform
        self.channel = channel
        self._stop = threading.Event()
        self._kick_chatroom: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.agg.set_status(self.platform, "connecting", None, self.channel, conn=self)
                if self.platform == "twitch":
                    self._run_twitch()
                else:
                    self._run_kick()
            except _PermanentError as exc:
                # A typo'd channel or a Cloudflare 4xx won't fix itself —
                # stop with the error visible instead of hammering the API.
                if not self._stop.is_set():
                    self.agg.set_status(self.platform, "error", str(exc), self.channel, conn=self)
                return
            except Exception as exc:
                if not self._stop.is_set():
                    label = "Twitch" if self.platform == "twitch" else "Kick"
                    self.agg.set_status(
                        self.platform,
                        "error",
                        f"{label} chat connection failed: {exc}",
                        self.channel,
                        conn=self,
                    )
            if self._stop.is_set():
                return
            self.agg.set_status(self.platform, "connecting", "Reconnecting…", self.channel, conn=self)
            self._stop.wait(RECONNECT_DELAY)

    # ---- Twitch: anonymous IRC ------------------------------------------
    def _run_twitch(self) -> None:
        if _ws_connect is None:
            raise RuntimeError("websockets library unavailable")
        with _ws_connect(TWITCH_IRC_URL, open_timeout=10, close_timeout=5) as ws:
            ws.send("CAP REQ :twitch.tv/tags")
            ws.send(f"NICK justinfan{random.randint(1000, 80999)}")
            ws.send(f"JOIN #{self.channel}")

            last_ping = time.monotonic()
            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=5.0)
                except TimeoutError:
                    if time.monotonic() - last_ping >= 60.0:
                        ws.send("PING :tmi.twitch.tv")
                        last_ping = time.monotonic()
                    continue
                for line in str(raw).split("\r\n"):
                    if not line:
                        continue
                    if line.startswith("PING"):
                        ws.send("PONG :tmi.twitch.tv")
                        continue
                    if " 366 " in line:  # end of NAMES: the JOIN really worked
                        self.agg.set_status(self.platform, "live", None, self.channel, conn=self)
                        continue
                    match = PRIVMSG_RE.match(line)
                    if not match:
                        continue
                    tags, nick, text = match.group(1) or "", match.group(2), match.group(3)
                    if text and text[0] == "\x01":  # /me ACTION
                        text = re.sub(r"^ACTION ", "", text[1:-1])
                    name_match = NAME_RE.search(tags)
                    author = _unescape_tag(name_match.group(1)) if name_match else nick
                    color_match = COLOR_RE.search(tags)
                    self.agg.push_message(
                        self, author or nick, text, color_match.group(1) if color_match else None
                    )

    # ---- Kick: public Pusher websocket ----------------------------------
    def _resolve_kick_chatroom(self) -> int:
        if self._kick_chatroom is not None:
            return self._kick_chatroom  # reconnects skip the HTTP lookup
        if re.fullmatch(r"\d+", self.channel):
            self._kick_chatroom = int(self.channel)
            return self._kick_chatroom
        if not re.fullmatch(r"[\w-]+", self.channel):
            raise _PermanentError("invalid Kick channel name")
        import httpx

        resp = httpx.get(
            f"https://kick.com/api/v2/channels/{self.channel}",
            headers={"User-Agent": KICK_LOOKUP_UA, "Accept": "application/json"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            hint = "you can paste the numeric chatroom id instead"
            if 400 <= resp.status_code < 500:
                raise _PermanentError(f"Kick API returned {resp.status_code} — {hint}")
            raise RuntimeError(f"Kick API returned {resp.status_code}")  # 5xx: retry
        chatroom = (resp.json() or {}).get("chatroom") or {}
        if not isinstance(chatroom.get("id"), int):
            raise _PermanentError("No chatroom found for that channel.")
        self._kick_chatroom = chatroom["id"]
        return self._kick_chatroom

    def _run_kick(self) -> None:
        if _ws_connect is None:
            raise RuntimeError("websockets library unavailable")
        chatroom = self._resolve_kick_chatroom()
        with _ws_connect(KICK_PUSHER_URL, open_timeout=10, close_timeout=5) as ws:
            ws.send(
                json.dumps(
                    {
                        "event": "pusher:subscribe",
                        "data": {"auth": "", "channel": f"chatrooms.{chatroom}.v2"},
                    }
                )
            )
            self.agg.set_status(self.platform, "live", None, self.channel, conn=self)

            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=5.0)
                except TimeoutError:
                    continue
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                event = frame.get("event")
                if event == "pusher:ping":
                    # The source never answered these; Pusher then drops the
                    # socket after ~2min of quiet chat. Pong keeps us alive.
                    ws.send(json.dumps({"event": "pusher:pong", "data": "{}"}))
                    continue
                if event != "App\\Events\\ChatMessageEvent":
                    continue
                try:
                    data = json.loads(frame.get("data") or "{}")  # doubly encoded
                except ValueError:
                    continue
                sender = data.get("sender") or {}
                author = sender.get("username") or "kick user"
                color = (sender.get("identity") or {}).get("color")
                text = KICK_EMOTE_RE.sub(r"\1", data.get("content") or "")
                if text:
                    self.agg.push_message(self, author, text, color)


chat = ChatAggregator()

router = APIRouter()


class ChatConnect(BaseModel):
    platform: Literal["twitch", "kick"]
    channel: str


class ChatDisconnect(BaseModel):
    platform: Literal["twitch", "kick"]


@router.post("/api/chat/connect")
async def api_chat_connect(body: ChatConnect):
    channel = normalize_channel(body.platform, body.channel)
    if not channel:
        raise HTTPException(status_code=400, detail="channel is required")
    chat.connect(body.platform, channel)
    return {"status": "ok", "platform": body.platform, "channel": channel}


@router.post("/api/chat/disconnect")
async def api_chat_disconnect(body: ChatDisconnect):
    chat.disconnect(body.platform)
    return {"status": "ok"}


@router.get("/api/chat/status")
async def api_chat_status():
    return chat.status()


@router.get("/api/chat/history")
async def api_chat_history():
    with chat._lock:
        return {"messages": list(chat.history)}
