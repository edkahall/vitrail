"""Endpoints Feed Display (§XXV.3) — flux RSS/Atom comme contenu schedulable.

Un feed = une source pollable (`cms_datafeeds`, porte display_mode/config) + un média pointeur
(`cms_media` `feed_display`, `metadata.id_feed`) qui le rend schedulable nativement (médiathèque,
pickers, scheduling, sync, offline-first). RBAC = module `media` (comme URL Display).

GET    /feeds            — liste
POST   /feeds            — crée (source + média feed_display) + fetch initial
GET    /feeds/{id}        — détail
PUT    /feeds/{id}        — met à jour (source + média) + resync
DELETE /feeds/{id}        — suppression logique + cascade (média + playlists/programmes/events)
GET    /feeds/{id}/items  — items agrégés (aperçu / modération)
POST   /feeds/{id}/fetch  — déclenche une récupération immédiate
"""

import json
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_any_permission
from app.models.audit_log import CmsAuditLog
from app.models.feed import CmsDatafeed, CmsFeedItem
from app.models.media import CmsMedia
from app.models.user import CmsUser
from app.schemas.feed import (
    IMAGELESS_MODES,
    PROXY_MODE_COMPAT,
    VIDEO_MODES,
    FeedCreate,
    FeedDisplayConfig,
    FeedItemRead,
    FeedModerateRequest,
    FeedRead,
    FeedUpdate,
    mode_allowed,
    mode_error,
)
from app.services import media_deletion

router = APIRouter(prefix="/feeds", tags=["feeds"])


def _enqueue_fetch(id_feed: int) -> None:
    """Récupération initiale/forcée du flux (best-effort : pas d'échec si broker indispo)."""
    try:
        from app.workers.feed_worker import fetch_feed
        fetch_feed.delay(id_feed)
    except Exception:  # noqa: BLE001
        pass


def _enqueue_summarize(id_feed: int) -> None:
    """Résumé IA des items déjà stockés (bascule ai_summary_enabled→on). Best-effort."""
    try:
        from app.workers.feed_worker import summarize_feed_items
        summarize_feed_items.delay(id_feed)
    except Exception:  # noqa: BLE001
        pass


async def _media_for_feed(db: AsyncSession, id_feed: int) -> CmsMedia | None:
    """Média feed_display pointant sur ce feed (1:1 pour la tranche 1)."""
    rows = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.media_type == "feed_display",
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalars()
    for m in rows:
        if (m.media_metadata or {}).get("id_feed") == id_feed:
            return m
    return None


async def _to_read(db: AsyncSession, feed: CmsDatafeed) -> FeedRead:
    media = await _media_for_feed(db, feed.id_feed)
    count = (await db.execute(
        select(func.count()).select_from(CmsFeedItem).where(CmsFeedItem.id_feed == feed.id_feed)
    )).scalar() or 0
    return FeedRead(
        id_feed=feed.id_feed,
        id_media=media.id_media if media else None,
        feed_name=feed.feed_name,
        feed_type=feed.feed_type,
        url=(feed.config or {}).get("url"),
        config=feed.config or {},
        polling_interval_s=feed.polling_interval_s,
        display_mode=feed.display_mode,
        display_config=FeedDisplayConfig(**(feed.display_config or {})),
        max_items=feed.max_items,
        moderation_enabled=feed.moderation_enabled,
        ai_summary_enabled=feed.ai_summary_enabled,
        status=feed.status,
        last_fetched_at=feed.last_fetched_at,
        last_error=feed.last_error,
        item_count=count,
        created_at=feed.created_at,
        updated_at=feed.updated_at,
    )


async def _get_feed_or_404(db: AsyncSession, id_feed: int, id_org: int) -> CmsDatafeed:
    feed = (await db.execute(
        select(CmsDatafeed).where(
            CmsDatafeed.id_feed == id_feed,
            CmsDatafeed.id_org == id_org,
            CmsDatafeed.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")
    return feed


async def _broadcast_org(db: AsyncSession, id_org: int) -> None:
    """Resync graceful des players de l'org (le feed affiché a pu changer de config)."""
    from app.models.player import CmsPlayer
    ids = [r for (r,) in (await db.execute(
        select(CmsPlayer.id_player).where(CmsPlayer.id_org == id_org)
    )).all()]
    if not ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    payload = json.dumps({"type": "player:force_sync", "preemptive": False})
    async with r:
        for pid in ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)


@router.get("", response_model=list[FeedRead])
async def list_feeds(
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FeedRead]:
    feeds = (await db.execute(
        select(CmsDatafeed).where(
            CmsDatafeed.id_org == user.id_org,
            CmsDatafeed.is_deleted == False,  # noqa: E712
        ).order_by(CmsDatafeed.created_at.desc())
    )).scalars().all()
    return [await _to_read(db, f) for f in feeds]


@router.post("", response_model=FeedRead, status_code=status.HTTP_201_CREATED)
async def create_feed(
    payload: FeedCreate,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FeedRead:
    feed = CmsDatafeed(
        id_org=user.id_org,
        feed_name=payload.feed_name,
        feed_type=payload.feed_type,
        config=payload.config.model_dump(),
        polling_interval_s=payload.polling_interval_s,
        display_mode=payload.display_mode,
        display_config=payload.display_config.model_dump(),
        max_items=payload.max_items,
        moderation_enabled=payload.moderation_enabled,
        ai_summary_enabled=payload.ai_summary_enabled,
        status="active",
        created_by=user.id_user,
    )
    db.add(feed)
    await db.flush()

    # Média feed_display pointeur (schedulable nativement, comme url_display).
    media = CmsMedia(
        id_org=user.id_org,
        media_name=payload.feed_name,
        media_type="feed_display",
        mime_type="application/rss+xml",
        storage_key="",
        checksum_sha256="",
        source="feed",
        media_metadata={"id_feed": feed.id_feed},
        transcode_status="done",
        is_encrypted=False,
        conformance_status="ready",
        created_by=user.id_user,
    )
    db.add(media)
    await db.commit()
    await db.refresh(feed)

    _enqueue_fetch(feed.id_feed)   # première récupération
    return await _to_read(db, feed)


@router.get("/{id_feed}", response_model=FeedRead)
async def get_feed(
    id_feed: int,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FeedRead:
    feed = await _get_feed_or_404(db, id_feed, user.id_org)
    return await _to_read(db, feed)


@router.put("/{id_feed}", response_model=FeedRead)
async def update_feed(
    id_feed: int,
    payload: FeedUpdate,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FeedRead:
    feed = await _get_feed_or_404(db, id_feed, user.id_org)
    fields = payload.model_dump(exclude_unset=True)
    refetch = False
    summarize_now = False
    if "feed_name" in fields:
        feed.feed_name = fields["feed_name"]
    if "feed_type" in fields:
        feed.feed_type = fields["feed_type"]
        refetch = True
    if "config" in fields and fields["config"] is not None:
        feed.config = payload.config.model_dump()
        refetch = True
    if "polling_interval_s" in fields:
        feed.polling_interval_s = fields["polling_interval_s"]
    if "display_mode" in fields:
        feed.display_mode = fields["display_mode"]
    if "display_config" in fields and fields["display_config"] is not None:
        feed.display_config = payload.display_config.model_dump()
    if "max_items" in fields:
        feed.max_items = fields["max_items"]
    if "moderation_enabled" in fields:
        feed.moderation_enabled = fields["moderation_enabled"]
    if "ai_summary_enabled" in fields and fields["ai_summary_enabled"] is not None:
        # Bascule false→true : on résume les items déjà stockés (sinon ils restent sans résumé
        # jusqu'au prochain poll). L'inverse n'efface pas les résumés existants (bénins).
        if fields["ai_summary_enabled"] and not feed.ai_summary_enabled:
            summarize_now = True
        feed.ai_summary_enabled = fields["ai_summary_enabled"]
    if "status" in fields and fields["status"] is not None:
        feed.status = fields["status"]

    # Google News ne fournit jamais d'image → seuls les modes sans image sont valides.
    # Choix explicite d'un mode image = erreur ; sinon (changement de type / ligne legacy) =
    # auto-correction silencieuse vers le ticker.
    if feed.feed_type == "google_news" and feed.display_mode not in IMAGELESS_MODES:
        if "display_mode" in fields and fields["display_mode"] is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The Google News type provides no images: choose a mode without "
                       "images (scrolling ticker or compact list).",
            )
        feed.display_mode = "ticker"

    # video_feed = chaîne YouTube uniquement. Choix explicite incohérent = 422 ; sinon (changement
    # de type away from youtube) auto-correction vers le carrousel de cartes.
    if feed.display_mode in VIDEO_MODES and feed.feed_type != "youtube_channel":
        if "display_mode" in fields and fields["display_mode"] is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Video mode (video_feed) is only available for a YouTube channel.",
            )
        feed.display_mode = "card_carousel"

    # Modes proxy (social_wall / data_chart / matrice §25.3). Choix explicite incompatible = 422 ;
    # sinon (changement de type rendant le mode invalide) auto-correction vers le 1er mode permis.
    if not mode_allowed(feed.feed_type, feed.display_mode or "ticker"):
        if "display_mode" in fields and fields["display_mode"] is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=mode_error(feed.feed_type, feed.display_mode or ""),
            )
        feed.display_mode = (PROXY_MODE_COMPAT.get(feed.feed_type) or ("ticker",))[0]

    # Maintenir le nom du média en miroir.
    if "feed_name" in fields:
        media = await _media_for_feed(db, feed.id_feed)
        if media:
            media.media_name = fields["feed_name"]

    await db.commit()
    await db.refresh(feed)

    if refetch:
        _enqueue_fetch(feed.id_feed)
    elif summarize_now:
        _enqueue_summarize(feed.id_feed)   # refetch résume déjà via fetch_feed → éviter le doublon
    await _broadcast_org(db, user.id_org)
    return await _to_read(db, feed)


@router.get("/{id_feed}/deletion-impact")
async def feed_deletion_impact(
    id_feed: int,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Arbre d'impact de la suppression d'un feed (mêmes règles que les autres contenus :
    playlists/programmes/événements/paniers + état de diffusion). Lecture seule."""
    feed = await _get_feed_or_404(db, id_feed, user.id_org)
    media = await _media_for_feed(db, feed.id_feed)
    if media is None:
        return media_deletion.empty_impact(feed.feed_name)
    return media_deletion.public_impact(await media_deletion.analyze(db, media))


@router.delete("/{id_feed}")
async def delete_feed(
    id_feed: int,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "delete"), ("media", "delete")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Supprime un feed avec les mêmes règles que les autres contenus : cascade (retrait
    des playlists, suppression des playlists vides, programmes orphelins, annulation des
    events) PUIS soft-delete du média pointeur ET du feed → plus d'orphelin (le feed
    disparaît de la médiathèque/pickers et n'est plus pollé). Renvoie l'impact appliqué."""
    feed = await _get_feed_or_404(db, id_feed, user.id_org)
    media = await _media_for_feed(db, feed.id_feed)
    impact = media_deletion.empty_impact(feed.feed_name)
    if media:
        impact = await media_deletion.cascade_delete(db, media)
        media.is_deleted = True   # soft-delete du pointeur (sinon il reste visible)
    feed.is_deleted = True
    await db.commit()
    await _broadcast_org(db, user.id_org)
    return media_deletion.public_impact(impact)


@router.get("/{id_feed}/items", response_model=list[FeedItemRead])
async def list_feed_items(
    id_feed: int,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
    status_filter: str | None = None,   # pending|approved|rejected (file de modération §XXI)
) -> list[FeedItemRead]:
    await _get_feed_or_404(db, id_feed, user.id_org)
    query = select(CmsFeedItem).where(CmsFeedItem.id_feed == id_feed)
    if status_filter in ("pending", "approved", "rejected"):
        query = query.where(CmsFeedItem.moderation_status == status_filter)
    items = (await db.execute(
        query.order_by(CmsFeedItem.published_at.desc().nullslast(), CmsFeedItem.fetched_at.desc())
        .limit(min(limit, 200))
    )).scalars().all()
    return [
        FeedItemRead(
            id_item=i.id_item, title=i.title, description=i.description, summary_ai=i.summary_ai,
            image_url=i.image_url, link_url=i.link_url, author=i.author,
            value_num=i.value_num, value_unit=i.value_unit, change_pct=i.change_pct,
            published_at=i.published_at, moderation_status=i.moderation_status,
        )
        for i in items
    ]


@router.post("/{id_feed}/moderate")
async def moderate_feed_items(
    id_feed: int,
    payload: FeedModerateRequest,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Approuve / rejette un lot d'items (§XXI). Seuls les `approved` sont diffusés (sync) →
    un resync graceful propage l'effet au parc. Action journalisée (cms_audit_log)."""
    await _get_feed_or_404(db, id_feed, user.id_org)
    new_status = "approved" if payload.action == "approve" else "rejected"
    items = (await db.execute(
        select(CmsFeedItem).where(
            CmsFeedItem.id_feed == id_feed,
            CmsFeedItem.id_item.in_(payload.item_ids),
        )
    )).scalars().all()
    for it in items:
        it.moderation_status = new_status
    db.add(CmsAuditLog(
        id_org=user.id_org,
        id_user=user.id_user,
        action=f"feed_moderate_{payload.action}",
        resource_type="feed",
        resource_id=str(id_feed),
        detail={"item_ids": [it.id_item for it in items], "count": len(items)},
        source="cms",
    ))
    await db.commit()
    await _broadcast_org(db, user.id_org)
    return {"updated": len(items), "status": new_status}


@router.post("/{id_feed}/fetch", status_code=status.HTTP_202_ACCEPTED)
async def trigger_fetch(
    id_feed: int,
    user: Annotated[CmsUser, Depends(require_any_permission(("feeds", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    await _get_feed_or_404(db, id_feed, user.id_org)
    _enqueue_fetch(id_feed)
    return {"status": "queued", "id_feed": id_feed}
