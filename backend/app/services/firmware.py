"""MAJ firmware BrightSign (P1.1, §XXX).

Garde-fou central anti-brick : l'UI ne pousse JAMAIS une version arbitraire — seulement la version
**épinglée** (`is_pinned`) correspondant à la **famille** du player (`capabilities.bs_family`). Un
firmware non validé peut re-casser la compile de l'autorun (leçon 8.5.70) → le pin est une décision
super-admin, et le push résout la version côté serveur (l'org-admin ne choisit pas).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit_log import CmsAuditLog
from app.models.player import CmsPlayer
from app.models.player_version import (
    CmsPlayerUpdateVersion,
    CmsUpdateDeployment,
    CmsUpdateDeploymentTarget,
)


def player_family(player: CmsPlayer) -> Optional[str]:
    """Famille BrightSign du player (capabilities.bs_family, ex. 'malibu' pour le XT4)."""
    caps = player.capabilities or {}
    fam = caps.get("bs_family")
    return fam.strip().lower() if isinstance(fam, str) and fam.strip() else None


def player_current_firmware(player: CmsPlayer) -> Optional[str]:
    """Version firmware (BOS) courante remontée par le device (capabilities.bs_bos)."""
    caps = player.capabilities or {}
    bos = caps.get("bs_bos")
    return bos.strip() if isinstance(bos, str) and bos.strip() else None


async def pinned_for_family(db: AsyncSession, model_family: Optional[str]) -> Optional[CmsPlayerUpdateVersion]:
    """Version firmware ÉPINGLÉE pour cette famille (None si aucune). Comparaison insensible à la casse."""
    if not model_family:
        return None
    rows = (await db.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.track == "firmware",
            CmsPlayerUpdateVersion.is_pinned.is_(True),
        )
    )).scalars().all()
    fam = model_family.strip().lower()
    for r in rows:
        if (r.model_family or "").strip().lower() == fam:
            return r
    return None


def firmware_at_least(current: Optional[str], minimum: Optional[str]) -> bool:
    """`current` satisfait-il l'exigence `minimum` ? (BOS « 9.1.132 », comparaison numérique.)

    Aucune exigence (minimum vide) → oui. Version courante INCONNUE → **non** : c'est la
    branche fail-safe — on ne sert jamais un renderer à un device dont on ne peut pas
    vérifier le firmware (il garde son renderer actuel, jamais d'écran blanc).
    """
    if not minimum or not str(minimum).strip():
        return True
    if not current or not str(current).strip():
        return False

    def key(v: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", v)]

    return key(str(current)) >= key(str(minimum))


class FirmwareRequired(Exception):
    """Un déploiement renderer BS vise des players sous le firmware minimum et AUCUN firmware
    épinglé n'y répond : le geste est refusé EN LE NOMMANT (l'admin télécharge le `.bsfw` chez
    BrightSign, l'importe par le menu Firmware, l'épingle, puis rejoue le déploiement)."""

    def __init__(self, minimum: str, families: list[str], player_names: list[str]):
        self.minimum = minimum
        self.families = families
        self.player_names = player_names
        fams = ", ".join(sorted(families)) or "unknown"
        names = ", ".join(player_names[:5]) + ("…" if len(player_names) > 5 else "")
        super().__init__(
            f"This renderer version requires BrightSign firmware ≥ {minimum}, but no "
            f"pinned firmware meets it for the family \"{fams}\" ({names}). Download the "
            f".bsfw from the BrightSign website, import it via Settings → Firmware, pin it, "
            f"then rerun this deployment — it will roll out the firmware, then the renderer."
        )


async def stage_push(
    db: AsyncSession, id_org: int, id_user: int,
    pinned: CmsPlayerUpdateVersion, players: list[CmsPlayer],
) -> CmsUpdateDeployment:
    """Écrit (sans commit) un déploiement firmware « immediate » + une cible par player + l'audit.

    Définition UNIQUE du staging de push (endpoint mono-player ET orchestration renderer→firmware
    de `ota.create_deployment`) : deux copies divergeraient sur previous_version ou le rollup.
    La publication Redis est séparée (`publish_push_commands`) — elle ne doit partir qu'APRÈS
    le commit, sinon un device rapide rebooterait sur un déploiement jamais persisté.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # colonnes TIMESTAMP naïves (asyncpg)
    deployment = CmsUpdateDeployment(
        id_org=id_org, id_version=pinned.id_version, approved_by=id_user,
        approved_at=now, strategy="immediate", status="in_progress",
        total_targets=len(players), started_at=now,
    )
    db.add(deployment)
    await db.flush()
    for p in players:
        db.add(CmsUpdateDeploymentTarget(
            id_deployment=deployment.id_deployment, id_player=p.id_player,
            status="queued", previous_version=player_current_firmware(p),
        ))
        db.add(CmsAuditLog(
            id_org=id_org, id_user=id_user,
            action="firmware_push", resource_type="player", resource_id=str(p.id_player),
            detail={"version": pinned.version, "from": player_current_firmware(p),
                    "model_family": pinned.model_family},
        ))
    return deployment


async def publish_push_commands(pinned: CmsPlayerUpdateVersion, players: list[CmsPlayer]) -> None:
    """Commande `firmware_update` à chaque player (URL signée du `.bsfw`, TTL 1 h : le
    téléchargement device peut être long). À appeler APRÈS commit du staging."""
    from app.services.media import get_signed_url

    url = get_signed_url(pinned.storage_key, ttl=3600, download_name=f"{pinned.version}.bsfw")
    redis = aioredis.from_url(settings.REDIS_URL)
    try:
        for p in players:
            await redis.publish(
                f"signflow:player:{p.id_player}:cmds",
                json.dumps({
                    "type": "player:command",
                    "command": "firmware_update",
                    "params": {"url": url, "version": pinned.version,
                               "sha256": pinned.sha256_checksum},
                }),
            )
    finally:
        await redis.aclose()


async def pin_version(db: AsyncSession, version: CmsPlayerUpdateVersion) -> None:
    """Épingle `version` et DÉ-ÉPINGLE toute autre version firmware de la même famille (un seul pin
    actif par famille = la garde-fou ne propose qu'UNE cible déterministe)."""
    await db.execute(
        update(CmsPlayerUpdateVersion)
        .where(
            CmsPlayerUpdateVersion.track == "firmware",
            CmsPlayerUpdateVersion.model_family == version.model_family,
            CmsPlayerUpdateVersion.id_version != version.id_version,
        )
        .values(is_pinned=False)
    )
    version.is_pinned = True
