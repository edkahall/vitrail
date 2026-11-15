#!/usr/bin/env bash
# Vitrail - install the controlled OS update helper on a Linux player (issue vitrail#6).
#
# Called by install.sh on a new player. On a player that is ALREADY installed, run it once:
#     sudo bash install-os-update.sh [service_user]      (default user: signflow)
# ⚠️ From a FRESH copy of the release (git clone / downloaded archive), NEVER from the player's
# own folder: the player account can rewrite that folder (code updates), and this script runs as
# root and installs the helper it finds next to it.
#
# What it installs (nothing else, no player running as root):
#   /usr/local/bin/signflow-os-update                    the helper (root, validated requests only)
#   /etc/systemd/system/signflow-os-update.service       oneshot unit that runs it
#   /etc/polkit-1/rules.d/47-signflow-os-update.rules    lets the player account START that unit
#   /var/lib/signflow/os-update/                         inbox for the request, owned by the player
#   /var/lib/signflow-os-update/                         status/result, owned by ROOT (read-only to the player)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0 [service_user]" >&2; exit 1; }
SERVICE_USER="${1:-signflow}"
id "$SERVICE_USER" >/dev/null 2>&1 || { echo "Unknown user: $SERVICE_USER" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$HERE/signflow-os-update" ]] || { echo "Missing $HERE/signflow-os-update" >&2; exit 1; }

install -m 755 -o root -g root "$HERE/signflow-os-update" /usr/local/bin/signflow-os-update
# /var/lib/signflow belongs to the player (NTP request): never change an existing one.
[[ -d /var/lib/signflow ]] || install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/signflow
install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/signflow/os-update
install -d -m 755 -o root -g root /var/lib/signflow-os-update

cat > /etc/systemd/system/signflow-os-update.service <<'UNIT'
[Unit]
Description=Vitrail controlled OS update (reads /var/lib/signflow/os-update/request)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/signflow-os-update
TimeoutStartSec=3h
Nice=10
UNIT
systemctl daemon-reload

mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/47-signflow-os-update.rules <<POLKITRULE
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "signflow-os-update.service" &&
        action.lookup("verb") == "start" &&
        subject.user == "$SERVICE_USER") {
        return polkit.Result.YES;
    }
});
POLKITRULE
systemctl restart polkit 2>/dev/null || true

echo "OS update helper installed for user $SERVICE_USER."
echo "Test (as $SERVICE_USER): systemctl start signflow-os-update.service && cat /var/lib/signflow-os-update/status.json"
