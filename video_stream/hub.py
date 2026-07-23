"""Studio Bus: one WebSocket hub that every dashboard and overlay page rides.

Server code — route handlers, the director thread, capture threads — calls
``hub.emit(event, payload)``; every connected page receives one
``{"type": event, "payload": payload}`` JSON frame. Events emitted with
``retain=True`` (the default) are remembered per event name and re-sent to each
new client right after it connects, so a page opened mid-show hydrates
instantly instead of waiting for the next change.

Threading rules (the reason this module exists at all):

* ``emit()`` is safe from ANY thread and never blocks. It only schedules the
  fan-out onto the server's event loop via ``call_soon_threadsafe`` — a capture
  or director thread can never freeze the event loop by emitting.
* Each client gets its own outbound queue drained by a single ``pump`` task, so
  two emits can never interleave writes on one socket. A client that stops
  reading long enough to fill its queue loses the oldest frames, never the
  connection.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import WebSocket

_QUEUE_LIMIT = 256  # frames buffered per slow client before dropping oldest


class Hub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[WebSocket, asyncio.Queue[str]] = {}
        self._retained: dict[str, Any] = {}
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the server's event loop. Called once from the lifespan."""
        self._loop = loop

    async def connect(self, ws: WebSocket) -> asyncio.Queue[str]:
        await ws.accept()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        with self._lock:
            retained = list(self._retained.items())
            self._queues[ws] = queue
        for event, payload in retained:
            queue.put_nowait(json.dumps({"type": event, "payload": payload}))
        return queue

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            self._queues.pop(ws, None)

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._queues)

    async def pump(self, ws: WebSocket, queue: asyncio.Queue[str]) -> None:
        """Drain one client's queue onto its socket. Cancelled on disconnect."""
        while True:
            await ws.send_text(await queue.get())

    def emit(self, event: str, payload: Any, retain: bool = True) -> None:
        if retain:
            with self._lock:
                self._retained[event] = payload
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        message = json.dumps({"type": event, "payload": payload})
        try:
            loop.call_soon_threadsafe(self._fan_out, message)
        except RuntimeError:
            pass  # loop is shutting down; nobody left to tell

    def _fan_out(self, message: str) -> None:
        # Runs on the event loop thread.
        with self._lock:
            queues = list(self._queues.values())
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()  # drop the oldest frame, keep the newest
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass


hub = Hub()
