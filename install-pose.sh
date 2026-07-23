#!/usr/bin/env bash
# video-stream · optional pose-estimation add-on
#
# MediaPipe drags in the GUI build of OpenCV, which fights the headless build the
# rig relies on (and reintroduces the libGL error on headless Linux). This script
# installs MediaPipe and then puts opencv-python-headless back so it owns `cv2`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${ROOT}/.venv"
PY="${VENV}/bin/python"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '  → %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

bold "video-stream · pose add-on"
echo

[[ -x "${PY}" ]] || die "venv not found. Run ./install.sh first."

info "Installing MediaPipe"
"${PY}" -m pip install mediapipe -q

info "Removing the GUI OpenCV builds MediaPipe pulled in"
"${PY}" -m pip uninstall -y opencv-contrib-python opencv-python -q >/dev/null 2>&1 || true

info "Restoring headless OpenCV (so it owns cv2)"
"${PY}" -m pip install "opencv-python-headless>=4.10.0" --force-reinstall --no-deps -q

info "Verifying"
"${PY}" - <<'PY'
import cv2, mediapipe
print(f"  cv2 {cv2.__version__} · mediapipe {mediapipe.__version__}")
from mediapipe.tasks.python.vision import PoseLandmarker  # noqa: F401
PY

ok "pose add-on ready"
echo
echo "  Try it:"
echo "    video-stream --pose"
echo
echo "  The pose model (~5 MB) downloads once on first use."
