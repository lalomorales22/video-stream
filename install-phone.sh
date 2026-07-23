#!/usr/bin/env bash
# Phone-as-camera setup: generate the self-signed cert the HTTPS phone pages
# need (phones only allow camera access on secure pages). Run once, then
# restart video-stream — it picks the cert up automatically.
set -euo pipefail

CONFIG_DIR="${VIDEO_STREAM_CONFIG:-$HOME/.config/video-stream}"
CERT_DIR="$CONFIG_DIR/certs"
mkdir -p "$CERT_DIR"

LAN_IP="$(python3 - <<'PY'
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
    s.close()
except OSError:
    print("127.0.0.1")
PY
)"

echo "Generating a self-signed certificate for $LAN_IP …"
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
  -subj "/CN=video-stream" \
  -addext "subjectAltName=IP:$LAN_IP,IP:127.0.0.1,DNS:localhost" >/dev/null 2>&1
chmod 600 "$CERT_DIR/key.pem"

echo
echo "  ✓ Certificate written to $CERT_DIR"
echo
echo "  Restart video-stream, then click '📱 Add phone' on the dashboard."
echo "  Your phone will warn about the self-signed certificate once —"
echo "  tap Advanced → Proceed. (If your LAN IP changes, re-run this script.)"
