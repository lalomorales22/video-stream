#!/usr/bin/env bash
# video-stream · auto-rig add-on check
#
# Auto-rigging (raw .glb → drivable VRM) runs in headless Blender with the VRM
# add-on. Neither is something an installer should put on your machine behind
# your back — Blender is a 400 MB desktop app and the add-on lives in *your*
# Blender preferences — so this script checks what's there and tells you
# exactly what's missing. Nothing here downloads or installs anything.
#
# Re-run anytime. Exits 0 even when Blender is missing: a rig-less install is a
# perfectly good install, it just can't auto-rig.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${ROOT}/video_stream/blender/autorig_blender.py"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
info() { printf '  → %s\n' "$*"; }

bold "video-stream · auto-rig"
echo

find_blender() {
  if [[ -n "${VIDEO_STREAM_BLENDER:-}" && -x "${VIDEO_STREAM_BLENDER}" ]]; then
    printf '%s' "${VIDEO_STREAM_BLENDER}"; return 0
  fi
  if command -v blender >/dev/null 2>&1; then
    command -v blender; return 0
  fi
  local candidate
  for candidate in \
    "/Applications/Blender.app/Contents/MacOS/Blender" \
    "${HOME}/Applications/Blender.app/Contents/MacOS/Blender" \
    "/usr/bin/blender" \
    "/usr/local/bin/blender" \
    "/var/lib/flatpak/exports/bin/org.blender.Blender" \
    "${HOME}/.local/share/flatpak/exports/bin/org.blender.Blender" \
    "/snap/bin/blender"; do
    [[ -x "${candidate}" ]] && { printf '%s' "${candidate}"; return 0; }
  done
  return 1
}

if ! BLENDER="$(find_blender)"; then
  warn "Blender not found"
  echo
  info "Install it from https://www.blender.org/download (or your package manager:"
  info "  Arch/Omarchy: sudo pacman -S blender · Debian/Ubuntu: sudo apt install blender)"
  info "Then re-run ./install-rig.sh."
  info "Keep it somewhere unusual? Point VIDEO_STREAM_BLENDER at the binary."
  echo
  info "Everything else still works — you just can't auto-rig a raw model yet."
  exit 0
fi
ok "Blender: ${BLENDER}"

# Ask Blender itself, rather than guessing from the extensions directory: the
# add-on can be installed per-user, system-wide, or as a legacy add-on, and
# only Blender knows which of those actually loads.
RESULT="$("${BLENDER}" --background --python "${SCRIPT}" -- --verb probe 2>/dev/null | grep '^RIGRESULT>' | tail -1 || true)"
if [[ "${RESULT}" == *'"vrm_addon": true'* ]]; then
  VERSION="$(printf '%s' "${RESULT}" | sed -n 's/.*"version": "\([^"]*\)".*/\1/p')"
  ADDON="$(printf '%s' "${RESULT}" | sed -n 's/.*"vrm_addon_version": "\([^"]*\)".*/\1/p')"
  ok "Blender ${VERSION} with VRM add-on ${ADDON}"
  echo
  bold "Ready."
  echo
  echo "  Open the avatar studio and press Auto-rig:"
  echo "    http://<this-machine>:8765/avatar"
  echo
  echo "  Drop in a .glb (or .gltf / .fbx) and you get back a VRM with a"
  echo "  skeleton, skin weights, and blink/mouth shapes — about a minute."
  exit 0
fi

warn "Blender is here, but its VRM add-on isn't"
echo
info "In Blender: Edit → Preferences → Get Extensions → search \"VRM\""
info "  → install \"VRM Add-on for Blender\", then re-run ./install-rig.sh"
echo
info "Auto-rig stays switched off until then; the rest of the studio is unaffected."
exit 0
