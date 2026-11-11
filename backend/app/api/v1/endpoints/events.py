"""
Event Engine endpoints (§XX) — Sprint 2.1.

GET    /events                                    — liste
POST   /events                                    — créer
GET    /events/active                             — events actifs de l'org (temps réel)
GET    /events/ai-suggestions                     — suggestions IA en attente
POST   /events/ai-suggestions/{id}/approve        — approuver (crée l'event)
POST   /events/ai-suggestions/{id}/reject         — rejeter
GET    /events/{id}                               — détail
PUT    /events/{id}                               — modifier (impossible si status=active)
DELETE /events/{id}                               — supprimer (inactive ou cancelled uniquement)
POST   /events/{id}/activate                      — activation manuelle → active
POST   /events/{id}/deactivate                    — revert manuel → reverting (puis finalisé)
POST   /events/{id}/cancel                        — annuler depuis n'importe quel état
GET    /players/{id_player}/event-stack           — stack active pour ce player (prio décroissante)
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_permission
from app.models.audio_basket import CmsAudioBasket
from app.models.audit_log import CmsAuditLog
from app.models.event import CmsEvent, CmsEventAiSuggestion
from app.models.governance import CmsGovernanceNode
from app.models.media import CmsMedia
from app.models.organization import CmsSite
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroup, CmsPlayerGroupMember
from app.models.playlist import CmsPlaylist
from app.models.site_group import CmsSiteGroup
from app.models.user import CmsUser
from app.schemas.event import (
    EventAiSuggestionRead,
    EventCreate,
    EventRead,
    EventStackItem,
    EventUpdate,
)
from app.services.governance_access import (
    assert_can_detach,
    assert_can_edit_node,
    content_visibility,
)
from app.services.governance_scope import out_of_scope_targets, user_player_scope
from app.services.targeting import (
    Target,
    event_config_targets,
    event_target_config_without,
    event_target_entries,
    event_target_matches,
    player_site_group_ids,
    player_site_id,
    players_for_targets,
)

router = APIRouter(tags=["events"])


# ── Redis helpers ──────────────────────────────────────────────────────────────

async def _push_sync_to_targets(player_ids: set[int], is_preemptive: bool = False) -> None:
    """Publie player:force_sync sur le canal Redis de chaque player ciblé."""
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        payload = json.dumps({"type": "player:force_sync", "preemptive": is_preemptive})
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)
    finally:
        await r.aclose()


async def _resolve_player_ids(
    db: AsyncSession, id_org: int, target_config: dict
) -> set[int]:
    """Résout les IDs de players d'un target_config JSONB (§XX.7) — pour le force_sync.

    🔴 Réécrit le 2026-07-29 pour DÉLÉGUER au résolveur canonique. L'implémentation
    précédente recopiait la résolution et **ne traitait pas `site`** : elle rendait
    l'ensemble vide, sans erreur. Conséquence mesurable — activer un événement ciblant un
    SITE ne prévenait AUCUN écran ; la diffusion finissait par se faire (le matcher
    player-centrique, lui, connaît `site`), mais seulement à la resynchronisation
    suivante. Pour une alerte, c'est exactement ce qu'on ne veut pas.

    Deux autres défauts disparaissent au passage, tous deux d'origine « copie » :
    `player` renvoyait les identifiants BRUTS sans vérifier l'organisation, et le join de
    `group` ne filtrait pas non plus par organisation — on pouvait donc publier une
    commande sur le canal Redis d'un écran d'une AUTRE organisation.

    ⚠️ Le module importait déjà `players_for_targets` et ne s'en servait que pour filtrer
    des listes. C'est la leçon de D-b : sur ce genre de sujet, le risque n'est pas la
    règle, c'est la COUVERTURE — une deuxième implémentation finit toujours par diverger.

    Comprend les deux formes de target_config (hétérogène = union) via le normaliseur
    partagé `event_config_targets`.
    """
    return await players_for_targets(db, id_org, event_config_targets(target_config))


async def _enrich_event(db: AsyncSession, event: CmsEvent) -> EventRead:
    """Convertit CmsEvent en EventRead avec playlist_name et media_name."""
    data = EventRead.model_validate(event)
    if event.id_playlist:
        playlist = (
            await db.execute(
                select(CmsPlaylist).where(CmsPlaylist.id_playlist == event.id_playlist)
            )
        ).scalar_one_or_none()
        data.playlist_name = playlist.playlist_name if playlist else None
    if event.id_media:
        media = (
            await db.execute(
                select(CmsMedia).where(CmsMedia.id_media == event.id_media)
            )
        ).scalar_one_or_none()
        data.media_name = media.media_name if media else None
    if event.id_basket:
        basket = (
            await db.execute(
                select(CmsAudioBasket).where(CmsAudioBasket.id_basket == event.id_basket)
            )
        ).scalar_one_or_none()
        data.basket_name = basket.basket_name if basket else None
    return data


def _get_event_or_404(event: CmsEvent | None) -> CmsEvent:
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return event


def _audit(user: CmsUser, action: str, event: CmsEvent, detail: dict | None = None) -> CmsAuditLog:
    """Traçabilité des événements (audit 2026-07-29) — il n'en existait AUCUNE.

    Mesuré sur la production : **0 ligne** d'audit pour un événement, sur 859 au total. Ni
    la création, ni la modification, ni la suppression, ni surtout la MISE À L'ANTENNE
    n'étaient journalisées. Or un événement coupe le programme en cours sur les écrans
    qu'il vise : « qui a fait passer ça à l'écran, et quand ? » est exactement la question
    à laquelle il faut pouvoir répondre — c'est celle qu'Ed a posée pour les templates.

    La cible est conservée dans le détail : après suppression, la ligne disparaît mais le
    journal dit encore quels écrans étaient visés.
    """
    base = {
        "event_name": event.event_name,
        "event_kind": event.event_kind,
        "priority": event.priority,
        "target_config": event.target_config,
    }
    base.update(detail or {})
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="event", resource_id=str(event.id_event), source="cms",
        detail=base,
    )


# ── CRUD endpoints (chemins fixes avant le paramétré {id}) ────────────────────

@router.get("/events/active", response_model=list[EventRead])
async def list_active_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "read"))],
) -> list[EventRead]:
    """Tous les events actifs de l'org, triés par priorité décroissante."""
    stmt = (
        select(CmsEvent)
        .where(CmsEvent.id_org == current_user.id_org, CmsEvent.status == "active")
        .order_by(CmsEvent.priority.desc())
    )
    # Même filtre que `GET /events` : sans lui, cette route montrait les événements
    # gouvernés de TOUS les nœuds — y compris ceux que la liste principale masque.
    gov_clause, _visible, _editable = await content_visibility(db, current_user)
    clause = gov_clause(CmsEvent.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    rows = (await db.execute(stmt)).scalars().all()
    return [await _enrich_event(db, e) for e in rows]


@router.get("/events/ai-suggestions", response_model=list[EventAiSuggestionRead])
async def list_ai_suggestions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "read"))],
) -> list[EventAiSuggestionRead]:
    """Suggestions IA en attente de validation."""
    rows = (
        await db.execute(
            select(CmsEventAiSuggestion)
            .where(
                CmsEventAiSuggestion.id_org == current_user.id_org,
                CmsEventAiSuggestion.status == "pending",
            )
            .order_by(CmsEventAiSuggestion.created_at.desc())
        )
    ).scalars().all()
    return [EventAiSuggestionRead.model_validate(s) for s in rows]


@router.post(
    "/events/ai-suggestions/{id_suggestion}/approve",
    response_model=EventAiSuggestionRead,
)
async def approve_ai_suggestion(
    id_suggestion: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "create"))],
) -> EventAiSuggestionRead:
    """Approuve une suggestion IA → crée l'event correspondant."""
    suggestion = (
        await db.execute(
            select(CmsEventAiSuggestion).where(
                CmsEventAiSuggestion.id_suggestion == id_suggestion,
                CmsEventAiSuggestion.id_org == current_user.id_org,
            )
        )
    ).scalar_one_or_none()
    _get_event_or_404(suggestion)

    if suggestion.status != "pending":
        raise HTTPException(status_code=400, detail="Suggestion already reviewed")

    cfg = suggestion.suggested_config or {}
    new_event = CmsEvent(
        id_org=current_user.id_org,
        event_name=suggestion.trigger_summary[:255],
        event_type="ai_predicted",
        id_playlist=suggestion.id_playlist or cfg.get("id_playlist", 0),
        trigger_config=cfg.get("trigger_config"),
        target_config=cfg.get("target_config", {"target_type": "org", "target_ids": []}),
        created_by=current_user.id_user,
    )
    db.add(new_event)
    await db.flush()

    now_utc = datetime.now(UTC)
    suggestion.status = "approved"
    suggestion.reviewed_by = current_user.id_user
    suggestion.reviewed_at = now_utc
    suggestion.id_event_created = new_event.id_event
    await db.commit()
    await db.refresh(suggestion)
    return EventAiSuggestionRead.model_validate(suggestion)


@router.post(
    "/events/ai-suggestions/{id_suggestion}/reject",
    response_model=EventAiSuggestionRead,
)
async def reject_ai_suggestion(
    id_suggestion: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "create"))],
) -> EventAiSuggestionRead:
    suggestion = (
        await db.execute(
            select(CmsEventAiSuggestion).where(
                CmsEventAiSuggestion.id_suggestion == id_suggestion,
                CmsEventAiSuggestion.id_org == current_user.id_org,
            )
        )
    ).scalar_one_or_none()
    _get_event_or_404(suggestion)

    if suggestion.status != "pending":
        raise HTTPException(status_code=400, detail="Suggestion already reviewed")

    now_utc = datetime.now(UTC)
    suggestion.status = "rejected"
    suggestion.reviewed_by = current_user.id_user
    suggestion.reviewed_at = now_utc
    await db.commit()
    await db.refresh(suggestion)
    return EventAiSuggestionRead.model_validate(suggestion)


# ── Liste + création ───────────────────────────────────────────────────────────

async def _validate_event_node(db: AsyncSession, user: CmsUser, id_node: int | None) -> None:
    if id_node is None:
        return
    ok = (await db.execute(
        select(CmsGovernanceNode.id_node).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Governance node not found")


async def _get_visible_event(
    db: AsyncSession, user: CmsUser, id_event: int
) -> CmsEvent:
    """Événement de l'org ET VISIBLE (D6/G4) — 404 sinon.

    🔴 Les 6 routes qui chargent un événement par son identifiant ne filtraient QUE sur
    l'organisation : un événement absent de la liste (nœud frère, ou rôle de gouvernance
    scopé) restait lisible — et surtout **activable** — par son identifiant. C'est la
    famille « la liste filtre, le lien direct ment », déjà fermée sur les templates
    (28/07), les médias et les playlists ; ici elle portait une mise à l'antenne.
    """
    stmt = select(CmsEvent).where(
        CmsEvent.id_event == id_event, CmsEvent.id_org == user.id_org
    )
    gov_clause, _visible, _editable = await content_visibility(db, user)
    clause = gov_clause(CmsEvent.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    event = (await db.execute(stmt)).scalar_one_or_none()
    _get_event_or_404(event)
    return event


async def _validate_event_targets(
    db: AsyncSession, id_org: int, target_config: dict | None
) -> None:
    """Les identifiants de cible existent-ils, DANS cette organisation ? (audit 29/07)

    🔴 Rien ne le vérifiait. Seul le *type* de cible était validé par le schéma Pydantic, et
    la garde de périmètre `_enforce_event_targets_in_scope` sort immédiatement pour un
    événement NON gouverné — c'est-à-dire la majorité. On pouvait donc créer un événement
    visant l'identifiant d'un écran d'une autre organisation, ou un identifiant inexistant :
    accepté en 201, compté à l'aperçu, jamais diffusé. C'est la classe de défaut de l'audit
    du 18/07 (« cible acceptée à l'écriture, ignorée à la lecture »), ici sur l'existence.

    Refus en 422 NOMMANT les identifiants fautifs — jamais un silence. Chaque entrée de
    la forme hétérogène est vérifiée : une 2ᵉ entrée fautive ne passe pas parce que la
    1ʳᵉ est saine.
    """
    for t_type, t_ids in event_target_entries(target_config):
        if t_type == "org" or not t_ids:
            continue
        model, column = {
            "player": (CmsPlayer, CmsPlayer.id_player),
            "group": (CmsPlayerGroup, CmsPlayerGroup.id_group),
            "site": (CmsSite, CmsSite.id_site),
            "site_group": (CmsSiteGroup, CmsSiteGroup.id_site_group),
        }.get(t_type, (None, None))
        if model is None:
            continue
        found = set((await db.execute(
            select(column).where(column.in_(t_ids), model.id_org == id_org)
        )).scalars().all())
        missing = sorted(set(t_ids) - found)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Target not found in the organization ({t_type}): {missing}",
            )


async def _validate_event_content(
    db: AsyncSession, id_org: int, id_playlist: int | None, id_media: int | None
) -> None:
    """La playlist / le média visé existe-t-il dans l'organisation ? (audit 29/07)

    Le panier audio était vérifié, mais ni la playlist ni le média : un événement pouvait
    naître en pointant le contenu d'une AUTRE organisation, ou un identifiant supprimé.
    """
    if id_playlist:
        ok = (await db.execute(select(CmsPlaylist.id_playlist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        ))).scalar_one_or_none()
        if ok is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Playlist not found")
    if id_media:
        ok = (await db.execute(select(CmsMedia.id_media).where(
            CmsMedia.id_media == id_media,
            CmsMedia.id_org == id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
        ))).scalar_one_or_none()
        if ok is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Media not found")


async def _enforce_event_targets_in_scope(
    db: AsyncSession, id_org: int, id_node: int | None, target_config: dict | None
) -> None:
    """Un événement GOUVERNÉ ne vise que des écrans de son nœud (phase 4, miroir de D3).

    Un événement coupe le contenu en cours : c'est la diffusion la plus immédiatement
    visible du produit. Sans cette garde, rattacher un événement à un nœud n'aurait été
    qu'une étiquette — il aurait continué à viser n'importe quel écran du parc, exactement
    ce que l'audit a mesuré (N3 : programme créé vers le BrightSign d'un autre périmètre).

    ⚠️ La cible `org` est refusée d'emblée sur un événement gouverné : « tout le parc » ne
    peut par construction pas tenir dans un nœud.
    """
    if id_node is None or not target_config:
        return
    node = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == id_org)
    )).scalar_one_or_none()
    if node is None:
        return
    pairs: list[tuple[str, int]] = []
    for t_type, t_ids in event_target_entries(target_config):
        if t_type == "org":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"An event attached to node \"{node.node_name}\" cannot target "
                       "the whole organization: choose players within its scope.",
            )
        pairs.extend((t_type, tid) for tid in t_ids)
    bad = await out_of_scope_targets(db, id_org, node, pairs)
    if bad:
        names = ", ".join(b["target_name"] or f"#{b['target_id']}" for b in bad)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Target outside the scope of node \"{node.node_name}\": {names}.",
        )


@router.get("/events", response_model=list[EventRead])
async def list_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "read"))],
    kind: str | None = None,
    target_type: str | None = Query(
        None, pattern="^(player|player_group|group|site|site_group)$",
        description="Target filter: keep only events that reach this target",
    ),
    target_id: int | None = Query(None),
) -> list[EventRead]:
    stmt = (
        select(CmsEvent)
        .where(CmsEvent.id_org == current_user.id_org)
        .order_by(CmsEvent.priority.desc(), CmsEvent.created_at.desc())
    )
    if kind in ("content", "audio"):
        stmt = stmt.where(CmsEvent.event_kind == kind)
    # Gouvernance §XVIII phase 4 : D6 (nœuds visibles) + G4 (les événements d'organisation
    # disparaissent pour un porteur de rôle de gouvernance).
    gov_clause, _visible, _editable = await content_visibility(db, current_user)
    clause = gov_clause(CmsEvent.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    rows = list((await db.execute(stmt)).scalars().all())

    # Filtre cible (calendrier unifié) : event retenu si son target_config résolu en
    # players intersecte la cible-filtre. target_config = JSONB dénormalisé
    # (org / player / group / site_group + target_ids) → expansion via targeting.
    if rows and target_type is not None and target_id is not None:
        fset = await players_for_targets(db, current_user.id_org, [
            Target(target_type=target_type, target_id=target_id)
        ])
        cache: dict[tuple[str, int], set[int]] = {}
        kept: list[CmsEvent] = []
        for e in rows:
            # Les deux formes via le normaliseur ; un event est retenu dès qu'UNE de ses
            # entrées touche la cible-filtre (même sémantique d'union que la diffusion).
            hit = False
            for t_type, t_ids in event_target_entries(e.target_config):
                if t_type == "org":
                    hit = bool(fset)
                elif t_type == "player":
                    hit = bool(set(t_ids) & fset)
                else:
                    resolved: set[int] = set()
                    for tid in t_ids:
                        key = (t_type, tid)
                        if key not in cache:
                            cache[key] = await players_for_targets(db, current_user.id_org, [
                                Target(target_type=t_type, target_id=tid)
                            ])
                        resolved |= cache[key]
                    hit = bool(resolved & fset)
                if hit:
                    break
            if hit:
                kept.append(e)
        rows = kept

    return [await _enrich_event(db, e) for e in rows]


@router.post("/events", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "create"))],
) -> EventRead:
    if body.event_kind == "audio":
        basket = (await db.execute(
            select(CmsAudioBasket).where(
                CmsAudioBasket.id_basket == body.id_basket,
                CmsAudioBasket.id_org == current_user.id_org,
            )
        )).scalar_one_or_none()
        if basket is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio basket not found")

    await _validate_event_node(db, current_user, body.id_governance_node)
    await assert_can_edit_node(db, current_user, body.id_governance_node, "this event")
    await _validate_event_targets(db, current_user.id_org, body.target_config)
    if body.event_kind == "content":
        await _validate_event_content(db, current_user.id_org, body.id_playlist, body.id_media)
    await _enforce_event_targets_in_scope(
        db, current_user.id_org, body.id_governance_node, body.target_config)

    event = CmsEvent(
        id_org=current_user.id_org,
        event_name=body.event_name,
        event_type=body.event_type,
        event_kind=body.event_kind,
        id_playlist=body.id_playlist if body.event_kind == "content" else None,
        id_media=body.id_media if body.event_kind == "content" else None,
        id_basket=body.id_basket if body.event_kind == "audio" else None,
        mix_ratio=body.mix_ratio if body.event_kind == "audio" else None,
        priority=body.priority,
        id_governance_node=body.id_governance_node,
        duration_ms=body.duration_ms,
        revert_on_end=body.revert_on_end,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        is_recurring=body.is_recurring,
        is_preemptive=body.is_preemptive,
        keep_screens_on=body.keep_screens_on,
        not_before_utc=body.not_before_utc,
        timezone=body.timezone,
        trigger_config=body.trigger_config,
        target_config=body.target_config,
        created_by=current_user.id_user,
    )
    db.add(event)
    await db.flush()
    db.add(_audit(current_user, "create", event))
    await db.commit()
    await db.refresh(event)
    return await _enrich_event(db, event)


# ── Opérations sur un event spécifique ────────────────────────────────────────

@router.get("/events/{id_event}", response_model=EventRead)
async def get_event(
    id_event: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "read"))],
) -> EventRead:
    event = await _get_visible_event(db, current_user, id_event)
    return await _enrich_event(db, event)


@router.put("/events/{id_event}", response_model=EventRead)
async def update_event(
    id_event: int,
    body: EventUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    # `events:edit` et non plus `events:trigger` (migr 0117) : cette route réécrit le
    # contenu diffusé, les cibles, la priorité et l'embargo — ce n'est pas un geste
    # d'exploitation. La permission n'existait tout simplement pas au catalogue (0 ligne
    # en production) et la migration 0099 croyait à tort l'accorder.
    _: Annotated[None, Depends(require_permission("events", "edit"))],
) -> EventRead:
    event = await _get_visible_event(db, current_user, id_event)

    # Gouvernance §XVIII phase 4 : éditer exige d'administrer le nœud ACTUEL, puis le nœud
    # CIBLE en cas de déplacement ; détacher est réservé à un nœud PARENT (N2).
    await assert_can_edit_node(db, current_user, event.id_governance_node, "this event")
    if "id_governance_node" in body.model_fields_set:
        await _validate_event_node(db, current_user, body.id_governance_node)
        await assert_can_edit_node(db, current_user, body.id_governance_node,
                                   "the destination node")
        if body.id_governance_node is None:
            await assert_can_detach(db, current_user, event.id_governance_node,
                                    "this event")
    node_after = (body.id_governance_node if "id_governance_node" in body.model_fields_set
                  else event.id_governance_node)
    targets_after = (body.target_config if "target_config" in body.model_fields_set
                     else event.target_config)
    # Mêmes contrôles qu'à la création : la mise à jour réécrit cibles ET contenu par une
    # boucle `setattr` générique, elle ne doit pas être la porte d'entrée de ce que la
    # création refuse.
    if "target_config" in body.model_fields_set:
        await _validate_event_targets(db, current_user.id_org, targets_after)
    if "id_playlist" in body.model_fields_set or "id_media" in body.model_fields_set:
        await _validate_event_content(
            db, current_user.id_org,
            body.id_playlist if "id_playlist" in body.model_fields_set else None,
            body.id_media if "id_media" in body.model_fields_set else None,
        )
    await _enforce_event_targets_in_scope(db, current_user.id_org, node_after, targets_after)

    if event.status == "active":
        raise HTTPException(
            status_code=400, detail="Cannot modify an active event. Deactivate it first."
        )

    # Un event terminé ou annulé repasse en inactive pour pouvoir se relancer
    if event.status in ("completed", "cancelled"):
        event.status = "inactive"
        event.activated_at = None
        event.expires_at = None

    # exclude_unset=True (pas exclude_none) pour permettre d'effacer id_playlist
    # quand on passe en mode media unique (id_playlist: null explicite dans le body)
    changed = sorted(body.model_dump(exclude_unset=True).keys())
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(event, field, value)
    db.add(_audit(current_user, "update", event, {"champs": changed}))
    await db.commit()
    await db.refresh(event)
    return await _enrich_event(db, event)


@router.delete("/events/{id_event}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    id_event: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "delete"))],
) -> None:
    event = await _get_visible_event(db, current_user, id_event)
    await assert_can_edit_node(db, current_user, event.id_governance_node, "this event")

    if event.status not in ("inactive", "cancelled", "completed"):
        raise HTTPException(
            status_code=400,
            detail="Only inactive, completed, or cancelled events can be deleted.",
        )

    # Sauvegarder les infos avant la suppression pour le push sync
    was_recently_active = event.status in ("completed", "cancelled")
    event_target_config = event.target_config
    event_id_org = event.id_org

    db.add(_audit(current_user, "delete", event, {"status_avant": event.status}))
    await db.delete(event)
    await db.commit()

    # Push sync après suppression pour les events récemment actifs (ils ont pu marquer
    # les players dans un état override — assure le retour au contenu nominal).
    if was_recently_active:
        player_ids = await _resolve_player_ids(db, event_id_org, event_target_config)
        await _push_sync_to_targets(player_ids)


# ── Contrôle temps réel ────────────────────────────────────────────────────────

@router.post("/events/{id_event}/activate", response_model=EventRead)
async def activate_event(
    id_event: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "trigger"))],
) -> EventRead:
    """Force l'activation manuelle d'un event (inactive → active)."""
    event = await _get_visible_event(db, current_user, id_event)
    # 🔴 Mettre à l'antenne est un acte d'ÉDITION du contenu diffusé, pas un simple geste
    # d'exploitation : un événement coupe le programme en cours sur les écrans visés. Les
    # trois transitions (activer / rendre la main / annuler) n'avaient AUCUNE garde de
    # nœud, alors que créer, modifier et supprimer en ont une depuis la phase 4 — donc
    # `events:trigger` suffisait à diffuser dans un périmètre qu'on n'administre pas.
    await assert_can_edit_node(db, current_user, event.id_governance_node, "this event")

    if event.status not in ("inactive", "reverting", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot activate event in status '{event.status}'.",
        )

    now_utc = datetime.now(UTC)

    # Vérification de la fenêtre de validité (s'applique aussi à l'activation manuelle)
    if event.valid_from and now_utc < event.valid_from:
        raise HTTPException(
            status_code=400,
            detail="Event not yet valid (before valid_from).",
        )
    if event.valid_until and now_utc > event.valid_until:
        raise HTTPException(
            status_code=400,
            detail="Event validity period has ended (after valid_until).",
        )
    if event.not_before_utc and now_utc < event.not_before_utc:
        raise HTTPException(
            status_code=400,
            detail=f"Embargo active: playback allowed from {event.not_before_utc.isoformat()}",
        )
    db.add(_audit(current_user, "activate", event))
    event.status = "active"
    event.activated_at = now_utc
    event.expires_at = (
        now_utc + timedelta(milliseconds=event.duration_ms)
        if event.duration_ms
        else None
    )
    event.updated_at = now_utc
    await db.commit()
    await db.refresh(event)

    player_ids = await _resolve_player_ids(db, current_user.id_org, event.target_config)
    await _push_sync_to_targets(player_ids, is_preemptive=event.is_preemptive)

    return await _enrich_event(db, event)


@router.post("/events/{id_event}/deactivate", response_model=EventRead)
async def deactivate_event(
    id_event: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "trigger"))],
) -> EventRead:
    """Revert manuel d'un event actif (active → reverting) — retour au programme nominal (§20.3).

    Le force_sync émis ici fait recalculer la pile aux players ciblés : l'event actif
    suivant (priorité inférieure) ou le programme nominal reprend immédiatement la main.
    L'event reste en `reverting` jusqu'à la finalisation par le worker
    finalize_reverting_events (→ completed, ou inactive si récurrent encore valide).
    """
    event = await _get_visible_event(db, current_user, id_event)
    await assert_can_edit_node(db, current_user, event.id_governance_node, "this event")

    if event.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot deactivate event in status '{event.status}'.",
        )

    db.add(_audit(current_user, "deactivate", event))
    event.status = "reverting"
    await db.commit()
    await db.refresh(event)

    # Force sync pour restaurer le programme nominal (cascade) sur les players ciblés
    player_ids = await _resolve_player_ids(db, current_user.id_org, event.target_config)
    await _push_sync_to_targets(player_ids)

    return await _enrich_event(db, event)


@router.post("/events/{id_event}/cancel", response_model=EventRead)
async def cancel_event(
    id_event: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("events", "trigger"))],
) -> EventRead:
    """Annule définitivement un event depuis n'importe quel état."""
    event = await _get_visible_event(db, current_user, id_event)
    await assert_can_edit_node(db, current_user, event.id_governance_node, "this event")

    db.add(_audit(current_user, "cancel", event, {"status_avant": event.status}))
    event.status = "cancelled"
    await db.commit()
    await db.refresh(event)

    # Toujours push sync lors d'une annulation :
    # - Si l'event était actif → restaure le programme nominal sur les players
    # - Si l'event était completed → le push sync de expire_events a pu être manqué
    #   (player déconnecté, Redis fire-and-forget) → re-sync défensif
    player_ids = await _resolve_player_ids(db, current_user.id_org, event.target_config)
    await _push_sync_to_targets(player_ids)

    return await _enrich_event(db, event)


# ── Stack events d'un player ───────────────────────────────────────────────────

@router.get("/players/{id_player}/event-stack", response_model=list[EventStackItem])
async def get_player_event_stack(
    id_player: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("players", "read"))],
) -> list[EventStackItem]:
    """
    Retourne la stack d'events actifs ciblant ce player, triée par priorité décroissante.
    Inclut les events de type org-wide et les events de groupe si le player est membre.
    """
    player = (
        await db.execute(
            select(CmsPlayer).where(
                CmsPlayer.id_player == id_player,
                CmsPlayer.id_org == current_user.id_org,
            )
        )
    ).scalar_one_or_none()
    # N8 — le périmètre d'écrans s'applique ici comme sur `/players/` : cette route dit ce
    # qui diffuse sur un écran donné, elle ne doit pas répondre pour un écran que
    # l'utilisateur ne voit pas. `None` = aucune restriction (org-admin / rôle classique).
    scope = await user_player_scope(db, current_user)
    if player is None or (scope is not None and id_player not in scope):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    stmt = (
        select(CmsEvent)
        .where(CmsEvent.id_org == current_user.id_org, CmsEvent.status == "active")
        .order_by(CmsEvent.priority.desc())
    )
    gov_clause, _visible, _editable = await content_visibility(db, current_user)
    clause = gov_clause(CmsEvent.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    active_events = (await db.execute(stmt)).scalars().all()

    # Groupes du player (+ groupes de sites) pour le filtrage des events ciblés
    player_group_ids: set[int] = set(
        (
            await db.execute(
                select(CmsPlayerGroupMember.id_group).where(
                    CmsPlayerGroupMember.id_player == id_player
                )
            )
        ).scalars().all()
    )
    site_group_ids = await player_site_group_ids(db, id_player)
    id_site = await player_site_id(db, id_player)

    result: list[EventStackItem] = []
    for ev in active_events:
        if event_target_matches(
            ev.target_config, id_player, player_group_ids, site_group_ids, id_site=id_site
        ):
            result.append(EventStackItem.model_validate(ev))

    return result
