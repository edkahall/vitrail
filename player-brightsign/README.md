# BrightSign Player (HD1024 / Series 5) — Vitrail

🇫🇷 [Version française](README.fr.md)

Port of the Vitrail player to **BrightSign** (BrightSignOS). Test target: **HD1024**
(Series 5 "expanded I/O": **4K**, **H.265/HEVC**, microSD, RS232, GPIO).

## Principle

BrightSignOS runs a **Chromium** engine via `roHtmlWidget`. We therefore reuse the existing
**web player** (the same one as ChromeOS / extension):

```
autorun.brs (BrightScript, sur la carte SD)
   └─ crée un roHtmlWidget plein écran  +  brightsign_js_objects_enabled=true
        └─ charge  {backend}/player-brightsign/index.html?backend=&token=
              └─ index.html pose window.__SF_PLAYER_OS__='brightsign'
                   └─ charge le renderer PARTAGÉ /player-chromeos/{signflow,audio_player,player}.js
```

(Diagram: `autorun.brs` on the SD card creates a full-screen `roHtmlWidget` with JS objects
enabled, which loads `index.html`; the page sets the `brightsign` identity, then loads the
SHARED renderer.)

- **Renderer = 100 % reused** (served by the frontend, a single source of truth).
- **BrightSign-specific** = only `autorun.brs` (shell) + the `index.html` page + a branch
  **inert elsewhere** in `signflow.js` (native OS reboot, device info via
  `@brightsign/deviceinfo`).
- **Rendition selection**: no server change — the pipeline is driven by the
  **capabilities** reported by the player (`browser_only` via `video_renderer='auto'` → MP4
  containers; 4K HEVC served only if `canPlayType('hvc1')` is true). The HD1024 will receive
  **H.264 FHD** by default, and **HEVC/4K** if it declares it.

## Files

| File | Role |
|---|---|
| `player-brightsign/autorun.brs` | BrightScript shell: reads `config.json`, creates the `roHtmlWidget`, enables JS objects, reboot bridge |
| `player-brightsign/config.json.example` | Config template (backend_url, token, resolution) |
| `frontend/public/player-brightsign/index.html` | Served page: sets the `brightsign` identity + loads the shared renderer |
| `player-chromeos/src/signflow.js` | Shared bridge — BrightSign branch (reboot/deviceinfo) guarded by `IS_BS`, inert elsewhere |

## Server prerequisites

1. The `/player-brightsign/index.html` page must be served by the frontend.
   - **Test bench**: already served (`frontend/public/...`), URL `http(s)://<banc>/player-brightsign/index.html`.
   - **Production**: deploy = `git pull` + **frontend rebuild** (`docker compose restart frontend`).
2. **Create the player in the CMS** (Players → Provisioning) to get a **provisioning
   token `sfp_…`** (or a direct player JWT).

## Preparing the (micro)SD card

> **Recommended approach**: Use the built-in SD card generator in Vitrail (**Players → Provisioning → BrightSign SD card**). It automatically bundles the configuration, `autorun.brs`, `bootstrap.html`, and pre-packaged offline renderer files (`sf_rnd_*`) into a ready-to-unzip archive.

If preparing the microSD card manually:

1. Format the microSD as **FAT32** (or exFAT depending on the BOS).
> **⚠️ Required firmware: BOS 9.1.x** (validated on 9.1.132). The old BrightScript fork of 8.5.70 **refuses
> to compile** `Left()`/`Chr()`/`Str()` → update it (copy the `.bsfw` to the root of the SD card, boot,
> it applies itself). A clean reference SD image is in `SD_card_BS/sd_image_clean/`.

2. Copy all required files to the **root** of the card:
   - `autorun.brs` (from `player-brightsign/autorun.brs`)
   - `bootstrap.html` (from `frontend/public/player-chromeos/brightsign/bootstrap.html` — required for the local HTTP origin and initial widget load)
   - `config.json` (copy of `config.json.example`, filled in):
     ```json
     {
       "backend_url": "https://cms.example.com",
       "token": "sfp_…",
       "width": 1920,
       "height": 1080,
       "video_mode": "1920x1080x60p",
       "dws": true,
       "autorun_autoupdate": true
     }
     ```
     > **`"dws": true`** → the autorun calls `roNetworkConfiguration.SetupDWS({port:80, open:<mdp>, basic:true})`
     > which **enables the local DWS** (disabled by default in BOS 9.x) + sets the password (`admin` / password
     > **derived per device** `Sf9Kqr<serial>Dx7!`, P0.3 — **basic** auth because digest fails on a POST-with-body:
     > no rewind). The screenshot captures the **composite** framebuffer (HW video included) via the LDWS snapshot.
     > Effective on the **2nd reboot** after insertion (the LDWS is configured at boot, effective on the next one).
     > **`"autorun_autoupdate": true`** (P1.2) → the autorun updates itself from production
     > (`/player-chromeos/brightsign/autorun.brs`) at boot, with anti-brick safeguards (size/prefix), effective on the
     > next boot. Opt-in (risky feature). **Publication sync**: after any change to
     > `player-brightsign/autorun.brs`, copy the file to **`frontend/public/player-chromeos/brightsign/autorun.brs`**
     > (= the copy served by production, same dual model as the renderer) then rebuild the production frontend.
   - **`ss_body.json`** = `{"width":1280,"height":720}` — _OPTIONAL since P0.2_: it is the body of the DWS
     snapshot POST (capture size). **The autorun creates it by itself at boot if it is missing** (`FormatJson`,
     no escaped quote at compile time) and the screenshot handler rebuilds the body if the file is
     unreadable → no more silently failing screenshots on an SD card without this file. Only copy it by hand
     to **force a different** capture size.
   - **Renderer files (for offline first boot)**: Pre-provisioning these files prevents the player from remaining stuck on "Connecting…" when network connectivity takes time to initialize at boot:
     - `sf_rnd_ed25519.js` (copy of `frontend/public/player-chromeos/ed25519.js`)
     - `sf_rnd_signflow.js` (copy of `frontend/public/player-chromeos/signflow.js`)
     - `sf_rnd_audio_player.js` (copy of `frontend/public/player-chromeos/audio_player.js`)
     - `sf_rnd_player.js` (copy of `frontend/public/player-chromeos/player.js`)
     - `sf_rnd_style.css` (copy of `frontend/public/player-chromeos/style.css`)
3. Insert the card into the HD1024, connect the screen (HDMI) + the network (Ethernet/DHCP), power on.

> On first boot: the bridge exchanges `sfp_…` for a **JWT**, persists it in localStorage
> (`storage_path` on the card → survives reboots). Token rotation ("Option B") is
> handled as on the other players (`token:refresh`).

## ✅ Validation checklist on the HD1024

The **VERIFY** items = possible variations depending on the HD1024's BrightSignOS version.

- [ ] **Boot**: the screen shows the player (Vitrail logo / content), not a blank page.
- [ ] **Online**: the player goes **online** in the CMS (WS connected).
- [ ] **Content**: an **image** is displayed; an **H.264 FHD video** plays (HTML5 `<video>`).
- [ ] **Enriched heartbeat** (BrightSign JS objects active): the player drawer shows `bs_model`,
      `bs_serial`, `osversion`, `temperature_c`, `loadavg`/`meminfo` in `capabilities`.
      → if **missing**, `brightsign_js_objects_enabled`/`AllowJavaScriptUrls` did not take effect
      (the player still works, in standard browser mode). **VERIFY**.
- [ ] **`restart` command** (from the CMS): the player reloads.
- [ ] **`reboot_os` command**: the HD1024 **really reboots**.
      → tests the native path (`@brightsign/system`) then the **bridge** (`PostBSMessage` →
      `RebootSystem()`). **VERIFY** which path works; otherwise fallback = reload.
- [ ] **Persistent token**: after a reboot, the player reconnects **without re-provisioning**
      (JWT in localStorage via `storage_path`). **VERIFY** the `SD:/sf_html_storage` path.
- [ ] **Screenshot**: the screenshot command returns a thumbnail (canvas — OK for image/HTML;
      possibly tinted on video, like the other browser players).
- [ ] **HEVC / 4K** (HD1024 bonus): assign a **4K HEVC** media → check whether the server serves
      the HEVC variant (depends on `canPlayType('hvc1')` of the BrightSign Chromium). Otherwise it serves
      H.264 FHD (safe). **VERIFY**.

## MVP limitations (accepted)

- **Streaming-only** (no local offline cache): the Service Worker is not attached (the page
  loads the renderer from `/player-chromeos/` → different SW scope) → graceful streaming fallback,
  like the Chrome extension. Offline = roadmap (BrightSign disk cache + URL rewriting).
- **Screenshot** = canvas (not the native `@brightsign/screenshot` capture → upload, later).
- **MAC / LAN IP** not reported (possible via `@brightsign/networkconfiguration` — roadmap).
- **No LAN sync** (UDP) and no mpv — inert (like ChromeOS/extension).

## Roadmap (post-validation)

1. **Native offline cache**: download media to the card via BrightScript / `@brightsign`
   filesystem + rewrite `localMediaUrl`/`rewriteSyncForStore` (download-then-play) — or validate
   that the HTML5 SW works inside `roHtmlWidget` and attach it.
2. **Native screenshot** (`@brightsign/screenshot` → file read → `POST /players/{id}/screenshot`).
3. **MAC/IP/uptime** via `@brightsign/networkconfiguration` + `@brightsign/deviceinfo`.
4. **4K HEVC** confirmed + possible native `roVideoPlayer` renderer as an overlay if the HTML `<video>`
   hits a ceiling (unlikely on HD1024, HW HEVC).
5. **GPIO / RS232** (screen control §III) via BrightScript if needed.

## Development notes

- `autorun.brs` (BrightScript) **cannot be linted** off-device (no local tooling) → validated
  on the HD1024.
- The BrightSign branch of `signflow.js` is **guarded by `IS_BS`** (`window.__SF_PLAYER_OS__==='brightsign'`)
  → **strictly inert** for ChromeOS and the extension (same shared files). Re-sync source →
  public after any change: `cp player-chromeos/src/signflow.js frontend/public/player-chromeos/signflow.js`.
