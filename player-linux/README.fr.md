# Vitrail Player — Linux

🇬🇧 [English version](README.md)

Player Electron pour **Debian 12 / Ubuntu x86, X11** (machine dédiée au kiosk — lightdm devient
le display manager).

## Installation

- **Terrain (recommandé)** : `docs/RUNBOOK_POSE_PLAYER.md` — canal `signflow-provision` SSH
  (une commande sur une machine déjà en service).
- **Manuelle locale** : `sudo bash install.sh [username] [backend_url]` (utilisateur par
  défaut : `signflow`, doit exister). Idempotent. Non-interactif : `SF_NTP_ROLE=client|server|none`,
  `SF_NTP_SERVER=<ip>`, `SF_HW_DECODE=auto|on|off`.

## Décisions techniques à connaître

- **X11 + openbox + lightdm**, autologin (`autologin-session=openbox` obligatoire + groupe
  `autologin` — exigence PAM Debian). **Autostart UNIQUE via `~/.config/openbox/autostart`**,
  jamais de `.desktop` XDG en plus : les deux se déclenchent sur certaines sessions → double
  lancement d'Electron → course sur le SingletonLock Chromium. L'autostart purge aussi un
  `Singleton*` périmé (reste après coupure de courant).
- **Coupure de sortie vidéo = `xrandr --output X --off/--auto`** — ⚠️ jamais
  `xset dpms force off` : validé HW (NUC, 2026-07-06), le DPMS off **gèle Electron/Chromium**
  (Xorg+Intel) — main+renderer figés, WS morte, pas de reprise au rallumage, même déclenché
  hors process. La fonction est **idempotente** (garde `_videoOutputOn`) et applique
  **directement** le mode forcé au rallumage (jamais `--auto` quand une consigne existe :
  `--auto` = mode préféré EDID, ex. 4K sur une TV → double changement de mode → l'écran perd
  le signal).
- **Redémarrage** : `app.relaunch(); app.exit(0)` (openbox ne relance pas un process quitté),
  pas `app.quit()`. Après relaunch, sudo est bloqué (`PR_SET_NO_NEW_PRIVS` hérité) → reboot via
  D-Bus logind, fuseau via polkit/timedated, NTP via l'unité systemd `signflow-ntp-apply.service`.
- **VA-API Electron sur Intel/Linux = impossible** (mesuré : Chromium reste en décodage
  software malgré les flags) — ne pas re-tenter ; `capabilities.json` garde
  `hw_decode_active:false` par défaut, c'est l'état réel. La voie de décodage HW est **mpv**
  (`display_config.video_renderer=mpv`, `src/mpv_controller.js`, dépendance dure `wmctrl` pour
  le z-order) quand la chaîne VA-API est détectée à l'install.
- NTP : chrony (le LAN sync exige des horloges alignées) — une machine serveur, les autres
  clientes ; re-pilotable depuis le CMS.
- Pare-feu : UDP 7778 (LAN sync) + 7779 (trigger VOD) autorisés si UFW actif.

## Mises à jour du moteur (Electron)

Le moteur Electron se met à jour depuis le serveur (**Réglages → Mises à jour → Moteur Electron**),
pas par la mise à jour du code. `src/engine_update.js` télécharge et vérifie le moteur, l'extrait
dans `node_modules/electron/dist.next`, puis passe la main à `src/engine_guard.js` (exécuté par le
`node` système, indépendant des deux moteurs), qui bascule `dist` → `dist.prev` et `dist.next` →
`dist`, relance le player et remet l'ancien moteur si le nouveau ne confirme pas son affichage en
5 minutes. Le lanceur exécute `engine_guard.js boot-check` avant Electron (coupure pendant une
bascule). Journal : `node_modules/electron/engine-guard.log`. `install.sh` donne `node_modules` au compte du player (aucun root pour changer de moteur) ; le garde relance Electron lui-même (détaché, même environnement).

## Config

`~/.config/signflow-player/config.json` — répertoire **dérivé du `name` de `package.json`**
(`app.getPath('userData')` ; ici le paquet s'appelle `signflow-player`). Clés : `backend_url`,
`player_token` (`sfp_…` de provisioning, remplacé par un JWT à l'enregistrement).
`capabilities.json` (GPU/VA-API/mpv) vit au même endroit.

## Logs / diagnostic

- `/var/log/signflow-player.log` (stdout/stderr Electron via l'autostart openbox, logrotate
  10 M ×3 copytruncate — vu 144 Mo sans borne) ; log applicatif borné dans `userData/logs/player.log`.
- Crash renderer : relaunch auto, sauf uptime < 60 s (anti-boucle).
