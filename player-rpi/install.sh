#!/usr/bin/env bash
# install.sh -- Vitrail Player installation on Raspberry Pi OS Trixie (arm64)
# Tested on: Raspberry Pi 5, RPi OS Trixie (Debian 13), Wayland/labwc-pi
# Display: cage kiosk Wayland compositor (lightdm user-session=signflow-player)
#
# Usage: sudo bash install.sh [username] [--overlay] [--watchdog]
#
# NOTE: if this file was created on Windows, strip CRLF before running:
#   sed -i 's/\r//' install.sh

set -euo pipefail

PLAYER_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_USER="${1:-signflow}"
ENABLE_OVERLAY=false
ENABLE_WATCHDOG=false
# Wayland output name (wlr-randr to list). Leave empty to skip resolution forcing.
WLR_OUTPUT="${WLR_OUTPUT:-}"
WLR_MODE="${WLR_MODE:-1920x1080@60}"

for arg in "$@"; do
  case "$arg" in
    --overlay)  ENABLE_OVERLAY=true  ;;
    --watchdog) ENABLE_WATCHDOG=true ;;
  esac
done

# ⚠️ Le répertoire de config n'est PAS libre : Electron lit `app.getPath('userData')`,
# c'est-à-dire ~/.config/<name de package.json> — ici « signflow-player-rpi ». Ecrire dans
# « signflow-player » (ce que faisait cette ligne) crée un dossier que le player n'ouvre
# JAMAIS : mesuré le 2026-08-09, les deux répertoires coexistaient sur la machine et le
# player affichait « Setup required » avec sa config à côté. On dérive donc le nom au lieu
# de le réécrire — deux définitions finissent toujours par diverger.
APP_NAME="$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLAYER_DIR/package.json" | head -1)"
[[ -n "$APP_NAME" ]] || { echo "Error: nom d'application illisible dans package.json" >&2; exit 1; }
CONFIG_DIR="/home/$SERVICE_USER/.config/$APP_NAME"
START_SCRIPT="/home/$SERVICE_USER/start-player.sh"
LOG_FILE="/var/log/signflow-player.log"
WAYLAND_SESSIONS_DIR="/usr/share/wayland-sessions"

# Detect boot config path (RPi 5 uses /boot/firmware/, RPi 4 uses /boot/)
if [[ -f /boot/firmware/config.txt ]]; then
  BOOT_CONFIG=/boot/firmware/config.txt
else
  BOOT_CONFIG=/boot/config.txt
fi

echo "=== Vitrail Player -- Raspberry Pi OS Trixie (Wayland) ==="
echo "    Player directory : $PLAYER_DIR"
echo "    User             : $SERVICE_USER"
echo "    Overlay rootfs   : $ENABLE_OVERLAY"
echo "    Kernel watchdog  : $ENABLE_WATCHDOG"
echo ""

if [[ $EUID -ne 0 ]]; then
  echo "Error: run with sudo  (sudo bash install.sh)" >&2
  exit 1
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "armv7l" ]]; then
  echo "Warning: unexpected architecture '$ARCH' -- this script targets Raspberry Pi OS (arm64/armhf)."
fi

# ── 1. Node.js 22 LTS ─────────────────────────────────────────────────────────
if ! command -v node &>/dev/null || \
   [ "$(node -e 'console.log(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)" -lt 22 ]; then
  echo "[1/7] Installing Node.js 22 LTS..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
else
  echo "[1/7] Node.js $(node --version) already present."
fi

# ── 2. System dependencies ────────────────────────────────────────────────────
# RPi OS Trixie (Debian 13) uses Wayland by default (labwc-pi compositor).
# cage is a minimal Wayland kiosk compositor that forces a single app fullscreen.
# No X11 required -- Electron uses --ozone-platform=wayland natively.
echo "[2/7] Installing system dependencies..."
apt-get install -y --no-install-recommends \
  cage \
  lightdm \
  logrotate \
  wlr-randr \
  v4l-utils \
  libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libxkbcommon0 libxcomposite1 libxrandr2 libgtk-3-0 \
  libdrm2 libgbm1 libasound2

# ── 3. npm install ────────────────────────────────────────────────────────────
echo "[3/7] Installing npm dependencies (ARM64)..."
cd "$PLAYER_DIR"
npm install --omit=dev
# Le moteur Electron appartient au compte du player (vitrail#5, décision D7) : c'est le player
# lui-même qui le remplace depuis le serveur, sans aucun chemin vers root. Aucun privilège
# nouveau : ce compte réécrit déjà `src/` à chaque mise à jour du code.
chown -R "$SERVICE_USER:$SERVICE_USER" "$PLAYER_DIR/node_modules"

# ── 4. User + autologin ───────────────────────────────────────────────────────
echo "[4/7] Configuring user and autologin..."

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$SERVICE_USER"
  echo "    -> User '$SERVICE_USER' created."
fi

# video + render groups needed for Wayland/DRM access
usermod -aG video,render "$SERVICE_USER" 2>/dev/null || true

# lightdm autologin with cage kiosk session
# user-session=signflow-player points to the .desktop file created in step 5
mkdir -p /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/signflow.conf <<EOF
[Seat:*]
autologin-user=$SERVICE_USER
autologin-user-timeout=0
user-session=signflow-player
EOF

systemctl enable lightdm
systemctl set-default graphical.target

# ── 5. cage session + watchdog start script ───────────────────────────────────
echo "[5/7] Configuring cage session and start script..."

# Wayland session descriptor: lightdm launches cage which runs start-player.sh
# cage forces the single application fullscreen -- no extra config needed.
mkdir -p "$WAYLAND_SESSIONS_DIR"
cat > "$WAYLAND_SESSIONS_DIR/signflow-player.desktop" <<EOF
[Desktop Entry]
Name=Vitrail Player
Comment=Vitrail digital signage kiosk
Exec=cage -- $START_SCRIPT
Type=Application
EOF

# Watchdog wrapper: restarts Electron automatically on crash
cat > "$START_SCRIPT" <<EOF
#!/usr/bin/env bash
# SignFlow Player watchdog wrapper (Wayland/cage)
LOG="$LOG_FILE"

# Force display resolution (runs inside cage Wayland session).
# WLR_OUTPUT: set at install time (e.g. WLR_OUTPUT=HDMI-A-2 sudo bash install.sh).
# If empty, auto-detects the first connected output via wlr-randr.
# WLR_MODE: target resolution (default: 1920x1080@60 = FHD).
WLR_OUTPUT="${WLR_OUTPUT}"
WLR_MODE="${WLR_MODE:-1920x1080@60}"

# ── Resolution guard (recover FHD on HDMI hotplug) ───────────────────────────
# Turning the display off then back on triggers an HDMI hotplug; wlroots (cage)
# reverts to the monitor's preferred mode (often 4K@30). The compositor grows
# the Electron window to 4K (playback stutters). Simply re-forcing the output
# mode back to FHD does NOT shrink the already-4K window -> only the top-left
# quarter shows. So instead we restart the player: the watchdog loop below
# re-forces FHD *before* relaunching Electron, recreating a correctly sized
# FHD window -- exactly the (working) boot path.
_target_res="\${WLR_MODE%@*}"   # 1920x1080@60 -> 1920x1080
(
  while true; do
    sleep 5
    _g_cur=\$(wlr-randr 2>/dev/null | grep -i current | grep -oiP '[0-9]+x[0-9]+' | head -1)
    if [[ -n "\$_g_cur" && "\$_g_cur" != "\$_target_res" ]]; then
      echo "[\$(date -Iseconds)] Guard: mode drifted to \$_g_cur, restarting player to recover \$WLR_MODE" >> "\$LOG"
      # Target ELECTRON only: 'SignFlow/player-rpi' also matched the engine guard
      # (src/engine_guard.js) and killed it in the middle of an engine trial (14/11).
      pkill -f '$PLAYER_DIR/node_modules/electron/dist/electron'   # watchdog re-forces FHD + relaunches
    fi
  done
) &

# Engine trial interrupted by a power cut (vitrail#5): repair an unfinished switch and re-arm the
# rollback guard BEFORE the first Electron start. Once per session, never blocks the launch.
node "$PLAYER_DIR/src/engine_guard.js" boot-check --app-root "$PLAYER_DIR" --platform rpi >> "\$LOG" 2>&1 || true

while true; do
  # Auto-detect output if WLR_OUTPUT not set at install time
  _wlr_out="\$WLR_OUTPUT"
  if [[ -z "\$_wlr_out" ]]; then
    _wlr_out=\$(wlr-randr 2>/dev/null | grep -oP '^\S+' | head -1)
    [[ -n "\$_wlr_out" ]] && echo "[\$(date -Iseconds)] Auto-detected output: \$_wlr_out" >> "\$LOG"
  fi

  if [[ -n "\$_wlr_out" ]]; then
    # Retry up to 5 times: cage may not be fully ready immediately after session start
    for _i in 1 2 3 4 5; do
      if wlr-randr --output "\$_wlr_out" --mode "\$WLR_MODE" >> "\$LOG" 2>&1; then
        echo "[\$(date -Iseconds)] Resolution: \$_wlr_out \$WLR_MODE (attempt \$_i)" >> "\$LOG"
        break
      fi
      echo "[\$(date -Iseconds)] wlr-randr attempt \$_i failed, retrying in 1s..." >> "\$LOG"
      sleep 1
    done
  else
    echo "[\$(date -Iseconds)] wlr-randr: no output detected, skipping resolution force" >> "\$LOG"
  fi
  sleep 1  # Let cage Wayland compositor fully initialize before Electron connects

  # ── Engine-dependent flags: read from launcher-flags.conf (issue #4) ─────────
  # GPU/GL/codec flags depend on the Electron version (Electron 42 dropped --use-gl=egl),
  # so they live in a file that can change WITH the engine instead of being frozen here at
  # install time. Read again before EVERY Electron start: a change applies at the next
  # player restart, no reinstall, no reboot.
  # Format: one flag per line, starting with "--"; lines starting with "#" and blank lines
  # are ignored; anything else is logged and skipped. No file, or no valid flag in it
  # -> the built-in defaults below.
  # Defaults = what works with Electron 42 on a RPi 5 (measured: gpu_compositing=enabled):
  #   --enable-gpu --use-gl=angle --use-angle=gles : GPU compositing/raster through ANGLE
  #     on the V3D OpenGL ES driver. The former --use-gl=egl no longer exists in Electron
  #     42, and even under Electron 30 it never worked here (GPU process exit_code=5).
  #     --disable-gpu breaks fullscreen under cage/Wayland: keep --enable-gpu.
  #   --use-v4l2-codec : video stays CPU-decoded in practice (no H.264 decoder on the
  #     RPi 5, and Chromium on Linux only uses VA-API), kept for the RPi 4.
  _flags_file="$PLAYER_DIR/launcher-flags.conf"
  _engine_flags=()
  if [[ -r "\$_flags_file" ]]; then
    while read -r _line || [[ -n "\$_line" ]]; do
      _line="\${_line%\$'\r'}"            # file edited on Windows (CRLF)
      [[ -z "\$_line" || "\$_line" == \#* ]] && continue
      if [[ "\$_line" == --?* ]]; then
        _engine_flags+=("\$_line")
      else
        echo "[\$(date -Iseconds)] Launcher flags: ignored line (not a --flag): \$_line" >> "\$LOG"
      fi
    done < "\$_flags_file"
  fi
  if (( \${#_engine_flags[@]} )); then
    _flags_src="\$_flags_file"
  else
    _engine_flags=(--enable-gpu --use-gl=angle --use-angle=gles --use-v4l2-codec)
    _flags_src="built-in defaults"
  fi
  echo "[\$(date -Iseconds)] Launcher flags (\$_flags_src): \${_engine_flags[*]}" >> "\$LOG"

  # Fixed flags, whatever the engine (the kiosk does not work without them):
  # --ozone-platform=wayland : use Wayland backend (RPi OS Trixie default)
  # --no-sandbox             : required on RPi OS (no setuid sandbox helper)
  # --disable-dev-shm-usage  : /dev/shm too small on some RPi configs
  "$PLAYER_DIR/node_modules/.bin/electron" "$PLAYER_DIR" \\
    --ozone-platform=wayland \\
    --no-sandbox \\
    "\${_engine_flags[@]}" \\
    --disable-dev-shm-usage \\
    --start-fullscreen \\
    >> "\$LOG" 2>&1
  echo "[\$(date -Iseconds)] Player exited -- restarting in 3s" >> "\$LOG"
  sleep 3
done
EOF

chmod +x "$START_SCRIPT"
chown "$SERVICE_USER:$SERVICE_USER" "$START_SCRIPT"

# Log file
touch "$LOG_FILE"
chmod 666 "$LOG_FILE"

# Rotation du log OS (le start-script redirige le stderr natif Electron/Chromium ici =
# croissance sans limite sinon, CRITIQUE sur la carte SD du RPi). `copytruncate` tronque le
# fichier EN PLACE sans redémarrer le player. Le logger applicatif interne reste borné à part.
if command -v logrotate >/dev/null 2>&1; then
  cat > /etc/logrotate.d/signflow-player <<LOGROTATE
$LOG_FILE {
    size 10M
    rotate 3
    missingok
    notifempty
    copytruncate
    compress
    delaycompress
}
LOGROTATE
  echo "    → logrotate: /etc/logrotate.d/signflow-player (10M x3, copytruncate)"
else
  echo "    ⚠ logrotate absent — installez-le pour borner $LOG_FILE"
fi

# Pare-feu LAN : si UFW est actif, autoriser les broadcasts UDP inter-players —
# 7778 = LAN sync (§III.3), 7779 = trigger VOD multi-cible (Feature B Phase 3).
if command -v ufw >/dev/null 2>&1; then
  ufw allow 7778/udp >/dev/null 2>&1 || true
  ufw allow 7779/udp >/dev/null 2>&1 || true
fi

# ── 6. Sudoers — allow player to reboot without password ─────────────────────
SUDOERS_FILE="/etc/sudoers.d/signflow-player"
echo "$SERVICE_USER ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/bin/systemctl reboot, /sbin/shutdown, /usr/local/bin/signflow-ntp-apply, /usr/bin/timedatectl set-timezone *" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"

# Polkit : autorise le player a changer le fuseau OS via timedated (commande CMS
# set_timezone). Indispensable : apres un app.relaunch() (OTA/restart), le process
# herite NoNewPrivs=1 -> sudo BLOQUE ; le chemin D-Bus/polkit, lui, fonctionne.
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/45-signflow-timezone.rules <<POLKITRULE
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.timedate1.set-timezone" && subject.user == "$SERVICE_USER") {
        return polkit.Result.YES;
    }
});
POLKITRULE
systemctl restart polkit 2>/dev/null || true

echo "[6/8] Sudoers entry added for reboot command."

# ── NTP (chrony) + helper piloté depuis l'UI (relais NTP par groupe LAN sync) ──
# Le RPi reçoit son rôle NTP depuis le CMS (display_config.ntp_role/ntp_server) :
# le player appelle `sudo signflow-ntp-apply <role> <server_ip>` sur sync.
echo "[chrony] NTP / horloge (helper piloté depuis l'UI)..."
apt-get install -y chrony
cat > /usr/local/bin/signflow-ntp-apply <<'NTPHELPER'
#!/usr/bin/env bash
set -euo pipefail
# --from-request : lit "role server" depuis /var/lib/signflow/ntp_request (ecrit par le
# player) -> chemin systemd+polkit NNP-immune (sudo est bloque apres un app.relaunch OTA,
# NoNewPrivs herite ; meme pattern que le fuseau/timedated). Args directs = mode legacy.
if [[ "${1:-}" == "--from-request" ]]; then
  REQ="/var/lib/signflow/ntp_request"
  [[ -f "$REQ" ]] || { echo "no request file: $REQ" >&2; exit 2; }
  read -r ROLE SERVER < "$REQ" || true
  ROLE="${ROLE:-none}"
  SERVER="${SERVER:-}"
else
  ROLE="${1:-none}"
  SERVER="${2:-}"
fi
CONF="/etc/chrony/chrony.conf"
[[ -f "$CONF" ]] || CONF="/etc/chrony.conf"

case "$ROLE" in server|client|none) ;; *) echo "role invalide: $ROLE" >&2; exit 2 ;; esac
if [[ "$ROLE" == "client" ]]; then
  [[ "$SERVER" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "serveur NTP invalide (IP ou nom d hote): $SERVER" >&2; exit 2; }
fi

sed -i '/# >>> SignFlow NTP/,/# <<< SignFlow NTP/d' "$CONF" 2>/dev/null || true

if [[ "$ROLE" == "server" ]]; then
  LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
  [[ -z "$LAN_IP" ]] && LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  SUBNET="$(echo "${LAN_IP:-0.0.0.0}" | awk -F. '{print $1"."$2"."$3".0/24"}')"
  { echo "# >>> SignFlow NTP"; echo "allow $SUBNET"; echo "local stratum 10"; echo "# <<< SignFlow NTP"; } >> "$CONF"
elif [[ "$ROLE" == "client" ]]; then
  { echo "# >>> SignFlow NTP"; echo "server $SERVER iburst prefer"; echo "# <<< SignFlow NTP"; } >> "$CONF"
fi

systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || true
[[ "$ROLE" == "client" ]] && chronyc -a makestep >/dev/null 2>&1 || true
echo "ntp applied: role=$ROLE server=${SERVER:-none}"
NTPHELPER
chmod 755 /usr/local/bin/signflow-ntp-apply

# NNP-immunité : après un app.relaunch() (OTA/restart), sudo est BLOQUÉ (NoNewPrivs
# hérité) → le player passe par une unité systemd oneshot autorisée par polkit :
# il écrit /var/lib/signflow/ntp_request puis `systemctl start signflow-ntp-apply.service`
# (D-Bus, insensible à NNP — même pattern que 45-signflow-timezone.rules). Le sudoers
# reste en repli pour les sessions pré-relaunch.
mkdir -p /var/lib/signflow
chown "$SERVICE_USER:$SERVICE_USER" /var/lib/signflow
cat > /etc/systemd/system/signflow-ntp-apply.service <<'UNIT'
[Unit]
Description=Vitrail NTP apply (reads /var/lib/signflow/ntp_request)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/signflow-ntp-apply --from-request
UNIT
systemctl daemon-reload
cat > /etc/polkit-1/rules.d/46-signflow-ntp.rules <<POLKITRULE
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "signflow-ntp-apply.service" &&
        action.lookup("verb") == "start" &&
        subject.user == "$SERVICE_USER") {
        return polkit.Result.YES;
    }
});
POLKITRULE
systemctl restart polkit 2>/dev/null || true

# Choix du rôle NTP à l'install (comme le player linux). Le RPi se cale en
# général en client sur l'hôte backend du LAN. Override non-interactif :
# SF_NTP_ROLE=client|server|none  SF_NTP_SERVER=<ip>
NTP_ROLE="${SF_NTP_ROLE:-}"
NTP_SERVER="${SF_NTP_SERVER:-}"
if [[ -z "$NTP_ROLE" ]]; then
  if [[ -t 0 ]]; then
    echo ""
    echo "  Rôle NTP de cette machine (pour la synchro LAN entre players) :"
    echo "    [c] client  — cale son horloge sur un serveur NTP du LAN (défaut)"
    echo "    [s] serveur — sert l'heure aux autres players"
    echo "    [n] aucun   — ne pas toucher au NTP"
    read -rp "  Choix [c/s/n] : " _ans || true
    case "${_ans,,}" in s) NTP_ROLE="server" ;; n) NTP_ROLE="none" ;; *) NTP_ROLE="client" ;; esac
  else
    [[ -n "$NTP_SERVER" ]] && NTP_ROLE="client" || NTP_ROLE="none"
  fi
fi
if [[ "$NTP_ROLE" == "client" && -z "$NTP_SERVER" && -t 0 ]]; then
  read -rp "  IP du serveur NTP du LAN (ex: 192.168.1.10) : " NTP_SERVER || true
fi
if [[ "$NTP_ROLE" == "client" && -z "$NTP_SERVER" ]]; then
  echo "    -> Client NTP demandé sans IP serveur — ignoré (à configurer via le helper ou l'UI)."
elif [[ "$NTP_ROLE" == "none" ]]; then
  echo "    -> NTP non configuré (choix 'aucun')."
else
  if /usr/local/bin/signflow-ntp-apply "$NTP_ROLE" "$NTP_SERVER" >/dev/null 2>&1; then
    echo "    -> NTP: role=$NTP_ROLE server=${NTP_SERVER:-none}"
  else
    echo "    -> NTP: échec de l'application (role=$NTP_ROLE server=${NTP_SERVER:-none})"
  fi
fi

# ── 7. Config JSON ────────────────────────────────────────────────────────────
echo "[7/8] Configuration..."
mkdir -p "$CONFIG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  cat > "$CONFIG_DIR/config.json" <<'EOF'
{
  "backend_url": "",
  "player_token": ""
}
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/config.json"
  echo "    -> Config created at $CONFIG_DIR/config.json"
  echo "    -> The First-Boot Wizard will prompt for these values on first launch."
else
  echo "    -> Existing config kept."
fi

# ── Détection décodage vidéo matériel (V4L2 stateful, Broadcom VideoCore) ─────
# Le RPi décode en V4L2 (pas de VA-API). Les flags HW sont déjà passés sur la CLI
# Electron par le start script (cage --use-v4l2-codec) → chromium_flags reste vide
# (on ne double-applique pas). Best-effort, n'échoue jamais l'install.
# Override : SF_HW_DECODE=auto|on|off.
echo "[hw-decode] Capacités décodage (V4L2)..."
SF_HW_DECODE="${SF_HW_DECODE:-auto}"
RPI_METHOD="v4l2"; RPI_ACTIVE="true"
if [[ "$SF_HW_DECODE" == "off" ]]; then RPI_METHOD="none"; RPI_ACTIVE="false"; fi

# Cible = userData Electron = ~/.config/<package.json name> (rpi suffixe en
# signflow-player-rpi) → sans ça le player ne trouve pas capabilities.json.
CAP_APP_NAME="$(grep -m1 '"name"' "$PLAYER_DIR/package.json" 2>/dev/null | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
[ -z "$CAP_APP_NAME" ] && CAP_APP_NAME="signflow-player-rpi"
CAP_DIR="/home/$SERVICE_USER/.config/$CAP_APP_NAME"
mkdir -p "$CAP_DIR"
SF_CAP_METHOD="$RPI_METHOD" SF_CAP_ACTIVE="$RPI_ACTIVE" SF_CAP_MODE="$SF_HW_DECODE" \
python3 - "$CAP_DIR/capabilities.json" <<'PYCAP' || true
import json, os, sys, datetime
method = os.environ.get("SF_CAP_METHOD", "v4l2")
data = {
    "gpu_vendor": "broadcom",
    "gpu_model": "VideoCore (Raspberry Pi)",
    "hw_decode": method,
    "hw_decode_active": os.environ.get("SF_CAP_ACTIVE") == "true",
    # Codecs HW dépendants du modèle (Pi4 H.264+HEVC ; Pi5 HEVC HW, H.264 software)
    # -> laissé vide tant que non confirmé par validation terrain.
    "codecs": [],
    "gl_backend": "angle-gles",
    "sf_hw_decode": os.environ.get("SF_CAP_MODE", "auto"),
    "chromium_flags": [],  # déjà sur la CLI cage (--use-v4l2-codec --use-gl=angle --use-angle=gles)
    "detection_source": "install",
    "detected_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(sys.argv[1], "w") as f:
    json.dump(data, f, indent=2)
print("    -> broadcom | method=%s active=%s" % (data["hw_decode"], data["hw_decode_active"]))
PYCAP
chown -R "$SERVICE_USER:$SERVICE_USER" "$CAP_DIR" 2>/dev/null || true

# ── 7. Kernel watchdog (optional) ─────────────────────────────────────────────
if [[ "$ENABLE_WATCHDOG" == "true" ]]; then
  echo "[8/8] Enabling kernel hardware watchdog..."

  if ! grep -q "dtparam=watchdog=on" "$BOOT_CONFIG"; then
    echo "dtparam=watchdog=on" >> "$BOOT_CONFIG"
    echo "    -> Added dtparam=watchdog=on to $BOOT_CONFIG"
  fi

  apt-get install -y watchdog
  cat > /etc/watchdog.conf <<'EOF'
watchdog-device      = /dev/watchdog
watchdog-timeout     = 60
interval             = 10
max-load-1           = 24
min-memory           = 1
EOF
  systemctl enable watchdog
  echo "    -> watchdog daemon configured (/dev/watchdog, timeout=60s)"
else
  echo "[8/8] Kernel watchdog skipped (pass --watchdog to enable)."
fi

# ── Overlay rootfs (optional) ─────────────────────────────────────────────────
if [[ "$ENABLE_OVERLAY" == "true" ]]; then
  echo ""
  echo "[overlay] Enabling overlay rootfs..."
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [[ -f "$SCRIPT_DIR/scripts/setup-overlay.sh" ]]; then
    bash "$SCRIPT_DIR/scripts/setup-overlay.sh"
  else
    echo "Warning: scripts/setup-overlay.sh not found -- skipping overlay setup."
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Enable SSH if needed (disabled by default on RPi OS):"
echo "       sudo systemctl enable --now ssh"
echo ""
echo "  2. Reboot the Raspberry Pi:"
echo "       sudo reboot"
echo ""
echo "  3. The player will start automatically (cage kiosk, fullscreen)."
echo "     The First-Boot Wizard will appear -- enter your Vitrail server URL"
echo "     and provisioning token (sfp_... token from CMS > Players > Provisioning)."
echo ""
echo "  4. Config file (if you prefer to edit manually):"
echo "       $CONFIG_DIR/config.json"
echo ""
echo "  5. Player logs:"
echo "       tail -f $LOG_FILE"
echo ""
if [[ "$ENABLE_OVERLAY" == "false" ]]; then
  echo "  Tip: run with --overlay to protect the SD card from corruption on power loss:"
  echo "       sudo bash install.sh $SERVICE_USER --overlay"
  echo ""
fi
if [[ "$ENABLE_WATCHDOG" == "false" ]]; then
  echo "  Tip: run with --watchdog to enable hardware watchdog (auto-reboot on hang):"
  echo "       sudo bash install.sh $SERVICE_USER --watchdog"
  echo ""
fi
echo "  WebSocket URL: ws://<BACKEND>:8080/api/v1/ws/player?token=<JWT>"
