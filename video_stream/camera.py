"""Camera discovery, capture, and MJPEG frame generation."""

from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Generator

import cv2
import numpy as np


@dataclass
class CameraInfo:
    index: int
    name: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    active: bool = False
    error: str | None = None


@dataclass
class StreamStats:
    frames: int = 0
    dropped: int = 0
    clients: int = 0
    last_frame_at: float = 0.0
    fps: float = 0.0


class CameraStream:
    """Owns one camera device and continuously grabs frames for streaming."""

    def __init__(
        self,
        index: int,
        name: str,
        width: int = 1280,
        height: int = 720,
        jpeg_quality: int = 80,
        target_fps: float = 30.0,
    ) -> None:
        self.index = index
        self.name = name
        self.requested_width = width
        self.requested_height = height
        self.jpeg_quality = max(40, min(95, jpeg_quality))
        self.target_fps = target_fps
        self.stats = StreamStats()

        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._jpeg: bytes | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._clients = 0
        self._clients_lock = threading.Lock()
        self._error: str | None = None
        self._actual_width = 0
        self._actual_height = 0
        self._actual_fps = 0.0

    @property
    def active(self) -> bool:
        return self._running and self._cap is not None and self._cap.isOpened()

    @property
    def error(self) -> str | None:
        return self._error

    def info(self) -> CameraInfo:
        return CameraInfo(
            index=self.index,
            name=self.name,
            width=self._actual_width,
            height=self._actual_height,
            fps=self._actual_fps or self.stats.fps,
            active=self.active,
            error=self._error,
        )

    def start(self) -> bool:
        if self._running:
            return True

        cap = self._open_capture(self.index)
        if cap is None or not cap.isOpened():
            self._error = "Could not open camera"
            return False

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.requested_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.requested_height)
        cap.set(cv2.CAP_PROP_FPS, self.target_fps)
        # Prefer MJPEG from device when available (lower CPU)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            self._error = "Camera opened but produced no frames"
            return False

        self._actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or frame.shape[1])
        self._actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or frame.shape[0])
        self._actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or self.target_fps)

        self._cap = cap
        self._running = True
        self._error = None
        self._encode_frame(frame)

        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"camera-{self.index}",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        with self._lock:
            self._frame = None
            self._jpeg = None

    def _capture_loop(self) -> None:
        interval = 1.0 / max(1.0, self.target_fps)
        frame_times: list[float] = []

        while self._running and self._cap is not None:
            t0 = time.perf_counter()
            ok, frame = self._cap.read()
            if not ok or frame is None:
                self.stats.dropped += 1
                time.sleep(0.02)
                continue

            self._encode_frame(frame)
            self.stats.frames += 1
            now = time.time()
            self.stats.last_frame_at = now
            frame_times.append(now)
            frame_times = [t for t in frame_times if now - t < 2.0]
            if len(frame_times) >= 2:
                self.stats.fps = (len(frame_times) - 1) / max(
                    0.001, frame_times[-1] - frame_times[0]
                )

            elapsed = time.perf_counter() - t0
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _encode_frame(self, frame: np.ndarray) -> None:
        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
        )
        if not ok:
            return
        jpeg = buf.tobytes()
        with self._lock:
            self._frame = frame
            self._jpeg = jpeg

    def get_jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def mjpeg_generator(self) -> Generator[bytes, None, None]:
        boundary = b"frame"
        with self._clients_lock:
            self._clients += 1
            self.stats.clients = self._clients
        try:
            last: bytes | None = None
            while self._running:
                jpeg = self.get_jpeg()
                if jpeg is None:
                    time.sleep(0.05)
                    continue
                if jpeg is last:
                    time.sleep(0.01)
                    continue
                last = jpeg
                yield (
                    b"--" + boundary + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
        finally:
            with self._clients_lock:
                self._clients = max(0, self._clients - 1)
                self.stats.clients = self._clients

    @staticmethod
    def _open_capture(index: int) -> cv2.VideoCapture | None:
        system = platform.system()
        if system == "Darwin":
            # AVFoundation is the reliable backend on macOS
            cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
            return cap if cap.isOpened() else None
        if system == "Windows":
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if cap.isOpened():
                return cap
            cap.release()

        cap = cv2.VideoCapture(index)
        return cap if cap.isOpened() else None


class CameraManager:
    """Discovers cameras and manages their streams."""

    def __init__(
        self,
        max_probe: int = 8,
        width: int = 1280,
        height: int = 720,
        jpeg_quality: int = 80,
        target_fps: float = 30.0,
    ) -> None:
        self.max_probe = max_probe
        self.width = width
        self.height = height
        self.jpeg_quality = jpeg_quality
        self.target_fps = target_fps
        self._streams: dict[int, CameraStream] = {}
        self._lock = threading.Lock()

    def discover(self, auto_start: bool = True) -> list[CameraInfo]:
        found: list[CameraInfo] = []
        misses = 0
        for index in range(self.max_probe):
            # Skip re-probe of already-known streams
            with self._lock:
                existing = self._streams.get(index)
            if existing is not None:
                if auto_start and not existing.active:
                    existing.start()
                found.append(existing.info())
                misses = 0
                continue

            name = self._probe_name(index)
            if name is None:
                misses += 1
                # Indices are usually dense; stop probing once the run of misses grows.
                if found and misses >= 1:
                    break
                if not found and misses >= 3:
                    break
                continue

            misses = 0
            stream = CameraStream(
                index=index,
                name=name,
                width=self.width,
                height=self.height,
                jpeg_quality=self.jpeg_quality,
                target_fps=self.target_fps,
            )
            with self._lock:
                self._streams[index] = stream

            if auto_start and not stream.active:
                stream.start()

            found.append(stream.info())

        return found

    def list_cameras(self) -> list[CameraInfo]:
        with self._lock:
            return [s.info() for s in sorted(self._streams.values(), key=lambda s: s.index)]

    def get(self, index: int) -> CameraStream | None:
        with self._lock:
            return self._streams.get(index)

    def start(self, index: int) -> CameraInfo | None:
        stream = self.get(index)
        if stream is None:
            return None
        stream.start()
        return stream.info()

    def stop(self, index: int) -> CameraInfo | None:
        stream = self.get(index)
        if stream is None:
            return None
        stream.stop()
        return stream.info()

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
        for stream in streams:
            stream.stop()

    def _probe_name(self, index: int) -> str | None:
        # If already known and was valid, keep it
        existing = self.get(index)
        if existing and (existing.active or existing.error is None):
            if existing.active or index in self._streams:
                # Re-probe only if not yet opened successfully
                pass

        cap = CameraStream._open_capture(index)
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            return None

        # Confirm a frame can be read (some indices open but are dead)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None

        return f"Camera {index}"
