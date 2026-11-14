# Vitrail — Operator's guide

🇫🇷 [Version française](GUIDE.fr.md)

This guide takes you from nothing to a working digital-signage network, on **your own
hardware**. Vitrail is self-hosted: no account, no cloud service, nothing that calls home.

- **Part 1 — Try it in 15 minutes**: one server, one screen, with what you already have.
- **Part 2 — Going to production**: sizing, HTTPS, players on every platform, updates,
  backups, the Telegram assistant and the rest.

---

## How Vitrail works

```
                ┌──────────────────────────────┐
  web browser → │  Vitrail server (Docker)     │ ← you manage content here
                │  CMS · API · media storage   │
                └──────────────┬───────────────┘
                               │  players connect TO the server
             ┌─────────────────┼─────────────────┐
         player              player            player
   (Linux, Raspberry Pi, Windows, Android, ChromeOS, browser, BrightSign)
```

- The **server** hosts the web interface, the database and the media.
- A **player** is the device behind a screen. It downloads its content, stores it locally and
  plays it from its own storage, so playback keeps going if the network drops.
- Connections always go **from the player to the server**. Nothing needs to be opened on the
  player side, which makes players easy to place behind any router.

---

# Part 1 — Try it in 15 minutes

## What you need

| | Minimum for a trial |
|---|---|
| **Server** | A machine running **Ubuntu 24.04**, real or virtual (old PC, mini-PC, VM, rented VPS): **4 CPU cores, 8 GB RAM, 40 GB free disk**, Internet access during installation. No graphics card needed. |
| **Screen** | Any device with a web browser on the same network: a PC, a laptop, a tablet, a TV with a browser. |

The server needs no prior setup: the installer installs Docker and everything else.

## 1. Install the server

On the Ubuntu machine:

```bash
curl -fsSL https://raw.githubusercontent.com/edkahall/vitrail/main/install-ubuntu.sh -o install-vitrail.sh
sudo bash install-vitrail.sh
```

The script builds and starts everything. It takes **10 to 20 minutes**, depending on the
machine and the connection. It asks no questions. At the end it prints:

- the address of the interface, `http://<server-ip>:8080`;
- the administrator e-mail (`admin@vitrail.local` unless you set `ADMIN_EMAIL=...` before
  running the script), its **password** and its **2FA secret**.

> ⚠️ The password and the 2FA secret are shown **once**. Copy them now.

Port 8080 is opened to your **local network only**. To reach the server from the Internet
for a quick test, re-run the script with `VITRAIL_PUBLIC_HTTP=1` in front of `sudo bash ...`.
Traffic is then plain HTTP, so do not leave it like that: Part 2 explains HTTPS.

## 2. Log in

1. Open `http://<server-ip>:8080` in a browser.
2. Enter the e-mail and password.
3. Two-factor authentication is **mandatory for administrators**. Add the 2FA secret to an
   authenticator app (Aegis, Google Authenticator, 2FAS, Microsoft Authenticator…), then type
   the 6-digit code.

The interface is in English by default; change the language in your **Profile**.

## 3. (Optional) Load demo content

To explore with a realistic network (fictitious shop chain, players, media, playlists,
schedules, campaigns…), run on the server:

```bash
cd /opt/signflow
sudo docker compose exec -T backend python3 /app/scripts/seed_demo_content.py
```

All demo content is generated on the spot (no real brand, photo or music). It is in English;
add `--lang fr` for French names and visuals.

## 4. Connect your first screen, in a browser

The **web player** is served by your own server: there is nothing to install.

1. In the interface: **Players → Provisioning → New token**. Copy the token (it starts with
   `sfp_`).
2. On the screen device, open:
   `http://<server-ip>:8080/player-chromeos/index.html`
3. Fill in **Backend URL** = `http://<server-ip>:8080` and **Player token** = the `sfp_` token,
   then click **Connect**.
4. The page shows *“No content to display”*: that is the expected state of a player with
   nothing scheduled yet. Press **F11** for full screen.

In **Players**, the new player is listed as **online**.

## 5. Put something on the screen

1. **Media → Upload**: add a few images or a video.
2. **Playlists → New playlist**: give it a name, **Add media**, then **Publish**.
3. **Schedules → New schedule**: choose the playlist, set *When to play?* to **Always**, and
   add your player as a target. Save.

Within a few seconds the screen starts playing the playlist.

You now have a working Vitrail network. The built-in manual (**Help** menu) covers every
screen in detail.

---

# Part 2 — Going to production

## 1. Choose the server

Vitrail's load does not grow like a video-streaming platform: players cache their content and
play it locally. Three resources size independently:

- **bandwidth** follows *how much content you push and how fast you want it everywhere*;
- **media storage** follows *the size of your catalogue*;
- **video encoding** follows *how much video you upload*. A GPU is far faster than the CPU.

| Fleet | Server (everything in Docker on one machine up to ~500 players) | Video encoding | Bandwidth |
|---|---|---|---|
| < 200 players | 8 vCPU · 32 GB RAM · 0.5–1 TB NVMe | 1 consumer GPU (NVIDIA NVENC) or Intel iGPU; CPU-only works for light video use | 100–200 Mbps symmetric |
| 200–500 | 16 vCPU · 64 GB RAM · 1–2 TB; consider external S3 storage | 1 GPU (T4/L4 class) | 300–500 Mbps, or a CDN |
| 500–1000 | several app servers, managed PostgreSQL and Redis, S3 + CDN | dedicated GPU node | CDN strongly recommended |

Detailed formulas and three costed scenarios: [`guide-infrastructure.md`](guide-infrastructure.md).

**Do I need a graphics card?** No. Without a GPU, videos are encoded on the CPU: fine for a
few videos a week, slow for many large or 4K videos. Vitrail detects and uses an **NVIDIA**
GPU (NVENC) or an **Intel** iGPU (QSV/VAAPI) automatically, once the GPU is made visible to
Docker: see [`GPU_ENCODE.md`](GPU_ENCODE.md). The **Settings → Server** page shows which
encoder is in use and the speeds actually measured.

**Players' storage**: plan 2 to 3 times the content each screen plays, plus the system. The
usual trap is low-end Android TV boxes with 16 GB of soldered storage: fine for Full HD, too
small for 4K playlists.

## 2. Install

Use the same installer as in Part 1 on the production machine. It installs into
`/opt/signflow`, stores its secrets in `/opt/signflow/.env` (generated, mode 600), starts the
stack, creates a systemd service (`signflow.service`, starts at boot) and an nginx front on
port 8080.

Useful variables, set in front of `sudo bash install-vitrail.sh`:

| Variable | Purpose |
|---|---|
| `ADMIN_EMAIL=you@example.com` | first administrator's e-mail |
| `VITRAIL_PUBLIC_HTTP=1` | open port 8080 to everyone (trial only) |
| `REPO_URL=...` | install from your own fork |

## 3. Domain name and HTTPS

For players and users outside your local network, put the server behind a reverse proxy with
TLS. Caddy obtains and renews Let's Encrypt certificates on its own.

1. Point a DNS name (for example `cms.example.com`) and a second one for media
   (`media.cms.example.com`) to the server, and open ports **80** and **443**.
2. Start from [`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example).
3. In `/opt/signflow/.env`, set:
   ```
   PUBLIC_WEB_URL=https://cms.example.com
   MINIO_PUBLIC_ENDPOINT=media.cms.example.com
   MINIO_PUBLIC_USE_SSL=true
   CORS_ORIGINS=["https://cms.example.com"]
   ```
   `MINIO_PUBLIC_ENDPOINT` is the address **browsers and players** use to download media
   (signed URLs). It must be reachable by them.
4. Apply: `cd /opt/signflow && sudo docker compose up -d`.

Players then use `https://cms.example.com` as their backend URL.

## 4. Secure the server

- Keep the firewall to the strict minimum: SSH, 80/443 (proxy), and the media port only if
  you do not proxy it.
- Docker publishes ports **past `ufw`**. The stack only publishes what it needs; do not add
  port bindings in `docker-compose.yml`, put them in `docker-compose.override.yml` and bind
  them to `127.0.0.1` when they are internal.
- 2FA is enforced for administrators; enable it for every account that can change content.
- **Organisations and roles.** One server can host several organisations (brands, customers),
  isolated from each other by PostgreSQL row-level security. The platform role that manages
  organisations is the **super-admin**, created on the server only:
  ```bash
  cd /opt/signflow
  sudo docker compose exec -T backend python3 /app/scripts/create_super_admin.py --email you@example.com
  ```
  Then manage users, groups and custom roles in **Settings → Users & Roles**.

## 5. Optional services

All are configured in `/opt/signflow/.env`, then applied with
`sudo docker compose up -d`.

| Feature | What to set |
|---|---|
| **E-mail** (alerts, approvals, advertiser portal) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS`; and `PUBLIC_WEB_URL` for the links in e-mails |
| **AI assistant** (built-in help, ask questions about your network) | `ANTHROPIC_API_KEY` |
| **AI content generation** (images, video, voice) | per organisation, in **Settings → AI Keys** (fal.ai, ElevenLabs, Anthropic): each organisation uses its own keys, encrypted at rest |
| **Free stock photos** | `UNSPLASH_ACCESS_KEY`, `PEXELS_API_KEY` |
| **Telegram assistant** | see §8 |

Without these, Vitrail works normally; only the corresponding feature is unavailable.

## 6. Players for production

Every player is enrolled the same way: create a **provisioning token** (`sfp_…`) in
**Players → Provisioning**, give it to the device once, and the device exchanges it for its
own credentials. A token can enrol several players and can expire.

| Platform | Best for | How to install |
|---|---|---|
| **Web / ChromeOS** | trials, Chromebox/Chromebook kiosks, any browser | open `<server>/player-chromeos/index.html`, or pass `?backend=<url>&token=sfp_…` in a managed kiosk |
| **Chrome extension** | screens already driven by a Chrome PC | load `player-extension/` in Chrome (developer mode) and enter the token in its popup |
| **Linux** (Debian 12) | mini-PCs, NUCs; best for 4K and video walls | on a machine dedicated to the screen: `sudo git clone --depth 1 https://github.com/edkahall/vitrail /opt/signflow`, then `sudo bash /opt/signflow/player-linux/install.sh signflow https://cms.example.com` (kiosk session, autologin, autostart), then put the `sfp_` token in `player_token` of `~signflow/.config/signflow-player/config.json` and reboot — or let `signflow-provision` do all of it |
| **Raspberry Pi** 4/5 (Raspberry Pi OS) | cheap Full HD screens, audio-only players | same, with `player-rpi/install.sh`: an on-screen wizard asks for the server URL and the token; or build a ready-to-flash SD image that enrols itself at first boot (`installer/image-rpi/`) |
| **Windows** 10/11 | existing Windows signage PCs | copy `player-windows/` to the PC and run `install.ps1` as administrator |
| **Android** 9+ (Android TV boxes, tablets) | low-cost players | **trials**: the APK from the [Releases](https://github.com/edkahall/vitrail/releases) page; **a real fleet**: an APK signed with **your own key** (see [Android: your own signing key](#android-your-own-signing-key)). Install it (`adb install`), then enter the server URL and token; for a locked-down kiosk see [`player-android/README.md`](../player-android/README.md) |
| **BrightSign** | professional media players | **Players → Provisioning** generates a ready-to-copy SD card |

**Installing many players**: the `signflow-provision` tool enrols a device remotely over SSH
(Linux, Raspberry Pi), WinRM (Windows) or ADB (Android), in one command. See
[`installer/README.md`](../installer/README.md) and the field runbook
[`RUNBOOK_POSE_PLAYER.md`](RUNBOOK_POSE_PLAYER.md).

**Checking a player**: it is **online** in **Players**, it appears only once (re-enrolling an
existing device needs the re-attach option, otherwise you get a duplicate), and the screen
plays, or shows the idle screen if nothing is scheduled.

**Taking an Android device back**: in kiosk mode (Device Owner) Android refuses to uninstall
the app. Run `adb shell content call --uri content://app.vitrail.player.admin --method release_device_owner`, then `adb uninstall app.vitrail.player` — no factory reset. Only adb can
send this command; apps on the device are refused.

### Android: your own signing key

**Rule: for trials, the official APK; for a real fleet, your own key.**

Android only installs an update if it is signed with **the same key** as the app already on
the device. Whoever holds the key of the APK you installed is therefore the only one who can
ever produce updates for your devices.

- The APK on the Releases page is signed with the **project's** key. It is fine for trying
  Vitrail. But a fleet installed with it depends on the project to receive Android updates:
  if the project stops publishing releases, or the key is lost, your devices can only be
  updated by uninstalling and re-enrolling each one.
- With **your own key**, you depend on nobody: you can rebuild the APK whenever you want
  (with your fixes, or the community's) and update your fleet yourself.
- **Choose at the start.** The official APK and yours share the same package name
  (`app.vitrail.player`) but not the same key, so moving a device from one to the other
  means uninstalling, reinstalling and re-enrolling it.

Nobody else can update your players either way: an update is only installed if it comes
from **your** server (Settings → Updates) **and** is signed with the right key.

**1. Create the key** (once, on your own computer; `keytool` comes with any Java JDK 17+):

```bash
keytool -genkeypair -keystore my-vitrail.jks -storetype PKCS12 -alias vitrail   -keyalg RSA -keysize 4096 -validity 10000 -dname "CN=My company signage"
```

It asks for a password (with PKCS12, the key password is the same).

**2. Back it up, now, in at least two places**, one of them offline (NAS, encrypted USB
drive, password manager for the password). **If you lose this key, no update can ever be
installed on your Android players again** without reinstalling every device. If it leaks,
someone could sign an update your devices would accept. Never commit it to a repository.

**3. Build your APK**, with either method:

- **On GitHub** (no Android tooling needed): fork the repository, add four repository
  secrets (Settings → Secrets and variables → Actions): `ANDROID_KEYSTORE_BASE64` (the file
  in base64: `base64 -w0 my-vitrail.jks`), `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`
  (`vitrail` above) and `ANDROID_KEY_PASSWORD` (the same password). Then push a tag
  (`git tag v1.0.1-mine && git push origin v1.0.1-mine`): the `Release players` workflow
  publishes a release in your fork with an APK signed by your key. The release notes say
  `release-signed`; `debug-signed` means a secret is missing.
- **On your computer** (Android SDK + JDK 17): copy the key into `player-android/`, create
  `player-android/keystore.properties` (both are ignored by git):

  ```properties
  storeFile=my-vitrail.jks
  storePassword=…
  keyAlias=vitrail
  keyPassword=…
  ```

  then `cd player-android && ./gradlew assembleRelease`. The APK is in
  `app/build/outputs/apk/release/`.

**4. Check the signature** before deploying (`apksigner` is in the Android SDK build-tools):
`apksigner verify --print-certs app-release.apk` must show your certificate.

**5. Use it everywhere**: install it on new devices (`adb install`, or
`signflow-provision --os android --apk app-release.apk`), and import the same kind of APK,
built later with the same key and a higher `versionCode`, in **Settings → Updates** to update
the fleet.

## 7. Updates

### The server

Re-run the installer:

```bash
sudo bash install-vitrail.sh
```

It pulls the latest code, keeps your `.env`, rebuilds the containers and applies the database
migrations. Interface and API are briefly unavailable during the rebuild; screens keep playing
from their local cache.

Watch the repository's releases to know when to update.

### The players

Players update **over the air**, from your server, and **only when you approve it**.

1. Download the player packages from the
   [Releases](https://github.com/edkahall/vitrail/releases) page:
   `signflow-player-linux-<version>.tar.gz`, `-rpi-`, `-windows-`, and the Android `.apk`.
2. **Settings → Updates**: import the package, then create a deployment towards the players
   of your choice. Start with a small test group (canary), then extend.
3. Each player downloads the package **from your server**, installs it and reports its new
   version.

Web/ChromeOS players, the Chrome extension and BrightSign are updated with the server itself.

**Building the packages yourself** (fork, custom build): push a tag `v*` and the
`Release players` GitHub Actions workflow builds them and publishes a release. For Android,
see [Android: your own signing key](#android-your-own-signing-key): an update must be signed
with the **same key** as the installed app.

### The Electron engine (Linux, Raspberry Pi and Windows players)

A player update changes the player's code, never its Electron engine. The engine is updated on
its own, from **Settings → Updates → Electron engine**:

1. **The server operator** (super-admin, or the org admin of a server without one) types an
   Electron version (for example `42.11.18`, see [releases.electronjs.org](https://releases.electronjs.org)),
   ticks the platforms and clicks **Get**. The server downloads the official Electron release,
   checks it against the `SHASUMS256.txt` published by Electron, repackages it and stores it.
   It arrives in **beta**. Players never download anything from the Internet.
2. **Each org admin** deploys it to their own screens, from the same drawer: choose the
   target, optional time windows, then **Deploy**. A beta engine can only be tried on **one
   player** at a time. Once the operator **promotes** it, it can go to groups, sites or the whole
   organisation.
3. The player downloads the engine, checks it, extracts it next to the current one, then hands
   over to a small guard and stops. The guard switches engines and restarts the player. The new
   engine must confirm it renders within **5 minutes**; otherwise the guard puts the previous
   engine back on its own, and the deployment stops at the first failure.

A power cut during the switch is handled at the next start (the launchers run the guard's
`boot-check` before Electron). The guard writes its log to
`node_modules/electron/engine-guard.log` in the player folder.

Players installed before this feature need their launcher refreshed once (re-run the player's
`install.sh` / `install.ps1`) to get the start-up check and, on Linux, an engine owned by the
player account.

## 8. Telegram assistant (“Jeanine”)

Your users can talk to Vitrail from Telegram: ask about the network, and run actions after
confirmation, with their own permissions. It needs the server to be reachable over **HTTPS**
(§3).

1. In Telegram, talk to **@BotFather** → `/newbot`, choose a name. Keep the **bot token**.
2. Generate a random secret: `openssl rand -hex 32`.
3. In `/opt/signflow/.env`:
   ```
   CMS_TELEGRAM_BOT_TOKEN=<bot token>
   CMS_TELEGRAM_WEBHOOK_SECRET=<random secret>
   ```
   and apply with `sudo docker compose up -d`.
4. Tell Telegram where to send messages (HTTPS on port 443, 80, 88 or 8443 only):
   ```bash
   curl "https://api.telegram.org/bot<bot token>/setWebhook" \
        -d url=https://cms.example.com/api/v1/messaging/telegram/webhook \
        -d secret_token=<random secret>
   ```
5. Each user links their account from **Profile → Messaging — Telegram** (a one-time code
   sent to the bot). The organisation administrator sees and revokes linked accounts in
   **Settings → Messaging**.

The assistant uses the AI keys of §5 and the quotas set per organisation.

## 9. Alerts and monitoring

- **Settings → Notifications**: webhook (Slack, Teams, n8n…) and e-mail channels, with rules
  for player offline, feed in error, failed update, campaigns and approvals.
- External monitoring: `GET /health` answers `{"status":"ok"}` when the API is up. Any uptime
  monitor (Uptime Kuma, for example) can poll it.

## 10. Backups and restore

Back up three things, daily:

| What | How |
|---|---|
| **Database** | `cd /opt/signflow && sudo docker compose exec -T postgres pg_dump -U signflow -Fc signflow > vitrail-$(date +%F).dump` |
| **Media** | copy the MinIO volume (`docker volume inspect signflow_minio_data` gives its path), or mirror the bucket with `mc mirror` |
| **Configuration** | `/opt/signflow/.env` and `docker-compose.override.yml`. They contain the secrets, including the key that decrypts the organisations' AI keys (`CMS_VAULT_KEY`): store them safely. |

Restore: reinstall, put back `.env`, restore the dump with `pg_restore --clean`, put back the
media, start the stack. **Test a restore** regularly on a throwaway machine: an untested
backup is not a backup.

[`deploy/backup/`](../deploy/backup/) contains a complete daily backup script (database,
roles, configuration, media mirror with a 30-day trash) and a restore check. Use them as a
starting point and adapt the destination to your storage.

## 11. Troubleshooting

| Symptom | Where to look |
|---|---|
| The interface does not open | `sudo docker compose ps` in `/opt/signflow`, then `sudo docker compose logs backend --tail 50` |
| The backend stops at start with a `DATABASE_APP_URL` error | your `docker-compose.override.yml` predates the fix; compare it with the current `docker-compose.yml` |
| A player stays offline | check its backend URL, that it reaches the server (`curl <server>/health` from the device), and the date/time of the device (a Raspberry Pi without network has no clock) |
| A video is “processing” for a long time | **Settings → Server**: encoder in use and queue |
| Thumbnails or media do not load in the browser | `MINIO_PUBLIC_ENDPOINT` must be reachable by the browser and the players |

## 12. Uninstall

```bash
cd /opt/signflow
sudo docker compose down -v          # ⚠️ deletes the database and the media
sudo systemctl disable --now signflow.service
sudo rm -f /etc/systemd/system/signflow.service
sudo rm -rf /opt/signflow
```

Then remove the nginx site the installer created in `/etc/nginx/sites-enabled/` and the
firewall rules for port 8080.

---

## Getting help, contributing

- Questions and bugs: open an issue on GitHub.
- Security issues: see [`SECURITY.md`](../SECURITY.md); do not open a public issue.
- Contributions are welcome: see [`CONTRIBUTING.md`](../CONTRIBUTING.md).
