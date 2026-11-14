# Vitrail Player — Raspberry Pi

🇫🇷 [Version française](README.fr.md)

Electron player for **Raspberry Pi 4/5 on Raspberry Pi OS Trixie (Debian 13, arm64), Wayland**.

## Installation

- **Field (recommended)**: `docs/RUNBOOK_POSE_PLAYER.md` — `signflow-provision` channels
  (SD image for a new RPi: flash, insert, power on, do nothing; or SSH for a
  machine already in service).
- **Local manual install**: `sudo bash install.sh [username] [--overlay] [--watchdog]`
  (default user: `signflow`). Variables: `WLR_OUTPUT` (Wayland output, auto-detected
  otherwise), `WLR_MODE` (default `1920x1080@60`), `SF_NTP_ROLE`/`SF_NTP_SERVER`, `SF_HW_DECODE`.

## Technical decisions to know

- **Wayland + cage** (kiosk compositor), launched by lightdm (`user-session=signflow-player`).
  The code **never** calls `setFullScreen(true)` / `ready-to-show` / `show:false`: cage handles
  fullscreen natively, these calls create a fatal XDG conflict. Fullscreen comes from the
  `--start-fullscreen` CLI flag.
- **GPU ENABLED, flags in `launcher-flags.conf`**: the GPU/GL/codec flags depend on the Electron
  version, so `start-player.sh` reads them from `launcher-flags.conf` at the root of the player
  (one `--flag` per line, `#` = comment), **again before every Electron start**: editing the
  file applies at the next player restart, no reinstall. Missing file or no valid flag = the
  built-in defaults, which are the same as the shipped file. The launch log says which source
  was used (`Launcher flags (...)`). The kiosk flags (`--ozone-platform=wayland`, `--no-sandbox`,
  `--start-fullscreen`, `--disable-dev-shm-usage`) stay in `start-player.sh`.
  With Electron 42: `--enable-gpu --use-gl=angle --use-angle=gles --use-v4l2-codec` = GPU
  compositing, rasterization and WebGL (measured on a RPi 5). `--use-gl=egl` no longer exists in
  Electron 42 and never gave the GPU on this board anyway (everything in software).
  ⚠️ `--disable-gpu` **breaks fullscreen under cage** — do not put it back.
- **FHD forced, 4K eliminated** (stutters even with GPU enabled): `wlr-randr --mode $WLR_MODE` in the
  `~/start-player.sh` loop (5 attempts — cage may not be ready).
- **HDMI drift guard**: turning the screen off/on = hotplug → wlroots switches back to the preferred
  mode (often 4K@30) and the Electron window grows; re-forcing the mode does not shrink it
  (only the top-left quarter is displayed). The guard (loop every 5 s in start-player.sh)
  **restarts the player** (`pkill`): the watchdog re-forces FHD *before* relaunching — the boot
  path, which works.
- **Video output off** = `wlr-randr --output X --off/--on` — never DPMS (DPMS off
  freezes Electron/Chromium, validated on hardware).
- **Restart**: `app.quit()` (cage/lightdm relaunches); OTA finishes with an **OS reboot**
  (`app.relaunch()` crashes the GPU under cage). After relaunch, sudo is blocked (inherited
  NoNewPrivs) → timezone and NTP go through polkit/D-Bus (unit `signflow-ntp-apply.service`).
- Application watchdog: `start-player.sh` loop (relaunch after 3 s); `--watchdog` option =
  kernel hardware watchdog; `--overlay` = rootfs overlay (protects the SD card).
- NTP: chrony, server/client role chosen at install, can be re-driven from the CMS.

## Engine updates (Electron)

The Electron engine is updated from the server (**Settings → Updates → Electron engine**), not by
the code update. `src/engine_update.js` downloads and checks the engine, extracts it to
`node_modules/electron/dist.next`, then hands over to `src/engine_guard.js` (run by the system
`node`, independent of both engines), which switches `dist` → `dist.prev` and `dist.next` → `dist`,
restarts the player and puts the previous engine back if the new one does not confirm rendering
within 5 minutes. The launcher runs `engine_guard.js boot-check` before Electron (power cut during
a switch). Log: `node_modules/electron/engine-guard.log`. `install.sh` gives `node_modules` to the player account (no root needed to switch engines); the resolution guard of `start-player.sh` only kills Electron, never the engine guard.

## Config

`~/.config/signflow-player-rpi/config.json` — the directory is **derived from the `name` in
`package.json`** (`app.getPath('userData')`), never hard-coded (lesson learned: a
hand-named directory next to it is never read → "Setup required" with the config in place).
Keys: `backend_url`, `player_token` (provisioning `sfp_…`, replaced by a JWT on
registration). `capabilities.json` (V4L2) lives in the same place.

## Logs / diagnostics

- `/var/log/signflow-player.log` (Electron stdout/stderr, logrotate 10 M ×3 copytruncate —
  critical on an SD card); bounded application log in `userData/logs/`.
- The resolution guard and the watchdog timestamp their actions in this same log.
