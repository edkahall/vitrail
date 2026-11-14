# Vitrail Player — Windows

🇫🇷 [Version française](README.fr.md)

Electron player for **Windows 11 Pro** (existing PC turned into a player).

## Installation

- **Field (recommended)**: `docs/RUNBOOK_POSE_PLAYER.md` — `signflow-provision` WinRM channel
  (opening WinRM is the only manual step, then one command).
- **Local manual install**: **administrator** PowerShell, `.\install.ps1` (option
  `-InstallDir`, default `C:\Program Files\SignFlow Player`). The script installs Node 20 LTS via
  winget if missing (and refreshes the current process Path — otherwise `npm` fails right after
  a successful install).

## Technical decisions to know

- **No asar**: the sources are copied as-is into
  `C:\Program Files\SignFlow Player\src\` (a fix can be dropped in file by file).
- **Scheduled Task "SignFlow Player"** (at logon, RunLevel Highest, restart 99× every
  1 min, no time limit, hidden PowerShell). Application restart =
  `app.relaunch(); app.exit(0)` (the task does not relaunch a cleanly exited process).
- **Registration**: the install config carries `org_token` (`sfp_…`) + `player_name`; on the
  first register the server returns a **`player_token`** (JWT) that replaces `org_token`
  (removed from the config).
- **Video output off = `src/display_power.ps1`**: ① **DDC/CI** (VCP 0xD6 — the GPU
  keeps sending, the screen sleeps on command, instant wake-up and always reversible);
  ② fallback `SC_MONITORPOWER` (exit 10). Wake-up = DDC on + SC -1 + **`ChangeDisplaySettingsEx`
  reset** + ES_DISPLAY_REQUIRED + synthetic input — several levers because some drivers
  (Intel HD 6000) ignore part of them, and `SC_MONITORPOWER off` is **unrecoverable** there without
  the mode reset. Broadcast via `SendNotifyMessageW`, never `SendMessage(HWND_BROADCAST)`
  (blocks 30 s if a window does not pump its messages — observed in the field).
- **Never `focus()` from the `blur` handler** (unreliable under Windows 11 foreground lock
  rules) → `setAlwaysOnTop` + `moveTop()` to regain z-order.
- `.ps1` scripts: **100 % ASCII** (PowerShell 5.1 reads them as ANSI).
- Kiosk set up by the install: no screen/machine sleep, screensaver off, taskbar auto-hide,
  last user hidden at login, firewall UDP 7778/7779 (LAN sync + VOD trigger).
- Renderer crash: automatic relaunch, except when uptime < 60 s (anti-loop); `gpu_enabled` changed
  live → relaunch.

## Engine updates (Electron)

The Electron engine is updated from the server (**Settings → Updates → Electron engine**), not by
the code update. `src/engine_update.js` downloads and checks the engine, extracts it to
`node_modules/electron/dist.next`, then hands over to `src/engine_guard.js` (run by the system
`node`, independent of both engines), which switches `dist` → `dist.prev` and `dist.next` → `dist`,
restarts the player and puts the previous engine back if the new one does not confirm rendering
within 5 minutes. The launcher runs `engine_guard.js boot-check` before Electron (power cut during
a switch). Log: `node_modules/electron/engine-guard.log`. The guard starts `dist\electron.exe` directly (not through the scheduled task: `schtasks /run` asked from the guard started nothing). A first failure on Windows can come from the environment (antivirus scanning a new unsigned engine): the rollback absorbs it.

## Config

`%APPDATA%\signflow-player-windows\config.json` — directory = `app.getPath('userData')`,
**derived from the `name` in `package.json`** (`signflow-player-windows`). Keys: `backend_url`,
`org_token` (install) → `player_token` (after registration), `player_name`.
`capabilities.json` (D3D11/MediaFoundation, active by default except on "Basic Display Adapter")
lives in the same place.

## Logs / diagnostics

- `%USERPROFILE%\signflow-player.log` (Electron stdout/stderr) — no logrotate on
  Windows: **truncated when the task starts** if it exceeds 10 MB.
- Bounded persistent trace: `%APPDATA%\signflow-player-windows\logs\player.log`.
