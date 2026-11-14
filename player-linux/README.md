# Vitrail Player — Linux

🇫🇷 [Version française](README.fr.md)

Electron player for **Debian 12 / Ubuntu x86, X11** (machine dedicated to the kiosk — lightdm becomes
the display manager).

## Installation

- **Field (recommended)**: `docs/RUNBOOK_POSE_PLAYER.md` — `signflow-provision` SSH channel
  (one command on a machine already in service).
- **Local manual install**: `sudo bash install.sh [username] [backend_url]` (default
  user: `signflow`, must exist). Idempotent. Non-interactive: `SF_NTP_ROLE=client|server|none`,
  `SF_NTP_SERVER=<ip>`, `SF_HW_DECODE=auto|on|off`.

## Technical decisions to know

- **X11 + openbox + lightdm**, autologin (`autologin-session=openbox` mandatory + `autologin`
  group — Debian PAM requirement). **SINGLE autostart via `~/.config/openbox/autostart`**,
  never an additional XDG `.desktop`: both fire on some sessions → Electron launched
  twice → race on the Chromium SingletonLock. The autostart also purges a stale
  `Singleton*` (left over after a power cut).
- **Video output off = `xrandr --output X --off/--auto`** — ⚠️ never
  `xset dpms force off`: validated on hardware, DPMS off **freezes Electron/Chromium**
  (Xorg+Intel) — main+renderer frozen, WS dead, no recovery when turned back on, even when triggered
  out of process. The function is **idempotent** (`_videoOutputOn` guard) and applies the forced
  mode **directly** when turning back on (never `--auto` when a setting exists:
  `--auto` = EDID preferred mode, e.g. 4K on a TV → double mode change → the screen loses
  the signal).
- **Restart**: `app.relaunch(); app.exit(0)` (openbox does not relaunch an exited process),
  not `app.quit()`. After relaunch, sudo is blocked (inherited `PR_SET_NO_NEW_PRIVS`) → reboot via
  D-Bus logind, timezone via polkit/timedated, NTP via the systemd unit `signflow-ntp-apply.service`.
- **VA-API in Electron on Intel/Linux = impossible** (measured: Chromium stays in software
  decoding despite the flags) — do not retry; `capabilities.json` keeps
  `hw_decode_active:false` by default, which is the real state. The HW decoding path is **mpv**
  (`display_config.video_renderer=mpv`, `src/mpv_controller.js`, hard dependency on `wmctrl` for
  z-order) when the VA-API chain is detected at install time.
- NTP: chrony (LAN sync requires aligned clocks) — one server machine, the others
  clients; can be re-driven from the CMS.
- Firewall: UDP 7778 (LAN sync) + 7779 (VOD trigger) allowed if UFW is active.

## Engine updates (Electron)

The Electron engine is updated from the server (**Settings → Updates → Electron engine**), not by
the code update. `src/engine_update.js` downloads and checks the engine, extracts it to
`node_modules/electron/dist.next`, then hands over to `src/engine_guard.js` (run by the system
`node`, independent of both engines), which switches `dist` → `dist.prev` and `dist.next` → `dist`,
restarts the player and puts the previous engine back if the new one does not confirm rendering
within 5 minutes. The launcher runs `engine_guard.js boot-check` before Electron (power cut during
a switch). Log: `node_modules/electron/engine-guard.log`. `install.sh` gives `node_modules` to the player account (no root needed to switch engines); the guard restarts Electron itself (detached, same environment).

## Config

`~/.config/signflow-player/config.json` — directory **derived from the `name` in `package.json`**
(`app.getPath('userData')`; here the package is called `signflow-player`). Keys: `backend_url`,
`player_token` (provisioning `sfp_…`, replaced by a JWT on registration).
`capabilities.json` (GPU/VA-API/mpv) lives in the same place.

## Logs / diagnostics

- `/var/log/signflow-player.log` (Electron stdout/stderr via the openbox autostart, logrotate
  10 M ×3 copytruncate — seen at 144 MB unbounded); bounded application log in `userData/logs/player.log`.
- Renderer crash: automatic relaunch, except when uptime < 60 s (anti-loop).
