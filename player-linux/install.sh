#!/usr/bin/env bash
# install.sh — Vitrail Player installation on Debian 12 / Ubuntu
#
# Turnkey : installe les prérequis (curl, git, Node 20), une session kiosk
# minimale (xorg + openbox + lightdm) avec autologin, les dépendances Electron,
# l'autostart du player, les sudoers reboot, et chrony (NTP) pour la synchro LAN.
# Idempotent (relançable). Cible : machine Debian dédiée au player (pas un poste
# de bureau partagé — lightdm devient le display manager).
#
# Usage:
#   sudo bash install.sh [username] [backend_url]
#   (username par défaut : signflow)
#
# NTP : le script demande à l'install si la machine est serveur ou client NTP.
#   Override non-interactif : SF_NTP_ROLE=client|server|none  SF_NTP_SERVER=<ip>
#   ex: sudo SF_NTP_ROLE=client SF_NTP_SERVER=172.16.168.130 bash install.sh signflow http://172.16.168.130:8010
#
# Examples:
#   sudo bash install.sh signflow
#   sudo bash install.sh signflow http://172.16.168.130:8010
#
# After install:
#   1. Create a provisioning token in the CMS (Players → Provisioning → New token)
#   2. Edit ~/.config/signflow-player/config.json → set player_token (sfp_...)
#      (backend_url is set if passed as 2nd argument)
#   3. Reboot — autologin → openbox → the player registers automatically

set -euo pipefail

# apt non-interactif : évite les prompts (choix du display manager, etc.) qui
# bloqueraient le script lancé via sudo bash.
export DEBIAN_FRONTEND=noninteractive

PLAYER_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_USER="${1:-signflow}"
BACKEND_URL="${2:-}"
# Même règle que player-rpi : Electron lit ~/.config/<name de package.json>. Ici les deux
# coïncident (le paquet s'appelle « signflow-player »), donc rien ne change — mais on dérive
# quand même, pour que renommer le paquet un jour ne casse pas silencieusement le
# provisioning, comme c'est arrivé au RPi le 2026-08-09.
APP_NAME="$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLAYER_DIR/package.json" | head -1)"
[[ -n "$APP_NAME" ]] || { echo "Error: nom d'application illisible dans package.json" >&2; exit 1; }
CONFIG_DIR="/home/$SERVICE_USER/.config/$APP_NAME"
AUTOSTART_XDG="/home/$SERVICE_USER/.config/autostart"
AUTOSTART_OB="/home/$SERVICE_USER/.config/openbox"

echo "=== Vitrail Player — Debian 12 Install ==="
echo "    Player directory : $PLAYER_DIR"
echo "    User             : $SERVICE_USER"
[[ -n "$BACKEND_URL" ]] && echo "    Backend URL      : $BACKEND_URL"

if [[ $EUID -ne 0 ]]; then
  echo "Error: run with sudo  (sudo bash install.sh)" >&2
  exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  echo "Error: user '$SERVICE_USER' not found." >&2
  exit 1
fi

# ── 1. Base tools + environnement graphique kiosk ─────────────────────────────
# Debian minimal n'a souvent ni curl ni stack graphique. On installe le tout
# (idempotent : apt ignore ce qui est déjà présent). xorg+openbox+lightdm =
# session kiosk minimale ; wlr-randr/xrandr = forçage résolution/rotation.
echo "[1/8] Base tools + graphical stack (curl, git, xorg, openbox, lightdm)..."
apt-get update
apt-get install -y --no-install-recommends \
  curl ca-certificates git python3 logrotate \
  xserver-xorg xinit openbox lightdm x11-xserver-utils \
  v4l-utils

# ── 2. Node.js 22 LTS ─────────────────────────────────────────────────────────
if ! command -v node &>/dev/null || [ "$(node -e 'console.log(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)" -lt 22 ]; then
  echo "[2/8] Installing Node.js 22 LTS..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
else
  echo "[2/8] Node.js $(node --version) already present."
fi

# ── 3. Electron system dependencies ───────────────────────────────────────────
echo "[3/8] Electron/Chromium system dependencies..."
apt-get install -y --no-install-recommends \
  libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libxkbcommon0 libxcomposite1 libxrandr2 libgtk-3-0 \
  libdrm2 libgbm1 libasound2

# ── 4. npm install ────────────────────────────────────────────────────────────
echo "[4/8] Installing npm dependencies..."
cd "$PLAYER_DIR"
npm install --omit=dev

# ── 5. Initial config ─────────────────────────────────────────────────────────
echo "[5/8] Configuration..."
mkdir -p "$CONFIG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  cat > "$CONFIG_DIR/config.json" <<EOF
{
  "backend_url": "${BACKEND_URL:-http://YOUR_SERVER_IP:8080}",
  "player_token": ""
}
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/config.json"
  echo "    → Config created at $CONFIG_DIR/config.json"
else
  # Update backend_url if provided as argument
  if [[ -n "$BACKEND_URL" ]]; then
    TMP=$(mktemp)
    python3 -c "
import json, sys
with open('$CONFIG_DIR/config.json') as f: d = json.load(f)
d['backend_url'] = '$BACKEND_URL'
with open('$TMP', 'w') as f: json.dump(d, f, indent=2)
" && mv "$TMP" "$CONFIG_DIR/config.json"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/config.json"
    echo "    → backend_url updated to $BACKEND_URL"
  else
    echo "    → Existing config kept."
  fi
fi

# ── Détection décodage vidéo matériel (VA-API Intel/AMD) ──────────────────────
# Détecte le GPU + la chaîne VA-API et écrit capabilities.json (lu+remonté par le
# player dans player:status). Best-effort : ne fait jamais échouer l'install.
# Override : SF_HW_DECODE=auto|on|off (off = pas de décodage HW, on = forcer actif).
echo "[hw-decode] Détection du décodage vidéo matériel..."
SF_HW_DECODE="${SF_HW_DECODE:-auto}"
HW_VENDOR="unknown"; HW_MODEL=""; HW_METHOD="none"; HW_ACTIVE="false"
HW_CODECS="[]"; HW_GL="egl"

if [[ "$SF_HW_DECODE" != "off" ]]; then
  apt-get install -y pciutils >/dev/null 2>&1 || true
  if command -v lspci >/dev/null 2>&1; then
    GPU_LINE="$(lspci -nn 2>/dev/null | grep -iE 'VGA|3D controller|Display controller' | head -1 || true)"
    HW_MODEL="$(printf '%s' "$GPU_LINE" | sed -E 's/^.*\]: //; s/ \[[0-9a-f]{4}:[0-9a-f]{4}\].*//' | head -c 120)"
    case "$GPU_LINE" in
      *[Ii]ntel*|*8086*)  HW_VENDOR="intel" ;;
      *AMD*|*ATI*|*1002*) HW_VENDOR="amd" ;;
      *NVIDIA*|*10de*)    HW_VENDOR="nvidia" ;;
    esac
  fi
  if [[ -e /dev/dri/renderD128 ]]; then
    case "$HW_VENDOR" in
      intel)
        apt-get install -y intel-media-va-driver vainfo intel-gpu-tools >/dev/null 2>&1 || true
        HW_METHOD="vaapi" ;;
      amd)
        apt-get install -y mesa-va-drivers vainfo >/dev/null 2>&1 || true
        HW_METHOD="vaapi" ;;
      nvidia)
        # NVDEC / nvidia-vaapi-driver = à traiter à part (proprio) — non auto-configuré ici.
        HW_METHOD="none" ;;
    esac
  fi
  if [[ "$HW_METHOD" == "vaapi" ]] && command -v vainfo >/dev/null 2>&1; then
    VA_OUT="$(vainfo 2>/dev/null || true)"
    CODECS=()
    echo "$VA_OUT" | grep -q "VAProfileH264"  && CODECS+=('"h264"') || true
    echo "$VA_OUT" | grep -q "VAProfileHEVC"  && CODECS+=('"hevc"') || true
    echo "$VA_OUT" | grep -q "VAProfileVP9"   && CODECS+=('"vp9"')  || true
    echo "$VA_OUT" | grep -q "VAProfileAV1"   && CODECS+=('"av1"')  || true
    HW_CODECS="[$(IFS=,; echo "${CODECS[*]:-}")]"
  fi
fi

# mpv : voie de décodage HW VAAPI alternative au <video> Chromium (qui reste software
# sur Intel/Linux — cf. EVOLUTIONS + spike mpv). Deps RUNTIME du mode mpv :
#   - mpv    : le lecteur (spawn)
#   - wmctrl : raise/lower de la fenêtre mpv vs le kiosk (z-order) — DEP DURE
#   (l'IPC mpv passe par Node `net`, PAS par socat ; pkill = procps, présent par défaut)
# socat n'est installé que pour les scripts de diagnostic PoC (player-linux/scripts/mpv-poc*.sh).
# capabilities.mpv_vaapi = true ⇒ le CMS propose le rendu vidéo mpv (display_config.video_renderer).
MPV_VAAPI="false"
if [[ "$SF_HW_DECODE" != "off" && "$HW_METHOD" == "vaapi" ]]; then
  apt-get install -y --no-install-recommends mpv socat wmctrl >/dev/null 2>&1 || true
  # mpv présent + au moins un profil decode VLD (codecs non vides) → voie HW exploitable.
  if command -v mpv >/dev/null 2>&1 && [[ "$HW_CODECS" != "[]" ]]; then
    MPV_VAAPI="true"
  fi
fi
# hw_decode_active : provisoire à l'install. Sur Linux/Electron la VA-API n'est PAS
# encore déclenchée (Chromium reste en software malgré les flags — cf. EVOLUTIONS,
# constat riri/fifi). On laisse 'false' par défaut → le CMS alerte « GPU VA-API
# détecté mais décodage HW inactif », ce qui est l'état réel. SF_HW_DECODE=on force.
[[ "$SF_HW_DECODE" == "on" ]] && HW_ACTIVE="true"

# Cible = userData Electron = ~/.config/<package.json name>. Le name est suffixé sur
# rpi/windows (signflow-player-rpi/-windows) → sans ça le player ne trouve pas le fichier.
CAP_APP_NAME="$(grep -m1 '"name"' "$PLAYER_DIR/package.json" 2>/dev/null | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
[ -z "$CAP_APP_NAME" ] && CAP_APP_NAME="signflow-player"
CAP_DIR="/home/$SERVICE_USER/.config/$CAP_APP_NAME"
mkdir -p "$CAP_DIR"
SF_CAP_VENDOR="$HW_VENDOR" SF_CAP_MODEL="$HW_MODEL" SF_CAP_METHOD="$HW_METHOD" \
SF_CAP_ACTIVE="$HW_ACTIVE" SF_CAP_CODECS="$HW_CODECS" SF_CAP_GL="$HW_GL" \
SF_CAP_MODE="$SF_HW_DECODE" SF_CAP_MPV_VAAPI="$MPV_VAAPI" \
python3 - "$CAP_DIR/capabilities.json" <<'PYCAP' || true
import json, os, sys, datetime
method = os.environ.get("SF_CAP_METHOD", "none")
flags = []
if method == "vaapi":
    # Combo de flags à valider hardware (VA-API Electron). Piste backend GL si KO.
    flags = ["--ignore-gpu-blocklist",
             "--enable-features=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL",
             "--disable-features=UseChromeOSDirectVideoDecoder",
             "--disable-gpu-driver-bug-workarounds"]
try:
    codecs = json.loads(os.environ.get("SF_CAP_CODECS") or "[]")
except Exception:
    codecs = []
data = {
    "gpu_vendor": os.environ.get("SF_CAP_VENDOR", "unknown"),
    "gpu_model": os.environ.get("SF_CAP_MODEL", ""),
    "hw_decode": method,
    "hw_decode_active": os.environ.get("SF_CAP_ACTIVE") == "true",
    "codecs": codecs,
    "gl_backend": os.environ.get("SF_CAP_GL", "egl"),
    "sf_hw_decode": os.environ.get("SF_CAP_MODE", "auto"),
    "mpv_vaapi": os.environ.get("SF_CAP_MPV_VAAPI") == "true",
    "chromium_flags": flags,
    "detection_source": "install",
    "detected_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(sys.argv[1], "w") as f:
    json.dump(data, f, indent=2)
print("    -> %s | method=%s active=%s codecs=%s" % (
    data["gpu_vendor"], data["hw_decode"], data["hw_decode_active"],
    ",".join(codecs) or "-"))
PYCAP
chown -R "$SERVICE_USER:$SERVICE_USER" "$CAP_DIR" 2>/dev/null || true

# ── 6. Sudoers ────────────────────────────────────────────────────────────────
echo "[6/8] Sudoers (reboot without password)..."
cat > /etc/sudoers.d/signflow-player <<EOF
$SERVICE_USER ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/bin/systemctl reboot, /sbin/shutdown, /usr/local/bin/signflow-ntp-apply, /usr/bin/timedatectl set-timezone *
Defaults:$SERVICE_USER !requiretty
EOF
chmod 440 /etc/sudoers.d/signflow-player

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


# ── 7. Autostart + autologin ──────────────────────────────────────────────────
echo "[7/8] Autostart + autologin..."

ELECTRON_BIN="$PLAYER_DIR/node_modules/.bin/electron"
LOG_FILE="/var/log/signflow-player.log"

# Log file (world-writable so signflow user can write from openbox session)
touch "$LOG_FILE"
chmod 666 "$LOG_FILE"
echo "    → Log file: $LOG_FILE"

# Rotation du log OS (la redirection openbox `>> $LOG_FILE 2>&1` capte le stderr natif
# Electron/Chromium = croissance sans limite sinon — vu 144 Mo sur un NUC). `copytruncate`
# tronque le fichier EN PLACE sans redémarrer le player (qui garde le fd ouvert). Le logger
# applicatif interne (userData/logs/player.log) reste borné à part. logrotate tourne en cron.daily.
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
  echo "    → logrotate: /etc/logrotate.d/signflow-player (10M ×3, copytruncate)"
else
  echo "    ⚠ logrotate absent — installez-le pour borner $LOG_FILE (apt-get install logrotate)"
fi

# Pare-feu LAN : si UFW est actif, autoriser les broadcasts UDP inter-players —
# 7778 = LAN sync (§III.3), 7779 = trigger VOD multi-cible (Feature B Phase 3).
# (UFW est inactif par défaut sur Debian → no-op le cas échéant, mais indispensable
# si l'opérateur active le pare-feu, sinon LAN sync + trigger VOD cassent.)
if command -v ufw >/dev/null 2>&1; then
  ufw allow 7778/udp >/dev/null 2>&1 || true
  ufw allow 7779/udp >/dev/null 2>&1 || true
fi

# Autostart UNIQUE via openbox (la session est forcée à openbox par l'autologin
# lightdm ci-dessous). On NE crée PAS d'autostart XDG en plus : sur certaines
# sessions les deux se déclenchent → double lancement d'Electron → course sur le
# SingletonLock Chromium ("File exists"). La 2ᵉ instance se referme (singleInstanceLock)
# mais ça génère du bruit. Une seule source d'autostart = pas de course.
# On purge un éventuel ancien .desktop XDG laissé par une install précédente.
rm -f "$AUTOSTART_XDG/signflow-player.desktop" 2>/dev/null || true

mkdir -p "$AUTOSTART_OB"
OPENBOX_AUTOSTART="$AUTOSTART_OB/autostart"
if [[ -f "$OPENBOX_AUTOSTART" ]]; then
  sed -i '/signflow/Id' "$OPENBOX_AUTOSTART"   # retire l'ancienne entrée avant de réécrire
fi
# Nettoie un SingletonLock périmé (reste après coupure de courant / kill brutal)
# AVANT de lancer — en début de session aucune instance ne tourne, c'est sûr.
echo "rm -f $CONFIG_DIR/Singleton* 2>/dev/null" >> "$OPENBOX_AUTOSTART"
echo "$ELECTRON_BIN $PLAYER_DIR >> $LOG_FILE 2>&1 &" >> "$OPENBOX_AUTOSTART"
chown -R "$SERVICE_USER:$SERVICE_USER" "$AUTOSTART_OB"
echo "    → Openbox autostart (source unique) : $OPENBOX_AUTOSTART"

# 6c. Lightdm autologin
if command -v lightdm &>/dev/null || systemctl list-units --type=service 2>/dev/null | grep -q lightdm; then
  LIGHTDM_CONF_DIR="/etc/lightdm/lightdm.conf.d"
  mkdir -p "$LIGHTDM_CONF_DIR"
  # autologin-session=openbox est INDISPENSABLE : sans lui lightdm ne sait pas
  # quelle session lancer → reste sur l'écran de login malgré l'autologin.
  cat > "$LIGHTDM_CONF_DIR/50-signflow-autologin.conf" <<EOF
[Seat:*]
autologin-user=$SERVICE_USER
autologin-user-timeout=0
autologin-session=openbox
EOF
  # Sur Debian, PAM exige que l'utilisateur soit dans le groupe 'autologin'
  # pour que l'autologin sans mot de passe se déclenche réellement.
  groupadd -f autologin
  usermod -aG autologin "$SERVICE_USER"
  # Démarrer en mode graphique + activer lightdm comme display manager.
  systemctl set-default graphical.target
  systemctl enable lightdm >/dev/null 2>&1 || true
  echo "    → Lightdm autologin configured for $SERVICE_USER (session=openbox, group autologin)"
else
  echo "    → Lightdm not detected — configure autologin manually"
fi

# ── 8. NTP / horloge (chrony) ─────────────────────────────────────────────────
# Le LAN sync (§III.3) exige des horloges synchronisées entre players → tous
# pointent vers UN serveur NTP du LAN (souvent l'hôte du backend). Une machine
# du LAN joue le rôle serveur, les autres sont clients.
echo "[8/8] NTP / horloge (chrony)..."
apt-get install -y chrony

# Helper NTP piloté depuis l'UI (relais NTP par groupe LAN sync). Appelé par le
# player via `sudo signflow-ntp-apply <role> <server_ip>` quand le CMS pousse
# display_config.ntp_role/ntp_server. Entrées validées (pas d'injection).
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

# Retire un eventuel ancien bloc SignFlow (idempotent)
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

# Rôle : surcharge non-interactive via SF_NTP_ROLE (client|server|none) + SF_NTP_SERVER
NTP_ROLE="${SF_NTP_ROLE:-}"
NTP_SERVER="${SF_NTP_SERVER:-}"
DEFAULT_NTP="$(echo "${BACKEND_URL:-}" | sed -E 's#^[a-z]+://##; s#[:/].*$##')"

if [[ -z "$NTP_ROLE" ]]; then
  if [[ -t 0 ]]; then
    echo ""
    echo "  Rôle NTP de cette machine (pour la synchro LAN entre players) :"
    echo "    [c] client  — cale son horloge sur un serveur NTP du LAN (défaut)"
    echo "    [s] serveur — sert l'heure aux autres players (UNE seule machine du LAN)"
    echo "    [n] aucun   — ne pas toucher au NTP"
    read -rp "  Choix [c/s/n] : " _ans || true
    case "${_ans,,}" in
      s) NTP_ROLE="server" ;;
      n) NTP_ROLE="none" ;;
      *) NTP_ROLE="client" ;;
    esac
  else
    NTP_ROLE="client"   # non-interactif → client par défaut
  fi
fi

CHRONY_CONF="/etc/chrony/chrony.conf"
# Retire un éventuel ancien bloc SignFlow (idempotent)
sed -i '/# >>> SignFlow NTP/,/# <<< SignFlow NTP/d' "$CHRONY_CONF" 2>/dev/null || true

if [[ "$NTP_ROLE" == "server" ]]; then
  LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
  [[ -z "$LAN_IP" ]] && LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  SUBNET="$(echo "${LAN_IP:-0.0.0.0}" | awk -F. '{print $1"."$2"."$3".0/24"}')"
  {
    echo "# >>> SignFlow NTP"
    echo "allow $SUBNET"
    echo "local stratum 10"   # sert l'heure même sans source Internet
    echo "# <<< SignFlow NTP"
  } >> "$CHRONY_CONF"
  systemctl restart chrony && systemctl enable chrony >/dev/null 2>&1 || true
  command -v ufw &>/dev/null && ufw allow 123/udp >/dev/null 2>&1 || true
  echo "    → Serveur NTP : sert le LAN $SUBNET (IP de cette machine : ${LAN_IP:-?})"
elif [[ "$NTP_ROLE" == "client" ]]; then
  if [[ -z "$NTP_SERVER" && -t 0 ]]; then
    read -rp "  IP du serveur NTP du LAN [${DEFAULT_NTP:-172.16.168.130}] : " NTP_SERVER || true
  fi
  NTP_SERVER="${NTP_SERVER:-$DEFAULT_NTP}"
  if [[ -n "$NTP_SERVER" ]]; then
    {
      echo "# >>> SignFlow NTP"
      echo "server $NTP_SERVER iburst prefer"
      echo "# <<< SignFlow NTP"
    } >> "$CHRONY_CONF"
    systemctl restart chrony && systemctl enable chrony >/dev/null 2>&1 || true
    chronyc -a makestep >/dev/null 2>&1 || true
    echo "    → Client NTP : synchronise sur $NTP_SERVER"
  else
    echo "    → Client NTP demandé mais aucune IP serveur fournie — à configurer manuellement."
  fi
else
  echo "    → NTP non configuré (choix 'aucun')."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Create a provisioning token in the CMS:"
echo "       http://YOUR_SERVER/settings/players/provisioning → New token"
echo ""
echo "  2. Edit the player config:"
echo "       nano $CONFIG_DIR/config.json"
echo ""
echo "     Set backend_url to your Vitrail server URL, and"
echo "     player_token to the provisioning token (sfp_...)"
echo "     The player will register automatically on first start."
echo ""
echo "  3. Reboot: sudo reboot"
echo ""
echo "  Logs: tail -f $LOG_FILE"
