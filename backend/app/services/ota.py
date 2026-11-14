"""Gestion des versions & déploiements OTA (§XXX) — orchestration CMS (Phase 2, slice 1).

**Règle absolue (§30.1) : aucune mise à jour automatique sans approbation explicite d'un admin.**
Ce module gère, côté serveur : l'inventaire des versions du parc, le catalogue de versions
(track `player`/`server`), la création d'un déploiement approuvé (cibles + stratégie) et son suivi
par player. L'**apply** côté player (téléchargement + installation par OS) = slice 2 (dépendant HW).

Réutilise le noyau `services.targeting` pour résoudre org/groupe/players/tags → set d'ids.
Tenant-scoped : requêtes filtrées `id_org` (les déploiements portent `id_org`, les versions
sont globales = catalogue plateforme).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import CmsSite
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroup, CmsPlayerGroupMember
from app.models.player_version import (
    CmsPlayerUpdateVersion,
    CmsUpdateDeployment,
    CmsUpdateDeploymentTarget,
)
from app.services.served_versions import served_player_versions
from app.services.targeting import Target, players_for_targets

# ── Plateformes de bundle ↔ player_os ────────────────────────────────────────
# Source UNIQUE (l'endpoint updates.py les réexporte) : le catalogue raisonne en
# PLATEFORME DE BUNDLE (linux/rpi/windows/android) alors que le parc raisonne en
# `player_os` (linux-x64, linux-arm64, android-armeabi-v7a…). Un bundle sert
# PLUSIEURS player_os — la correspondance ne peut donc pas être une simple égalité.
BUNDLE_PLATFORMS = {"linux", "rpi", "windows", "android"}

# Piste du MOTEUR Electron (issue vitrail#5). Une version `engine` a pour `model_family` le
# `player_os` EXACT de ses players (le filtre d'égalité de `create_deployment` les cible donc
# déjà) et pour version la version d'Electron. Elle voyage dans sa PROPRE clé du payload
# (`engine_update`) : la clé historique `update` ne doit JAMAIS en porter une — un player
# actuel la prendrait pour un bundle de code (« 42.11.18 » > sa 1.6.x) et extrairait le
# moteur par-dessus son application.
ENGINE_TRACK = "engine"
ENGINE_FAMILIES = {"linux-arm64", "linux-x64", "windows"}
# player_os → plateforme de bundle autorisée (garde anti-mismatch, incident 2026-07-06 :
# un player linux qui télécharge un bundle windows l'installe par-dessus son code).
# Android (2026-07-19) : toutes les ABI partagent le même APK universel → un seul artefact.
OS_TO_BUNDLE = {
    "windows": "windows",
    "linux": "linux", "linux-x64": "linux",
    "rpi": "rpi", "linux-arm64": "rpi",
    "android": "android", "android-armeabi-v7a": "android",
    "android-arm64-v8a": "android", "android-x86_64": "android",
}


def player_os_for_bundle(platform: str) -> list[str]:
    """Tous les `player_os` servis par une plateforme de bundle."""
    return sorted(os_ for os_, b in OS_TO_BUNDLE.items() if b == platform)

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def semver_key(v: str | None) -> tuple:
    """Clé de tri SemVer (major, minor, patch). Non-parsable → (0,0,0) = le plus ancien."""
    m = _SEMVER.match((v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


async def latest_player_version(
    db: AsyncSession, platform: str | None = None, channel: str = "stable"
) -> CmsPlayerUpdateVersion | None:
    """Dernière version `player` publiée (SemVer max) pour un canal, filtrée par PLATEFORME.

    `platform` = identité de plateforme = `player_os` (linux / linux-arm64 / windows / android-… /
    chromeos / chrome_extension / brightsign). BrightSign scope sous « brightsign » (code uniforme
    quelle que soit la famille SoC). Une version dont `model_family` est NULL est **générique**
    (toutes plateformes) ; sinon elle ne
    s'applique qu'aux players dont la plateforme = `model_family`. → un player n'apparaît « en
    retard » que sur une version de SA plateforme (ou générique), jamais sur celle d'un autre OS."""
    q = select(CmsPlayerUpdateVersion).where(
        CmsPlayerUpdateVersion.track == "player",
        CmsPlayerUpdateVersion.channel == channel,
    )
    rows = list((await db.execute(q)).scalars().all())
    if platform:
        # 🔴 Une version IMPORTÉE porte une PLATEFORME DE BUNDLE dans `model_family` (« rpi »,
        # « android »), pas un `player_os` (« linux-arm64 », « android-armeabi-v7a ») : la
        # comparaison par égalité seule la rendait INVISIBLE au calcul du « latest » — le CMS
        # affichait donc une version disponible PÉRIMÉE, voire aucune sur une install dont
        # TOUTES les versions viennent de l'import (cas du banc client). Constaté par Ed le
        # 2026-07-30 : prod bloquée sur android 2.5.15 alors que 2.5.17 était publiée, et
        # 3 plateformes sur 6 concernées (android, rpi→linux-arm64, linux→linux-x64) ; les
        # 3 autres passaient inaperçues parce que le nom de bundle et le player_os coïncident
        # (windows, chromeos, brightsign).
        # 🔑 EXACTEMENT le défaut déjà corrigé dans `create_deployment` le 2026-07-26 (« comparer
        # les deux par égalité ne renvoyait AUCUN player ») — la même confusion, dans l'autre
        # sens, avait survécu ici. On accepte donc les deux écritures, via la table centrale.
        bundle = OS_TO_BUNDLE.get(platform)
        rows = [
            r for r in rows
            if not r.model_family or r.model_family in (platform, bundle)
        ]
    else:
        rows = [r for r in rows if not r.model_family]
    if not rows:
        return None
    return max(rows, key=lambda r: (semver_key(r.version), r.created_at))


async def parc_inventory(db: AsyncSession, id_org: int) -> list[dict]:
    """Inventaire du parc : par player, version courante + dernière dispo + statut."""
    players = list((await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == id_org).order_by(CmsPlayer.player_name)
    )).scalars().all())
    # Site (nom) par id_site + groupes (noms) par id_player — enrichissement de l'inventaire.
    sites = dict((await db.execute(
        select(CmsSite.id_site, CmsSite.site_name).where(CmsSite.id_org == id_org)
    )).all())
    groups_by_player: dict[int, list[str]] = {}
    for pid, gname in (await db.execute(
        select(CmsPlayerGroupMember.id_player, CmsPlayerGroup.group_name)
        .join(CmsPlayerGroup, CmsPlayerGroup.id_group == CmsPlayerGroupMember.id_group)
        .where(CmsPlayerGroup.id_org == id_org)
    )).all():
        groups_by_player.setdefault(pid, []).append(gname)

    latest_by_platform: dict[str, CmsPlayerUpdateVersion | None] = {}
    out: list[dict] = []
    for p in players:
        # Plateforme = `player_os` (une version linux ne doit PAS faire apparaître un ChromeOS/
        # Windows/BrightSign « en retard »). Pour BrightSign, `player_os` vaut « brightsign » : le
        # renderer partagé + l'autorun = code UNIFORME quelle que soit la famille SoC (malibu/
        # pagani) → une seule version « brightsign » couvre le parc BS, et matche le sélecteur
        # `model_family` de l'UI (peuplé par les `player_os` du parc). La granularité par SoC relève
        # du firmware (P1.1), pas du track `player` (§XXX).
        platform = p.player_os or "_"
        if platform not in latest_by_platform:
            latest_by_platform[platform] = await latest_player_version(db, platform)
        latest = latest_by_platform[platform]
        cur = p.player_version
        latest_ver = latest.version if latest else None
        # 🔑 TROIS états, pas deux. Sans version au catalogue pour cette plateforme il n'y a AUCUNE
        # référence : l'écran ne peut ni dire « à jour » ni « en retard ». L'ancien code repliait ce
        # cas sur « à jour » (`(latest_ver is None) or …`) ⇒ une installation neuve, dont le
        # catalogue est vide, affichait « 0 en retard » sur tout son parc et personne ne mettait
        # jamais rien à jour. Une absence de mesure ne se lit pas comme un succès.
        if not latest_ver:
            state = "unknown"
        elif semver_key(cur) >= semver_key(latest_ver):
            state = "up_to_date"
        else:
            state = "outdated"
        # 🔑 Renderer DÉGRADÉ (2026-08-12) : le player a connu une version épinglée et n'a pas pu la
        # charger — il exécute le chemin plat, donc du code potentiellement très ancien. Sans ce
        # champ, l'écran ne montre qu'un numéro de version en retard, qu'on lit spontanément comme
        # « pas encore déployé » : c'est ainsi qu'un ChromeOS a passé trois semaines en 2.0.8
        # pendant que le serveur épinglait 2.0.17, en repassant deux fois par un déploiement qui le
        # remontait. Un recul n'est pas un retard, et il ne doit pas pouvoir se cacher derrière.
        caps = p.capabilities if isinstance(p.capabilities, dict) else {}
        degraded = caps.get("renderer_degraded") or None
        out.append({
            "id_player": p.id_player, "player_name": p.player_name, "player_os": p.player_os,
            "status": p.status, "current_version": cur, "latest_version": latest_ver,
            "update_state": state, "up_to_date": state == "up_to_date",
            "renderer_degraded": degraded,
            # Moteur Electron courant (vitrail#5) : None pour les players non Electron.
            "electron_version": engine_version_of(caps),
            "id_latest_version": latest.id_version if latest else None,
            "site": sites.get(p.id_site) if p.id_site else None,
            "groups": sorted(groups_by_player.get(p.id_player, [])),
        })
    return out


async def resolve_renderer_version(db: AsyncSession, player: CmsPlayer) -> str | None:
    """Version de renderer ÉPINGLÉE qu'un player SERVI par le serveur (BrightSign/ChromeOS) doit
    charger au boot (§XXX #4). Le renderer n'est PAS téléchargé (fichier statique de l'image frontend) :
    au lieu de prendre « la dernière version servie » — ce qui mettrait à jour le parc involontairement
    à chaque MAJ serveur — le player charge la version ÉPINGLÉE.

    Pin = version du **dernier déploiement** (par `created_at`, statut non annulé/suspendu) ciblant ce
    player dont la version a `model_family == player_os`. Les déploiements portant `id_org` et étant
    résolus org-scoped, le pin est piloté par l'org du player (per-org, per-groupe → bac à sables).
    Défaut, dans l'ordre : sa **version courante connue** (`player_version`, préserve l'existant) →
    sinon la version **SERVIE** (manifeste = dernière disponible). Renvoie None seulement si la
    plateforme est inconnue du manifeste ET sans version courante (le bootstrap retombe alors sur le
    chemin plat = comportement historique — fail-safe, jamais d'écran blanc)."""
    from app.services import firmware as fw

    platform = player.player_os or ""
    row = (await db.execute(
        select(CmsPlayerUpdateVersion.version, CmsPlayerUpdateVersion.min_firmware_version)
        .join(CmsUpdateDeployment,
              CmsUpdateDeployment.id_version == CmsPlayerUpdateVersion.id_version)
        .join(CmsUpdateDeploymentTarget,
              CmsUpdateDeploymentTarget.id_deployment == CmsUpdateDeployment.id_deployment)
        .where(
            CmsUpdateDeploymentTarget.id_player == player.id_player,
            CmsUpdateDeployment.status.in_(("pending", "in_progress", "completed")),
            CmsPlayerUpdateVersion.model_family == platform,
        )
        .order_by(CmsUpdateDeployment.created_at.desc())
        .limit(1)
    )).one_or_none()
    if row:
        version, min_fw = row
        # Décision 2 « canal unique » : JAMAIS un renderer à un device sous son firmware
        # minimum (BOS remonté par capabilities.bs_bos ; inconnu = sous le minimum, fail-safe).
        # Le device garde son renderer courant ; quand le firmware orchestré est appliqué et
        # remonté, la même résolution rend enfin la version épinglée — l'ordre firmware PUIS
        # renderer est garanti ICI, quel que soit le timing des déploiements.
        if fw.firmware_at_least(fw.player_current_firmware(player), min_fw):
            return version
    if player.player_version:
        return player.player_version
    return served_player_versions().get(platform)


class DeploymentInFlight(Exception):
    """La même version est déjà en cours de déploiement vers au moins un des players visés."""

    def __init__(self, player_names: list[str]):
        self.player_names = player_names
        names = ", ".join(sorted(set(player_names))[:5]) + ("…" if len(set(player_names)) > 5 else "")
        super().__init__(f"This version is already being deployed to: {names}.")


async def create_deployment(
    db: AsyncSession, id_org: int, id_version: int, targets: list[Target],
    strategy: str, strategy_params: dict, id_user: int,
) -> CmsUpdateDeployment:
    """Crée un déploiement APPROUVÉ (§30.3) : résout les cibles → un target/player (pending), avec
    version courante en `previous_version` (base du rollback). Statut initial `pending`."""
    player_ids = sorted(await players_for_targets(db, id_org, targets))
    # Garde plateforme (incident 2026-07-06) : une version à `model_family` défini ne cible QUE les
    # players de cette plateforme (`player_os`, même convention que le parc). Sans ce filtre, un
    # déploiement org-wide d'un bundle windows partait aussi sur les Electron linux/rpi, qui
    # l'installaient par-dessus leur player (identité d'app changée → écran de setup).
    version = (await db.execute(
        select(CmsPlayerUpdateVersion).where(CmsPlayerUpdateVersion.id_version == id_version)
    )).scalar_one_or_none()
    if player_ids and version is not None and version.model_family:
        fam = version.model_family.lower()
        if fam in BUNDLE_PLATFORMS:
            # Version issue d'un IMPORT de bundle : `model_family` est une PLATEFORME
            # (« rpi »), pas un `player_os` (« linux-arm64 »). Comparer les deux par égalité
            # ne renvoyait AUCUN player — le déploiement était créé avec 0 cible et ne
            # faisait rien, sans le moindre message (constaté le 2026-07-26 en préparant
            # l'OTA de la sync gouvernée). On filtre donc sur tous les OS que sert le bundle.
            os_filter = CmsPlayer.player_os.in_(player_os_for_bundle(fam))
        else:
            # Familles hors bundle (firmware BrightSign : « malibu », ou anciennes lignes
            # écrites directement avec un player_os) → égalité stricte, comportement d'origine.
            os_filter = CmsPlayer.player_os == version.model_family
        player_ids = sorted((await db.execute(
            select(CmsPlayer.id_player).where(
                CmsPlayer.id_player.in_(player_ids), os_filter,
            )
        )).scalars().all())
    # ── Doublon : la même version déjà en cours vers un de ces players (retour Ed 14/11 : le
    # bouton ne montrait rien, 4 clics = 4 déploiements identiques). Refusé EN NOMMANT les players.
    if player_ids:
        dup = (await db.execute(
            select(CmsPlayer.player_name, CmsPlayer.id_player)
            .join(CmsUpdateDeploymentTarget, CmsUpdateDeploymentTarget.id_player == CmsPlayer.id_player)
            .join(CmsUpdateDeployment,
                  CmsUpdateDeployment.id_deployment == CmsUpdateDeploymentTarget.id_deployment)
            .where(
                CmsUpdateDeployment.id_version == id_version,
                CmsUpdateDeployment.status.in_(("pending", "in_progress", "paused")),
                CmsUpdateDeploymentTarget.status.in_(_IN_FLIGHT),
                CmsUpdateDeploymentTarget.id_player.in_(player_ids),
            )
        )).all()
        if dup:
            raise DeploymentInFlight([n or f"#{i}" for n, i in dup])

    # ── Décision 2 « canal unique » : orchestration firmware → renderer (BrightSign) ──
    # Une version renderer BS peut exiger un BOS minimum. Les cibles SOUS le minimum reçoivent
    # d'abord le firmware ÉPINGLÉ de leur famille (poussé ici même) ; la garde de
    # `resolve_renderer_version` retient le renderer jusqu'à ce que le device remonte le BOS
    # attendu. Si AUCUN firmware épinglé ne répond à l'exigence → refus 422 NOMMANT familles et
    # players (l'admin importe le `.bsfw` puis rejoue) — refuser ce qui est détectable au moment
    # du geste plutôt qu'un déploiement créé qui ne s'appliquerait jamais, en silence.
    firmware_pushes: list[tuple[CmsPlayerUpdateVersion, list[CmsPlayer]]] = []
    if (version is not None and (version.model_family or "").lower() == "brightsign"
            and version.min_firmware_version and player_ids):
        from app.services import firmware as fw

        bs_players = (await db.execute(
            select(CmsPlayer).where(CmsPlayer.id_player.in_(player_ids))
        )).scalars().all()
        below = [p for p in bs_players
                 if not fw.firmware_at_least(fw.player_current_firmware(p),
                                             version.min_firmware_version)]
        if below:
            by_family: dict[str | None, list[CmsPlayer]] = {}
            for p in below:
                by_family.setdefault(fw.player_family(p), []).append(p)
            missing_fams: list[str] = []
            missing_players: list[str] = []
            for fam, plist in by_family.items():
                pinned = await fw.pinned_for_family(db, fam) if fam else None
                if (pinned is None or not pinned.storage_key
                        or not fw.firmware_at_least(pinned.version,
                                                    version.min_firmware_version)):
                    missing_fams.append(fam or "inconnue")
                    missing_players.extend(p.player_name or f"#{p.id_player}" for p in plist)
                else:
                    firmware_pushes.append((pinned, plist))
            if missing_fams:
                raise fw.FirmwareRequired(version.min_firmware_version,
                                          missing_fams, missing_players)
            for pinned, plist in firmware_pushes:
                await fw.stage_push(db, id_org, id_user, pinned, plist)

    # Moteur (vitrail#5, décision D3) : arrêt au PREMIER échec ou retour arrière, sauf refus
    # explicite. Un moteur qui casse un écran ne doit pas partir sur les suivants.
    strategy_params = dict(strategy_params or {})
    if version is not None and version.track == ENGINE_TRACK:
        strategy_params.setdefault("stop_on_failure", True)

    now = datetime.now(UTC).replace(tzinfo=None)   # colonne TIMESTAMP naïf (asyncpg)
    dep = CmsUpdateDeployment(
        id_org=id_org, id_version=id_version, approved_by=id_user, approved_at=now,
        strategy=strategy, strategy_params=strategy_params,
        status="pending", total_targets=len(player_ids),
    )
    db.add(dep)
    await db.flush()  # id_deployment

    versions = {}
    if player_ids:
        rows = (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_version, CmsPlayer.capabilities)
            .where(CmsPlayer.id_player.in_(player_ids))
        )).all()
        if version is not None and version.track == ENGINE_TRACK:
            # Base du retour arrière d'un moteur = l'Electron COURANT, pas la version du code.
            versions = {pid: engine_version_of(caps) for pid, _ver, caps in rows}
        else:
            versions = {pid: ver for pid, ver, _caps in rows}
    for pid in player_ids:
        db.add(CmsUpdateDeploymentTarget(
            id_deployment=dep.id_deployment, id_player=pid, status="pending",
            previous_version=versions.get(pid),
        ))
    await db.commit()
    await db.refresh(dep)
    # Commandes firmware APRÈS commit (un device rapide ne doit jamais rebooter sur un
    # déploiement non persisté). Best-effort : un player hors ligne recevra la MAJ au
    # prochain passage par l'état firmware (le déploiement, lui, est en base).
    if firmware_pushes:
        from app.services import firmware as fw

        for pinned, plist in firmware_pushes:
            await fw.publish_push_commands(pinned, plist)
    return dep


async def deployment_targets(
    db: AsyncSession, id_deployment: int
) -> list[CmsUpdateDeploymentTarget]:
    return list((await db.execute(
        select(CmsUpdateDeploymentTarget)
        .where(CmsUpdateDeploymentTarget.id_deployment == id_deployment)
        .order_by(CmsUpdateDeploymentTarget.id_target)
    )).scalars().all())


# ── Slice 2 : apply côté player (directive au heartbeat + détection de succès) ──────────
# États d'une cible « en vol » (le player a reçu la directive et progresse). La directive est émise
# tant que la cible est non terminale → le player PILOTE sa machine à états (download windowé →
# `staged` → install dans la fenêtre de maintenance). `staged` = téléchargé & vérifié, en attente de
# la fenêtre d'install.
_IN_FLIGHT = ("pending", "queued", "downloading", "staged", "installing")


def engine_version_of(capabilities: dict | None) -> str | None:
    """Version d'Electron remontée par le player dans ses `capabilities` (heartbeat)."""
    v = (capabilities or {}).get("electron_version") if isinstance(capabilities, dict) else None
    return str(v) if v else None


def _track_filter(track: str | None):
    """`engine` → la seule piste moteur ; sinon → TOUT SAUF le moteur (comportement historique
    de la clé `update` : piste player, et firmware BrightSign qui l'emprunte aussi)."""
    if track == ENGINE_TRACK:
        return CmsPlayerUpdateVersion.track == ENGINE_TRACK
    return CmsPlayerUpdateVersion.track != ENGINE_TRACK


async def directive_for_player(
    db: AsyncSession, id_player: int, track: str | None = None
) -> dict | None:
    """Directive `update` à joindre au sync payload d'un player : sa cible de déploiement
    ACTIONNABLE, ou None. Émise tant que la cible est **non terminale** (le player s'en sert de
    source de vérité pour piloter download→staged→install ; si elle disparaît = annulée/terminée →
    le player abandonne). `scheduled` = pas émise avant `strategy_params.start_at`. Porte aussi les
    **fenêtres** (Slice 2c) : `download_window`/`install_window` "HH:MM-HH:MM" (heure locale)
    + `force` (ignorer les fenêtres) issus de `strategy_params`. Backward-compatible : un renderer
    sans handler ignore ce bloc. Le plus récent déploiement d'abord."""
    row = (await db.execute(
        select(CmsUpdateDeploymentTarget, CmsUpdateDeployment, CmsPlayerUpdateVersion)
        .join(CmsUpdateDeployment,
              CmsUpdateDeployment.id_deployment == CmsUpdateDeploymentTarget.id_deployment)
        .join(CmsPlayerUpdateVersion,
              CmsPlayerUpdateVersion.id_version == CmsUpdateDeployment.id_version)
        .where(
            CmsUpdateDeploymentTarget.id_player == id_player,
            CmsUpdateDeploymentTarget.status.in_(_IN_FLIGHT),
            CmsUpdateDeployment.status.in_(("pending", "in_progress")),
            _track_filter(track),
        )
        .order_by(CmsUpdateDeployment.created_at.desc())
    )).first()
    if not row:
        return None
    target, dep, ver = row
    params = dep.strategy_params or {}
    if dep.strategy == "scheduled":
        start_at = params.get("start_at")
        if start_at:
            try:
                sa = datetime.fromisoformat(str(start_at).replace("Z", "")).replace(tzinfo=None)
                if datetime.now(UTC).replace(tzinfo=None) < sa:
                    return None
            except ValueError:
                pass  # start_at illisible → on ne bloque pas (fail-open)
    return {
        "id_deployment": dep.id_deployment,
        "id_target": target.id_target,
        "version": ver.version,
        "track": ver.track,
        "download_url": ver.download_url,
        "sha256": ver.sha256_checksum,
        "strategy": dep.strategy,
        "model_family": ver.model_family,
        # Slice 2c — fenêtres (heure locale du site) : le player télécharge dans download_window
        # (reprise possible) puis n'installe/reboote que dans install_window. force = tout de suite.
        "download_window": params.get("download_window"),
        "install_window": params.get("install_window"),
        "force": bool(params.get("force")),
    }


async def mark_success_if_updated(
    db: AsyncSession, id_player: int, reported_version: str | None
) -> list[int]:
    """Détection de SUCCÈS observable (appelée au heartbeat) : si le player remonte une
    `player_version` >= la version cible d'un de ses déploiements en cours, la cible passe
    `success`. On ne fait pas confiance à un auto-« success » : on constate la montée de version.
    Renvoie les id_deployment modifiés (à rollup). NB : commit fait par l'appelant."""
    if not reported_version:
        return []
    # `paused` inclus : un player déjà `installing` au moment de la suspension peut finir quand
    # même — sa montée de version doit être constatée (le statut du déploiement, lui, reste paused).
    rows = (await db.execute(
        select(CmsUpdateDeploymentTarget, CmsPlayerUpdateVersion.version)
        .join(CmsUpdateDeployment,
              CmsUpdateDeployment.id_deployment == CmsUpdateDeploymentTarget.id_deployment)
        .join(CmsPlayerUpdateVersion,
              CmsPlayerUpdateVersion.id_version == CmsUpdateDeployment.id_version)
        .where(
            CmsUpdateDeploymentTarget.id_player == id_player,
            CmsUpdateDeploymentTarget.status.in_(_IN_FLIGHT),
            CmsUpdateDeployment.status.in_(("pending", "in_progress", "paused")),
            _track_filter(None),   # la version du CODE ne dit rien du moteur
        )
    )).all()
    now = datetime.now(UTC).replace(tzinfo=None)
    changed: set[int] = set()
    for target, target_version in rows:
        if semver_key(reported_version) >= semver_key(target_version):
            target.status = "success"
            target.completed_at = now
            changed.add(target.id_deployment)
    if changed:
        await db.flush()
    return list(changed)


async def mark_engine_success(
    db: AsyncSession, id_player: int, electron_version: str | None
) -> list[int]:
    """Succès d'un déploiement MOTEUR, constaté au heartbeat : l'Electron remonté par le player
    est EXACTEMENT la version cible. Égalité et non « >= » : une descente de version doit
    réussir aussi (retour à une version sûre). Renvoie les id_deployment modifiés."""
    if not electron_version:
        return []
    rows = (await db.execute(
        select(CmsUpdateDeploymentTarget, CmsPlayerUpdateVersion.version)
        .join(CmsUpdateDeployment,
              CmsUpdateDeployment.id_deployment == CmsUpdateDeploymentTarget.id_deployment)
        .join(CmsPlayerUpdateVersion,
              CmsPlayerUpdateVersion.id_version == CmsUpdateDeployment.id_version)
        .where(
            CmsUpdateDeploymentTarget.id_player == id_player,
            CmsUpdateDeploymentTarget.status.in_(_IN_FLIGHT),
            CmsUpdateDeployment.status.in_(("pending", "in_progress", "paused")),
            _track_filter(ENGINE_TRACK),
        )
    )).all()
    now = datetime.now(UTC).replace(tzinfo=None)
    changed: set[int] = set()
    for target, target_version in rows:
        if str(electron_version).strip() == str(target_version).strip():
            target.status = "success"
            target.completed_at = now
            changed.add(target.id_deployment)
    if changed:
        await db.flush()
    return list(changed)


# Statuts qu'un player a le droit de remonter (`update:status`). `rolled_back` : le moteur
# essayé n'a pas rendu dans le délai, le player est revenu SEUL sur l'ancien (vitrail#5).
REPORTABLE_STATUSES = ("downloading", "staged", "installing", "success", "failed", "rolled_back")

_TERMINAL_TARGET = ("success", "failed", "cancelled", "rolled_back")


async def report_target(
    db: AsyncSession, id_player: int, id_deployment: int, new_status: str, error: str | None = None
) -> bool:
    """Applique un report d'avancement d'apply venu du player à SA cible (il ne peut modifier que la
    sienne : WHERE id_player). Ne rétrograde pas une cible déjà terminale (idempotent, anti-rejeu).
    Pose `started_at` au 1er démarrage (base du timeout), `completed_at` à la fin. Renvoie True si
    une ligne a changé (→ l'appelant enqueue un rollup). NB : commit fait par l'appelant."""
    tgt = (await db.execute(
        select(CmsUpdateDeploymentTarget).where(
            CmsUpdateDeploymentTarget.id_deployment == id_deployment,
            CmsUpdateDeploymentTarget.id_player == id_player,
        )
    )).scalar_one_or_none()
    if tgt is None or tgt.status in _TERMINAL_TARGET:
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    tgt.status = new_status
    # `started_at` = base du timeout : posé (réinitialisé) à l'ENTRÉE en `installing` uniquement →
    # le timeout ne concerne QUE la phase d'install (rapide). `downloading`/`staged` peuvent durer
    # (fenêtre de download, attente de la fenêtre de maintenance) sans jamais timeouter.
    if new_status == "installing":
        tgt.started_at = now
    if new_status in ("success", "failed", "rolled_back"):
        tgt.completed_at = now
        if new_status in ("failed", "rolled_back"):
            tgt.error_message = (error or "")[:1000]
    await db.flush()
    return True
