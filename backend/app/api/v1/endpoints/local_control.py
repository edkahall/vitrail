"""Store Control Panel LAN — profils d'accès local (§19.3) — Sprint 2.5 lot 3.

POST   /players/{id_player}/local-control/profile   — génère un profil + token QR
GET    /players/{id_player}/local-control/profile   — profil actif (ou null)
DELETE /players/{id_player}/local-control/profile   — révoque le profil actif

Le profil (token + permissions) est transporté vers le player dans le payload
player:sync (clé `local_control`, cf. ws.py). Une génération ou révocation publie
player:force_sync pour une prise en compte immédiate.
"""

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_permission
from app.models.local_control import CmsLocalControlProfile
from app.models.player import CmsPlayer
from app.models.user import CmsUser
from app.schemas.local_control import LocalControlProfileCreate, LocalControlProfileRead

router = APIRouter(tags=["local-control"])


async def _get_player_or_404(db: AsyncSession, id_player: int, id_org: int) -> CmsPlayer:
    player = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_player == id_player,
            CmsPlayer.id_org == id_org,
        )
    )).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return player


async def _push_force_sync(id_player: int) -> None:
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        await r.publish(
            f"signflow:player:{id_player}:cmds",
            json.dumps({"type": "player:force_sync", "preemptive": False}),
        )
    finally:
        await r.aclose()


def _build_lan_url(player: CmsPlayer, token: str) -> str | None:
    if not player.ip_address:
        return None
    return f"http://{player.ip_address}:{settings.LOCAL_CONTROL_PORT}/control?token={token}"


def _to_read(profile: CmsLocalControlProfile, player: CmsPlayer) -> LocalControlProfileRead:
    out = LocalControlProfileRead.model_validate(profile)
    out.token = profile.token
    out.lan_url = _build_lan_url(player, profile.token)
    return out


@router.post(
    "/players/{id_player}/local-control/profile",
    response_model=LocalControlProfileRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_local_control_profile(
    id_player: int,
    body: LocalControlProfileCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> LocalControlProfileRead:
    player = await _get_player_or_404(db, id_player, current_user.id_org)

    # Révoque tout profil actif existant (un seul profil non-révoqué par player)
    await db.execute(
        update(CmsLocalControlProfile)
        .where(
            CmsLocalControlProfile.id_player == id_player,
            CmsLocalControlProfile.revoked == False,  # noqa: E712
        )
        .values(revoked=True, updated_at=datetime.now(UTC))
    )

    profile = CmsLocalControlProfile(
        id_org=current_user.id_org,
        id_player=id_player,
        token=secrets.token_urlsafe(24),
        role=body.role,
        allowed_basket_ids=body.allowed_basket_ids,
        can_edit_ratio=body.can_edit_ratio,
        can_set_volume=body.can_set_volume,
        can_trigger_event=body.can_trigger_event,
        # validity_hours == 0 → profil permanent (pas d'expiration)
        expires_at=(
            None if body.validity_hours == 0
            else datetime.now(UTC) + timedelta(hours=body.validity_hours)
        ),
        created_by=current_user.id_user,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    await _push_force_sync(id_player)
    return _to_read(profile, player)


@router.get(
    "/players/{id_player}/local-control/profile",
    response_model=LocalControlProfileRead | None,
)
async def get_local_control_profile(
    id_player: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "read"))],
) -> LocalControlProfileRead | None:
    player = await _get_player_or_404(db, id_player, current_user.id_org)
    profile = (await db.execute(
        select(CmsLocalControlProfile).where(
            CmsLocalControlProfile.id_player == id_player,
            CmsLocalControlProfile.revoked == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if profile is None:
        return None
    return _to_read(profile, player)


@router.delete(
    "/players/{id_player}/local-control/profile",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_local_control_profile(
    id_player: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> None:
    await _get_player_or_404(db, id_player, current_user.id_org)
    await db.execute(
        update(CmsLocalControlProfile)
        .where(
            CmsLocalControlProfile.id_player == id_player,
            CmsLocalControlProfile.revoked == False,  # noqa: E712
        )
        .values(revoked=True, updated_at=datetime.now(UTC))
    )
    await db.commit()
    await _push_force_sync(id_player)
