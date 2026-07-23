"""Smart Zoom: the eased virtual-camera crop baked into the capture loop."""

import numpy as np

from video_stream.camera import ZoomState
from video_stream.obs import _parse_replay_path


def frame(w=640, h=360):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_idle_zoom_returns_the_frame_untouched():
    z = ZoomState()
    f = frame()
    assert z.apply(f) is f


def test_set_target_clamps_inputs():
    z = ZoomState()
    z.set_target(-0.5, 1.5, 99.0)
    assert z.tx == 0.0
    assert z.ty == 1.0
    assert z.tzoom == ZoomState.MAX_ZOOM


def test_apply_eases_toward_target_and_keeps_size():
    z = ZoomState()
    z.set_target(0.5, 0.5, 2.0)
    f = frame()
    out = None
    for _ in range(60):  # ~2s at 30fps: plenty to converge
        out = z.apply(f)
    assert out.shape == f.shape
    assert abs(z.zoom - 2.0) < 0.05


def test_reset_eases_back_out_then_goes_idle():
    z = ZoomState()
    z.set_target(0.2, 0.8, 2.0)
    f = frame()
    for _ in range(60):
        z.apply(f)
    z.reset()
    for _ in range(120):
        z.apply(f)
    assert z.idle
    # Idle apply snaps fully home and passes the frame through.
    assert z.apply(f) is f
    assert (z.cx, z.cy, z.zoom) == (0.5, 0.5, 1.0)


def test_crop_window_stays_inside_the_frame_at_edges():
    z = ZoomState()
    z.set_target(0.0, 0.0, 3.0)  # aim at the very corner
    f = frame()
    for _ in range(120):
        out = z.apply(f)
        assert out.shape == f.shape  # never a short/empty crop


def test_to_frame_coords_maps_view_clicks_when_zoomed():
    z = ZoomState()
    z.set_target(0.5, 0.5, 2.0)
    f = frame()
    for _ in range(120):
        z.apply(f)
    # Centered 2x crop: the view's top-left corner is frame (0.25, 0.25).
    fx, fy = z.to_frame_coords(0.0, 0.0)
    assert abs(fx - 0.25) < 0.02
    assert abs(fy - 0.25) < 0.02
    # The view center maps back to the frame center.
    cx, cy = z.to_frame_coords(0.5, 0.5)
    assert abs(cx - 0.5) < 0.02
    assert abs(cy - 0.5) < 0.02


def test_to_frame_coords_is_identity_when_idle():
    z = ZoomState()
    assert z.to_frame_coords(0.3, 0.9) == (0.3, 0.9)


def test_parse_replay_path_probes_known_keys():
    assert _parse_replay_path({"savedReplayPath": "/tmp/replay.mkv"}) == "/tmp/replay.mkv"
    assert _parse_replay_path({"outputPath": "  /tmp/r2.mkv  "}) == "/tmp/r2.mkv"
    assert _parse_replay_path({"savedReplayPath": "", "path": "/tmp/r3.mkv"}) == "/tmp/r3.mkv"
    assert _parse_replay_path({"unrelated": 1}) is None
