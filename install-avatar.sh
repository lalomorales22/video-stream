#!/usr/bin/env bash
# video-stream · avatar (Path B) asset installer
#
# Downloads the browser libraries + models the /avatar VTuber page needs, into
# static/vendor and static/models (both gitignored). Everything is vendored so the
# avatar runs offline once installed — no CDN dependency mid-stream.
#
# Re-run anytime to refresh. Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="${ROOT}/video_stream/static/vendor"
MODELS="${ROOT}/video_stream/static/models"

THREE=0.170.0        # three-vrm v3 needs three >= 0.154
MP=0.10.20           # @mediapipe/tasks-vision

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '  → %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# fetch <dest> <url>
fetch() {
  local dest="$1" url="$2"
  mkdir -p "$(dirname "${dest}")"
  local code
  code="$(curl -sL -o "${dest}" "${url}" -w '%{http_code}')"
  [[ "${code}" == "200" ]] || die "download failed (${code}): ${url}"
}

command -v curl >/dev/null 2>&1 || die "curl is required"

bold "video-stream · avatar assets"
echo

info "three.js ${THREE}"
fetch "${VENDOR}/three.module.js"                    "https://unpkg.com/three@${THREE}/build/three.module.js"
fetch "${VENDOR}/jsm/loaders/GLTFLoader.js"          "https://unpkg.com/three@${THREE}/examples/jsm/loaders/GLTFLoader.js"
fetch "${VENDOR}/jsm/utils/BufferGeometryUtils.js"   "https://unpkg.com/three@${THREE}/examples/jsm/utils/BufferGeometryUtils.js"
ok "three.js"

info "@pixiv/three-vrm v3"
fetch "${VENDOR}/three-vrm.module.js"                "https://unpkg.com/@pixiv/three-vrm@3/lib/three-vrm.module.js"
ok "three-vrm"

info "kalidokit"
fetch "${VENDOR}/kalidokit.es.js"                    "https://unpkg.com/kalidokit@1.1/dist/kalidokit.es.js"
ok "kalidokit"

info "@mediapipe/tasks-vision ${MP} (+ WASM runtime, ~20 MB)"
fetch "${VENDOR}/mediapipe/vision_bundle.mjs"        "https://unpkg.com/@mediapipe/tasks-vision@${MP}/vision_bundle.mjs"
for f in vision_wasm_internal.js vision_wasm_internal.wasm vision_wasm_nosimd_internal.js vision_wasm_nosimd_internal.wasm; do
  fetch "${VENDOR}/mediapipe/wasm/${f}"              "https://unpkg.com/@mediapipe/tasks-vision@${MP}/wasm/${f}"
done
ok "mediapipe"

info "face landmarker model (blendshapes, ~3.7 MB)"
fetch "${MODELS}/face_landmarker.task" \
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
ok "face model"

echo
if [[ -f "${MODELS}/avatar.vrm" ]]; then
  ok "default avatar present ($(du -h "${MODELS}/avatar.vrm" | cut -f1))"
else
  info "no default avatar yet — drop a .vrm at:"
  printf '      %s\n' "${MODELS}/avatar.vrm"
  info "or upload one from the /avatar page. Get avatars at vroid.com / vroidhub or make your own."
fi

echo
bold "Done."
echo
echo "  Start the app, then open:"
echo "    http://<this-machine>:8765/avatar"
echo
echo "  Add that URL as an OBS Browser Source (transparent) to composite your avatar."
