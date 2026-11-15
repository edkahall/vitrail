# CLAUDE.md — Vitrail

This file briefs an AI assistant (Claude Code, or Claude in a chat where the user uploads this
file) so it can help someone **install, run or contribute to Vitrail**. Humans are welcome to
read it too.

**Answer in the user's language.** Be concrete: one step at a time, with the exact command, and
what the user should see when it worked.

---

## What Vitrail is

Vitrail is a free (AGPL-3.0) self-hosted **digital-signage platform**: a multi-tenant web CMS
plus players for Linux, Raspberry Pi, Windows, Android, ChromeOS / any web browser, a Chrome
extension and BrightSign. Everything runs on the user's own hardware; nothing calls home.

- Server: Docker Compose stack — FastAPI (Python 3.12) backend, Next.js 15 frontend,
  PostgreSQL 16 + pgvector, Redis, Celery workers, MinIO (media storage), nginx front on port
  **8080** (installed by the installer, outside Docker).
- Players **connect to the server** (never the reverse). They cache content and play it
  locally.
- Main documentation: `docs/GUIDE.md` (English) / `docs/GUIDE.fr.md` (French). Read it before
  improvising; it was written from a real installation on a fresh machine.

---

## Your role when helping with a first installation

### 1. Understand the situation first (ask, briefly)

- **Goal**: a quick trial, or a production network? How many screens, roughly?
- **Server**: what machine? Vitrail's installer targets **Ubuntu 24.04** (real machine, VM or
  rented VPS). Trial minimum: 4 CPU cores, 8 GB RAM, 40 GB free disk. A GPU is optional
  (it only speeds up video encoding).
- **Screens**: for a trial, any device with a web browser on the same network is enough.
- **Network**: local network only, or reachable from the Internet? Do they have a domain name?

If they are on Windows or macOS without Ubuntu, suggest an Ubuntu 24.04 virtual machine or a
small rented VPS rather than an unsupported manual install.

### 2. Trial path (follow `docs/GUIDE.md`, Part 1)

1. **Install** (on the Ubuntu machine):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/edkahall/vitrail/main/install-ubuntu.sh -o install-vitrail.sh
   sudo bash install-vitrail.sh
   ```
   10–20 minutes, no questions asked. **Success looks like**: the script ends with the
   interface address `http://<ip>:8080`, an admin e-mail, a password and a 2FA secret.
   Tell the user to **copy the password and the 2FA secret immediately** (shown once).
   If they are lost, they are at the end of the installer's output; they can also create a new
   admin (see below).
2. **Check the stack**: `cd /opt/signflow && sudo docker compose ps` (all containers *Up*,
   postgres/redis/minio *healthy*) and `curl -s http://127.0.0.1:8080/health` →
   `{"status":"ok",...}`.
3. **Log in** at `http://<ip>:8080` — **with `:8080`**. 2FA is mandatory for admins: add the
   secret to an authenticator app (Aegis, Google Authenticator, 2FAS…).
4. **Optional demo data**: `sudo docker compose exec -T backend python3 /app/scripts/seed_demo_content.py`.
5. **First screen in a browser**: Players → Provisioning → **New token** (starts with `sfp_`);
   on the screen device open `http://<ip>:8080/player-chromeos/index.html`, enter
   Backend URL `http://<ip>:8080` and the token, **Connect**. **Success looks like**: the page
   shows "No content to display", and the player is **online** in Players.
6. **Show content**: Media → Upload; Playlists → New playlist → Add media → **Publish**;
   Schedules → New schedule → playlist, *When to play?* = Always, target = the player.

### 3. Production path (`docs/GUIDE.md`, Part 2)

Walk through, in this order, only what they need: sizing (with/without GPU, see
`docs/guide-infrastructure.md` and `docs/GPU_ENCODE.md`) → domain + HTTPS with Caddy
(`deploy/caddy/Caddyfile.example`, then `PUBLIC_WEB_URL`, `MINIO_PUBLIC_ENDPOINT`,
`MINIO_PUBLIC_USE_SSL`, `CORS_ORIGINS` in `/opt/signflow/.env`) → firewall → players per
platform (`docs/GUIDE.md` §6, `installer/README.md`, `docs/RUNBOOK_POSE_PLAYER.md`) →
updates → backups → optional services (SMTP, AI keys, Telegram assistant).

**Android players: ask early whether this is a trial or a real fleet.** The APK on the
Releases page is signed with the project's key: fine for a trial, but a fleet installed with
it can only get Android updates from the project. For a real fleet, the operator must create
**their own signing key**, back it up in two places, and build the APK with it
(`docs/GUIDE.md` §6, "Android: your own signing key"). Say it before the first device is
installed: switching keys later means reinstalling and re-enrolling every device. Never ask
the user to paste the key or its password into the conversation.

---

## Pitfalls actually met — check these first

| Symptom | Cause / fix |
|---|---|
| "I can't open the interface" | The URL needs **`:8080`**. Port 8080 is opened by the installer to **private networks only**; from the Internet use an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 user@server`, then `http://localhost:8080`), HTTPS via Caddy, or re-run the installer with `VITRAIL_PUBLIC_HTTP=1` (plain HTTP — trial only). |
| `adb uninstall app.vitrail.player` fails with `DELETE_FAILED_DEVICE_POLICY_MANAGER` | The app is Device Owner (kiosk). Release it first: `adb shell content call --uri content://app.vitrail.player.admin --method release_device_owner` (player 2.5.36+), then uninstall. Never suggest a factory reset before trying this. |
| Electron engine update refused with `launcher not updated` | The player was installed before engine updates existed: its launcher does not run the start-up check (`engine_guard.js boot-check`), so a power cut during a switch could leave the screen dark. Re-run the player's installer once (`install.sh` / `install.ps1`), restart the player, then deploy again. The guard's log is `node_modules/electron/engine-guard.log` in the player folder. |
| OS updates drawer says "OS updates not set up" for a Linux / Raspberry Pi player | The root helper is missing (player installed before OS updates existed). Run `sudo bash scripts/install-os-update.sh` once on the player, from a **fresh copy of the release** (git clone or downloaded archive), never from the player's own folder, which the player account can rewrite. Check: `systemctl start signflow-os-update.service` as the player account, then `cat /var/lib/signflow-os-update/status.json`. |
| OS deployment fails with `obsolete: …` | A version of the manifest has left the Debian archive. Update a bench player again to produce a new manifest, approve it, deploy that one. Nothing else was installed. |
| OS deployment fails with `outside the manifest: …` | apt wanted to install or remove something that is not in the manifest (the player differs from the bench). Nothing was installed. Use a bench closer to that player, or let that player be its own bench. |
| Backend container restarts, log says `DATABASE_APP_URL manquant` | An old `docker-compose.override.yml`. `DATABASE_APP_URL` must reach the backend and workers (it is defined in the current `docker-compose.yml`). |
| Thumbnails / media do not load | `MINIO_PUBLIC_ENDPOINT` must be an address the **browser and players** can reach (signed URLs). Port 9000 must be reachable, or MinIO proxied by Caddy with `MINIO_PUBLIC_USE_SSL=true`. |
| A port the user did not expect is reachable from the Internet | Docker publishes ports **past ufw**. Internal ports must be bound to `127.0.0.1` in `docker-compose.override.yml`. |
| Player stays offline | Wrong backend URL (forgot `:8080` or `https`), device cannot reach the server (`curl <server>/health` from the device), or wrong clock on a Raspberry Pi without network. |
| Re-enrolling a device created a duplicate player | Use the re-attach option of `signflow-provision` (`--reattach <id>`), or delete the duplicate. |
| Video stuck "processing" | Settings → Server shows the encoder in use and the queue. Without a GPU, large/4K videos encode slowly on the CPU. |
| LiteLLM container restarting | The AI assistant needs `ANTHROPIC_API_KEY` in `.env`; everything else works without it. |

Useful commands (in `/opt/signflow`):
`sudo docker compose ps` · `sudo docker compose logs backend --tail 50` ·
`sudo docker compose exec -T backend alembic current` ·
update = **re-run the installer** (`sudo bash install-vitrail.sh`: git pull, rebuild, migrations,
keeps `.env`).

Create another administrator or a super-admin (platform role that manages organisations):
`sudo docker compose exec -T backend python3 /app/scripts/create_super_admin.py --email you@example.com`
(prints the password and 2FA secret once).

---

## Safety rules for you, the assistant

- **Ask before anything destructive or hard to undo**: `docker compose down -v` (deletes the
  database and media), removing volumes, `rm -rf /opt/signflow`, dropping tables, re-running
  `bootstrap.py` on an existing install.
- **Ask before exposing anything to the Internet** (opening ports, `VITRAIL_PUBLIC_HTTP=1`),
  and explain that the interface is plain HTTP until HTTPS is set up.
- **Never print, paste or log secrets**: `.env` values, passwords, 2FA secrets, `sfp_` tokens,
  API keys, `CMS_VAULT_KEY`. When you need to show configuration, mask the values.
- **Verify, don't assume**: after each step, check the observable result (container state,
  `/health`, player online) before moving on. "The script finished" is not "it works".
- Do not edit `docker-compose.yml` for site-specific settings: use `.env` and
  `docker-compose.override.yml` (both kept across updates).

---

## Helping a contributor

- Layout: `backend/` (FastAPI + Celery, Alembic migrations in `backend/alembic/versions`),
  `frontend/` (Next.js App Router, next-intl, Tailwind), `player-*/`, `installer/`,
  `deploy/`, `docs/`. Details in `README.md`.
- Dev stack: `cp .env.example .env && docker compose up -d && make dev-init`, then
  `docker compose exec -T backend python3 /app/scripts/bootstrap.py`. Put port bindings in
  `docker-compose.override.yml`.
- Tests: backend `make test` (pytest, needs postgres/redis/minio up); frontend
  `cd frontend && npx tsc --noEmit && npx eslint . && npx playwright test`. CI runs both on
  every push (`.github/workflows/ci.yml`). A new test should fail on the code before the fix.
- Rules that bite (see `CONTRIBUTING.md`): tenant isolation is PostgreSQL row-level security
  (another organisation's resource answers **404**); RBAC permissions are `module:operation`
  pairs seeded by migrations; never edit a released migration; a new env variable goes in
  `backend/app/core/config.py` **and** in every compose service that uses it; numeric inputs use
  `components/ui/NumberField.tsx`; every UI string needs a key in all 5 locales
  (`frontend/messages/*.json`), in the right namespace; players display English only.
- Python dependency versions are pinned in `backend/constraints.txt`
  (`pip install -c constraints.txt -e ".[dev]"`); regenerate it after a validated upgrade.
- Player packages are built by `.github/workflows/release.yml` on a `v*` tag.
- Some code comments and design notes are in French; English versions of the docs exist
  next to them (`*.md` = English, `*.fr.md` = French).
- Commits must be signed off (DCO): `git commit -s`.
