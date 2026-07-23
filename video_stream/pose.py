"""Optional MediaPipe pose-estimation overlay.

This module is deliberately isolated and imports MediaPipe lazily, so the core
rig install stays light and Linux-clean. MediaPipe is only pulled in when a
camera is actually asked to draw a skeleton (``--pose``).

Design notes
------------
* One :class:`PoseOverlay` instance per camera stream. The MediaPipe landmarker
  is stateful and not safe to share across threads, and every ``CameraStream``
  runs its own capture thread, so each builds and owns its own overlay.
* Inference runs on every ``stride``-th frame; the most recent skeleton is
  redrawn on the frames in between. That keeps CPU sane for a multi-camera rig
  while the overlay still appears on every streamed frame.
* The model file (~5–30 MB depending on variant) is downloaded once to a local
  cache on first use. Nothing about the network stream changes — the skeleton is
  drawn straight onto the frame before JPEG encoding, so it flows through to OBS
  with no OBS-side configuration.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import cv2
import numpy as np

# Model variants, cheapest → most accurate. "lite" is the right default for a
# real-time multi-camera rig; "heavy" is noticeably slower.
_MODEL_NAMES = {
    "lite": "pose_landmarker_lite",
    "full": "pose_landmarker_full",
    "heavy": "pose_landmarker_heavy",
}
_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "{name}/float16/latest/{name}.task"
)
_CACHE_DIR = Path(
    os.environ.get("VIDEO_STREAM_CACHE", Path.home() / ".cache" / "video-stream")
)

_INSTALL_HINT = (
    "Pose estimation needs MediaPipe, which isn't installed.\n"
    "  Install it (and keep the headless OpenCV the rig relies on):\n"
    "    pip install mediapipe\n"
    "    pip uninstall -y opencv-contrib-python opencv-python\n"
    "    pip install --force-reinstall opencv-python-headless\n"
    "  Or use the helper:  ./install-pose.sh"
)

# Skeleton style (BGR).
_LINE_COLOR = (120, 255, 0)
_POINT_COLOR = (0, 90, 255)
_MIN_VISIBILITY = 0.5


class PoseUnavailable(RuntimeError):
    """Raised when pose is requested but MediaPipe can't be loaded."""


def _ensure_model(variant: str) -> Path:
    """Return a local path to the requested model, downloading it once if needed."""
    name = _MODEL_NAMES[variant]
    dest = _CACHE_DIR / f"{name}.task"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url = _MODEL_URL.format(name=name)
    tmp = dest.with_name(dest.name + ".part")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    except Exception as exc:  # network, disk, etc.
        tmp.unlink(missing_ok=True)
        raise PoseUnavailable(f"Could not download pose model from {url}: {exc}") from exc
    return dest


class PoseOverlay:
    """Draws a live pose skeleton onto BGR frames from one camera."""

    def __init__(
        self,
        variant: str = "lite",
        stride: int = 2,
        fps: float = 30.0,
    ) -> None:
        if variant not in _MODEL_NAMES:
            variant = "lite"
        self.variant = variant
        self.stride = max(1, int(stride))

        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python.vision import (
                PoseLandmarker,
                PoseLandmarkerOptions,
                PoseLandmarksConnections,
                RunningMode,
            )
        except ImportError as exc:
            raise PoseUnavailable(_INSTALL_HINT) from exc

        self._mp = mp
        self._connections = PoseLandmarksConnections.POSE_LANDMARKS

        model_path = _ensure_model(variant)
        options = PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=RunningMode.VIDEO,
        )
        self._landmarker = PoseLandmarker.create_from_options(options)

        self._frame_index = 0
        self._ts_ms = 0
        self._ts_step = max(1, int(1000.0 / max(1.0, fps)))
        self._last_landmarks: list | None = None

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Detect (every ``stride`` frames) and draw the latest skeleton in place."""
        run_now = self._frame_index % self.stride == 0
        self._frame_index += 1

        if run_now:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = self._mp.Image(
                image_format=self._mp.ImageFormat.SRGB, data=rgb
            )
            self._ts_ms += self._ts_step
            try:
                result = self._landmarker.detect_for_video(mp_image, self._ts_ms)
                self._last_landmarks = (
                    result.pose_landmarks[0] if result.pose_landmarks else None
                )
            except Exception:
                # A single bad frame should never take the stream down.
                self._last_landmarks = None

        if self._last_landmarks:
            self._draw(frame, self._last_landmarks)
        return frame

    def _draw(self, frame: np.ndarray, landmarks: list) -> None:
        h, w = frame.shape[:2]

        def visible(lm) -> bool:
            # Older/newer builds may omit visibility; treat missing as visible.
            return getattr(lm, "visibility", 1.0) >= _MIN_VISIBILITY

        for conn in self._connections:
            a, b = landmarks[conn.start], landmarks[conn.end]
            if not (visible(a) and visible(b)):
                continue
            cv2.line(
                frame,
                (int(a.x * w), int(a.y * h)),
                (int(b.x * w), int(b.y * h)),
                _LINE_COLOR,
                3,
                cv2.LINE_AA,
            )
        for lm in landmarks:
            if not visible(lm):
                continue
            cv2.circle(frame, (int(lm.x * w), int(lm.y * h)), 4, _POINT_COLOR, -1, cv2.LINE_AA)

    def focus_point(self) -> tuple[float, float] | None:
        """Normalized (x, y) where a punch-in should aim: the nose pulled toward
        the shoulder midpoint, so the crop frames head + shoulders, not just
        the face. None when no body is currently tracked."""
        landmarks = self._last_landmarks
        if not landmarks or len(landmarks) < 13:
            return None
        nose, l_shoulder, r_shoulder = landmarks[0], landmarks[11], landmarks[12]
        x = (nose.x + (l_shoulder.x + r_shoulder.x) / 2) / 2
        y = (nose.y + (l_shoulder.y + r_shoulder.y) / 2) / 2
        return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))

    def close(self) -> None:
        landmarker = getattr(self, "_landmarker", None)
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception:
                pass
            self._landmarker = None
