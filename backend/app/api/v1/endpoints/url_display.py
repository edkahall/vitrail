"""Endpoints URL Display (§XXV.2) — « Site Web » comme contenu schedulable.

Un url_display est un `cms_media` (`media_type='url_display'`, sans fichier MinIO) : l'URL +
la config webview vivent dans `media_metadata`. Il apparaît donc nativement dans la médiathèque
et les pickers de playlist/event, et réutilise toute la machinerie de diffusion (scheduling,
targeting, sync, offline-first). RBAC = module `media`. Cf. docs/SPIKE_url_display.md.

GET    /url-display          — liste des configs url_display
POST   /url-display          — crée une config (média url_display)
GET    /url-display/{id}      — détail
PUT    /url-display/{id}      — met à jour nom / url / config + resync
DELETE /url-display/{id}      — suppression logique + cascade playlists/programmes/events
"""

import json
from datetime import UTC, datetime
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_any_permission
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.user import CmsUser
from app.services.governance_access import user_can_edit_node
from app.schemas.url_display import (
    UrlDisplayConfig,
    UrlDisplayCreate,
    UrlDisplayRead,
    UrlDisplayUpdate,
)
from app.services import media_deletion

router = APIRouter(prefix="/url-display", tags=["url-display"])


def _to_read(media: CmsMedia) -> UrlDisplayRead:
    """Construit la vue url_display depuis un cms_media (URL + config dans metadata)."""
    meta = dict(media.media_metadata or {})
    url = meta.pop("url", "")
    return UrlDisplayRead(
        id_media=media.id_media,
        name=media.media_name,
        url=url,
        config=UrlDisplayConfig(**{k: v for k, v in meta.items() if k in UrlDisplayConfig.model_fields}),
        created_at=media.created_at,
        updated_at=media.updated_at,
    )


def _build_metadata(url: str, config: UrlDisplayConfig) -> dict:
    """metadata persistée d'un url_display : l'URL + la config à plat."""
    return {"url": url, **config.model_dump()}


async def _broadcast_org(db: AsyncSession, id_org: int, message: dict) -> None:
    """Resync graceful des players de l'org (le contenu d'une playlist a pu changer)."""
    player_ids = [r for (r,) in (await db.execute(
        select(CmsPlayer.id_player).where(CmsPlayer.id_org == id_org)
    )).all()]
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    payload = json.dumps(message)
    async with r:
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)


def _enqueue_capture(id_media: int) -> None:
    """Lance une capture Playwright (vignette d'accueil pour tous les modes + image servie en
    mode screenshot_server). Best-effort : si Celery/broker indispo, on n'échoue pas la requête."""
    try:
        from app.workers.screenshot_worker import capture_url_display
        capture_url_display.delay(id_media)
    except Exception:  # noqa: BLE001
        pass


async def _get_url_display_or_404(db: AsyncSession, id_media: int) -> CmsMedia:
    media = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media == id_media,
            CmsMedia.media_type == "url_display",
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="URL display not found")
    return media


async def _get_url_display_editable(db: AsyncSession, user: CmsUser, id_media: int) -> CmsMedia:
    """Charge une page web + applique la garde de noeud (D9).

    Une page web EST une ligne `cms_media`, qui porte donc un `id_governance_node`. Ce
    module etait un chemin d'ecriture PARALLELE a `media.py` : il modifiait et supprimait
    ces lignes sans jamais passer par la garde, alors que renommer le meme contenu depuis
    la mediatheque etait refuse (audit du menu Media, 2026-07-28).
    """
    media = await _get_url_display_or_404(db, id_media)
    if not await user_can_edit_node(db, user, media.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer the governance node of this content",
        )
    return media


@router.get("", response_model=list[UrlDisplayRead])
async def list_url_displays(
    _: Annotated[CmsUser, Depends(require_any_permission(("url_display", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[UrlDisplayRead]:
    rows = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.media_type == "url_display",
            CmsMedia.is_deleted == False,  # noqa: E712
        ).order_by(CmsMedia.created_at.desc())
    )).scalars()
    return [_to_read(m) for m in rows]


@router.post("", response_model=UrlDisplayRead, status_code=status.HTTP_201_CREATED)
async def create_url_display(
    payload: UrlDisplayCreate,
    user: Annotated[CmsUser, Depends(require_any_permission(("url_display", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UrlDisplayRead:
    media = CmsMedia(
        id_org=user.id_org,
        media_name=payload.name,
        media_type="url_display",
        mime_type="text/uri-list",
        storage_key="",          # sentinel : pas de fichier (NOT NULL en base)
        checksum_sha256="",      # idem
        source="url",
        media_metadata=_build_metadata(payload.url, payload.config),
        transcode_status="done",  # pas de transcodage → directement diffusable
        is_encrypted=False,
        conformance_status="ready",
        created_by=user.id_user,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)
    _enqueue_capture(media.id_media)   # vignette + (screenshot_server) image servie
    return _to_read(media)


@router.get("/{id_media}", response_model=UrlDisplayRead)
async def get_url_display(
    id_media: int,
    _: Annotated[CmsUser, Depends(require_any_permission(("url_display", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UrlDisplayRead:
    return _to_read(await _get_url_display_or_404(db, id_media))


@router.put("/{id_media}", response_model=UrlDisplayRead)
async def update_url_display(
    id_media: int,
    payload: UrlDisplayUpdate,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("url_display", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UrlDisplayRead:
    media = await _get_url_display_editable(db, current_user, id_media)
    meta = dict(media.media_metadata or {})
    cur_url = meta.get("url", "")
    cur_config = UrlDisplayConfig(
        **{k: v for k, v in meta.items() if k in UrlDisplayConfig.model_fields}
    )
    new_url = payload.url if payload.url is not None else cur_url
    new_config = payload.config if payload.config is not None else cur_config
    values: dict = {
        "media_metadata": _build_metadata(new_url, new_config),
        # cms_media.updated_at = TIMESTAMP WITHOUT TIME ZONE → datetime naïf.
        "updated_at": datetime.now(UTC).replace(tzinfo=None),
    }
    if payload.name is not None:
        values["media_name"] = payload.name
    await db.execute(update(CmsMedia).where(CmsMedia.id_media == id_media).values(**values))
    await db.commit()
    media = await _get_url_display_or_404(db, id_media)
    _enqueue_capture(id_media)   # URL/config a pu changer → re-capture (vignette + image)
    # Le contenu peut être en diffusion → resync graceful (le player relit l'URL/config).
    await _broadcast_org(db, media.id_org, {"type": "player:force_sync", "preemptive": False})
    return _to_read(media)


@router.get("/{id_media}/deletion-impact")
async def url_display_deletion_impact(
    id_media: int,
    _: Annotated[CmsUser, Depends(require_any_permission(("url_display", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Arbre d'impact de la suppression d'une page web (mêmes règles que les autres
    contenus : playlists/programmes/événements + état de diffusion). Lecture seule."""
    media = await _get_url_display_or_404(db, id_media)
    return media_deletion.public_impact(await media_deletion.analyze(db, media))


@router.delete("/{id_media}")
async def delete_url_display(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("url_display", "delete"), ("media", "delete")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Suppression logique + cascade (retrait playlists, programmes orphelins, events).
    Pas de fichier MinIO à purger (url_display sans storage_key)."""
    media = await _get_url_display_editable(db, current_user, id_media)
    id_org = media.id_org
    impact = await media_deletion.cascade_delete(db, media)
    await db.execute(
        update(CmsMedia).where(CmsMedia.id_media == id_media).values(is_deleted=True)
    )
    await db.commit()
    if impact["summary"]["playlists_total"] or impact["summary"]["events_cancelled"]:
        await _broadcast_org(db, id_org, {"type": "player:force_sync", "preemptive": False})
    return {"deleted": True}
