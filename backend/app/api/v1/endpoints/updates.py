"""Gestion des versions & déploiements OTA (§XXX) — Phase 2 slice 1 (CMS-side).

Aucune MAJ auto sans approbation (§30.1). Org-admin : inventaire du parc, catalogue de versions,
création/suivi de déploiement. L'apply côté player (par OS) = slice 2. RLS + gate org-admin.
"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_player, get_db, require_org_admin
from app.db.session import get_db_worker
from app.models.player import CmsPlayer
from app.models.player_version import (
    CmsPlayerUpdateVersion,
    CmsUpdateDeployment,
    CmsUpdateDeploymentTarget,
)
from app.models.user import CmsUser
from app.schemas.updates import (
    BundleImportResult,
    DeploymentCreate,
    DeploymentDetail,
    DeploymentRead,
    DeploymentTargetRead,
    RendererVersionRead,
    ServedVersionRead,
    ServedVersionRegister,
    UpdateVersionCreate,
    UpdateVersionPatch,
    UpdateVersionRead,
)
from app.services import bundle_import, media, ota
from app.services.firmware import FirmwareRequired
from app.services.served_versions import served_player_versions
from app.services.targeting import Target

router = APIRouter(prefix="/updates", tags=["updates"])

_STRATEGIES = {"immediate", "gradual", "scheduled"}
_TRACKS = {"player", "server"}
# Source unique dans `services.ota` : le garde de déploiement en a besoin lui aussi (une
# version importée porte une PLATEFORME de bundle, pas un player_os — cf. ota.create_deployment).
_BUNDLE_PLATFORMS = ota.BUNDLE_PLATFORMS
_OS_TO_BUNDLE = ota.OS_TO_BUNDLE
# Extension + type MIME de l'artefact par plateforme — source unique dans `bundle_import` :
# l'import ÉCRIT la clé `player-bundles/<plat>/<ver><ext>`, cet endpoint la RELIT. Deux tables
# séparées se décaleraient un jour, et le player téléchargerait un 404.
_BUNDLE_ARTIFACT = bundle_import.BUNDLE_ARTIFACT


@router.get("/bundles/{platform}/{version}")
async def download_bundle(
    platform: str,
    version: str,
    _player: Annotated[CmsPlayer, Depends(get_current_player)],
) -> Response:
    """Sert un bundle OTA player (§XXX) DEPUIS le serveur du player — jamais GitHub. Authentifié
    par token machine. Le catalogue stocke un `download_url` RELATIF (`/api/v1/updates/bundles/
    <os>/<ver>`) que le player résout contre son propre `backend_url` → une release serveur est
    portable d'un client à l'autre (aucun hostname ni token GitHub dans le catalogue)."""
    if platform not in _BUNDLE_PLATFORMS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown platform.")
    if _OS_TO_BUNDLE.get((_player.player_os or "").lower()) != platform:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bundle is for a different platform than the player's.",
        )
    if not ota._SEMVER.match((version or "").strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid version.")
    ext, mime = _BUNDLE_ARTIFACT[platform]
    key = f"player-bundles/{platform}/{version}{ext}"
    try:
        data = await media.download_file(key)
    except Exception:  # noqa: BLE001 — clé absente / stockage indispo → bundle introuvable
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bundle not found.")
    return Response(
        content=data, media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{platform}-{version}{ext}"'},
    )


# GET + HEAD : la sonde de l'amorce ChromeOS est un HEAD — or `@router.get` FastAPI ne
# repond PAS au HEAD (405), contrairement aux statiques Next. Mesure du canary 2026-07-28 :
# la sonde echouait et l'amorce retombait a plat en silence.
@router.api_route("/renderers/{platform}/{version}/{filepath:path}", methods=["GET", "HEAD"])
async def download_renderer_file(
    platform: str,
    version: str,
    filepath: str,
) -> Response:
    """Sert UN fichier d'un renderer importé (décision 3 « canal unique ») : le bootstrap
    ChromeOS et l'autorun BrightSign consomment des fichiers INDIVIDUELS, pas une archive.

    PUBLIC comme les statiques `frontend/public/player-chromeos/…` d'aujourd'hui (même posture :
    le renderer n'est pas un secret, et les balises <script> du PWA ne peuvent pas porter
    d'en-tête Authorization). La surface est bornée : plateformes en liste fermée, version
    SemVer, chemin validé par la MÊME règle que l'import (un chemin qui n'a pas pu être écrit
    ne peut pas être demandé). Version dans l'URL ⇒ cache immutable."""
    if platform not in bundle_import.RENDERER_PLATFORMS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown platform.")
    if not ota._SEMVER.match((version or "").strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid version.")
    if (not bundle_import._RENDERER_PATH_RE.match(filepath or "")
            or ".." in (filepath or "").split("/")):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")
    key = f"player-renderers/{platform}/{version}/{filepath}"
    try:
        data = await media.download_file(key)
    except Exception:  # noqa: BLE001 — clé absente / stockage indispo → introuvable
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")
    return Response(
        content=data, media_type=bundle_import.renderer_file_mime(filepath),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/renderer-version", response_model=RendererVersionRead)
async def renderer_version(
    _player: Annotated[CmsPlayer, Depends(get_current_player)],
    # ⚠️ get_db_worker (PAS get_db) : get_db dépend de get_current_user → il exigerait un token
    # UTILISATEUR alors que cet endpoint est authentifié PLAYER (get_current_player) → un token
    # player recevait 401 « Token invalide ou expiré » (le pin ne se résolvait jamais → repli plat,
    # épinglage HS). get_db_worker est sans auth (celui qu'utilise get_current_player lui-même).
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> RendererVersionRead:
    """Version de renderer ÉPINGLÉE que CE player (BrightSign/ChromeOS) doit charger — appelée par le
    bootstrap AVANT de charger le renderer versionné (§XXX #4). Le player ne charge JAMAIS « la dernière
    version servie » : il charge la version approuvée par son org (pin = déploiements). `version=None`
    → le bootstrap retombe sur le chemin plat (rétro-compat / fail-safe, jamais d'écran blanc)."""
    version = await ota.resolve_renderer_version(db, _player)
    return RendererVersionRead(platform=(_player.player_os or ""), version=version)


@router.get("/parc")
async def updates_parc(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Inventaire des versions du parc : par player, version courante vs dernière dispo + à-jour."""
    inv = await ota.parc_inventory(db, current_user.id_org)
    # `unknown` (aucune version publiée pour cette plateforme) est compté À PART : le confondre avec
    # « en retard » crierait au loup sur une install neuve, le confondre avec « à jour » (l'ancien
    # comportement) annonce un parc sain qu'on n'a jamais comparé à rien.
    outdated = sum(1 for p in inv if p["update_state"] == "outdated")
    unknown = sum(1 for p in inv if p["update_state"] == "unknown")
    return {"players": inv, "total": len(inv), "outdated": outdated, "unknown": unknown}


@router.get("/versions", response_model=list[UpdateVersionRead])
async def updates_versions(
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    track: str = "player",
) -> list[CmsPlayerUpdateVersion]:
    """Catalogue de versions (par track), plus récentes d'abord."""
    return list((await db.execute(
        select(CmsPlayerUpdateVersion)
        .where(CmsPlayerUpdateVersion.track == track)
        .order_by(CmsPlayerUpdateVersion.created_at.desc())
    )).scalars().all())


def _validated_min_firmware(value: str | None, model_family: str | None) -> str | None:
    """Décision 2 « canal unique » : le BOS minimum n'a de sens que sur un renderer BrightSign,
    et doit être numérique à points (« 9.1.132 ») — un format libre casserait la comparaison de
    la garde `resolve_renderer_version` en silence."""
    v = (value or "").strip()
    if not v:
        return None
    if (model_family or "").strip().lower() != "brightsign":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="min_firmware_version only applies to a BrightSign renderer.")
    if not re.fullmatch(r"\d+(\.\d+)*", v):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid min_firmware_version (expected e.g. 9.1.132).")
    return v


@router.post("/versions", response_model=UpdateVersionRead, status_code=status.HTTP_201_CREATED)
async def updates_version_create(
    body: UpdateVersionCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerUpdateVersion:
    """Enregistre une version au catalogue (track player/server)."""
    if body.track not in _TRACKS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid track.")
    if (body.channel or "stable") not in ("stable", "beta", "lts"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel.")
    if not ota._SEMVER.match((body.version or "").strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid version (expected MAJOR.MINOR.PATCH).")
    min_fw = _validated_min_firmware(body.min_firmware_version, body.model_family)
    row = CmsPlayerUpdateVersion(
        track=body.track, version=body.version.strip(), channel=body.channel or "stable",
        download_url=body.download_url, sha256_checksum=body.sha256_checksum,
        model_family=(body.model_family or None), release_notes=body.release_notes,
        release_notes_fr=body.release_notes_fr, min_firmware_version=min_fw,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/versions/import", response_model=BundleImportResult)
async def updates_version_import(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: Annotated[UploadFile | None, File()] = None,
    url: Annotated[str | None, Form()] = None,
    platform: Annotated[str | None, Form()] = None,
    version: Annotated[str | None, Form()] = None,
    dry_run: Annotated[bool, Form()] = False,
) -> BundleImportResult:
    """Importe une version player INSTALLÉE sur l'appareil (linux/rpi/windows/android) depuis un
    FICHIER ou une URL (page de distribution). Détecte plateforme (nom de fichier) + version
    (`package.json` interne pour tar.gz), déduplique, puis — hors `dry_run` — stocke l'artefact dans le
    MinIO du client + crée la ligne catalogue à `download_url` RELATIF (le player tire depuis SON
    serveur). `dry_run=true` = aperçu sans écrire (l'UI l'utilise avant confirmation)."""
    # 1. Octets + nom de fichier (URL ou upload).
    if file is not None:
        data = await file.read()
        fname = file.filename or ""
    elif url:
        try:
            data = await bundle_import.fetch_bundle(url)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        fname = url.rstrip("/").split("/")[-1].split("?")[0]
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Provide a file or a URL.")
    # 2. Détection + dédup + écriture : MÊME service que l'amorçage automatique des bundles à
    #    l'installation (`scripts/seed_player_bundles.py`). Une seule définition de la clé de
    #    stockage et du `download_url` relatif — deux implémentations divergeraient.
    try:
        return await bundle_import.import_bundle(
            db, data=data, filename=fname, platform=platform, version=version, dry_run=dry_run
        )
    except bundle_import.BundleImportError as exc:
        # 400 = la requête elle-même est inexploitable (vide/trop gros) ; 422 = le contenu est
        # lisible mais refusé (plateforme, version, type d'artefact) — distinction d'origine.
        msg = str(exc)
        code = (
            status.HTTP_400_BAD_REQUEST
            if msg.startswith(("Empty content", "Archive too large"))
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(status_code=code, detail=msg)


@router.post("/versions/{id_version}/promote", response_model=UpdateVersionRead)
async def updates_version_promote(
    id_version: int,
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerUpdateVersion:
    """Promeut une version de TEST (beta) en STABLE — workflow canary : on crée la version en beta
    (invisible du « latest » du parc), on la déploie sur un groupe de test, et si tout va bien on la
    promeut ici. Une fois stable, elle devient la référence « à jour / en retard » de sa plateforme
    et se déploie au parc par un déploiement classique (approbation explicite, §30.1)."""
    row = (await db.execute(
        select(CmsPlayerUpdateVersion).where(CmsPlayerUpdateVersion.id_version == id_version)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    if row.channel == "stable":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already stable.")
    row.channel = "stable"
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/versions/{id_version}", status_code=status.HTTP_204_NO_CONTENT)
async def updates_version_delete(
    id_version: int,
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Supprime une version du catalogue si elle n'est référencée par aucun déploiement."""
    used = (await db.execute(
        select(CmsUpdateDeployment.id_deployment)
        .where(CmsUpdateDeployment.id_version == id_version).limit(1)
    )).scalar_one_or_none()
    if used is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Version is used by a deployment.")
    row = (await db.execute(
        select(CmsPlayerUpdateVersion).where(CmsPlayerUpdateVersion.id_version == id_version)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/versions/{id_version}", response_model=UpdateVersionRead)
async def updates_version_patch(
    id_version: int,
    body: UpdateVersionPatch,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerUpdateVersion:
    """Pose (ou efface) le BOS minimum d'une version de renderer BrightSign (décision 2)."""
    row = (await db.execute(
        select(CmsPlayerUpdateVersion).where(CmsPlayerUpdateVersion.id_version == id_version)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    row.min_firmware_version = _validated_min_firmware(
        body.min_firmware_version, row.model_family)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/served-versions", response_model=list[ServedVersionRead])
async def updates_served_versions(
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ServedVersionRead]:
    """Renderers SERVIS par ce serveur (BrightSign/ChromeOS) : pas de bundle à télécharger (apply =
    reload/RefreshRenderer). Pour chaque plateforme : version servie (constante du code, figée au
    manifeste) + si elle est déjà déclarée au catalogue + la dernière déclarée. L'admin déclare une
    version pour pouvoir l'ÉPINGLER via un déploiement (§30.1) — une MAJ serveur n'avance rien seule."""
    served = served_player_versions()
    if not served:
        return []
    rows = list((await db.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.track == "player",
            CmsPlayerUpdateVersion.model_family.in_(list(served.keys())),
        )
    )).scalars().all())
    by_platform: dict[str, list[CmsPlayerUpdateVersion]] = {}
    for r in rows:
        by_platform.setdefault(r.model_family or "", []).append(r)
    out: list[ServedVersionRead] = []
    for platform, version in sorted(served.items()):
        catalog = by_platform.get(platform, [])
        latest = max((c.version for c in catalog), key=ota.semver_key, default=None)
        out.append(ServedVersionRead(
            platform=platform, served_version=version,
            registered=any(c.version == version for c in catalog),
            latest_registered=latest,
        ))
    return out


@router.post("/served-versions/register", response_model=UpdateVersionRead)
async def updates_served_version_register(
    body: ServedVersionRegister,
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerUpdateVersion:
    """Déclare la version SERVIE d'une plateforme (BS/ChromeOS) au catalogue → elle devient déployable
    (= épinglable). La version vient du MANIFESTE, jamais d'une saisie libre (on ne déclare que ce que
    le serveur sert réellement). Idempotent : renvoie la ligne existante si déjà déclarée.
    `download_url=NULL` (rien à télécharger : l'apply est un reload/RefreshRenderer côté player)."""
    served = served_player_versions()
    version = served.get(body.platform)
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Unknown served platform.")
    existing = (await db.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.track == "player",
            CmsPlayerUpdateVersion.model_family == body.platform,
            CmsPlayerUpdateVersion.version == version,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    row = CmsPlayerUpdateVersion(
        track="player", version=version, channel="stable",
        model_family=body.platform, download_url=None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _force_sync_players(player_ids: list[int]) -> None:
    """Pousse `player:force_sync` aux players (best-effort) : ils re-synchronisent et voient la
    directive `update` apparaître/disparaître immédiatement (création, pause, reprise)."""
    if not player_ids:
        return
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        try:
            for pid in player_ids:
                await r.publish(f"signflow:player:{pid}:cmds",
                                json.dumps({"type": "player:force_sync"}))
        finally:
            await r.aclose()
    except Exception:  # noqa: BLE001 — la directive arrivera au reconnect si le push échoue
        pass


async def _deployment_or_404(
    db: AsyncSession, id_deployment: int, id_org: int
) -> CmsUpdateDeployment:
    dep = (await db.execute(
        select(CmsUpdateDeployment).where(
            CmsUpdateDeployment.id_deployment == id_deployment,
            CmsUpdateDeployment.id_org == id_org,
        )
    )).scalar_one_or_none()
    if dep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Deployment not found.")
    return dep


@router.post("/deployments", response_model=DeploymentRead, status_code=status.HTTP_201_CREATED)
async def deployment_create(
    body: DeploymentCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsUpdateDeployment:
    """Crée un déploiement APPROUVÉ (cibles + stratégie) → un target/player en attente (§30.3)."""
    if body.strategy not in _STRATEGIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid strategy.")
    version = (await db.execute(
        select(CmsPlayerUpdateVersion).where(CmsPlayerUpdateVersion.id_version == body.id_version)
    )).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    targets = [Target(target_type=t.target_type, target_id=t.target_id, tag_expr=t.tag_expr or {})
               for t in body.targets]
    try:
        dep = await ota.create_deployment(
            db, current_user.id_org, body.id_version, targets,
            body.strategy, body.strategy_params or {}, current_user.id_user,
        )
    # Décision 2 « canal unique » : renderer BS exigeant un BOS minimum sans firmware épinglé
    # adéquat → 422 actionnable (importer le .bsfw puis rejouer), jamais un déploiement muet.
    except FirmwareRequired as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    # Directive immédiate : force_sync les players ciblés → ils reçoivent le bloc `update` au
    # prochain sync (sinon ils l'auraient au reconnect / au prochain heartbeat-sync). Best-effort.
    tgt_ids = list((await db.execute(
        select(CmsUpdateDeploymentTarget.id_player)
        .where(CmsUpdateDeploymentTarget.id_deployment == dep.id_deployment)
    )).scalars().all())
    await _force_sync_players(tgt_ids)
    return dep


@router.get("/deployments", response_model=list[DeploymentRead])
async def deployments_list(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsUpdateDeployment]:
    """Déploiements de l'org, plus récents d'abord."""
    return list((await db.execute(
        select(CmsUpdateDeployment)
        .where(CmsUpdateDeployment.id_org == current_user.id_org)
        .order_by(CmsUpdateDeployment.created_at.desc())
    )).scalars().all())


@router.get("/deployments/{id_deployment}", response_model=DeploymentDetail)
async def deployment_detail(
    id_deployment: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeploymentDetail:
    """Détail d'un déploiement + statut par player."""
    dep = (await db.execute(
        select(CmsUpdateDeployment).where(
            CmsUpdateDeployment.id_deployment == id_deployment,
            CmsUpdateDeployment.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if dep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Deployment not found.")
    targets = await ota.deployment_targets(db, id_deployment)
    detail = DeploymentDetail.model_validate(dep)
    detail.targets = [DeploymentTargetRead.model_validate(t) for t in targets]
    return detail


@router.post("/deployments/{id_deployment}/cancel", response_model=DeploymentRead)
async def deployment_cancel(
    id_deployment: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsUpdateDeployment:
    """Annule un déploiement encore en attente/en cours/suspendu (targets pending → cancelled)."""
    dep = await _deployment_or_404(db, id_deployment, current_user.id_org)
    if dep.status in ("completed", "cancelled"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already completed/cancelled.")
    dep.status = "cancelled"
    dep.completed_at = datetime.now(UTC).replace(tzinfo=None)   # colonne TIMESTAMP (naïf)
    for tgt in await ota.deployment_targets(db, id_deployment):
        if tgt.status in ("pending", "queued", "downloading", "staged", "installing"):
            tgt.status = "cancelled"
    await db.commit()
    await db.refresh(dep)
    return dep


@router.post("/deployments/{id_deployment}/pause", response_model=DeploymentRead)
async def deployment_pause(
    id_deployment: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsUpdateDeployment:
    """Suspend un déploiement en attente/en cours : la directive `update` n'est plus servie au sync
    (les players non démarrés attendent ; un player mi-download abandonne au prochain sync). Les
    cibles gardent leur statut → la REPRISE repart exactement où on s'était arrêté."""
    dep = await _deployment_or_404(db, id_deployment, current_user.id_org)
    if dep.status not in ("pending", "in_progress"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Only a pending/in-progress deployment can be paused.")
    dep.status = "paused"
    await db.commit()
    await db.refresh(dep)
    # Les players en vol re-syncent → la directive a disparu → ils stoppent proprement.
    tgt_ids = [t.id_player for t in await ota.deployment_targets(db, id_deployment)
               if t.status in ota._IN_FLIGHT]
    await _force_sync_players(tgt_ids)
    return dep


@router.post("/deployments/{id_deployment}/resume", response_model=DeploymentRead)
async def deployment_resume(
    id_deployment: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsUpdateDeployment:
    """Reprend un déploiement suspendu : statut recalculé depuis les cibles (mêmes règles que le
    rollup worker — des retardataires ont pu finir pendant la pause) + force_sync des cibles en vol
    pour une reprise immédiate."""
    dep = await _deployment_or_404(db, id_deployment, current_user.id_org)
    if dep.status != "paused":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Only a paused deployment can be resumed.")
    targets = await ota.deployment_targets(db, id_deployment)
    counts: dict[str, int] = {}
    for t in targets:
        counts[t.status] = counts.get(t.status, 0) + 1
    total = len(targets)
    failed = counts.get("failed", 0) + counts.get("rolled_back", 0)
    terminal = sum(v for k, v in counts.items() if k in ota._TERMINAL_TARGET)
    non_pending = sum(v for k, v in counts.items() if k != "pending")
    if total == 0 or terminal >= total:
        dep.status = "failed" if (total and failed) else "completed"
        dep.completed_at = datetime.now(UTC).replace(tzinfo=None)
    elif non_pending > 0:
        dep.status = "in_progress"
    else:
        dep.status = "pending"
    dep.success_count = counts.get("success", 0)
    dep.failed_count = failed
    await db.commit()
    await db.refresh(dep)
    tgt_ids = [t.id_player for t in targets if t.status in ota._IN_FLIGHT]
    await _force_sync_players(tgt_ids)
    return dep


@router.delete("/deployments/{id_deployment}", status_code=status.HTTP_204_NO_CONTENT)
async def deployment_delete(
    id_deployment: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Supprime un déploiement ANNULÉ, SUSPENDU ou TERMINÉ (completed/failed) — jamais un
    déploiement actif (l'annuler d'abord). Purge ses cibles (FK) puis la ligne."""
    dep = await _deployment_or_404(db, id_deployment, current_user.id_org)
    if dep.status not in ("cancelled", "paused", "completed", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deployment is active — cancel or pause it before deleting it.",
        )
    await db.execute(sa_delete(CmsUpdateDeploymentTarget)
                     .where(CmsUpdateDeploymentTarget.id_deployment == id_deployment))
    await db.delete(dep)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
