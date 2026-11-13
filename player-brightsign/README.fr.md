# Player BrightSign (HD1024 / Série 5) — Vitrail

🇬🇧 [English version](README.md)

Portage du player Vitrail sur **BrightSign** (BrightSignOS). Cible de test : **HD1024**
(Série 5 « expanded I/O » : **4K**, **H.265/HEVC**, microSD, RS232, GPIO).

## Principe

BrightSignOS exécute un moteur **Chromium** via `roHtmlWidget`. On réutilise donc le
**player web** existant (le même que ChromeOS / extension) :

```
autorun.brs (BrightScript, sur la carte SD)
   └─ crée un roHtmlWidget plein écran  +  brightsign_js_objects_enabled=true
        └─ charge  {backend}/player-brightsign/index.html?backend=&token=
              └─ index.html pose window.__SF_PLAYER_OS__='brightsign'
                   └─ charge le renderer PARTAGÉ /player-chromeos/{signflow,audio_player,player}.js
```

- **Renderer = 100 % réutilisé** (servi par le frontend, une seule source de vérité).
- **Spécifique BrightSign** = uniquement `autorun.brs` (shell) + la page `index.html` + une
  branche **inerte-ailleurs** dans `signflow.js` (reboot OS natif, infos device via
  `@brightsign/deviceinfo`).
- **Sélection de rendition** : aucun changement serveur — le pipeline est piloté par les
  **capacités** que le player remonte (`browser_only` via `video_renderer='auto'` → conteneurs
  MP4 ; HEVC 4K servi seulement si `canPlayType('hvc1')` est vrai). Le HD1024 recevra du
  **H.264 FHD** par défaut, et du **HEVC/4K** s'il le déclare.

## Fichiers

| Fichier | Rôle |
|---|---|
| `player-brightsign/autorun.brs` | Shell BrightScript : lit `config.json`, crée le `roHtmlWidget`, active les objets JS, pont reboot |
| `player-brightsign/config.json.example` | Modèle de config (backend_url, token, résolution) |
| `frontend/public/player-brightsign/index.html` | Page servie : pose l'identité `brightsign` + charge le renderer partagé |
| `player-chromeos/src/signflow.js` | Bridge partagé — branche BrightSign (reboot/deviceinfo) guardée `IS_BS`, inerte ailleurs |

## Pré-requis serveur

1. La page `/player-brightsign/index.html` doit être servie par le frontend.
   - **Banc** : déjà servie (`frontend/public/...`), URL `http(s)://<banc>/player-brightsign/index.html`.
   - **Prod** : déployer = `git pull` + **rebuild frontend** (`docker compose restart frontend`).
2. **Créer le player dans le CMS** (Players → Provisioning) pour obtenir un **token de
   provisioning `sfp_…`** (ou un JWT player direct).

## Préparation de la carte (micro)SD

> **Méthode recommandée** : Utiliser le générateur de carte SD intégré dans Vitrail (**Écrans → Provisioning → Carte SD BrightSign**). Il archive automatiquement la configuration, `autorun.brs`, `bootstrap.html`, ainsi que les fichiers du renderer offline (`sf_rnd_*`) dans un fichier ZIP prêt à l'emploi.

Pour une préparation manuelle de la carte microSD :

1. Formater la microSD en **FAT32** (ou exFAT selon BOS).
> **⚠️ Firmware requis : BOS 9.1.x** (validé sur 9.1.132). Le vieux fork BrightScript de 8.5.70 **refuse
> de compiler** `Left()`/`Chr()`/`Str()` → mettre à jour (copier le `.bsfw` à la racine de la SD, boot,
> il s'applique tout seul). Une image SD propre de référence est dans `SD_card_BS/sd_image_clean/`.

2. Copier à la **racine** de la carte l'ensemble des fichiers nécessaires :
   - `autorun.brs` (depuis `player-brightsign/autorun.brs`)
   - `bootstrap.html` (depuis `frontend/public/player-chromeos/brightsign/bootstrap.html` — indispensable pour servir la page racine locale et initialiser le runtime)
   - `config.json` (copie de `config.json.example`, renseignée) :
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
     > **`"dws": true`** → l'autorun appelle `roNetworkConfiguration.SetupDWS({port:80, open:<mdp>, basic:true})`
     > qui **active le DWS local** (désactivé par défaut en BOS 9.x) + pose le mot de passe (`admin` / mdp
     > **dérivé par device** `Sf9Kqr<serial>Dx7!`, P0.3 — auth **basic** car le digest échoue sur un POST-avec-corps :
     > pas de rewind). Le screenshot capture le framebuffer **composite** (vidéo HW incluse) via le snapshot LDWS.
     > Effectif au **2e reboot** après insertion (le LDWS est configuré au boot, effectif au suivant).
     > **`"autorun_autoupdate": true`** (P1.2) → l'autorun se met à jour tout seul depuis prod
     > (`/player-chromeos/brightsign/autorun.brs`) au boot, avec garde-fous anti-brick (taille/préfixe), effectif au
     > prochain boot. Opt-in (feature à risque). **Sync de publication** : après tout changement de
     > `player-brightsign/autorun.brs`, copier le fichier dans **`frontend/public/player-chromeos/brightsign/autorun.brs`**
     > (= la copie servie par prod, modèle dual identique au renderer) puis rebuild frontend prod.
   - **`ss_body.json`** = `{"width":1280,"height":720}` — _OPTIONNEL depuis P0.2_ : c'est le corps du POST
     snapshot DWS (taille de la capture). **L'autorun le crée tout seul au boot s'il manque** (`FormatJson`,
     pas de quote échappée à la compile) et le handler screenshot reconstruit le corps si le fichier est
     illisible → plus jamais de screenshot KO silencieux sur une SD sans ce fichier. Ne le copier à la main
     que pour **forcer une autre taille** de capture.
   - **Fichiers du renderer (offline au 1er boot)** : Pré-embarquer ces fichiers évite que le player ne reste bloqué sur « Connecting… » si la liaison réseau / DHCP met plusieurs minutes à s'établir au démarrage :
     - `sf_rnd_ed25519.js` (copie de `frontend/public/player-chromeos/ed25519.js`)
     - `sf_rnd_signflow.js` (copie de `frontend/public/player-chromeos/signflow.js`)
     - `sf_rnd_audio_player.js` (copie de `frontend/public/player-chromeos/audio_player.js`)
     - `sf_rnd_player.js` (copie de `frontend/public/player-chromeos/player.js`)
     - `sf_rnd_style.css` (copie de `frontend/public/player-chromeos/style.css`)
3. Insérer la carte dans le HD1024, brancher l'écran (HDMI) + le réseau (Ethernet/DHCP), allumer.

> Au 1er boot : le bridge échange `sfp_…` contre un **JWT**, le persiste en localStorage
> (`storage_path` sur la carte → survit aux reboots). La rotation de token (« Option B ») est
> gérée comme sur les autres players (`token:refresh`).

## ✅ Checklist de validation sur le HD1024 (ce weekend)

Les points **VERIFY** = variantes possibles selon la version de BrightSignOS du HD1024.

- [ ] **Boot** : l'écran affiche le player (logo Vitrail / contenu), pas une page blanche.
- [ ] **En ligne** : le player passe **online** dans le CMS (WS connecté).
- [ ] **Contenu** : une **image** s'affiche ; une **vidéo H.264 FHD** se lit (HTML5 `<video>`).
- [ ] **Heartbeat enrichi** (objets JS BrightSign actifs) : le drawer player montre `bs_model`,
      `bs_serial`, `osversion`, `temperature_c`, `loadavg`/`meminfo` dans `capabilities`.
      → si **absent**, c'est que `brightsign_js_objects_enabled`/`AllowJavaScriptUrls` n'a pas pris
      (le player marche quand même, en mode navigateur standard). **VERIFY**.
- [ ] **Commande `restart`** (depuis le CMS) : le player recharge.
- [ ] **Commande `reboot_os`** : le HD1024 **redémarre vraiment**.
      → teste le chemin natif (`@brightsign/system`) puis le **pont** (`PostBSMessage` →
      `RebootSystem()`). **VERIFY** quel chemin fonctionne ; sinon repli = reload.
- [ ] **Token persistant** : après un reboot, le player se reconnecte **sans re-provisionner**
      (JWT en localStorage via `storage_path`). **VERIFY** le chemin `SD:/sf_html_storage`.
- [ ] **Screenshot** : la commande screenshot remonte une vignette (canvas — OK pour image/HTML ;
      teinté possible sur vidéo, comme les autres players navigateur).
- [ ] **HEVC / 4K** (bonus HD1024) : affecter un média **4K HEVC** → vérifier si le serveur sert
      la variante HEVC (dépend de `canPlayType('hvc1')` du Chromium BrightSign). Sinon il sert du
      H.264 FHD (sûr). **VERIFY**.

## Limites du MVP (assumées)

- **Streaming-only** (pas de cache offline local) : le Service Worker n'est pas attaché (la page
  charge le renderer depuis `/player-chromeos/` → scope SW différent) → repli streaming gracieux,
  comme l'extension Chrome. Offline = roadmap (cache disque BrightSign + réécriture des URLs).
- **Screenshot** = canvas (pas la capture native `@brightsign/screenshot` → upload, plus tard).
- **MAC / IP LAN** non remontées (possible via `@brightsign/networkconfiguration` — roadmap).
- **Pas de LAN sync** (UDP) ni mpv — inertes (comme ChromeOS/extension).

## Roadmap (post-validation)

1. **Cache offline natif** : télécharger les médias sur la carte via BrightScript / `@brightsign`
   filesystem + réécrire `localMediaUrl`/`rewriteSyncForStore` (download-then-play) — ou valider
   que le SW HTML5 fonctionne dans `roHtmlWidget` et l'attacher.
2. **Screenshot natif** (`@brightsign/screenshot` → lecture fichier → `POST /players/{id}/screenshot`).
3. **MAC/IP/up-time** via `@brightsign/networkconfiguration` + `@brightsign/deviceinfo`.
4. **4K HEVC** confirmé + éventuel renderer natif `roVideoPlayer` en overlay si le `<video>` HTML
   plafonne (peu probable sur HD1024, HEVC HW).
5. **GPIO / RS232** (contrôle écran §III) via BrightScript si besoin.

## Notes de développement

- `autorun.brs` (BrightScript) **n'est pas lintable** hors device (pas d'outillage local) → validé
  sur le HD1024.
- La branche BrightSign de `signflow.js` est **guardée `IS_BS`** (`window.__SF_PLAYER_OS__==='brightsign'`)
  → **strictement inerte** pour ChromeOS et l'extension (mêmes fichiers partagés). Re-sync source →
  public après toute modif : `cp player-chromeos/src/signflow.js frontend/public/player-chromeos/signflow.js`.
