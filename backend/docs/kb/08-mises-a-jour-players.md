---
title: Mises à jour des players (OTA)
section: players
roles: [CMS_ORG_ADMIN]
keywords: [mise à jour, OTA, version, déploiement, players, updates, gestion des versions, rollback, fenêtre de maintenance, à jour, en retard, bêta, canary, groupe de test, suspendre, pause, importer, import, archive, tar.gz, apk, versions servies, déclarer, épinglage, pin, brightsign, chromeos, firmware, bs_bos, moteur, electron, moteur electron, retour arrière, engine]
---

# Mises à jour des players (OTA)

Vitrail met à jour ses players **à distance** (over-the-air), sans intervention sur place.
Tout se pilote depuis **Paramètres → Mises à jour** (`/settings/updates`). Le mécanisme couvre
**tous les types de players** : Linux, Raspberry Pi, Windows (Electron), Android, BrightSign,
ChromeOS et l'extension Chrome.

**Règle de base : aucune mise à jour automatique sans votre approbation.** Vous publiez une
version au catalogue, puis vous créez un déploiement approuvé vers les players de votre choix.

## Inventaire du parc

La page liste chaque player avec sa **version courante**, la **dernière version disponible** pour
sa plateforme, et son état **à jour / en retard** (plus son site et ses groupes). Des filtres
combinés (OS, version, état, site, groupe, recherche) aident à cibler.

Un player est signalé « en retard » **uniquement** s'il existe, dans le catalogue, une version
publiée pour **sa plateforme** dont le numéro (SemVer) est supérieur à la sienne. S'il n'y a aucune
version publiée pour sa plateforme, il est considéré « à jour » (rien à lui pousser).

## Deux natures de players (important)

Selon la plateforme, la version arrive de deux façons différentes :

- **Players installés sur l'appareil** — **Linux, Raspberry Pi, Windows, Android**. Leur code est
  embarqué sur la machine : la mise à jour est une **archive** téléchargée depuis votre serveur.
  Vous **importez** cette archive dans le catalogue (voir ci-dessous).
- **Players servis par le serveur** — **BrightSign, ChromeOS**. Leur code est un fichier statique
  **servi par votre serveur** : il voyage donc **avec la mise à jour du serveur lui-même**. Vous
  n'importez rien ; vous **déclarez** la version servie pour pouvoir l'épingler et la déployer.

Dans les deux cas, **rien n'est appliqué sans votre déploiement** : mettre à jour le serveur
n'avance **pas** un player tant que vous n'avez pas déployé la nouvelle version vers lui
(**épinglage**). C'est ce qui permet de tester une version sur un **bac à sables** de players
pendant que le reste du parc reste sur l'ancienne.

## Importer une version (Linux, RPi, Windows, Android)

Dans **Mises à jour → Importer une version**, deux méthodes :
- **Par URL** : collez le lien de l'archive depuis la page de distribution
  (ex. `signflow-player-linux-1.5.15.tar.gz`) — le serveur la récupère.
- **Par fichier** : téléversez l'archive `.tar.gz` (Electron) ou `.apk` (Android).

Le système **détecte automatiquement** la plateforme (nom du fichier) et la **version**
(lue à l'intérieur de l'archive), et **vérifie si elle est déjà présente**. Un **aperçu**
s'affiche avant de confirmer. À la validation, l'archive est stockée **sur votre serveur** et
ajoutée au catalogue : les players la téléchargeront **depuis votre serveur** au déploiement.

## Versions servies (BrightSign, ChromeOS)

Le panneau **« Versions servies par ce serveur »** montre la version de renderer BrightSign et
ChromeOS que votre serveur sert actuellement. Cliquez **« Déclarer »** pour l'ajouter au catalogue
→ elle devient **déployable** (épinglable). Comme pour les autres, tant que vous ne la déployez
pas, les BrightSign/ChromeOS **ne changent pas** au redémarrage — ils restent sur la version que
vous avez déployée.

## Le catalogue de versions

Le **catalogue** contient vos versions (importées ou déclarées ; il ne se remplit pas tout seul).
Pour chaque version : le numéro (SemVer `M.m.p`), le canal, et la **plateforme** (`model_family`)
qui la limite aux players concernés (ex. `linux`, `windows`, `android-…`, `brightsign`,
`chromeos`, `chrome_extension`). Une version sans plateforme est générique (tous les players).
Le catalogue offre une **recherche**, un **filtre par plateforme** et affiche pour chaque
version **combien de players** du parc l'exécutent.

## Tester avant de généraliser (canal bêta / canary)

Le flux recommandé pour une nouvelle version :
1. publiez-la sur le canal **Bêta** : elle est **invisible du « dernière version » du parc**
   (personne n'est marqué « en retard ») ;
2. déployez-la sur un **groupe de test** (ou un player pilote) et validez ;
3. cliquez **« Passer en stable »** : elle devient la référence de sa plateforme ;
4. déployez vers le reste du parc (par site, groupe, ou toute l'organisation).

## Créer un déploiement

1. Choisissez la **version** à déployer.
2. Choisissez les **cibles** : un player, un groupe, un site, un jeu de tags, ou toute
   l'organisation.
3. Choisissez la **stratégie** : immédiate, graduelle, ou programmée.
4. En option, des **fenêtres horaires** (heure locale du site) : une fenêtre de **téléchargement**
   (le player télécharge dedans, reprise possible s'il est coupé) et une fenêtre d'**installation**
   (il n'installe/redémarre que dans cette plage → pas de reboot en pleine journée). La case
   **Forcer** ignore les fenêtres et applique tout de suite.

Le déploiement suit chaque player : `en attente → téléchargement → prêt (staged) → installation →
réussi / échec`, avec un **anneau de progression** (réussis / échecs) par déploiement. Vous pouvez
le **suspendre** (les players en cours terminent, les autres attendent) puis le **reprendre** là
où il s'était arrêté, l'**annuler**, ou **supprimer** un déploiement terminé/annulé. En cas de
problème, la version précédente sert de base de retour arrière (rollback).

## Le moteur Electron (Linux, Raspberry Pi, Windows)

Une mise à jour du player ne change que son code, jamais son **moteur Electron**. Le moteur se met
à jour à part : **Réglages → Mises à jour → bouton « Moteur Electron »** (tiroir).

- **Récupérer une version** (exploitant du serveur seulement) : saisir la version d'Electron
  (ex. `42.11.18`), cocher les plateformes, **Récupérer**. Le serveur télécharge la version
  officielle, vérifie sa somme, la stocke ; elle arrive en **bêta**.
- **Déployer** (chaque admin d'organisation, sur ses écrans) : dans « Déployer sur », choisir la
  cible et les fenêtres horaires, puis **Déployer** sur la ligne voulue. Avec un player choisi, seules
  les versions de sa plateforme sont proposées. Une version **bêta** ne s'essaie que sur **un seul
  player** ; une fois **promue** par l'exploitant, elle part sur des groupes, des sites ou toute l'org.
- **Sécurité** : le player essaie le nouveau moteur ; s'il ne confirme pas son affichage en
  **5 minutes**, il revient **seul** à l'ancien, la cible passe « rolled_back » et le déploiement
  s'arrête au premier échec. Une coupure de courant pendant l'essai est reprise au démarrage.
- Relancer la même version vers un player où elle est déjà en cours est refusé. Après le clic, le
  tiroir affiche « Déploiement n°… lancé sur N player(s) ».
- La colonne **Electron** de l'inventaire montre le moteur de chaque écran.

## Mettre à jour par l'assistant

Demandez à l'assistant (ou à Jeanine sur Telegram) « **mets à jour les players** » (ou un
player, un groupe, un site) : il identifie les players **en retard**, propose la dernière
version **stable** de chaque plateforme, et crée les déploiements après votre confirmation
(réservé à l'org-admin ; par Telegram, le **code PIN** est exigé). Pour une version
précise, une bêta ou un déploiement programmé, passez par la page Mises à jour.

## Bon à savoir

- **BrightSign** : ciblez un déploiement par **player** ou par **groupe** (pas « toute
  l'organisation »), pour ne pas embarquer d'autres types de players. Un BrightSign a **trois
  versions distinctes** : le **firmware (BOS)**, mis à jour à part dans **Paramètres → Firmware**
  (`/settings/firmware`, réservé au super-admin) ; l'**autorun** (le script embarqué) ; et le
  **renderer** (le player lui-même), c'est ce dernier que gère cette page.
- **Extension Chrome** : le code de l'extension se met à jour via le Web Store / le rechargement
  de l'extension ; le déploiement OTA sert alors à recharger le player pour appliquer le nouveau
  bundle.
- Si un player est hors ligne au moment du déploiement, il appliquera la mise à jour à son retour
  en ligne (rien n'est perdu).
