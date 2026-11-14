# Vitrail — Guide d'exploitation

🇬🇧 [English version](GUIDE.md)

Ce guide vous mène de zéro à un réseau d'affichage dynamique qui fonctionne, sur **votre
propre matériel** (celui de la personne qui installe Vitrail). Vitrail est auto-hébergé :
pas de compte, pas de service dans le cloud, rien qui « appelle la maison ».

- **Partie 1 — Essayer en 15 minutes** : un serveur et un écran, avec ce que vous avez déjà.
- **Partie 2 — Passer en production** : dimensionnement, HTTPS, players sur toutes les
  plateformes, mises à jour, sauvegardes, l'assistant Telegram et le reste.

---

## Comment fonctionne Vitrail

```
                ┌──────────────────────────────┐
  navigateur  → │  Serveur Vitrail (Docker)    │ ← vous gérez les contenus ici
                │  CMS · API · stockage médias │
                └──────────────┬───────────────┘
                               │  les players se connectent AU serveur
             ┌─────────────────┼─────────────────┐
          player            player            player
   (Linux, Raspberry Pi, Windows, Android, ChromeOS, navigateur, BrightSign)
```

- Le **serveur** héberge l'interface web, la base de données et les médias.
- Un **player** est l'appareil derrière un écran. Il télécharge ses contenus, les stocke et les
  lit depuis son propre stockage : la diffusion continue même si le réseau tombe.
- Les connexions vont toujours **du player vers le serveur**. Rien n'est à ouvrir côté player,
  qui se pose donc derrière n'importe quelle box.

---

# Partie 1 — Essayer en 15 minutes

## Ce qu'il vous faut

| | Minimum pour un essai |
|---|---|
| **Serveur** | Une machine sous **Ubuntu 24.04**, physique ou virtuelle (vieux PC, mini-PC, VM, VPS loué) : **4 cœurs, 8 Go de RAM, 40 Go de disque libre**, et un accès Internet pendant l'installation. Pas besoin de carte graphique. |
| **Écran** | N'importe quel appareil avec un navigateur, sur le même réseau : PC, portable, tablette, TV avec navigateur. |

Le serveur ne demande aucune préparation : l'installeur pose Docker et tout le reste.

## 1. Installer le serveur

Sur la machine Ubuntu :

```bash
curl -fsSL https://raw.githubusercontent.com/edkahall/vitrail/main/install-ubuntu.sh -o install-vitrail.sh
sudo bash install-vitrail.sh
```

Le script construit et démarre tout. Comptez **10 à 20 minutes** selon la machine et la
connexion. Il ne pose aucune question. À la fin, il affiche :

- l'adresse de l'interface, `http://<ip-du-serveur>:8080` ;
- l'e-mail de l'administrateur (`admin@vitrail.local`, sauf si vous lancez le script avec
  `ADMIN_EMAIL=...` devant), son **mot de passe** et son **secret 2FA**.

> ⚠️ Le mot de passe et le secret 2FA ne sont affichés **qu'une fois**. Notez-les tout de suite.

Le port 8080 n'est ouvert qu'à votre **réseau local**. Pour joindre le serveur depuis Internet
le temps d'un essai, relancez le script avec `VITRAIL_PUBLIC_HTTP=1` devant `sudo bash ...`.
Le trafic passe alors en HTTP clair : ne le laissez pas ainsi, la partie 2 explique le HTTPS.

## 2. Se connecter

1. Ouvrez `http://<ip-du-serveur>:8080` dans un navigateur.
2. Saisissez l'e-mail et le mot de passe.
3. La double authentification est **obligatoire pour les administrateurs**. Ajoutez le secret
   2FA dans une application d'authentification (Aegis, Google Authenticator, 2FAS, Microsoft
   Authenticator…), puis tapez le code à 6 chiffres.

L'interface est en anglais par défaut ; changez la langue dans votre **Profil**.

## 3. (Facultatif) Charger un contenu de démonstration

Pour explorer avec un réseau réaliste (enseigne fictive, players, médias, playlists,
programmes, campagnes…), lancez sur le serveur :

```bash
cd /opt/signflow
sudo docker compose exec -T backend python3 /app/scripts/seed_demo_content.py
```

Tout le contenu de démo est généré sur place (aucune marque, photo ou musique réelle). Il est en
anglais ; ajoutez `--lang fr` pour des noms et des visuels en français.

## 4. Brancher un premier écran, dans un navigateur

Le **player web** est servi par votre propre serveur : rien à installer.

1. Dans l'interface : **Players → Provisioning → Nouveau token**. Copiez le jeton (il commence
   par `sfp_`).
2. Sur l'appareil de l'écran, ouvrez :
   `http://<ip-du-serveur>:8080/player-chromeos/index.html`
3. Renseignez **Backend URL** = `http://<ip-du-serveur>:8080` et **Player token** = le jeton
   `sfp_`, puis cliquez sur **Connect**.
4. La page affiche *« No content to display »* : c'est l'état normal d'un player à qui rien
   n'est encore programmé. Passez en plein écran avec **F11**.

Dans **Players**, le nouveau player apparaît **en ligne**.

## 5. Afficher quelque chose

1. **Médiathèque → Importer** : ajoutez quelques images ou une vidéo.
2. **Playlists → Nouvelle playlist** : donnez-lui un nom, **Ajouter un média**, puis **Publier**.
3. **Programmes → Nouveau programme** : choisissez la playlist, réglez *Quand diffuser ?* sur
   **Toujours**, et ajoutez votre player comme cible. Enregistrez.

En quelques secondes, l'écran diffuse la playlist.

Votre réseau Vitrail fonctionne. Le manuel intégré (menu **Aide**) détaille chaque écran.

---

# Partie 2 — Passer en production

## 1. Choisir le serveur

La charge de Vitrail ne grandit pas comme celle d'une plateforme de streaming : les players
mettent leurs contenus en cache et les lisent en local. Trois ressources se dimensionnent
indépendamment :

- la **bande passante** suit *la quantité de contenu poussée et la vitesse à laquelle vous la
  voulez partout* ;
- le **stockage des médias** suit *la taille de votre catalogue* ;
- l'**encodage vidéo** suit *la quantité de vidéo importée*. Un GPU est bien plus rapide que le
  processeur.

| Parc | Serveur (tout en Docker sur une machine jusqu'à ~500 players) | Encodage vidéo | Bande passante |
|---|---|---|---|
| < 200 players | 8 vCPU · 32 Go RAM · 0,5–1 To NVMe | 1 GPU grand public (NVIDIA NVENC) ou iGPU Intel ; le processeur seul suffit pour un usage vidéo léger | 100–200 Mbps symétrique |
| 200–500 | 16 vCPU · 64 Go RAM · 1–2 To ; envisager un stockage S3 externe | 1 GPU (classe T4/L4) | 300–500 Mbps, ou un CDN |
| 500–1000 | plusieurs serveurs d'application, PostgreSQL et Redis managés, S3 + CDN | nœud GPU dédié | CDN fortement recommandé |

Formules détaillées et trois scénarios chiffrés : [`guide-infrastructure.fr.md`](guide-infrastructure.fr.md).

**Faut-il une carte graphique ?** Non. Sans GPU, les vidéos sont encodées par le processeur :
c'est très bien pour quelques vidéos par semaine, lent pour beaucoup de vidéos lourdes ou en
4K. Vitrail détecte et utilise automatiquement un GPU **NVIDIA** (NVENC) ou un iGPU **Intel**
(QSV/VAAPI), dès que le GPU est rendu visible à Docker : voir [`GPU_ENCODE.md`](GPU_ENCODE.fr.md).
La page **Réglages → Serveur** indique l'encodeur utilisé et les vitesses réellement mesurées.

**Stockage des players** : prévoyez 2 à 3 fois le volume de contenu diffusé par chaque écran,
plus le système. Le piège classique : les box Android TV d'entrée de gamme à 16 Go de stockage
soudé, très bien en Full HD, trop justes pour des playlists 4K.

## 2. Installer

Utilisez le même installeur qu'en partie 1, sur la machine de production. Il installe dans
`/opt/signflow`, range ses secrets dans `/opt/signflow/.env` (générés, droits 600), démarre la
stack, crée un service systemd (`signflow.service`, démarrage au boot) et un frontal nginx sur
le port 8080.

Variables utiles, à placer devant `sudo bash install-vitrail.sh` :

| Variable | Rôle |
|---|---|
| `ADMIN_EMAIL=vous@example.com` | e-mail du premier administrateur |
| `VITRAIL_PUBLIC_HTTP=1` | ouvre le port 8080 à tous (essai seulement) |
| `REPO_URL=...` | installer depuis votre propre fork |

## 3. Nom de domaine et HTTPS

Pour des players et des utilisateurs hors de votre réseau local, placez le serveur derrière un
reverse-proxy avec TLS. Caddy obtient et renouvelle seul les certificats Let's Encrypt.

1. Faites pointer un nom DNS (par exemple `cms.example.com`) et un second pour les médias
   (`media.cms.example.com`) vers le serveur, et ouvrez les ports **80** et **443**.
2. Partez de [`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example).
3. Dans `/opt/signflow/.env`, renseignez :
   ```
   PUBLIC_WEB_URL=https://cms.example.com
   MINIO_PUBLIC_ENDPOINT=media.cms.example.com
   MINIO_PUBLIC_USE_SSL=true
   CORS_ORIGINS=["https://cms.example.com"]
   ```
   `MINIO_PUBLIC_ENDPOINT` est l'adresse par laquelle **navigateurs et players** téléchargent les
   médias (URL signées). Elle doit leur être joignable.
4. Appliquez : `cd /opt/signflow && sudo docker compose up -d`.

Les players utilisent alors `https://cms.example.com` comme adresse de serveur.

## 4. Sécuriser le serveur

- Gardez le pare-feu au strict minimum : SSH, 80/443 (proxy), et le port des médias seulement
  si vous ne le passez pas par le proxy.
- Docker publie ses ports **en contournant `ufw`**. La stack ne publie que le nécessaire :
  n'ajoutez pas de ports dans `docker-compose.yml`, mettez-les dans
  `docker-compose.override.yml`, liés à `127.0.0.1` quand ils sont internes.
- La 2FA est imposée aux administrateurs ; activez-la pour tout compte qui modifie des contenus.
- **Organisations et rôles.** Un serveur peut héberger plusieurs organisations (enseignes,
  clients), cloisonnées entre elles par la sécurité au niveau des lignes de PostgreSQL. Le rôle
  plateforme qui gère les organisations est le **super-admin**, créé uniquement sur le serveur :
  ```bash
  cd /opt/signflow
  sudo docker compose exec -T backend python3 /app/scripts/create_super_admin.py --email vous@example.com
  ```
  Gérez ensuite utilisateurs, groupes et rôles personnalisés dans **Réglages → Utilisateurs &
  Rôles**.

## 5. Services optionnels

Tous se règlent dans `/opt/signflow/.env`, puis s'appliquent avec
`sudo docker compose up -d`.

| Fonction | À renseigner |
|---|---|
| **E-mail** (alertes, validations, portail annonceur) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS` ; et `PUBLIC_WEB_URL` pour les liens des e-mails |
| **Assistant IA** (aide intégrée, questions sur votre réseau) | `ANTHROPIC_API_KEY` |
| **Génération de contenu IA** (images, vidéo, voix) | par organisation, dans **Réglages → Clés IA** (fal.ai, ElevenLabs, Anthropic) : chaque organisation utilise ses propres clés, chiffrées au repos |
| **Photos libres de droits** | `UNSPLASH_ACCESS_KEY`, `PEXELS_API_KEY` |
| **Assistant Telegram** | voir §8 |

Sans eux, Vitrail fonctionne normalement ; seule la fonction concernée est indisponible.

## 6. Les players en production

Tous les players s'enrôlent de la même façon : créez un **jeton d'enrôlement** (`sfp_…`) dans
**Players → Provisioning**, donnez-le une fois à l'appareil, qui l'échange contre ses propres
identifiants. Un jeton peut enrôler plusieurs players et expirer.

| Plateforme | Idéal pour | Installation |
|---|---|---|
| **Web / ChromeOS** | essais, kiosques Chromebox/Chromebook, tout navigateur | ouvrir `<serveur>/player-chromeos/index.html`, ou passer `?backend=<url>&token=sfp_…` dans un kiosque managé |
| **Extension Chrome** | écrans déjà pilotés par un PC sous Chrome | charger `player-extension/` dans Chrome (mode développeur) et saisir le jeton dans sa fenêtre |
| **Linux** (Debian 12) | mini-PC, NUC ; le meilleur choix pour la 4K et les murs d'images | sur une machine dédiée à l'écran : `sudo git clone --depth 1 https://github.com/edkahall/vitrail /opt/signflow`, puis `sudo bash /opt/signflow/player-linux/install.sh signflow https://cms.example.com` (session kiosque, connexion automatique, démarrage auto), puis placer le jeton `sfp_` dans `player_token` de `~signflow/.config/signflow-player/config.json` et redémarrer — ou laisser `signflow-provision` tout faire |
| **Raspberry Pi** 4/5 (Raspberry Pi OS) | écrans Full HD économiques, players audio seuls | idem avec `player-rpi/install.sh` : un assistant à l'écran demande l'adresse du serveur et le jeton ; ou construire une image SD prête à flasher qui s'enrôle seule au premier démarrage (`installer/image-rpi/`) |
| **Windows** 10/11 | PC d'affichage Windows existants | copier `player-windows/` sur le PC et lancer `install.ps1` en administrateur |
| **Android** 9+ (box Android TV, tablettes) | players à bas coût | **pour un essai** : l'APK de la page [Releases](https://github.com/edkahall/vitrail/releases) ; **pour un vrai parc** : un APK signé avec **votre propre clé** (voir [Android : votre propre clé de signature](#android--votre-propre-clé-de-signature)). L'installer (`adb install`), puis saisir l'adresse du serveur et le jeton ; pour un kiosque verrouillé, voir [`player-android/README.md`](../player-android/README.fr.md) |
| **BrightSign** | lecteurs professionnels | **Players → Provisioning** génère une carte SD prête à copier |

**Poser beaucoup de players** : l'outil `signflow-provision` enrôle un appareil à distance en
une commande, par SSH (Linux, Raspberry Pi), WinRM (Windows) ou ADB (Android). Voir
[`installer/README.md`](../installer/README.fr.md) et le guide de terrain
[`RUNBOOK_POSE_PLAYER.md`](RUNBOOK_POSE_PLAYER.fr.md).

**Vérifier un player** : il est **en ligne** dans **Players**, il n'apparaît qu'une seule fois
(ré-enrôler un appareil existant demande l'option de ré-attache, sinon on crée un doublon), et
l'écran diffuse, ou affiche l'écran de repos si rien n'est programmé.

**Récupérer un boîtier Android** : en mode kiosque (Device Owner), Android refuse de désinstaller
l'appli. Lancez `adb shell content call --uri content://app.vitrail.player.admin --method release_device_owner`, puis `adb uninstall app.vitrail.player` — sans réinitialisation
d'usine. Seul adb peut envoyer cette commande ; les applis du boîtier sont refusées.

### Android : votre propre clé de signature

**Règle : pour un essai, l'APK officiel ; pour un vrai parc, votre propre clé.**

Android n'installe une mise à jour que si elle est signée avec **la même clé** que
l'application déjà présente sur l'appareil. Celui qui détient la clé de l'APK que vous avez
installé est donc le seul à pouvoir produire des mises à jour pour vos appareils.

- L'APK de la page Releases est signé avec la clé **du projet**. Il convient pour essayer
  Vitrail. Mais un parc installé avec lui dépend du projet pour recevoir ses mises à jour
  Android : si le projet cesse de publier des releases, ou si la clé est perdue, vos
  appareils ne pourront être mis à jour qu'en désinstallant et en ré-enrôlant chacun d'eux.
- Avec **votre propre clé**, vous ne dépendez de personne : vous recompilez l'APK quand vous
  le voulez (avec vos correctifs ou ceux de la communauté) et vous mettez votre parc à jour
  vous-même.
- **Choisissez dès le départ.** L'APK officiel et le vôtre portent le même nom de paquet
  (`app.vitrail.player`) mais pas la même clé : faire passer un appareil de l'un à l'autre
  impose de le désinstaller, de le réinstaller et de le ré-enrôler.

Dans les deux cas, personne d'autre ne peut mettre vos players à jour : une mise à jour n'est
installée que si elle vient de **votre** serveur (Réglages → Mises à jour) **et** qu'elle est
signée avec la bonne clé.

**1. Créer la clé** (une seule fois, sur votre ordinateur ; `keytool` est fourni avec tout
JDK Java 17 ou plus) :

```bash
keytool -genkeypair -keystore ma-cle-vitrail.jks -storetype PKCS12 -alias vitrail   -keyalg RSA -keysize 4096 -validity 10000 -dname "CN=Affichage de mon entreprise"
```

Il demande un mot de passe (en PKCS12, celui de la clé est le même).

**2. La sauvegarder, tout de suite, à au moins deux endroits**, dont un hors ligne (NAS, clé
USB chiffrée, gestionnaire de mots de passe pour le mot de passe). **Si vous perdez cette clé,
plus aucune mise à jour ne pourra être installée sur vos players Android** sans réinstaller
chaque appareil. Si elle fuit, quelqu'un pourrait signer une mise à jour que vos appareils
accepteraient. Ne la committez jamais dans un dépôt.

**3. Compiler votre APK**, au choix :

- **Sur GitHub** (aucun outil Android à installer) : forkez le dépôt, ajoutez quatre secrets
  (Settings → Secrets and variables → Actions) : `ANDROID_KEYSTORE_BASE64` (le fichier en
  base64 : `base64 -w0 ma-cle-vitrail.jks`), `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`
  (`vitrail` ci-dessus) et `ANDROID_KEY_PASSWORD` (le même mot de passe). Poussez ensuite un tag
  (`git tag v1.0.1-perso && git push origin v1.0.1-perso`) : le workflow `Release players`
  publie dans votre fork une release dont l'APK est signé avec votre clé. Les notes de release
  indiquent `release-signed` ; `debug-signed` signifie qu'un secret manque.
- **Sur votre ordinateur** (SDK Android + JDK 17) : copiez la clé dans `player-android/`, créez
  `player-android/keystore.properties` (tous deux sont ignorés par git) :

  ```properties
  storeFile=ma-cle-vitrail.jks
  storePassword=…
  keyAlias=vitrail
  keyPassword=…
  ```

  puis `cd player-android && ./gradlew assembleRelease`. L'APK se trouve dans
  `app/build/outputs/apk/release/`.

**4. Vérifier la signature** avant de déployer (`apksigner` fait partie des build-tools du
SDK Android) : `apksigner verify --print-certs app-release.apk` doit afficher votre certificat.

**5. L'utiliser partout** : installez-le sur les nouveaux appareils (`adb install`, ou
`signflow-provision --os android --apk app-release.apk`), et importez dans **Réglages → Mises à
jour** les APK compilés plus tard avec la même clé et un `versionCode` supérieur pour mettre le
parc à jour.

## 7. Mises à jour

### Le serveur

Relancez l'installeur :

```bash
sudo bash install-vitrail.sh
```

Il récupère le dernier code, conserve votre `.env`, reconstruit les conteneurs et applique les
migrations de la base. L'interface et l'API sont brièvement indisponibles pendant la
reconstruction ; les écrans continuent de diffuser depuis leur cache.

Suivez les releases du dépôt pour savoir quand mettre à jour.

### Les players

Les players se mettent à jour **à distance**, depuis votre serveur, et **uniquement quand vous
l'approuvez**.

1. Téléchargez les paquets players sur la page
   [Releases](https://github.com/edkahall/vitrail/releases) :
   `signflow-player-linux-<version>.tar.gz`, `-rpi-`, `-windows-`, et l'`.apk` Android.
2. **Réglages → Mises à jour** : importez le paquet, puis créez un déploiement vers les players
   de votre choix. Commencez par un petit groupe de test (canary), puis élargissez.
3. Chaque player télécharge le paquet **depuis votre serveur**, l'installe et remonte sa
   nouvelle version.

Les players Web/ChromeOS, l'extension Chrome et BrightSign se mettent à jour avec le serveur.

**Construire les paquets vous-même** (fork, build personnalisé) : poussez un tag `v*`, et le
workflow GitHub Actions `Release players` les construit et publie une release. Pour Android,
voir [Android : votre propre clé de signature](#android--votre-propre-clé-de-signature) : une
mise à jour doit être signée avec **la même clé** que l'application installée.

### Le moteur Electron (players Linux, Raspberry Pi et Windows)

Une mise à jour du player change son code, jamais son moteur Electron. Le moteur se met à jour à
part, depuis **Réglages → Mises à jour → Moteur Electron** :

1. **L'exploitant du serveur** (super-admin, ou l'admin d'org d'un serveur qui n'en a pas) saisit
   une version d'Electron (par exemple `42.11.18`, voir [releases.electronjs.org](https://releases.electronjs.org)),
   coche les plateformes et clique **Récupérer**. Le serveur télécharge la version officielle
   d'Electron, la vérifie contre le `SHASUMS256.txt` publié par Electron, la reconditionne et la
   stocke. Elle arrive en **bêta**. Les players ne téléchargent jamais rien sur Internet.
2. **Chaque admin d'organisation** la déploie sur ses propres écrans, depuis le même tiroir :
   cible, fenêtres horaires si besoin, puis **Déployer**. Un moteur bêta ne s'essaie que sur **un
   seul player** à la fois. Une fois **promu** par l'exploitant, il peut partir sur des groupes, des
   sites ou toute l'organisation.
3. Le player télécharge le moteur, le vérifie, l'extrait à côté de l'actuel, puis passe la main à
   un petit garde et s'arrête. Le garde bascule les moteurs et relance le player. Le nouveau
   moteur doit confirmer son affichage en moins de **5 minutes** ; sinon le garde remet seul
   l'ancien moteur, et le déploiement s'arrête au premier échec.

Une coupure de courant pendant la bascule est reprise au démarrage suivant (les lanceurs exécutent
le `boot-check` du garde avant Electron). Le garde écrit son journal dans
`node_modules/electron/engine-guard.log`, dans le dossier du player.

Les players installés avant cette fonction doivent voir leur lanceur rafraîchi une fois (relancer
le `install.sh` / `install.ps1` du player) pour avoir la vérification au démarrage et, sous Linux,
un moteur appartenant au compte du player.

## 8. Assistant Telegram (« Jeanine »)

Vos utilisateurs peuvent parler à Vitrail depuis Telegram : poser des questions sur le réseau
et lancer des actions après confirmation, avec leurs propres droits. Il faut que le serveur soit
joignable en **HTTPS** (§3).

1. Dans Telegram, écrivez à **@BotFather** → `/newbot`, choisissez un nom. Conservez le **jeton
   du bot**.
2. Générez un secret aléatoire : `openssl rand -hex 32`.
3. Dans `/opt/signflow/.env` :
   ```
   CMS_TELEGRAM_BOT_TOKEN=<jeton du bot>
   CMS_TELEGRAM_WEBHOOK_SECRET=<secret aléatoire>
   ```
   puis appliquez avec `sudo docker compose up -d`.
4. Indiquez à Telegram où envoyer les messages (HTTPS sur les ports 443, 80, 88 ou 8443
   uniquement) :
   ```bash
   curl "https://api.telegram.org/bot<jeton du bot>/setWebhook" \
        -d url=https://cms.example.com/api/v1/messaging/telegram/webhook \
        -d secret_token=<secret aléatoire>
   ```
5. Chaque utilisateur lie son compte depuis **Profil → Messagerie — Telegram** (un code à usage
   unique envoyé au bot). L'administrateur de l'organisation voit et révoque les comptes liés dans
   **Réglages → Messagerie**.

L'assistant utilise les clés IA du §5 et les quotas fixés par organisation.

## 9. Alertes et supervision

- **Réglages → Notifications** : canaux webhook (Slack, Teams, n8n…) et e-mail, avec des règles
  pour player hors ligne, flux en erreur, mise à jour échouée, campagnes et validations.
- Supervision externe : `GET /health` répond `{"status":"ok"}` quand l'API fonctionne. N'importe
  quel outil de supervision (Uptime Kuma par exemple) peut l'interroger.

## 10. Sauvegardes et restauration

Sauvegardez trois choses, chaque jour :

| Quoi | Comment |
|---|---|
| **Base de données** | `cd /opt/signflow && sudo docker compose exec -T postgres pg_dump -U signflow -Fc signflow > vitrail-$(date +%F).dump` |
| **Médias** | copier le volume MinIO (`docker volume inspect signflow_minio_data` donne son chemin), ou répliquer le bucket avec `mc mirror` |
| **Configuration** | `/opt/signflow/.env` et `docker-compose.override.yml`. Ils contiennent les secrets, dont la clé qui déchiffre les clés IA des organisations (`CMS_VAULT_KEY`) : rangez-les en lieu sûr. |

Restauration : réinstaller, remettre le `.env`, restaurer le dump avec `pg_restore --clean`,
remettre les médias, démarrer la stack. **Testez une restauration** régulièrement sur une
machine jetable : une sauvegarde jamais testée n'est pas une sauvegarde.

[`deploy/backup/`](../deploy/backup/) contient un script de sauvegarde quotidienne complet
(base, rôles, configuration, miroir des médias avec corbeille de 30 jours) et un contrôle de
restauration. Servez-vous-en comme point de départ, en adaptant la destination à votre stockage.

## 11. Dépannage

| Symptôme | Où regarder |
|---|---|
| L'interface ne s'ouvre pas | `sudo docker compose ps` dans `/opt/signflow`, puis `sudo docker compose logs backend --tail 50` |
| Le backend s'arrête au démarrage avec une erreur `DATABASE_APP_URL` | votre `docker-compose.override.yml` est antérieur au correctif ; comparez-le au `docker-compose.yml` actuel |
| Un player reste hors ligne | vérifiez son adresse de serveur, qu'il joint le serveur (`curl <serveur>/health` depuis l'appareil), et la date/heure de l'appareil (un Raspberry Pi sans réseau n'a pas d'horloge) |
| Une vidéo reste « en traitement » longtemps | **Réglages → Serveur** : encodeur utilisé et file d'attente |
| Vignettes ou médias absents dans le navigateur | `MINIO_PUBLIC_ENDPOINT` doit être joignable par le navigateur et les players |

## 12. Désinstaller

```bash
cd /opt/signflow
sudo docker compose down -v          # ⚠️ supprime la base de données et les médias
sudo systemctl disable --now signflow.service
sudo rm -f /etc/systemd/system/signflow.service
sudo rm -rf /opt/signflow
```

Supprimez ensuite le site nginx créé par l'installeur dans `/etc/nginx/sites-enabled/` et les
règles de pare-feu du port 8080.

---

## Obtenir de l'aide, contribuer

- Questions et bugs : ouvrez une issue sur GitHub.
- Failles de sécurité : voir [`SECURITY.md`](../SECURITY.md) ; n'ouvrez pas d'issue publique.
- Les contributions sont bienvenues : voir [`CONTRIBUTING.md`](../CONTRIBUTING.md).
