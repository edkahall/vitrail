# Vitrail Player — Windows

🇬🇧 [English version](README.md)

Player Electron pour **Windows 11 Pro** (PC existant transformé en player).

## Installation

- **Terrain (recommandé)** : `docs/RUNBOOK_POSE_PLAYER.md` — canal `signflow-provision` WinRM
  (ouvrir WinRM est le seul geste humain, puis une commande).
- **Manuelle locale** : PowerShell **administrateur**, `.\install.ps1` (option
  `-InstallDir`, défaut `C:\Program Files\SignFlow Player`). Le script installe Node 20 LTS via
  winget s'il manque (et rafraîchit le Path du process courant — sinon `npm` échoue juste après
  une install réussie).

## Décisions techniques à connaître

- **Pas d'asar** : les sources sont copiées telles quelles dans
  `C:\Program Files\SignFlow Player\src\` (un correctif se dépose fichier par fichier).
- **Scheduled Task « SignFlow Player »** (at logon, RunLevel Highest, restart 99× toutes les
  1 min, sans limite de durée, PowerShell caché). Redémarrage applicatif =
  `app.relaunch(); app.exit(0)` (la tâche ne relance pas un process quitté proprement).
- **Enregistrement** : la config de pose porte `org_token` (`sfp_…`) + `player_name` ; au
  premier register le serveur rend un **`player_token`** (JWT) qui remplace `org_token`
  (supprimé de la config).
- **Coupure de sortie vidéo = `src/display_power.ps1`** : ① **DDC/CI** (VCP 0xD6 — le GPU
  continue d'émettre, l'écran dort sur commande, réveil instantané et toujours réversible) ;
  ② repli `SC_MONITORPOWER` (exit 10). Réveil = DDC on + SC -1 + **`ChangeDisplaySettingsEx`
  reset** + ES_DISPLAY_REQUIRED + input synthétique — plusieurs leviers car certains drivers
  (Intel HD 6000) en ignorent une partie, et `SC_MONITORPOWER off` y est **irrécupérable** sans
  le reset de mode. Broadcast via `SendNotifyMessageW`, jamais `SendMessage(HWND_BROADCAST)`
  (bloque 30 s si une fenêtre ne pompe pas ses messages — vécu sur l'AOpen).
- **Jamais `focus()` depuis le handler `blur`** (peu fiable sous les règles de foreground lock
  Windows 11) → `setAlwaysOnTop` + `moveTop()` pour regagner le z-order.
- Scripts `.ps1` : **100 % ASCII** (PowerShell 5.1 les lit en ANSI).
- Kiosk posé par l'install : pas de veille écran/machine, screensaver off, taskbar auto-hide,
  dernier utilisateur masqué au login, pare-feu UDP 7778/7779 (LAN sync + trigger VOD).
- Crash renderer : relaunch auto, sauf uptime < 60 s (anti-boucle) ; `gpu_enabled` changé à
  chaud → relaunch.

## Mises à jour du moteur (Electron)

Le moteur Electron se met à jour depuis le serveur (**Réglages → Mises à jour → Moteur Electron**),
pas par la mise à jour du code. `src/engine_update.js` télécharge et vérifie le moteur, l'extrait
dans `node_modules/electron/dist.next`, puis passe la main à `src/engine_guard.js` (exécuté par le
`node` système, indépendant des deux moteurs), qui bascule `dist` → `dist.prev` et `dist.next` →
`dist`, relance le player et remet l'ancien moteur si le nouveau ne confirme pas son affichage en
5 minutes. Le lanceur exécute `engine_guard.js boot-check` avant Electron (coupure pendant une
bascule). Journal : `node_modules/electron/engine-guard.log`. Le garde lance `dist\electron.exe` directement (pas par la tâche planifiée : `schtasks /run` demandé depuis le garde ne lançait rien). Un premier échec sous Windows peut venir de l'environnement (antivirus qui analyse un nouveau moteur non signé) : le retour arrière l'absorbe.

## Config

`%APPDATA%\signflow-player-windows\config.json` — répertoire = `app.getPath('userData')`,
**dérivé du `name` de `package.json`** (`signflow-player-windows`). Clés : `backend_url`,
`org_token` (pose) → `player_token` (après enregistrement), `player_name`.
`capabilities.json` (D3D11/MediaFoundation, actif par défaut sauf « Basic Display Adapter »)
vit au même endroit.

## Logs / diagnostic

- `%USERPROFILE%\signflow-player.log` (stdout/stderr Electron) — pas de logrotate sous
  Windows : **tronqué au lancement de la tâche** s'il dépasse 10 Mo.
- Trace persistante bornée : `%APPDATA%\signflow-player-windows\logs\player.log`.
