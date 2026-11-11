"""MAJ firmware BrightSign depuis l'UI (P1.1, §XXX).

- Exploitant du serveur (super-admin, ou org-admin d'un serveur sans super-admin) :
  enregistre/épingle/supprime des versions firmware (`.bsfw` → MinIO).
- Org-admin  : consulte l'état de MAJ d'un player + déclenche la MAJ vers la version ÉPINGLÉE
               (garde-fou : la version est résolue côté serveur, jamais choisie par l'appelant).
"""
import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_server_operator, require_org_admin
from app.models.audit_log import CmsAuditLog
from app.models.player import CmsPlayer
from app.models.player_version import (
    CmsPlayerUpdateVersion,
    CmsUpdateDeployment,
    CmsUpdateDeploymentTarget,
)
from app.models.user import CmsUser
from app.schemas.firmware import (
    FirmwarePushResponse,
    FirmwareUpdateState,
    FirmwareVersionRead,
    PinnedFirmwareRead,
)
from app.services import firmware as fw
from app.services.media import delete_file, get_signed_url, stream_upload

router = APIRouter(prefix="/firmware", tags=["firmware"])

_MAX_BSFW_BYTES = 1024 * 1024 * 1024   # garde-fou 1 Go (un .bsfw fait ~100-400 Mo)


# ── Gestion des versions (exploitant du serveur) ───────────────────────────────────────────

@router.get("/versions", response_model=list[FirmwareVersionRead])
async def list_firmware_versions(
    current_user: Annotated[CmsUser, Depends(require_server_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsPlayerUpdateVersion]:
    """Liste des versions firmware enregistrées (track='firmware'), pinned d'abord."""
    rows = (await db.execute(
        select(CmsPlayerUpdateVersion)
        .where(CmsPlayerUpdateVersion.track == "firmware")
        .order_by(CmsPlayerUpdateVersion.is_pinned.desc(), CmsPlayerUpdateVersion.created_at.desc())
    )).scalars().all()
    return list(rows)


@router.get("/pinned", response_model=list[PinnedFirmwareRead])
async def list_pinned_firmware(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PinnedFirmwareRead]:
    """Firmwares épinglés par famille (+ URL signée), pour bundler le `.bsfw` validé
    dans le ZIP SD d'un nouveau player (P2.2). Org-admin : lecture seule."""
    rows = (await db.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.track == "firmware",
            CmsPlayerUpdateVersion.is_pinned.is_(True),
        )
    )).scalars().all()
    out: list[PinnedFirmwareRead] = []
    for r in rows:
        if not r.storage_key:
            continue
        fname = r.original_filename or f"{r.version}.bsfw"
        out.append(PinnedFirmwareRead(
            model_family=r.model_family, version=r.version, original_filename=fname,
            file_size=r.file_size,
            download_url=get_signed_url(r.storage_key, ttl=3600, download_name=fname),
        ))
    return out


@router.post("/versions", response_model=FirmwareVersionRead, status_code=status.HTTP_201_CREATED)
async def upload_firmware_version(
    current_user: Annotated[CmsUser, Depends(require_server_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile,
    version: Annotated[str, Form()],
    model_family: Annotated[str, Form()],
    channel: Annotated[str, Form()] = "stable",
    release_notes: Annotated[Optional[str], Form()] = None,
) -> CmsPlayerUpdateVersion:
    """Enregistre une version firmware : streame le `.bsfw` vers MinIO (multipart) + crée la ligne
    version (track='firmware'). N'épingle PAS automatiquement (le pin est une décision explicite)."""
    fam = (model_family or "").strip().lower()
    ver = (version or "").strip()
    if not fam or not ver:
        raise HTTPException(status_code=400, detail="version and model_family required")
    fname = file.filename or "firmware.bsfw"
    if not fname.lower().endswith(".bsfw"):
        raise HTTPException(status_code=415, detail="Firmware file expected (.bsfw)")
    if file.size and file.size > _MAX_BSFW_BYTES:
        raise HTTPException(status_code=413, detail="Firmware too large (> 1 GB)")

    storage_key = f"firmware/{fam}/{uuid.uuid4().hex}.bsfw"
    sha256, file_size = await stream_upload(file.file, storage_key, "application/octet-stream")

    row = CmsPlayerUpdateVersion(
        track="firmware", version=ver, channel=(channel or "stable").strip() or "stable",
        model_family=fam, storage_key=storage_key, sha256_checksum=sha256, file_size=file_size,
        original_filename=fname, release_notes=release_notes, is_pinned=False,
    )
    db.add(row)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="firmware_version_upload", resource_type="firmware_version",
        detail={"version": ver, "model_family": fam, "size": file_size},
    ))
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/versions/{id_version}/pin", response_model=FirmwareVersionRead)
async def pin_firmware_version(
    id_version: int,
    current_user: Annotated[CmsUser, Depends(require_server_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerUpdateVersion:
    """Épingle cette version (= la SEULE que l'UI poussera pour sa famille). Dé-épingle les autres
    de la même famille."""
    row = (await db.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.id_version == id_version,
            CmsPlayerUpdateVersion.track == "firmware",
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Firmware version not found")
    await fw.pin_version(db, row)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="firmware_version_pin", resource_type="firmware_version",
        resource_id=str(id_version), detail={"version": row.version, "model_family": row.model_family},
    ))
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/versions/{id_version}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_firmware_version(
    id_version: int,
    current_user: Annotated[CmsUser, Depends(require_server_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Supprime une version firmware + son objet MinIO. Refuse si elle est épinglée (dé-épingler d'abord)."""
    row = (await db.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.id_version == id_version,
            CmsPlayerUpdateVersion.track == "firmware",
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Firmware version not found")
    if row.is_pinned:
        raise HTTPException(status_code=409, detail="Version is pinned — unpin it before deleting")
    if row.storage_key:
        try:
            await delete_file(row.storage_key)
        except Exception:
            pass  # l'objet MinIO peut déjà être absent — on supprime la ligne quoi qu'il arrive
    await db.delete(row)
    await db.commit()


# ── État + push (org-admin) ──────────────────────────────────────────────────────

async def _player_or_404(db: AsyncSession, id_player: int, id_org: int) -> CmsPlayer:
    player = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player, CmsPlayer.id_org == id_org)
    )).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return player


async def _reconcile_firmware_targets(
    db: AsyncSession, id_player: int, current_bos: Optional[str]
) -> None:
    """Réconcilie les cibles de MAJ firmware non terminées de ce player.

    Si le device remonte la version poussée (bs_bos), la cible passe à 'success' — l'autorun
    reboote sans callback de complétion, sinon elle restait 'queued' à vie. Recompute le rollup
    du déploiement. Idempotent, déclenché à la lecture de l'état (0 coût hot-path).
    """
    if not current_bos:
        return
    rows = (await db.execute(
        select(CmsUpdateDeploymentTarget, CmsPlayerUpdateVersion.version)
        .join(
            CmsUpdateDeployment,
            CmsUpdateDeployment.id_deployment == CmsUpdateDeploymentTarget.id_deployment,
        )
        .join(
            CmsPlayerUpdateVersion,
            CmsPlayerUpdateVersion.id_version == CmsUpdateDeployment.id_version,
        )
        .where(
            CmsUpdateDeploymentTarget.id_player == id_player,
            CmsUpdateDeploymentTarget.status.not_in(("success", "failed", "rolled_back")),
            CmsPlayerUpdateVersion.track == "firmware",
        )
    )).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # TIMESTAMP sans fuseau (cf. push)
    affected: set[int] = set()
    for tgt, ver in rows:
        if ver and ver == current_bos:
            tgt.status = "success"
            tgt.completed_at = now
            affected.add(tgt.id_deployment)
    for dep_id in affected:
        dep = (await db.execute(
            select(CmsUpdateDeployment).where(CmsUpdateDeployment.id_deployment == dep_id)
        )).scalar_one_or_none()
        if dep is None:
            continue
        tgts = (await db.execute(
            select(CmsUpdateDeploymentTarget)
            .where(CmsUpdateDeploymentTarget.id_deployment == dep_id)
        )).scalars().all()
        dep.success_count = sum(1 for x in tgts if x.status == "success")
        dep.failed_count = sum(1 for x in tgts if x.status in ("failed", "rolled_back"))
        if all(x.status in ("success", "failed", "rolled_back") for x in tgts):
            dep.status = "completed"
            dep.completed_at = now
    if affected:
        await db.commit()


@router.get("/players/{id_player}/state", response_model=FirmwareUpdateState)
async def firmware_update_state(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FirmwareUpdateState:
    """État de MAJ firmware d'un player : version courante (bs_bos), version épinglée pour sa famille,
    si une MAJ est disponible, et le dernier statut de déploiement."""
    player = await _player_or_404(db, id_player, current_user.id_org)
    family = fw.player_family(player)
    current = fw.player_current_firmware(player)
    # Solde le « queued » éternel si le device est déjà sur la version poussée.
    await _reconcile_firmware_targets(db, id_player, current)
    pinned = await fw.pinned_for_family(db, family)
    pinned_ver = pinned.version if pinned else None
    available = bool(pinned_ver) and pinned_ver != current

    last = (await db.execute(
        select(CmsUpdateDeploymentTarget.status)
        .where(CmsUpdateDeploymentTarget.id_player == id_player)
        .order_by(desc(CmsUpdateDeploymentTarget.id_target))
        .limit(1)
    )).scalar_one_or_none()

    return FirmwareUpdateState(
        current_version=current, model_family=family, pinned_version=pinned_ver,
        update_available=available, last_status=last,
    )


@router.post("/players/{id_player}/update", response_model=FirmwarePushResponse, status_code=status.HTTP_202_ACCEPTED)
async def push_firmware_update(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FirmwarePushResponse:
    """Pousse la MAJ firmware ÉPINGLÉE vers le player (garde-fou : version résolue serveur).

    Crée un déploiement + une cible (§XXX) + publie la commande `firmware_update` avec une URL
    signée du `.bsfw` → le device télécharge à la racine SD et reboote (le BOS applique au boot)."""
    player = await _player_or_404(db, id_player, current_user.id_org)
    family = fw.player_family(player)
    if not family:
        raise HTTPException(status_code=400, detail="Unknown player family (capabilities not reported)")
    pinned = await fw.pinned_for_family(db, family)
    if pinned is None or not pinned.storage_key:
        raise HTTPException(
            status_code=400,
            detail=f"No firmware version pinned for family '{family}'",
        )
    current = fw.player_current_firmware(player)
    if current and current == pinned.version:
        raise HTTPException(status_code=409, detail="The player is already on the pinned version")

    # Staging + publication = définition UNIQUE dans `services.firmware` (partagée avec
    # l'orchestration renderer→firmware de `ota.create_deployment`, décision 2 du canal unique).
    deployment = await fw.stage_push(db, player.id_org, current_user.id_user, pinned, [player])
    await db.commit()
    await fw.publish_push_commands(pinned, [player])

    return FirmwarePushResponse(
        status="sent", id_deployment=deployment.id_deployment,
        version=pinned.version, previous_version=current,
    )
