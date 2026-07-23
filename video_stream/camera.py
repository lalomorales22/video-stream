"""Camera discovery, capture, and MJPEG frame generation."""

from __future__ import annotations

import asyncio
import platform
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Generator

import cv2
import numpy as np

_V4L_SYS = Path("/sys/class/video4linux")
_DEV_NODE_RE = re.compile(r"^video(\d+)$")


def _candidate_indices(max_probe: int) -> tuple[list[int], bool]:
    """Indices worth probing, plus whether the list is authoritative.

    On Linux the kernel tells us exactly which capture nodes exist, so we return the
    real (often sparse) device numbers and mark them exhaustive. Elsewhere we fall back
    to a blind 0..max_probe scan, where a run of misses means the end of the list.
    """
    if platform.system() == "Linux" and _V4L_SYS.is_dir():
        indices = sorted(
            int(m.group(1))
            for entry in _V4L_SYS.iterdir()
            if (m := _DEV_NODE_RE.match(entry.name))
        )
        if indices:
            return indices, True

    return list(range(max_probe)), False


def _device_label(index: int) -> str | None:
    """Human-readable name for a Linux V4L2 device, e.g. 'Logitech BRIO'."""
    try:
        raw = (_V4L_SYS / f"video{index}" / "name").read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    # Vendors commonly repeat the model ("Logitech BRIO: Logitech BRIO").
    head, sep, tail = raw.partition(":")
    if sep and tail.strip().startswith(head.strip()):
        raw = tail.strip()
    return raw


@dataclass
class CameraInfo:
    index: int
    name: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    active: bool = False
    error: str | None = None
    pose: bool = False
    zoom: float = 1.0


class ZoomState:
    """Virtual-camera punch-in, ported from ChromaCanvas's Smart Zoom.

    The capture thread calls ``apply()`` every frame: the view eases toward a
    normalized target and the frame is cropped + rescaled, so the punch-in is
    baked into the MJPEG and reaches every OBS with zero setup. Targets may be
    set from any thread — plain float writes, read once per frame.
    """

    EASE = 0.14      # per-frame interpolation factor
    MAX_ZOOM = 3.0   # webcams get mushy past ~3x (ChromaCanvas allowed 6x on screens)
    _EPS = 0.005

    def __init__(self) -> None:
        self.cx, self.cy, self.zoom = 0.5, 0.5, 1.0    # eased view state
        self.tx, self.ty, self.tzoom = 0.5, 0.5, 1.0   # target

    def set_target(self, nx: float, ny: float, level: float) -> None:
        self.tx = min(1.0, max(0.0, nx))
        self.ty = min(1.0, max(0.0, ny))
        self.tzoom = min(self.MAX_ZOOM, max(1.0, level))

    def reset(self) -> None:
        self.tx, self.ty, self.tzoom = 0.5, 0.5, 1.0

    def to_frame_coords(self, nx: float, ny: float) -> tuple[float, float]:
        """Map a normalized point in the *streamed* (possibly zoomed) view back
        to raw-frame coordinates. Clicks and pose landmarks live in view space
        — targets must be set in frame space, or re-aims while zoomed drift."""
        if self.idle:
            return nx, ny
        sw = 1.0 / self.zoom
        sh = 1.0 / self.zoom
        sx = max(0.0, min(1.0 - sw, self.cx - sw / 2))
        sy = max(0.0, min(1.0 - sh, self.cy - sh / 2))
        return (sx + nx * sw, sy + ny * sh)

    @property
    def idle(self) -> bool:
        return self.tzoom <= 1.0 and abs(self.zoom - 1.0) < self._EPS

    def apply(self, frame: np.ndarray) -> np.ndarray:
        if self.idle:
            # Snap fully home so the next punch-in starts from a clean state.
            self.cx, self.cy, self.zoom = 0.5, 0.5, 1.0
            return frame
        self.cx += (self.tx - self.cx) * self.EASE
        self.cy += (self.ty - self.cy) * self.EASE
        self.zoom += (self.tzoom - self.zoom) * self.EASE

        h, w = frame.shape[:2]
        sw = w / self.zoom
        sh = h / self.zoom
        sx = max(0.0, min(w - sw, self.cx * w - sw / 2))
        sy = max(0.0, min(h - sh, self.cy * h - sh / 2))
        crop = frame[int(sy) : int(sy + sh), int(sx) : int(sx + sw)]
        if crop.size == 0:
            return frame
        return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)


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
        pose_enabled: bool = False,
        pose_variant: str = "lite",
        pose_stride: int = 2,
        motion_enabled: bool = False,
    ) -> None:
        self.index = index
        self.name = name
        self.requested_width = width
        self.requested_height = height
        self.jpeg_quality = max(40, min(95, jpeg_quality))
        self.target_fps = target_fps
        self.pose_enabled = pose_enabled
        self.pose_variant = pose_variant
        self.pose_stride = pose_stride
        self._pose = None  # built lazily on start(), owned by the capture thread
        self.motion_enabled = motion_enabled
        self.motion_score = 0.0
        self._motion = None  # MotionScorer, built on start()
        self.zoom = ZoomState()
        self._punch_timer: threading.Timer | None = None
        self._punch_generation = 0  # bumped by every arm/cancel; stale ease-backs no-op
        self._punch_lock = threading.Lock()
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
            pose=self._pose is not None,
            zoom=round(self.zoom.tzoom, 2),
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

        self._maybe_init_pose()
        if self.motion_enabled and self._motion is None:
            from video_stream.motion import MotionScorer

            self._motion = MotionScorer()

        self._cap = cap
        self._running = True
        self._error = None
        self._encode_frame(self._apply_pose(frame))

        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"camera-{self.index}",
            daemon=True,
        )
        self._thread.start()
        return True

    def focus_point(self) -> tuple[float, float] | None:
        """Normalized (x, y) anchor for auto punch-ins, from the pose overlay."""
        pose = self._pose
        if pose is None:
            return None
        try:
            return pose.focus_point()
        except Exception:
            return None

    def punch_in(self, level: float = 1.6, duration: float = 2.5) -> None:
        """Director auto-punch: tighten on the subject, then ease back out.

        Aims at the tracked face when the pose overlay is running, otherwise
        center-frame. Pose landmarks are detected on the streamed (possibly
        already-zoomed) frame, so the point is mapped back to frame space.
        The ease-back timer keeps the shot wide again before the director's
        cooldown allows the next cut.
        """
        point = self.focus_point()
        point = self.zoom.to_frame_coords(*point) if point else (0.5, 0.5)
        # Generation-tagged like replay's lower-third hide: an ease-back armed
        # here can never fire once a manual zoom (or newer punch) supersedes it.
        with self._punch_lock:
            self.zoom.set_target(point[0], point[1], level)
            self._punch_generation += 1
            generation = self._punch_generation
            if self._punch_timer is not None:
                self._punch_timer.cancel()
            self._punch_timer = threading.Timer(duration, self._ease_back, args=(generation,))
            self._punch_timer.daemon = True
            self._punch_timer.start()

    def _ease_back(self, generation: int) -> None:
        with self._punch_lock:
            if generation != self._punch_generation:
                return  # superseded by a manual zoom or a newer punch
            self.zoom.reset()

    def set_manual_zoom(self, nx: float, ny: float, level: float) -> None:
        """Operator zoom from the dashboard. Cancels any pending auto-punch
        ease-back so the director can't silently revert a manual shot, and
        maps the clicked point (view space) into frame space."""
        self._cancel_punch_timer()
        if level <= 1.0:
            self.zoom.reset()
            return
        fx, fy = self.zoom.to_frame_coords(nx, ny)
        self.zoom.set_target(fx, fy, level)

    def _cancel_punch_timer(self) -> None:
        with self._punch_lock:
            self._punch_generation += 1  # anything already armed is now stale
            if self._punch_timer is not None:
                self._punch_timer.cancel()
                self._punch_timer = None

    def stop(self) -> None:
        self._running = False
        self._cancel_punch_timer()
        self.zoom.reset()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._pose is not None:
            self._pose.close()
            self._pose = None
        with self._lock:
            self._frame = None
            self._jpeg = None

    def _maybe_init_pose(self) -> None:
        """Build the pose overlay for this stream if enabled. Never fatal."""
        if not self.pose_enabled or self._pose is not None:
            return
        try:
            from video_stream.pose import PoseOverlay, PoseUnavailable
        except Exception:
            return
        try:
            self._pose = PoseOverlay(
                variant=self.pose_variant,
                stride=self.pose_stride,
                fps=self.target_fps,
            )
        except PoseUnavailable as exc:
            # Degrade to a plain stream rather than taking the camera down.
            print(f"[pose] camera {self.index}: disabled — {exc}")
            self._pose = None
            self.pose_enabled = False

    def _apply_pose(self, frame: np.ndarray) -> np.ndarray:
        if self._pose is None:
            return frame
        try:
            return self._pose.process(frame)
        except Exception:
            return frame

    def set_pose(self, enabled: bool) -> None:
        """Turn the pose overlay on/off while the stream is running.

        Raises PoseUnavailable if MediaPipe can't be loaded, so callers can
        surface a clear message.
        """
        if not enabled:
            self.pose_enabled = False
            pose, self._pose = self._pose, None
            if pose is not None:
                pose.close()
            return
        self.pose_enabled = True
        if self._pose is not None or not self._running:
            return  # already on, or will be built on start()
        from video_stream.pose import PoseOverlay

        self._pose = PoseOverlay(
            variant=self.pose_variant, stride=self.pose_stride, fps=self.target_fps
        )

    def set_motion(self, enabled: bool) -> None:
        """Turn cheap motion scoring on/off (used by the auto-director)."""
        self.motion_enabled = enabled
        if not enabled:
            self._motion = None
            self.motion_score = 0.0
            return
        if self._motion is None and self._running:
            from video_stream.motion import MotionScorer

            self._motion = MotionScorer()

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

            # Order matters here: motion is scored on the raw full frame (so the
            # director and auto-replay judge the real room, not the crop), the
            # punch-in zoom is applied next, and pose runs last so the skeleton
            # is detected and drawn in the same zoomed space the viewer sees.
            motion = self._motion  # local ref: set_motion(False) races this read
            if motion is not None:
                self.motion_score = motion.update(frame)

            frame = self.zoom.apply(frame)
            self._encode_frame(self._apply_pose(frame))
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

    async def mjpeg_async(self) -> AsyncGenerator[bytes, None]:
        """Async variant of mjpeg_generator.

        Frames are encoded on the capture thread; here we only read pre-encoded
        bytes and ``await asyncio.sleep`` between them. That means a live stream
        never holds a worker thread and never blocks the event loop, so the UI
        and other requests stay responsive even with several streams open.
        """
        boundary = b"frame"
        with self._clients_lock:
            self._clients += 1
            self.stats.clients = self._clients
        try:
            last: bytes | None = None
            while self._running:
                jpeg = self.get_jpeg()
                if jpeg is None:
                    await asyncio.sleep(0.05)
                    continue
                if jpeg is last:
                    await asyncio.sleep(0.01)
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
        elif system == "Linux":
            # V4L2 explicitly: the default backend can pick GStreamer, which is far
            # pickier about pixel formats on plain UVC webcams.
            cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
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
        pose_enabled: bool = False,
        pose_variant: str = "lite",
        pose_stride: int = 2,
        motion_enabled: bool = False,
    ) -> None:
        self.max_probe = max_probe
        self.width = width
        self.height = height
        self.jpeg_quality = jpeg_quality
        self.target_fps = target_fps
        self.pose_enabled = pose_enabled
        self.pose_variant = pose_variant
        self.pose_stride = pose_stride
        self.motion_enabled = motion_enabled
        self._streams: dict[int, CameraStream] = {}
        self._lock = threading.Lock()
        self._closed = False

    def discover(self, auto_start: bool = True) -> list[CameraInfo]:
        candidates, exhaustive = _candidate_indices(self.max_probe)
        found: list[CameraInfo] = []
        misses = 0
        for index in candidates:
            if self._closed:
                break  # teardown ran mid-scan; don't reopen released devices
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
                # When the candidate list came from real device nodes it is short and
                # authoritative, so probe all of it. Gaps are normal there: on Linux each
                # UVC camera also registers a metadata node that opens but yields no
                # frames, which would otherwise cut the scan short and hide later cameras.
                if not exhaustive:
                    # Blind probe of a fixed range; indices are dense, so a run of
                    # misses means we are past the end.
                    if found and misses >= 2:
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
                pose_enabled=self.pose_enabled,
                pose_variant=self.pose_variant,
                pose_stride=self.pose_stride,
                motion_enabled=self.motion_enabled,
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

    def shutdown(self) -> None:
        """stop_all for process teardown: also cancels an in-flight discover
        so a background scan can't reopen devices we just released."""
        self._closed = True
        self.stop_all()

    def set_motion_all(self, enabled: bool) -> None:
        """Enable/disable motion scoring on every stream (for the director)."""
        self.motion_enabled = enabled
        with self._lock:
            streams = list(self._streams.values())
        for stream in streams:
            stream.set_motion(enabled)

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

        # Confirm a frame can be read. Some nodes open but never yield frames —
        # notably the metadata node every Linux UVC camera registers alongside
        # its capture node.
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None

        return _device_label(index) or f"Camera {index}"
