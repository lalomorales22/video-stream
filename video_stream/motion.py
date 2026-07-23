"""Cheap per-camera motion scoring (pure OpenCV, no ML).

Used by the auto-director to decide which camera is "active". Deliberately tiny:
downscale to grayscale, take the absolute difference from the previous frame, and
report the fraction of the frame that changed. Runs in the capture loop, so it must
stay cheap — a few hundred microseconds per frame.
"""

from __future__ import annotations

import cv2
import numpy as np


class MotionScorer:
    """Rolling motion score in [0, 1] from frame-to-frame differences."""

    def __init__(self, sample_width: int = 160, threshold: int = 18, smoothing: float = 0.5) -> None:
        self.sample_width = sample_width
        self.threshold = threshold  # per-pixel intensity delta counted as "moved"
        self.smoothing = smoothing  # EMA factor; higher = steadier, slower to react
        self._prev: np.ndarray | None = None
        self.score = 0.0

    def update(self, frame: np.ndarray) -> float:
        h, w = frame.shape[:2]
        if w > self.sample_width:
            scale = self.sample_width / float(w)
            small = cv2.resize(frame, (self.sample_width, max(1, int(h * scale))))
        else:
            small = frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray
            return self.score

        delta = cv2.absdiff(gray, self._prev)
        self._prev = gray
        moved = float(np.count_nonzero(delta > self.threshold)) / delta.size

        # Exponential moving average so a single twitchy frame doesn't win.
        self.score = self.smoothing * self.score + (1.0 - self.smoothing) * moved
        return self.score
