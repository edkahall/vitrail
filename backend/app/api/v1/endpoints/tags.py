"""
Tags & filtrage de contenu §XVII — taxonomie, tagging, filtres player, simulation.

Taxonomie    : /tags/namespaces[/values]
Tagging      : /media/{id}/tags, /media/bulk-tag, /playlist-items/{id}/tags
Filtres      : /players/{id}/tag-filters, /players/{id}/fallback-config
Simulation   : /playlists/{id}/simulate-tags
"""

import json
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_any_permission, require_permission
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.tag import (
    CmsMediaTagValue,
    CmsPlayerTagFilter,
    CmsPlaylistItemNsOverride,
    CmsPlaylistItemTag,
    CmsTagNamespace,
    CmsTagValue,
)
from app.models.user import CmsUser
from app.services.governance_access import user_can_edit_node
from app.schemas.tag import (
    BulkTagRequest,
    FallbackConfig,
    FallbackConfigUpdate,
    IdTagList,
    NamespaceCreate,
    NamespaceRead,
    NamespaceUpdate,
    NamespaceWithValues,
    PlaylistItemTagsRead,
    PlaylistItemTagsUpdate,
    SimulateItem,
    SimulatePlayerResult,
    SimulateRequest,
    TagValueCreate,
    TagValueRead,
    TagValueUpdate,
)
from app.services.tags import compute_effective_tags, item_matches_player

router = APIRouter(tags=["tags"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _ns_or_404(db: AsyncSession, id_org: int, id_namespace: int) -> CmsTagNamespace:
    ns = (await db.execute(
        select(CmsTagNamespace).where(
            CmsTagNamespace.id_namespace == id_namespace, CmsTagNamespace.id_org == id_org
        )
    )).scalar_one_or_none()
    if ns is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Namespace not found")
    return ns


async def _value_or_404(db: AsyncSession, id_org: int, id_tag: int) -> CmsTagValue:
    val = (await db.execute(
        select(CmsTagValue)
        .join(CmsTagNamespace, CmsTagNamespace.id_namespace == CmsTagValue.id_namespace)
        .where(CmsTagValue.id_tag == id_tag, CmsTagNamespace.id_org == id_org)
    )).scalar_one_or_none()
    if val is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return val


async def _validate_tag_ids(db: AsyncSession, id_org: int, id_tags: list[int]) -> None:
    """Vérifie que tous les id_tag appartiennent à l'org (cross-tenant = 404)."""
    if not id_tags:
        return
    rows = (await db.execute(
        select(CmsTagValue.id_tag)
        .join(CmsTagNamespace, CmsTagNamespace.id_namespace == CmsTagValue.id_namespace)
        .where(CmsTagValue.id_tag.in_(id_tags), CmsTagNamespace.id_org == id_org)
    )).scalars().all()
    if set(rows) != set(id_tags):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag(s) not found")


async def _media_tags_dict(db: AsyncSession, id_media: int) -> dict[str, list[str]]:
    rows = (await db.execute(
        select(CmsTagNamespace.namespace_key, CmsTagValue.tag_value)
        .join(CmsTagValue, CmsTagValue.id_namespace == CmsTagNamespace.id_namespace)
        .join(CmsMediaTagValue, CmsMediaTagValue.id_tag == CmsTagValue.id_tag)
        .where(CmsMediaTagValue.id_media == id_media)
    )).all()
    out: dict[str, list[str]] = {}
    for ns_key, val in rows:
        out.setdefault(ns_key, []).append(val)
    return out


async def _player_filters_dict(db: AsyncSession, id_player: int) -> dict[str, list[str]]:
    rows = (await db.execute(
        select(CmsTagNamespace.namespace_key, CmsTagValue.tag_value)
        .join(CmsTagValue, CmsTagValue.id_namespace == CmsTagNamespace.id_namespace)
        .join(CmsPlayerTagFilter, CmsPlayerTagFilter.id_tag == CmsTagValue.id_tag)
        .where(CmsPlayerTagFilter.id_player == id_player)
    )).all()
    out: dict[str, list[str]] = {}
    for ns_key, val in rows:
        out.setdefault(ns_key, []).append(val)
    return out


# ── Taxonomie : namespaces ────────────────────────────────────────────────────

@router.get("/tags/namespaces", response_model=list[NamespaceWithValues])
async def list_namespaces(
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[NamespaceWithValues]:
    namespaces = (await db.execute(
        select(CmsTagNamespace)
        .where(CmsTagNamespace.id_org == current_user.id_org)
        .order_by(CmsTagNamespace.sort_order, CmsTagNamespace.namespace_key)
    )).scalars().all()
    values = (await db.execute(
        select(CmsTagValue)
        .join(CmsTagNamespace, CmsTagNamespace.id_namespace == CmsTagValue.id_namespace)
        .where(CmsTagNamespace.id_org == current_user.id_org)
        .order_by(CmsTagValue.sort_order, CmsTagValue.tag_value)
    )).scalars().all()
    by_ns: dict[int, list[TagValueRead]] = {}
    for v in values:
        by_ns.setdefault(v.id_namespace, []).append(TagValueRead.model_validate(v))
    result = []
    for ns in namespaces:
        nv = NamespaceWithValues.model_validate(ns)
        nv.values = by_ns.get(ns.id_namespace, [])
        result.append(nv)
    return result


@router.post("/tags/namespaces", response_model=NamespaceRead, status_code=status.HTTP_201_CREATED)
async def create_namespace(
    body: NamespaceCreate,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NamespaceRead:
    if body.match_mode not in ("any", "all"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="match_mode = any | all")
    dup = (await db.execute(
        select(CmsTagNamespace).where(
            CmsTagNamespace.id_org == current_user.id_org,
            CmsTagNamespace.namespace_key == body.namespace_key,
        )
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="namespace_key already in use")
    ns = CmsTagNamespace(
        id_org=current_user.id_org, namespace_key=body.namespace_key, label=body.label,
        match_mode=body.match_mode, applies_to=body.applies_to, is_system=False,
    )
    db.add(ns)
    await db.commit()
    await db.refresh(ns)
    return NamespaceRead.model_validate(ns)


@router.put("/tags/namespaces/{id_namespace}", response_model=NamespaceRead)
async def update_namespace(
    id_namespace: int,
    body: NamespaceUpdate,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NamespaceRead:
    ns = await _ns_or_404(db, current_user.id_org, id_namespace)
    if body.match_mode is not None and body.match_mode not in ("any", "all"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="match_mode = any | all")
    for field in ("label", "match_mode", "applies_to", "is_active", "sort_order"):
        val = getattr(body, field)
        if val is not None:
            setattr(ns, field, val)
    await db.commit()
    await db.refresh(ns)
    return NamespaceRead.model_validate(ns)


@router.delete("/tags/namespaces/{id_namespace}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_namespace(
    id_namespace: int,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ns = await _ns_or_404(db, current_user.id_org, id_namespace)
    if ns.is_system:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System namespace cannot be deleted")
    used = (await db.execute(
        select(CmsMediaTagValue.id_tag)
        .join(CmsTagValue, CmsTagValue.id_tag == CmsMediaTagValue.id_tag)
        .where(CmsTagValue.id_namespace == id_namespace).limit(1)
    )).scalar_one_or_none()
    if used is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Namespace is used by media")
    await db.delete(ns)
    await db.commit()


# ── Taxonomie : valeurs ───────────────────────────────────────────────────────

@router.get("/tags/namespaces/{id_namespace}/values", response_model=list[TagValueRead])
async def list_values(
    id_namespace: int,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TagValueRead]:
    await _ns_or_404(db, current_user.id_org, id_namespace)
    vals = (await db.execute(
        select(CmsTagValue).where(CmsTagValue.id_namespace == id_namespace)
        .order_by(CmsTagValue.sort_order, CmsTagValue.tag_value)
    )).scalars().all()
    return [TagValueRead.model_validate(v) for v in vals]


@router.post("/tags/namespaces/{id_namespace}/values", response_model=TagValueRead, status_code=status.HTTP_201_CREATED)
async def create_value(
    id_namespace: int,
    body: TagValueCreate,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TagValueRead:
    await _ns_or_404(db, current_user.id_org, id_namespace)
    dup = (await db.execute(
        select(CmsTagValue).where(
            CmsTagValue.id_namespace == id_namespace, CmsTagValue.tag_value == body.tag_value
        )
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Value already exists")
    v = CmsTagValue(
        id_namespace=id_namespace, tag_value=body.tag_value, label=body.label,
        color=body.color, sort_order=body.sort_order,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return TagValueRead.model_validate(v)


@router.put("/tags/values/{id_tag}", response_model=TagValueRead)
async def update_value(
    id_tag: int,
    body: TagValueUpdate,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TagValueRead:
    v = await _value_or_404(db, current_user.id_org, id_tag)
    for field in ("label", "color", "sort_order"):
        val = getattr(body, field)
        if val is not None:
            setattr(v, field, val)
    await db.commit()
    await db.refresh(v)
    return TagValueRead.model_validate(v)


@router.delete("/tags/values/{id_tag}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_value(
    id_tag: int,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("tags", "edit"), ("media", "edit")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    v = await _value_or_404(db, current_user.id_org, id_tag)
    used = (await db.execute(
        select(CmsMediaTagValue.id_media).where(CmsMediaTagValue.id_tag == id_tag).limit(1)
    )).scalar_one_or_none()
    if used is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag is used by media")
    await db.delete(v)
    await db.commit()


# ── Tagging des médias ────────────────────────────────────────────────────────

async def _media_or_404(db: AsyncSession, id_org: int, id_media: int) -> CmsMedia:
    m = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media == id_media, CmsMedia.id_org == id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    return m


@router.get("/media/{id_media}/tags", response_model=list[int])
async def get_media_tags(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[int]:
    await _media_or_404(db, current_user.id_org, id_media)
    return list((await db.execute(
        select(CmsMediaTagValue.id_tag).where(CmsMediaTagValue.id_media == id_media)
    )).scalars().all())


async def _assert_media_editable(db: AsyncSession, user: CmsUser, media_ids: list[int]) -> None:
    """Garde de nœud (D9) sur l'APPLICATION d'un tag à un média.

    Le vocabulaire des tags est un objet d'ORGANISATION — le gérer relève désormais de
    `tags:*`. Mais poser un tag SUR un média modifie ce média : un responsable de magasin
    ne doit pas pouvoir taguer un contenu national, exactement comme il ne peut ni le
    renommer ni le reclasser (décision d'Ed, audit du menu Média 2026-07-28 — c'était la
    dernière route de la famille à ne pas porter la garde).

    Refus de TOUT le lot en nommant le média fautif : un tagging partiellement appliqué
    ferait croire que tout est taggé.
    """
    if not media_ids:
        return
    rows = list((await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media.in_(media_ids),
            CmsMedia.id_org == user.id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalars().all())
    for media in rows:
        if not await user_can_edit_node(db, user, media.id_governance_node):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"'{media.media_name}' belongs to a governance node you do not "
                    "administer — no tag was applied"
                ),
            )


@router.put("/media/{id_media}/tags", response_model=list[int])
async def set_media_tags(
    id_media: int,
    body: IdTagList,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[int]:
    await _media_or_404(db, current_user.id_org, id_media)
    await _assert_media_editable(db, current_user, [id_media])
    await _validate_tag_ids(db, current_user.id_org, body.id_tags)
    await db.execute(delete(CmsMediaTagValue).where(CmsMediaTagValue.id_media == id_media))
    for id_tag in set(body.id_tags):
        db.add(CmsMediaTagValue(id_media=id_media, id_tag=id_tag))
    await db.commit()
    return list(set(body.id_tags))


@router.post("/media/bulk-tag", status_code=status.HTTP_204_NO_CONTENT)
async def bulk_tag_media(
    body: BulkTagRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if body.mode not in ("add", "replace"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="mode = add | replace")
    await _assert_media_editable(db, current_user, body.id_media)
    await _validate_tag_ids(db, current_user.id_org, body.id_tags)
    # Org-scoping des médias
    valid_media = set((await db.execute(
        select(CmsMedia.id_media).where(
            CmsMedia.id_media.in_(body.id_media), CmsMedia.id_org == current_user.id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalars().all())
    for id_media in valid_media:
        if body.mode == "replace":
            await db.execute(delete(CmsMediaTagValue).where(CmsMediaTagValue.id_media == id_media))
        existing = set((await db.execute(
            select(CmsMediaTagValue.id_tag).where(CmsMediaTagValue.id_media == id_media)
        )).scalars().all())
        for id_tag in set(body.id_tags) - existing:
            db.add(CmsMediaTagValue(id_media=id_media, id_tag=id_tag))
    await db.commit()


# ── Filtres + fallback player ─────────────────────────────────────────────────

async def _player_or_404(db: AsyncSession, id_org: int, id_player: int) -> CmsPlayer:
    p = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player, CmsPlayer.id_org == id_org)
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return p


@router.get("/players/{id_player}/tag-filters", response_model=list[int])
async def get_player_tag_filters(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[int]:
    await _player_or_404(db, current_user.id_org, id_player)
    return list((await db.execute(
        select(CmsPlayerTagFilter.id_tag).where(CmsPlayerTagFilter.id_player == id_player)
    )).scalars().all())


@router.put("/players/{id_player}/tag-filters", response_model=list[int])
async def set_player_tag_filters(
    id_player: int,
    body: IdTagList,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[int]:
    await _player_or_404(db, current_user.id_org, id_player)
    await _validate_tag_ids(db, current_user.id_org, body.id_tags)
    await db.execute(delete(CmsPlayerTagFilter).where(CmsPlayerTagFilter.id_player == id_player))
    # id_namespace requis par le modèle → résolu depuis le tag
    ns_by_tag = dict((await db.execute(
        select(CmsTagValue.id_tag, CmsTagValue.id_namespace).where(CmsTagValue.id_tag.in_(body.id_tags or [0]))
    )).all())
    for id_tag in set(body.id_tags):
        db.add(CmsPlayerTagFilter(id_player=id_player, id_namespace=ns_by_tag[id_tag], id_tag=id_tag))
    await db.commit()
    # force resync : le filtrage change ce que le player doit recevoir
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        try:
            await r.publish(
                f"signflow:player:{id_player}:cmds",
                json.dumps({"type": "player:force_sync", "preemptive": False}),
            )
        finally:
            await r.aclose()
    except Exception:
        pass
    return list(set(body.id_tags))


@router.get("/players/{id_player}/fallback-config", response_model=FallbackConfig)
async def get_fallback_config(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FallbackConfig:
    p = await _player_or_404(db, current_user.id_org, id_player)
    return FallbackConfig.model_validate(p)


@router.put("/players/{id_player}/fallback-config", response_model=FallbackConfig)
async def set_fallback_config(
    id_player: int,
    body: FallbackConfigUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FallbackConfig:
    p = await _player_or_404(db, current_user.id_org, id_player)
    if "fallback_audio_id" in body.model_fields_set:
        p.fallback_audio_id = body.fallback_audio_id
    if body.fallback_video_message is not None:
        p.fallback_video_message = body.fallback_video_message
    if body.fallback_video_config is not None:
        p.fallback_video_config = body.fallback_video_config
    await db.commit()
    await db.refresh(p)
    return FallbackConfig.model_validate(p)


# ── Tags des items de playlist ────────────────────────────────────────────────

async def _item_or_404(db: AsyncSession, id_org: int, id_item: int) -> CmsPlaylistItem:
    item = (await db.execute(
        select(CmsPlaylistItem)
        .join(CmsPlaylist, CmsPlaylist.id_playlist == CmsPlaylistItem.id_playlist)
        .where(CmsPlaylistItem.id_item == id_item, CmsPlaylist.id_org == id_org)
    )).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


async def _item_tags_dict(db: AsyncSession, id_item: int) -> dict[str, list[str]]:
    rows = (await db.execute(
        select(CmsTagNamespace.namespace_key, CmsTagValue.tag_value)
        .join(CmsTagValue, CmsTagValue.id_namespace == CmsTagNamespace.id_namespace)
        .join(CmsPlaylistItemTag, CmsPlaylistItemTag.id_tag == CmsTagValue.id_tag)
        .where(CmsPlaylistItemTag.id_item == id_item)
    )).all()
    out: dict[str, list[str]] = {}
    for ns_key, val in rows:
        out.setdefault(ns_key, []).append(val)
    return out


async def _item_override_keys(db: AsyncSession, id_item: int) -> set[str]:
    rows = (await db.execute(
        select(CmsTagNamespace.namespace_key)
        .join(CmsPlaylistItemNsOverride, CmsPlaylistItemNsOverride.id_namespace == CmsTagNamespace.id_namespace)
        .where(CmsPlaylistItemNsOverride.id_item == id_item)
    )).scalars().all()
    return set(rows)


@router.get("/playlist-items/{id_item}/tags", response_model=PlaylistItemTagsRead)
async def get_item_tags(
    id_item: int,
    current_user: Annotated[CmsUser, Depends(require_permission("playlists", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlaylistItemTagsRead:
    item = await _item_or_404(db, current_user.id_org, id_item)
    tags = list((await db.execute(
        select(CmsPlaylistItemTag.id_tag).where(CmsPlaylistItemTag.id_item == id_item)
    )).scalars().all())
    ns_overrides = list((await db.execute(
        select(CmsPlaylistItemNsOverride.id_namespace).where(CmsPlaylistItemNsOverride.id_item == id_item)
    )).scalars().all())
    media_tags = await _media_tags_dict(db, item.id_media)
    item_tags = await _item_tags_dict(db, id_item)
    override_keys = await _item_override_keys(db, id_item)
    effective = compute_effective_tags(media_tags, item_tags, override_keys)
    return PlaylistItemTagsRead(tags=tags, ns_overrides=ns_overrides, effective=effective)


@router.put("/playlist-items/{id_item}/tags", response_model=PlaylistItemTagsRead)
async def set_item_tags(
    id_item: int,
    body: PlaylistItemTagsUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("playlists", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlaylistItemTagsRead:
    await _item_or_404(db, current_user.id_org, id_item)
    await _validate_tag_ids(db, current_user.id_org, body.tags)
    # Valider que les namespaces override appartiennent à l'org
    if body.ns_overrides:
        valid_ns = set((await db.execute(
            select(CmsTagNamespace.id_namespace).where(
                CmsTagNamespace.id_namespace.in_(body.ns_overrides),
                CmsTagNamespace.id_org == current_user.id_org,
            )
        )).scalars().all())
        if valid_ns != set(body.ns_overrides):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Namespace(s) not found")
    await db.execute(delete(CmsPlaylistItemTag).where(CmsPlaylistItemTag.id_item == id_item))
    await db.execute(delete(CmsPlaylistItemNsOverride).where(CmsPlaylistItemNsOverride.id_item == id_item))
    for id_tag in set(body.tags):
        db.add(CmsPlaylistItemTag(id_item=id_item, id_tag=id_tag))
    for id_ns in set(body.ns_overrides):
        db.add(CmsPlaylistItemNsOverride(id_item=id_item, id_namespace=id_ns))
    await db.commit()
    return await get_item_tags(id_item, current_user, db)


# ── Simulation (§XVII.7) ────────────────────────────────────────────────────────

@router.post("/playlists/{id_playlist}/simulate-tags", response_model=list[SimulatePlayerResult])
async def simulate_tags(
    id_playlist: int,
    body: SimulateRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("playlists", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SimulatePlayerResult]:
    """Simule le filtrage par tags d'une playlist pour une liste de players."""
    pl = (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist, CmsPlaylist.id_org == current_user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    rows = (await db.execute(
        select(CmsPlaylistItem, CmsMedia.media_name)
        .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media, isouter=True)
        .where(CmsPlaylistItem.id_playlist == id_playlist)
        .order_by(CmsPlaylistItem.sort_order)
    )).all()

    # Pré-calcul des tags effectifs par item
    item_effective: list[tuple[CmsPlaylistItem, str | None, dict[str, list[str]]]] = []
    for item, media_name in rows:
        media_tags = await _media_tags_dict(db, item.id_media)
        item_tags = await _item_tags_dict(db, item.id_item)
        override_keys = await _item_override_keys(db, item.id_item)
        eff = compute_effective_tags(media_tags, item_tags, override_keys)
        item_effective.append((item, media_name, eff))

    results: list[SimulatePlayerResult] = []
    for id_player in body.player_ids:
        player = (await db.execute(
            select(CmsPlayer).where(CmsPlayer.id_player == id_player, CmsPlayer.id_org == current_user.id_org)
        )).scalar_one_or_none()
        if player is None:
            continue
        filters = await _player_filters_dict(db, id_player)
        items = [
            SimulateItem(
                id_item=item.id_item, id_media=item.id_media, media_name=media_name,
                matched=item_matches_player(eff, filters),
            )
            for item, media_name, eff in item_effective
        ]
        results.append(SimulatePlayerResult(id_player=id_player, player_name=player.player_name, items=items))
    return results
