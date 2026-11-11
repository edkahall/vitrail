"""
Audio Baskets endpoints (§XIX) — Sprint 2.5.

GET    /audio-baskets                          — liste des paniers de l'org
POST   /audio-baskets                          — créer un panier
GET    /audio-baskets/{id}                     — détail
PUT    /audio-baskets/{id}                     — modifier
DELETE /audio-baskets/{id}                     — supprimer
GET    /audio-baskets/{id}/items               — items du panier
POST   /audio-baskets/{id}/items               — ajouter un média au panier
DELETE /audio-baskets/{id}/items/{id_item}     — retirer un item
GET    /players/{id_player}/baskets            — paniers configurés sur un player
PUT    /players/{id_player}/baskets            — configurer les paniers actifs + ratios
"""

import json
from datetime import UTC, datetime
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_permission
from app.models.audio_basket import (
    CmsAudioBasket,
    CmsAudioBasketItem,
    CmsPlayerBasketConfig,
)
from app.models.audit_log import CmsAuditLog
from app.models.governance import CmsGovernanceNode
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.schedule import CmsScheduleBasket
from app.models.user import CmsUser
from app.services import audio_basket_sync
from app.services.governance_access import media_visibility, user_can_edit_node
from app.services.governance_scope import user_player_scope
from app.schemas.audio_basket import (
    AudioBasketCreate,
    AudioBasketRead,
    AudioBasketUpdate,
    BasketItemCreate,
    BasketItemRead,
    BasketItemsBulkCreate,
    BasketItemUpdate,
    PlayerBasketConfigItem,
    PlayerBasketConfigRead,
)

router = APIRouter(tags=["audio-baskets"])


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_basket_or_404(db: AsyncSession, user: CmsUser, id_basket: int) -> CmsAudioBasket:
    """Panier de l'org ET VISIBLE (A4).

    🔑 Le filtre est ici, pas seulement dans la liste : ne le calculer que côté liste
    laisse le lien direct mentir — c'est exactement le défaut trouvé sur les templates et
    sur les médias (28/07). Un panier d'un nœud frère répond donc 404, comme sa liste.
    """
    gov_clause, _visible, _editable = await media_visibility(db, user)
    stmt = select(CmsAudioBasket).where(
        CmsAudioBasket.id_basket == id_basket,
        CmsAudioBasket.id_org == user.id_org,
    )
    _clause = gov_clause(CmsAudioBasket.id_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    basket = (await db.execute(stmt)).scalar_one_or_none()
    if basket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio basket not found")
    return basket


async def _get_basket_editable(
    db: AsyncSession, user: CmsUser, id_basket: int
) -> CmsAudioBasket:
    """Panier de l'org, ET dont l'utilisateur administre le nœud de gouvernance (A4).

    Avant l'audit du 2026-07-29, seul le GESTE DE RATTACHEMENT était gardé
    (`_validate_node_org`, posé le 28/07) : une fois le panier rattaché à un nœud, le
    modifier, le supprimer ou changer ses morceaux ne vérifiait plus rien — la garde ne
    jouait que si `id_node` figurait dans le corps de la requête. Toutes les mutations
    passent désormais par ici, comme les 5 autres modules de contenu.
    """
    basket = await _get_basket_or_404(db, user, id_basket)
    if not await user_can_edit_node(db, user, basket.id_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not administer the governance node of '{basket.basket_name}'",
        )
    return basket


def _audit(user: CmsUser, action: str, id_basket: int, detail: dict) -> CmsAuditLog:
    """Traçabilité des paniers (A7) — il n'en existait AUCUNE côté CMS.

    Seules les actions du panneau local étaient journalisées (`_audit_local_control`) : les
    21 lignes mesurées en production venaient toutes de là. Créer, modifier ou supprimer un
    panier ou ses morceaux n'écrivait rien — donc impossible de répondre à « qui a changé ce
    panier ? ». Même correctif que pour les templates (28/07) : la ligne disparaît, le
    journal reste.
    """
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="audio_basket", resource_id=str(id_basket), source="cms",
        detail=detail,
    )


async def _sync_basket(db: AsyncSession, id_org: int, id_basket: int) -> None:
    """A6 — prévient les écrans concernés qu'un panier a changé.

    Un panier n'a pas de brouillon : la modification est déjà autoritaire côté serveur, et
    seuls les players l'ignorent jusqu'à leur prochaine resynchro. Le moment de la poussée
    est donc la mutation — l'équivalent, ici, du « Publier » d'une playlist.
    """
    await _push_force_sync(await audio_basket_sync.players_for_basket(db, id_org, id_basket))


async def _push_force_sync(player_ids: list[int]) -> None:
    """Notifie les players concernés qu'ils doivent recharger leur config audio."""
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        payload = json.dumps({"type": "player:force_sync", "preemptive": False})
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)
    finally:
        await r.aclose()


# ── CRUD paniers ─────────────────────────────────────────────────────────────

@router.get("/audio-baskets", response_model=list[AudioBasketRead])
async def list_baskets(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "read"))],
) -> list[AudioBasketRead]:
    # A4 — lecture filtrée par la gouvernance. `media_visibility` et non
    # `content_visibility` : les paniers NON gouvernés restent LISIBLES par les rôles
    # scopés (jamais éditables — l'écriture reste fermée par `user_can_edit_node(None)`).
    # C'est la décision d'Ed du 29/07 sur la médiathèque, et pour la même raison : aucun
    # panier n'est rattaché à un nœud en production, un filtre strict rendrait donc la
    # page VIDE pour un rôle de gouvernance et le module inutilisable.
    gov_clause, _visible, _editable = await media_visibility(db, current_user)
    stmt = select(CmsAudioBasket).where(CmsAudioBasket.id_org == current_user.id_org)
    _clause = gov_clause(CmsAudioBasket.id_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    rows = (await db.execute(stmt.order_by(CmsAudioBasket.basket_name))).scalars().all()

    # item_count + durée totale par panier (une seule requête groupée).
    # SUM ignore les NULL : une piste sans durée extraite n'ajoute rien au total.
    stats = {
        bid: (cnt, total)
        for bid, cnt, total in (await db.execute(
            select(
                CmsAudioBasketItem.id_basket,
                func.count(),
                func.sum(CmsMedia.duration_ms),
            )
            .join(CmsMedia, CmsMedia.id_media == CmsAudioBasketItem.id_media, isouter=True)
            .group_by(CmsAudioBasketItem.id_basket)
        )).all()
    }

    result = []
    for b in rows:
        item = AudioBasketRead.model_validate(b)
        cnt, total = stats.get(b.id_basket, (0, None))
        item.item_count = cnt
        item.total_duration_ms = int(total) if total else None
        result.append(item)
    return result


async def _validate_node_org(db: AsyncSession, user: CmsUser, id_node: int | None) -> None:
    """Nœud de gouvernance : appartient à l'org (cross-tenant = 404) ET administré (403).

    ⚠️ Ce contrôle ne vérifiait QUE l'appartenance à l'organisation : n'importe quel
    porteur d'`audio_baskets:configure` pouvait donc rattacher un panier à n'importe quel
    nœud, y compris ceux qu'il n'administre pas (audit du menu Média, 2026-07-28). Or
    rattacher, c'est décider QUI pilotera ce panier — exactement ce que ferment déjà
    `assert_can_edit_node` côté playlists/programmes et `_enforce_media_node_edit` côté
    médias. Même règle ici, même helper de gouvernance.
    """
    if id_node is None:
        return
    ok = (await db.execute(
        select(CmsGovernanceNode.id_node).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == user.id_org
        )
    )).scalar_one_or_none()
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Governance node not found")
    if not await user_can_edit_node(db, user, id_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer this governance node",
        )


@router.post("/audio-baskets", response_model=AudioBasketRead, status_code=status.HTTP_201_CREATED)
async def create_basket(
    body: AudioBasketCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> AudioBasketRead:
    await _validate_node_org(db, current_user, body.id_node)
    basket = CmsAudioBasket(
        id_org=current_user.id_org,
        id_node=body.id_node,
        basket_name=body.basket_name,
        basket_type=body.basket_type,
        basket_scope=body.basket_scope,
        description=body.description,
        is_mandatory=body.is_mandatory,
        min_ratio=body.min_ratio,
        max_ratio=body.max_ratio,
        ducking_config=body.ducking_config,
        trigger_mode=body.trigger_mode,
        trigger_value=body.trigger_value,
        shuffle=body.shuffle,
        created_by=current_user.id_user,
    )
    db.add(basket)
    await db.flush()
    db.add(_audit(current_user, "create", basket.id_basket, {
        "basket_name": basket.basket_name, "basket_type": basket.basket_type,
        "id_node": basket.id_node, "is_mandatory": basket.is_mandatory,
    }))
    await db.commit()
    await db.refresh(basket)
    return AudioBasketRead.model_validate(basket)


@router.get("/audio-baskets/{id_basket}", response_model=AudioBasketRead)
async def get_basket(
    id_basket: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "read"))],
) -> AudioBasketRead:
    basket = await _get_basket_or_404(db, current_user, id_basket)
    count, total = (await db.execute(
        select(func.count(), func.sum(CmsMedia.duration_ms))
        .select_from(CmsAudioBasketItem)
        .join(CmsMedia, CmsMedia.id_media == CmsAudioBasketItem.id_media, isouter=True)
        .where(CmsAudioBasketItem.id_basket == id_basket)
    )).one()
    item = AudioBasketRead.model_validate(basket)
    item.item_count = count
    item.total_duration_ms = int(total) if total else None
    return item


@router.put("/audio-baskets/{id_basket}", response_model=AudioBasketRead)
async def update_basket(
    id_basket: int,
    body: AudioBasketUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> AudioBasketRead:
    basket = await _get_basket_editable(db, current_user, id_basket)
    if "id_node" in body.model_fields_set:
        await _validate_node_org(db, current_user, body.id_node)
    changed = sorted(body.model_dump(exclude_unset=True).keys())
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(basket, field, value)
    basket.updated_at = datetime.now(UTC)
    db.add(_audit(current_user, "update", id_basket,
                  {"basket_name": basket.basket_name, "fields": changed}))
    await db.commit()
    await db.refresh(basket)
    # A6 — ducking, cadence de jingle, shuffle, activation… : rien de tout cela n'atteignait
    # les écrans avant leur prochaine resynchro.
    await _sync_basket(db, current_user.id_org, id_basket)
    return AudioBasketRead.model_validate(basket)


@router.get("/audio-baskets/{id_basket}/deletion-impact")
async def basket_deletion_impact(
    id_basket: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "read"))],
) -> dict:
    """Ce qu'une suppression emporterait — programmes, événements et écrans NOMMÉS."""
    await _get_basket_or_404(db, current_user, id_basket)
    scheds = await audio_basket_sync.schedules_using(db, current_user.id_org, id_basket)
    events = await audio_basket_sync.events_using(db, current_user.id_org, id_basket)
    players = await audio_basket_sync.players_for_basket(db, current_user.id_org, id_basket)
    return {
        "schedules": [{"id_schedule": s.id_schedule, "name": s.schedule_name} for s in scheds],
        "events": [{"id_event": e.id_event, "name": e.event_name} for e in events],
        "player_count": len(players),
    }


@router.delete("/audio-baskets/{id_basket}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_basket(
    id_basket: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
    force: bool = Query(False, description="Detach schedules/events, then delete"),
) -> None:
    """Supprime le panier — avec les mêmes garde-fous que les templates (A5, audit 29/07).

    La suppression était FRANCHE mais MUETTE : la contrainte `ON DELETE CASCADE` de
    `cms_schedule_baskets` retirait le panier de tous les programmes audio **sans un mot**,
    et la clé étrangère de `cms_events.id_basket` — sans action — faisait carrément échouer
    la requête en **500** dès qu'un événement le référençait.

    Désormais : 409 qui NOMME les programmes et événements concernés, `?force=true` pour
    détacher d'abord (l'événement voit son panier passer à NULL, ce qui le rend inerte et
    non silencieusement redirigé). La migration 0116 pose en plus `ON DELETE SET NULL` sur
    la clé étrangère : la garde protège le geste, la contrainte protège la base.
    """
    basket = await _get_basket_editable(db, current_user, id_basket)
    scheds = await audio_basket_sync.schedules_using(db, current_user.id_org, id_basket)
    events = await audio_basket_sync.events_using(db, current_user.id_org, id_basket)

    if (scheds or events) and not force:
        used_by = [f"'{s.schedule_name}'" for s in scheds] + [f"'{e.event_name}'" for e in events]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{basket.basket_name}' is used by " + ", ".join(used_by[:5])
                + (f" and {len(used_by) - 5} more" if len(used_by) > 5 else "")
                + " — they would lose this basket."
            ),
        )

    # Écrans capturés AVANT la suppression : après, plus rien ne relie le panier à eux.
    affected = await audio_basket_sync.players_for_basket(db, current_user.id_org, id_basket)
    for ev in events:
        ev.id_basket = None
    if scheds:
        await db.execute(
            CmsScheduleBasket.__table__.delete().where(
                CmsScheduleBasket.id_basket == id_basket
            )
        )
    db.add(_audit(current_user, "delete", id_basket, {
        "basket_name": basket.basket_name, "basket_type": basket.basket_type,
        "detached_schedules": [s.schedule_name for s in scheds],
        "detached_events": [e.event_name for e in events],
    }))
    await db.delete(basket)
    await db.commit()
    await _push_force_sync(affected)


# ── Items du panier ──────────────────────────────────────────────────────────

@router.get("/audio-baskets/{id_basket}/items", response_model=list[BasketItemRead])
async def list_basket_items(
    id_basket: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "read"))],
) -> list[BasketItemRead]:
    await _get_basket_or_404(db, current_user, id_basket)
    rows = (await db.execute(
        select(CmsAudioBasketItem, CmsMedia.media_name, CmsMedia.media_type, CmsMedia.duration_ms)
        .join(CmsMedia, CmsMedia.id_media == CmsAudioBasketItem.id_media, isouter=True)
        .where(CmsAudioBasketItem.id_basket == id_basket)
        .order_by(CmsAudioBasketItem.sort_order)
    )).all()
    result = []
    for it, media_name, media_type, duration_ms in rows:
        ri = BasketItemRead.model_validate(it)
        ri.media_name = media_name
        ri.media_type = media_type
        ri.duration_ms = duration_ms
        result.append(ri)
    return result


@router.post("/audio-baskets/{id_basket}/items", response_model=BasketItemRead, status_code=status.HTTP_201_CREATED)
async def add_basket_item(
    id_basket: int,
    body: BasketItemCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> BasketItemRead:
    await _get_basket_editable(db, current_user, id_basket)
    # Éligibilité = MÊME règle que l'ajout en lot (l'asymétrie unitaire/lot est ce qui a
    # laissé passer les médias supprimés côté playlists, audit 2026-07-29) : org explicite
    # (ne pas reposer sur la seule RLS) + visibilité de gouvernance des médias.
    gov_clause, _visible, _editable = await media_visibility(db, current_user)
    stmt = select(CmsMedia).where(
        CmsMedia.id_media == body.id_media,
        CmsMedia.id_org == current_user.id_org,
        CmsMedia.is_deleted == False,  # noqa: E712
    )
    _clause = gov_clause(CmsMedia.id_governance_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    media = (await db.execute(stmt)).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    # Un panier audio ne diffuse que du son pur : on refuse image/vidéo (contenus visuels).
    if media.media_type != "audio":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only audio media can be added to an audio basket",
        )
    item = CmsAudioBasketItem(
        id_basket=id_basket,
        id_media=body.id_media,
        weight=body.weight,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        sort_order=body.sort_order,
        rule_days=body.rule_days,
        rule_time_start=body.rule_time_start,
        rule_time_end=body.rule_time_end,
    )
    db.add(item)
    db.add(_audit(current_user, "add_item", id_basket,
                  {"id_media": media.id_media, "media_name": media.media_name}))
    await db.commit()
    await db.refresh(item)
    await _sync_basket(db, current_user.id_org, id_basket)
    ri = BasketItemRead.model_validate(item)
    ri.media_name = media.media_name
    ri.media_type = media.media_type
    ri.duration_ms = media.duration_ms
    return ri


@router.post(
    "/audio-baskets/{id_basket}/items/bulk",
    response_model=list[BasketItemRead],
    status_code=status.HTTP_201_CREATED,
)
async def add_basket_items_bulk(
    id_basket: int,
    body: BasketItemsBulkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> list[BasketItemRead]:
    """Ajoute PLUSIEURS médias audio au panier en une fois (multi-sélection du drawer).

    Même contrat que le bulk des playlists : les ids introuvables, non-audio, supprimés ou
    hors visibilité de gouvernance sont ignorés silencieusement (un id périmé ne doit pas
    faire échouer les autres) — mais l'éligibilité est LA MÊME que l'ajout unitaire, sinon
    le lot deviendrait le contournement de la garde unitaire. L'ordre est conservé, les
    doublons déjà présents dans le panier sont sautés.
    """
    await _get_basket_editable(db, current_user, id_basket)
    gov_clause, _visible, _editable = await media_visibility(db, current_user)
    stmt = select(CmsMedia).where(
        CmsMedia.id_media.in_(body.id_medias),
        CmsMedia.id_org == current_user.id_org,
        CmsMedia.media_type == "audio",
        CmsMedia.is_deleted == False,  # noqa: E712
    )
    _clause = gov_clause(CmsMedia.id_governance_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    media_map = {m.id_media: m for m in (await db.execute(stmt)).scalars().all()}

    existing_ids = set((await db.execute(
        select(CmsAudioBasketItem.id_media).where(CmsAudioBasketItem.id_basket == id_basket)
    )).scalars().all())
    next_sort = (await db.execute(
        select(func.coalesce(func.max(CmsAudioBasketItem.sort_order), -1)).where(
            CmsAudioBasketItem.id_basket == id_basket
        )
    )).scalar_one() + 1

    created: list[tuple[CmsAudioBasketItem, CmsMedia]] = []
    for id_media in body.id_medias:
        media = media_map.get(id_media)
        if media is None or id_media in existing_ids:
            continue
        item = CmsAudioBasketItem(id_basket=id_basket, id_media=id_media, sort_order=next_sort)
        db.add(item)
        created.append((item, media))
        existing_ids.add(id_media)
        next_sort += 1
    if created:
        db.add(_audit(current_user, "add_items", id_basket, {
            "count": len(created),
            "media_names": [m.media_name for _i, m in created][:20],
        }))
    await db.commit()
    if created:
        await _sync_basket(db, current_user.id_org, id_basket)

    result = []
    for item, media in created:
        await db.refresh(item)
        ri = BasketItemRead.model_validate(item)
        ri.media_name = media.media_name
        ri.media_type = media.media_type
        ri.duration_ms = media.duration_ms
        result.append(ri)
    return result


@router.patch("/audio-baskets/{id_basket}/items/{id_item}", response_model=BasketItemRead)
async def update_basket_item(
    id_basket: int,
    id_item: int,
    body: BasketItemUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> BasketItemRead:
    """Met à jour un item (poids, validité, règle de diffusion récurrente)."""
    await _get_basket_editable(db, current_user, id_basket)
    row = (await db.execute(
        select(CmsAudioBasketItem, CmsMedia)
        .join(CmsMedia, CmsMedia.id_media == CmsAudioBasketItem.id_media)
        .where(
            CmsAudioBasketItem.id_item == id_item,
            CmsAudioBasketItem.id_basket == id_basket,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    item, media = row
    changed = sorted(body.model_dump(exclude_unset=True).keys())
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    db.add(_audit(current_user, "update_item", id_basket,
                  {"id_item": id_item, "media_name": media.media_name, "fields": changed}))
    await db.commit()
    await db.refresh(item)
    await _sync_basket(db, current_user.id_org, id_basket)
    ri = BasketItemRead.model_validate(item)
    ri.media_name = media.media_name
    ri.media_type = media.media_type
    ri.duration_ms = media.duration_ms
    return ri


@router.delete("/audio-baskets/{id_basket}/items/{id_item}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_basket_item(
    id_basket: int,
    id_item: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "configure"))],
) -> None:
    await _get_basket_editable(db, current_user, id_basket)
    item = (await db.execute(
        select(CmsAudioBasketItem).where(
            CmsAudioBasketItem.id_item == id_item,
            CmsAudioBasketItem.id_basket == id_basket,
        )
    )).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    db.add(_audit(current_user, "remove_item", id_basket,
                  {"id_item": id_item, "id_media": item.id_media}))
    await db.delete(item)
    await db.commit()
    await _sync_basket(db, current_user.id_org, id_basket)


# ── Configuration paniers par player ─────────────────────────────────────────

async def _get_player_in_scope(db: AsyncSession, user: CmsUser, id_player: int) -> CmsPlayer:
    """Écran de l'org ET dans le périmètre de gouvernance de l'utilisateur (N8, A4).

    Ces deux routes étaient les dernières surfaces « écran » à ignorer `user_player_scope` :
    un admin de nœud pouvait lire — et écrire — la configuration audio d'un écran qui, sur
    `/players/`, lui répond 404. Hors périmètre = **404**, comme partout ailleurs.
    """
    player = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_player == id_player,
            CmsPlayer.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    scope = await user_player_scope(db, user)   # None = aucune restriction
    if player is None or (scope is not None and id_player not in scope):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return player


@router.get("/players/{id_player}/baskets", response_model=list[PlayerBasketConfigRead])
async def get_player_baskets(
    id_player: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "read"))],
) -> list[PlayerBasketConfigRead]:
    await _get_player_in_scope(db, current_user, id_player)

    rows = (await db.execute(
        select(CmsPlayerBasketConfig, CmsAudioBasket.basket_name, CmsAudioBasket.basket_type)
        .join(CmsAudioBasket, CmsAudioBasket.id_basket == CmsPlayerBasketConfig.id_basket)
        .where(CmsPlayerBasketConfig.id_player == id_player)
    )).all()
    result = []
    for cfg, basket_name, basket_type in rows:
        rc = PlayerBasketConfigRead.model_validate(cfg)
        rc.basket_name = basket_name
        rc.basket_type = basket_type
        result.append(rc)
    return result


@router.put("/players/{id_player}/baskets", response_model=list[PlayerBasketConfigRead])
async def set_player_baskets(
    id_player: int,
    body: list[PlayerBasketConfigItem],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("audio_baskets", "control"))],
) -> list[PlayerBasketConfigRead]:
    await _get_player_in_scope(db, current_user, id_player)

    # Validation : la somme des ratios actifs ne doit pas dépasser 100
    total = sum(c.mix_ratio for c in body if c.is_active)
    if total > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The sum of active ratios ({total}%) exceeds 100%",
        )

    # Vérifie que tous les paniers appartiennent à l'org
    basket_ids = [c.id_basket for c in body]
    if basket_ids:
        valid = {b.id_basket: b for b in (await db.execute(
            select(CmsAudioBasket).where(
                CmsAudioBasket.id_basket.in_(basket_ids),
                CmsAudioBasket.id_org == current_user.id_org,
            )
        )).scalars().all()}
        invalid = set(basket_ids) - set(valid)
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Baskets not found: {sorted(invalid)}",
            )
        # A2 — cette table N'EST QUE la couche d'override opérateur (§19.3) : elle sert à
        # RETIRER un panier programmé ou à changer son ratio. Sur un panier OBLIGATOIRE,
        # aucun override n'est donc légitime — c'est le sens du drapeau, que l'écran
        # annonce déjà (« ratio verrouillé pour les niveaux inférieurs »).
        forced = [valid[i].basket_name for i in basket_ids if valid[i].is_mandatory]
        if forced:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=("Mandatory basket — its state and ratio cannot be set per "
                        "player: " + ", ".join(f"'{n}'" for n in forced)),
            )

    # Remplace intégralement la config du player (delete + recreate)
    existing = (await db.execute(
        select(CmsPlayerBasketConfig).where(CmsPlayerBasketConfig.id_player == id_player)
    )).scalars().all()
    for e in existing:
        await db.delete(e)
    for c in body:
        db.add(CmsPlayerBasketConfig(
            id_player=id_player,
            id_basket=c.id_basket,
            mix_ratio=c.mix_ratio,
            is_active=c.is_active,
            activated_by=current_user.id_user,
        ))
    await db.commit()

    # Notifie le player de recharger sa config audio
    await _push_force_sync([id_player])

    return await get_player_baskets(id_player, db, current_user, None)
