# Vitrail Player — Raspberry Pi

🇬🇧 [English version](README.md)

Player Electron pour **Raspberry Pi 4/5 sous Raspberry Pi OS Trixie (Debian 13, arm64), Wayland**.

## Installation

- **Terrain (recommandé)** : `docs/RUNBOOK_POSE_PLAYER.md` — canaux `signflow-provision`
  (image SD pour un RPi neuf : flasher, insérer, allumer, ne rien faire ; ou SSH pour une
  machine déjà en service).
- **Manuelle locale** : `sudo bash install.sh [username] [--overlay] [--watchdog]`
  (utilisateur par défaut : `signflow`). Variables : `WLR_OUTPUT` (sortie Wayland, auto-détectée
  sinon), `WLR_MODE` (défaut `1920x1080@60`), `SF_NTP_ROLE`/`SF_NTP_SERVER`, `SF_HW_DECODE`.

## Décisions techniques à connaître

- **Wayland + cage** (compositeur kiosk), lancé par lightdm (`user-session=signflow-player`).
  Le code ne fait **jamais** `setFullScreen(true)` / `ready-to-show` / `show:false` : cage gère
  le fullscreen nativement, ces appels créent un conflit XDG fatal. Le fullscreen vient du flag
  CLI `--start-fullscreen`.
- **GPU ACTIVÉ, options dans `launcher-flags.conf`** : les options GPU/GL/codec dépendent de la
  version d'Electron, donc `start-player.sh` les lit dans `launcher-flags.conf` à la racine du
  player (une `--option` par ligne, `#` = commentaire), **à nouveau avant chaque démarrage
  d'Electron** : modifier le fichier prend effet à la prochaine relance du player, sans
  réinstallation. Fichier absent ou sans option valide = les options intégrées, identiques au
  fichier livré. Le journal de lancement indique la source utilisée (`Launcher flags (...)`).
  Les options du kiosque (`--ozone-platform=wayland`, `--no-sandbox`, `--start-fullscreen`,
  `--disable-dev-shm-usage`) restent dans `start-player.sh`.
  Avec Electron 42 : `--enable-gpu --use-gl=angle --use-angle=gles --use-v4l2-codec` = composition,
  dessin et WebGL par le GPU (mesuré sur un RPi 5). `--use-gl=egl` n'existe plus en Electron 42 et
  n'a de toute façon jamais donné le GPU sur cette carte (tout en logiciel).
  ⚠️ `--disable-gpu` **casse le plein écran sous cage** — ne pas le remettre.
- **FHD forcé, 4K éliminé** (saccade même GPU actif) : `wlr-randr --mode $WLR_MODE` dans la
  boucle de `~/start-player.sh` (5 tentatives — cage peut ne pas être prêt).
- **Garde anti-dérive HDMI** : couper/rallumer l'écran = hotplug → wlroots repasse au mode
  préféré (souvent 4K@30) et la fenêtre Electron grandit ; re-forcer le mode ne la rétrécit pas
  (seul le quart haut-gauche s'affiche). La garde (boucle toutes les 5 s dans start-player.sh)
  **redémarre le player** (`pkill`) : le watchdog re-force FHD *avant* de relancer — le chemin
  du boot, qui marche.
- **Coupure de sortie vidéo** = `wlr-randr --output X --off/--on` — jamais de DPMS (le DPMS off
  gèle Electron/Chromium, validé HW).
- **Redémarrage** : `app.quit()` (cage/lightdm relance) ; l'OTA finalise par un **reboot OS**
  (`app.relaunch()` crashe le GPU sous cage). Après relaunch, sudo est bloqué (NoNewPrivs
  hérité) → fuseau et NTP passent par polkit/D-Bus (unité `signflow-ntp-apply.service`).
- Watchdog applicatif : boucle `start-player.sh` (relance à 3 s) ; `--watchdog` en option =
  watchdog matériel kernel ; `--overlay` = rootfs overlay (protège la carte SD).
- NTP : chrony, rôle serveur/client choisi à l'install, re-pilotable depuis le CMS.

## Config

`~/.config/signflow-player-rpi/config.json` — le répertoire est **dérivé du `name` de
`package.json`** (`app.getPath('userData')`), jamais réécrit en dur (leçon du 2026-08-09 : un
répertoire nommé à la main à côté n'est jamais lu → « Setup required » avec la config posée).
Clés : `backend_url`, `player_token` (`sfp_…` de provisioning, remplacé par un JWT à
l'enregistrement). `capabilities.json` (V4L2) vit au même endroit.

## Logs / diagnostic

- `/var/log/signflow-player.log` (stdout/stderr Electron, logrotate 10 M ×3 copytruncate —
  critique sur carte SD) ; log applicatif borné dans `userData/logs/`.
- La garde de résolution et le watchdog datent leurs actions dans ce même log.
