#!/usr/bin/env bash
# Detection des capacites de decodage (Raspberry Pi / V4L2) -> capabilities.json.
# Pendant runtime de player-rpi/install.sh, sans root. Le player lit capabilities.json
# au boot et le remonte ; la commande 'detect_capabilities' (CMS) relance ce script.
#
# Usage : bash detect-capabilities.sh [chemin/capabilities.json]
# Sans argument -> ~/.config/<package.json name>/capabilities.json (userData Electron).
# Override : SF_HW_DECODE=auto|on|off (off = pas de HW).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAYER_DIR="$(dirname "$SCRIPT_DIR")"
SF_HW_DECODE="${SF_HW_DECODE:-auto}"

OUT="${1:-}"
if [ -z "$OUT" ]; then
  APP_NAME="$(grep -m1 '"name"' "$PLAYER_DIR/package.json" 2>/dev/null | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
  [ -z "$APP_NAME" ] && APP_NAME="signflow-player-rpi"
  OUT="$HOME/.config/$APP_NAME/capabilities.json"
fi
mkdir -p "$(dirname "$OUT")"

# Sur RPi, le decodage V4L2 est pousse par cage (--use-v4l2-codec --use-gl=angle --use-angle=gles) -> actif.
RPI_METHOD="v4l2"; RPI_ACTIVE="true"
if [ "$SF_HW_DECODE" = "off" ]; then RPI_METHOD="none"; RPI_ACTIVE="false"; fi
# /dev/video* absent -> pas de V4L2 (cas rare, ex. headless mal configure).
ls /dev/video* >/dev/null 2>&1 || { RPI_METHOD="none"; RPI_ACTIVE="false"; }
[ "$SF_HW_DECODE" = "on" ] && RPI_ACTIVE="true"

SF_CAP_METHOD="$RPI_METHOD" SF_CAP_ACTIVE="$RPI_ACTIVE" SF_CAP_MODE="$SF_HW_DECODE" \
python3 - "$OUT" <<'PYCAP'
import json, os, sys, datetime
method = os.environ.get("SF_CAP_METHOD", "v4l2")
data = {
    "gpu_vendor": "broadcom",
    "gpu_model": "VideoCore (Raspberry Pi)",
    "hw_decode": method,
    "hw_decode_active": os.environ.get("SF_CAP_ACTIVE") == "true",
    "codecs": [],  # dependant du modele (Pi4 H.264+HEVC, Pi5 HEVC HW) -> a confirmer terrain
    "gl_backend": "angle-gles",
    "sf_hw_decode": os.environ.get("SF_CAP_MODE", "auto"),
    "chromium_flags": [],  # deja sur la CLI cage
    "detection_source": "manual",
    "detected_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(sys.argv[1], "w") as f:
    json.dump(data, f, indent=2)
print("[detect] %s -> broadcom method=%s active=%s" % (sys.argv[1], data["hw_decode"], data["hw_decode_active"]))
PYCAP
