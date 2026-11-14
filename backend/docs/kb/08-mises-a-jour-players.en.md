---
title: Player updates (OTA)
section: players
roles: [CMS_ORG_ADMIN]
keywords: [update, OTA, version, deployment, players, updates, version management, rollback, maintenance window, up to date, outdated, beta, canary, test group, suspend, pause, import, archive, tar.gz, apk, served versions, declare, pinning, pin, brightsign, chromeos, firmware, bs_bos, mise à jour, déploiement, engine, electron, electron engine, rollback, roll back]
---

# Player updates (OTA)

Vitrail updates its players **remotely** (over-the-air), with no on-site intervention.
Everything is driven from **Settings → Updates** (`/settings/updates`). The mechanism covers
**all player types**: Linux, Raspberry Pi, Windows (Electron), Android, BrightSign,
ChromeOS and the Chrome extension.

**Basic rule: no automatic update without your approval.** You publish a
version to the catalog, then you create an approved deployment to the players of your choice.

## Fleet inventory

The page lists each player with its **current version**, the **latest version available** for
its platform, and its **up to date / outdated** status (plus its site and groups). Combined
filters (OS, version, status, site, group, search) help with targeting.

A player is flagged "outdated" **only** if the catalog contains a version
published for **its platform** whose number (SemVer) is higher than its own. If there is no
version published for its platform, it is considered "up to date" (nothing to push to it).

## Two kinds of players (important)

Depending on the platform, the version arrives in two different ways:

- **Players installed on the device** — **Linux, Raspberry Pi, Windows, Android**. Their code is
  embedded on the machine: the update is an **archive** downloaded from your server.
  You **import** this archive into the catalog (see below).
- **Players served by the server** — **BrightSign, ChromeOS**. Their code is a static file
  **served by your server**: it therefore travels **with the update of the server itself**. You
  import nothing; you **declare** the served version so you can pin and deploy it.

In both cases, **nothing is applied without your deployment**: updating the server
does **not** advance a player until you have deployed the new version to it
(**pinning**). This is what lets you test a version on a **sandbox** of players
while the rest of the fleet stays on the old one.

## Import a version (Linux, RPi, Windows, Android)

In **Updates → Import a version**, two methods:
- **By URL**: paste the archive link from the distribution page
  (e.g. `signflow-player-linux-1.5.15.tar.gz`) — the server fetches it.
- **By file**: upload the `.tar.gz` (Electron) or `.apk` (Android) archive.

The system **automatically detects** the platform (file name) and the **version**
(read inside the archive), and **checks whether it is already present**. A **preview**
is displayed before confirming. On validation, the archive is stored **on your server** and
added to the catalog: players will download it **from your server** at deployment.

## Served versions (BrightSign, ChromeOS)

The **"Versions served by this server"** panel shows the BrightSign and
ChromeOS renderer version your server currently serves. Click **"Declare"** to add it to the catalog
→ it becomes **deployable** (pinnable). As for the others, as long as you do not deploy it,
BrightSign/ChromeOS players **do not change** on restart — they stay on the version
you deployed.

## The version catalog

The **catalog** contains your versions (imported or declared; it does not fill itself).
For each version: the number (SemVer `M.m.p`), the channel, and the **platform** (`model_family`)
that restricts it to the relevant players (e.g. `linux`, `windows`, `android-…`, `brightsign`,
`chromeos`, `chrome_extension`). A version without a platform is generic (all players).
The catalog offers a **search**, a **platform filter** and shows for each
version **how many players** in the fleet run it.

## Test before rolling out (beta / canary channel)

The recommended flow for a new version:
1. publish it on the **Beta** channel: it is **invisible to the fleet's "latest version"**
   (nobody is flagged "outdated");
2. deploy it to a **test group** (or a pilot player) and validate;
3. click **"Promote to stable"**: it becomes the reference for its platform;
4. deploy to the rest of the fleet (by site, group, or the whole organization).

## Create a deployment

1. Choose the **version** to deploy.
2. Choose the **targets**: a player, a group, a site, a set of tags, or the whole
   organization.
3. Choose the **strategy**: immediate, gradual, or scheduled.
4. Optionally, **time windows** (site local time): a **download** window
   (the player downloads within it, can resume if interrupted) and an **installation** window
   (it only installs/restarts within this range → no reboot in the middle of the day). The
   **Force** checkbox ignores the windows and applies immediately.

The deployment tracks each player: `pending → downloading → ready (staged) → installing →
succeeded / failed`, with a **progress ring** (succeeded / failed) per deployment. You can
**suspend** it (players in progress finish, the others wait) then **resume** it where
it stopped, **cancel** it, or **delete** a finished/cancelled deployment. In case of
problems, the previous version serves as the rollback base.

## The Electron engine (Linux, Raspberry Pi, Windows)

A player update only changes its code, never its **Electron engine**. The engine is updated
separately: **Settings → Updates → "Electron engine" button** (drawer).

- **Get a version** (server operator only): type the Electron version (e.g. `42.11.18`), tick the
  platforms, **Get**. The server downloads the official release, checks its checksum and stores
  it; it arrives in **beta**.
- **Deploy** (each org admin, to their own screens): in "Deploy to", choose the target and time
  windows, then **Deploy** on the wanted row. With a player selected, only its platform's versions
  are listed. A **beta** version can only be tried on **one player**; once the operator
  **promotes** it, it can go to groups, sites or the whole organisation.
- **Safety**: the player tries the new engine; if it does not confirm rendering within
  **5 minutes**, it goes back to the previous one **on its own**, the target becomes "rolled_back"
  and the deployment stops at the first failure. A power cut during the trial is handled at start-up.
- Deploying the same version to a player where it is already in progress is refused. After the
  click, the drawer shows "Deployment #… started on N player(s)".
- The **Electron** column of the inventory shows each screen's engine.

## Update through the assistant

Ask the assistant (or Jeanine on Telegram) to "**update the players**" (or a
player, a group, a site): it identifies the **outdated** players, proposes the latest
**stable** version of each platform, and creates the deployments after your confirmation
(reserved to the org-admin; via Telegram, the **PIN code** is required). For a specific
version, a beta or a scheduled deployment, use the Updates page.

## Good to know

- **BrightSign**: target a deployment by **player** or by **group** (not "whole
  organization"), so as not to include other player types. A BrightSign has **three
  distinct versions**: the **firmware (BOS)**, updated separately in **Settings → Firmware**
  (`/settings/firmware`, reserved to the super-admin); the **autorun** (the embedded script); and the
  **renderer** (the player itself), which is the one managed by this page.
- **Chrome extension**: the extension's code is updated via the Web Store / reloading
  the extension; the OTA deployment then serves to reload the player to apply the new
  bundle.
- If a player is offline at deployment time, it will apply the update when it comes back
  online (nothing is lost).
