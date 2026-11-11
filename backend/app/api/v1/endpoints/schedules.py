"""
Endpoints Schedules (§XIII) + résolution priorité player.

POST   /schedules                            — créer (+ règles + cibles)
GET    /schedules                            — liste
GET    /schedules/{id}                       — détail + règles + cibles
PUT    /schedules/{id}                       — mise à jour métadonnées
DELETE /schedules/{id}                       — suppression logique
GET    /players/{id_player}/current-schedule — résolution de conflit de priorité
"""

import json
from datetime import date as date_cls, datetime, time as time_cls
from typing import Annotated, Optional
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_permission
from app.models.audio_basket import CmsAudioBasket
from app.models.event import CmsEvent
from app.models.governance import CmsGovernanceNode
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroupMember
from app.models.media import CmsMedia
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.audit_log import CmsAuditLog
from app.models.organization import CmsSite
from app.models.player_group import CmsPlayerGroup
from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget, CmsScheduleBasket
from app.models.site_group import CmsSiteGroup
from app.models.user import CmsUser
from app.services.governance_access import (
    content_visibility,
    user_can_detach_from_node,
    user_can_edit_node,
    user_is_governance_scoped,
)
from app.services.governance_scope import (
    lan_sync_split,
    node_perimeter_players,
    out_of_scope_targets,
    resolved_players,
    user_player_scope,
)
from app.services.scheduling import local_now, schedule_active_at, time_rule_from_schedule
from app.services.player_timezone import player_tz_name
from app.services.targeting import (
    Target,
    event_target_matches,
    player_site_group_ids,
    player_site_id,
    players_for_targets,
)
from app.schemas.schedule import (
    CurrentScheduleResponse,
    GovernanceScopeLanSplit,
    GovernanceScopePlayer,
    GovernanceScopeRequest,
    GovernanceScopeResult,
    GovernanceScopeTarget,
    DaySimAudioSegment,
    DaySimBasket,
    DaySimVideoSegment,
    HdmiCompatCheckRequest,
    HdmiCompatCheckResult,
    HdmiIncompatiblePlayer,
    PlayerDaySimRequest,
    PlayerDaySimResult,
    ScheduleBasketRead,
    ScheduleCreate,
    ScheduleDetail,
    ScheduleRead,
    ScheduleRuleRead,
    ScheduleTargetCreate,
    ScheduleTargetRead,
    ScheduleUpdate,
)

router = APIRouter(tags=["schedules"])


# ── Immutabilité par couche §XVIII Phase 2 ──────────────────────────────────────

async def _validate_node_org(db: AsyncSession, current_user: CmsUser, id_node: int | None) -> None:
    """404 si le nœud de gouvernance n'existe pas dans l'org de l'utilisateur."""
    if id_node is None:
        return
    node = (await db.execute(
        select(CmsGovernanceNode.id_node).where(
            CmsGovernanceNode.id_node == id_node,
            CmsGovernanceNode.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Governance node not found"
        )


async def _enforce_node_edit(db: AsyncSession, current_user: CmsUser, id_node: int | None) -> None:
    """403 si l'utilisateur n'administre pas le nœud (ni un de ses ancêtres). NULL = libre."""
    if not await user_can_edit_node(db, current_user, id_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer the governance node of this schedule",
        )


async def _get_visible_schedule(
    db: AsyncSession, user: CmsUser, id_schedule: int
) -> CmsSchedule:
    """Programme de l'org ET VISIBLE (D6/G4) — 404 sinon (audit menu ⑧, 2026-07-31).

    🔴 Les 4 routes qui chargeaient un programme par son identifiant ne filtraient ni
    l'organisation ni la gouvernance : un programme absent de la liste (nœud frère, rôle
    de gouvernance scopé) restait lisible — règles et CIBLES comprises — par son seul
    identifiant. Famille « la liste filtre, le lien direct ment », déjà fermée sur
    templates, médias, playlists, événements et VOD. L'ÉDITABILITÉ reste une question
    distincte, gardée ensuite par `_enforce_node_edit` (un nœud ancêtre se lit sans
    s'éditer, contrat D6).
    """
    stmt = select(CmsSchedule).where(
        CmsSchedule.id_schedule == id_schedule,
        CmsSchedule.id_org == user.id_org,
        CmsSchedule.is_deleted == False,  # noqa: E712
    )
    gov_clause, _visible, _editable = await content_visibility(db, user)
    clause = gov_clause(CmsSchedule.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    sched = (await db.execute(stmt)).scalar_one_or_none()
    if sched is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return sched


async def _validate_targets_exist(
    db: AsyncSession, id_org: int, targets
) -> None:
    """Chaque identifiant de cible existe-t-il, DANS cette organisation ? (audit menu ⑧)

    🔴 Rien ne le vérifiait : un écran inexistant, ou le site d'une AUTRE organisation,
    était accepté en 201/204 — enregistré, affiché, jamais résolu (classe « cible acceptée
    à l'écriture, ignorée à la lecture », audit du 18/07). Même patron que
    `_validate_event_targets` (événements, 29/07) : refus 422 NOMMANT les identifiants.
    """
    by_type: dict[str, set[int]] = {}
    for t in targets:
        by_type.setdefault(t.target_type, set()).add(t.target_id)
    models = {
        "player": (CmsPlayer, CmsPlayer.id_player),
        "player_group": (CmsPlayerGroup, CmsPlayerGroup.id_group),
        "site": (CmsSite, CmsSite.id_site),
        "site_group": (CmsSiteGroup, CmsSiteGroup.id_site_group),
    }
    for t_type, t_ids in by_type.items():
        model, column = models.get(t_type, (None, None))
        if model is None:
            continue
        found = set((await db.execute(
            select(column).where(column.in_(t_ids), model.id_org == id_org)
        )).scalars().all())
        missing = sorted(t_ids - found)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Target not found in the organization ({t_type}): {missing}",
            )


async def _validate_playlist_choice(
    db: AsyncSession, user: CmsUser, id_playlist: int
) -> None:
    """La playlist programmée existe, dans l'org, non supprimée, et VISIBLE — 404 sinon.

    🔴 À la création, seule l'existence était vérifiée (pas l'organisation) ; à l'update,
    RIEN : `id_playlist` inexistant, supprimé ou étranger passait en 200 et l'écran
    cassait en silence à la synchro suivante. Et le défaut documenté le 27/07 restait
    ouvert : un admin de site pouvait pointer son programme vers une playlist de
    gouvernance qu'il reçoit en 404 (les gardes portaient sur le nœud du PROGRAMME,
    jamais sur la playlist choisie). Le refus (plutôt qu'un avertissement) est aligné
    sur l'affectation croisée des sous-playlists, fermée le 29/07 : programmer un
    contenu qu'on ne peut pas lire n'est jamais un geste volontaire sain.
    """
    stmt = select(CmsPlaylist.id_playlist).where(
        CmsPlaylist.id_playlist == id_playlist,
        CmsPlaylist.id_org == user.id_org,
        CmsPlaylist.is_deleted == False,  # noqa: E712
    )
    gov_clause, _visible, _editable = await content_visibility(db, user)
    clause = gov_clause(CmsPlaylist.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    ok = (await db.execute(stmt)).scalar_one_or_none()
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")


def _audit(user: CmsUser, action: str, sched: CmsSchedule, detail: dict | None = None) -> CmsAuditLog:
    """Traçabilité des programmes (audit menu ⑧) — il n'en existait AUCUNE.

    Mesuré sur la production : 4 lignes « schedule » sur 866, toutes émises par
    l'ASSISTANT — jamais par ce menu. Un programme décide de ce que diffusent les
    écrans : « qui a changé la programmation, et quand ? » doit avoir une réponse.
    La cible est conservée dans le détail : après suppression, le journal dit encore
    quels écrans étaient visés.
    """
    base = {
        "schedule_name": sched.schedule_name,
        "schedule_kind": sched.schedule_kind,
        "id_playlist": sched.id_playlist,
        "priority": sched.priority,
        "id_governance_node": sched.id_governance_node,
    }
    base.update(detail or {})
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="schedule", resource_id=str(sched.id_schedule), source="cms",
        detail=base,
    )


def _targets_detail(targets) -> list[dict]:
    """Cibles au moment du geste, pour le détail d'audit."""
    return [{"target_type": t.target_type, "target_id": t.target_id} for t in targets]


# ── Redis sync push ───────────────────────────────────────────────────────────

async def _push_sync(player_ids: set[int]) -> None:
    """Publie player:force_sync sur le canal Redis de chaque player ciblé."""
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        payload = json.dumps({"type": "player:force_sync"})
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)
    finally:
        await r.aclose()


async def _governed_node(db: AsyncSession, id_org: int, id_node: int | None) -> CmsGovernanceNode | None:
    if id_node is None:
        return None
    return (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == id_org
        )
    )).scalar_one_or_none()


async def _enforce_targets_in_scope(
    db: AsyncSession, id_org: int, id_node: int | None, targets
) -> None:
    """D-b / D3 — refuse AU MOMENT DU GESTE une cible qui ne retient aucun écran du nœud.

    Les cibles d'un programme gouverné **restreignent** le périmètre du nœud (elles ne
    l'élargissent jamais). Une cible qui résout à zéro écran de ce périmètre est donc une
    erreur de saisie : on la nomme au lieu de l'enregistrer silencieusement.

    ⛔ **Jamais de ré-invalidation après coup** : si le périmètre bouge plus tard, la cible
    devenue inerte reste en base et s'AFFICHE — la supprimer dans le dos de l'utilisateur
    serait pire que de la montrer inefficace.
    Un recouvrement PARTIEL (groupe de sites à cheval sur deux départements) est accepté :
    seule la part du nœud est retenue.
    """
    node = await _governed_node(db, id_org, id_node)
    if node is None or not targets:
        return
    bad = await out_of_scope_targets(
        db, id_org, node, [(t.target_type, t.target_id) for t in targets]
    )
    if bad:
        names = ", ".join(b["target_name"] or f"#{b['target_id']}" for b in bad)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Target outside the scope of node '{node.node_name}': {names}. "
                "A governed schedule's target can only narrow down the node's players."
            ),
        )


async def _governed_sync_ids(db: AsyncSession, sched: CmsSchedule) -> set[int]:
    """Écrans à faire recompiler pour un programme gouverné = tout le périmètre du nœud.

    Résoudre les seules cibles ne suffit pas : un écran qui vient d'être RETIRÉ du ciblage
    doit lui aussi recompiler, sinon il continue de diffuser une couche qu'il n'a plus.
    Et un programme sans cible ne résout à rien du tout (D0 = tout le nœud).
    """
    node = await _governed_node(db, sched.id_org, sched.id_governance_node)
    if node is None:
        return set()
    return await node_perimeter_players(db, sched.id_org, node)


async def _resolve_player_ids(targets, id_org: int, db: AsyncSession) -> set[int]:
    """Expand player/player_group targets to a flat set of player IDs (via le noyau Targeting)."""
    return await players_for_targets(
        db, id_org, [Target(t.target_type, t.target_id) for t in targets]
    )


async def _attach_conformance_advisory(
    db: AsyncSession, id_org: int, id_playlist: int | None, response: Response | None
) -> None:
    """Conformité §XVI.2 à l'AFFECTATION (bug : n'était câblé qu'à la publication playlist).

    Affecter une playlist déjà construite à une nouvelle audience (create/update/targets)
    doit déclencher l'alerte + l'auto-rendition FHD pour ses médias trop lourds, comme
    l'ajout d'un média 4K à une playlist déjà affectée. À appeler APRÈS commit (audience
    à jour). Advisory exposé en en-tête `X-Rendition-Advisory` (même protocole que publish).
    """
    if id_playlist is None:
        return
    from app.api.v1.endpoints.playlists import conformance_check_playlist
    advisories = await conformance_check_playlist(db, id_org, id_playlist)
    if advisories and response is not None:
        response.headers["X-Rendition-Advisory"] = json.dumps(advisories)


# ── helpers ────────────────────────────────────────────────────────────────────

async def _get_schedule_detail(db: AsyncSession, schedule: CmsSchedule) -> ScheduleDetail:
    rules = list(
        (
            await db.execute(
                select(CmsScheduleRule).where(
                    CmsScheduleRule.id_schedule == schedule.id_schedule
                )
            )
        ).scalars().all()
    )
    targets = list(
        (
            await db.execute(
                select(CmsScheduleTarget).where(
                    CmsScheduleTarget.id_schedule == schedule.id_schedule
                )
            )
        ).scalars().all()
    )
    baskets: list[ScheduleBasketRead] = []
    if schedule.schedule_kind == "audio":
        rows = (await db.execute(
            select(CmsScheduleBasket, CmsAudioBasket.basket_name, CmsAudioBasket.basket_type)
            .join(CmsAudioBasket, CmsAudioBasket.id_basket == CmsScheduleBasket.id_basket)
            .where(CmsScheduleBasket.id_schedule == schedule.id_schedule)
        )).all()
        baskets = [
            ScheduleBasketRead(id_basket=sb.id_basket, mix_ratio=sb.mix_ratio,
                               basket_name=name, basket_type=btype)
            for sb, name, btype in rows
        ]
    playlist_name = None
    if schedule.id_playlist is not None:
        playlist_name = (await db.execute(
            select(CmsPlaylist.playlist_name)
            .where(CmsPlaylist.id_playlist == schedule.id_playlist)
        )).scalar_one_or_none()
    return ScheduleDetail(
        **{c.name: getattr(schedule, c.name) for c in schedule.__table__.columns},
        playlist_name=playlist_name,
        rules=[ScheduleRuleRead.model_validate(r) for r in rules],
        targets=[ScheduleTargetRead.model_validate(t) for t in targets],
        baskets=baskets,
    )


# ── schedules CRUD ─────────────────────────────────────────────────────────────

@router.post(
    "/schedules/hdmi-check",
    response_model=HdmiCompatCheckResult,
    dependencies=[Depends(require_permission("schedules", "read"))],
)
async def hdmi_compat_check(
    body: HdmiCompatCheckRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HdmiCompatCheckResult:
    """Warning d'affectation HDMI-in : une playlist contenant un item `hdmi_input` ne le sert
    JAMAIS aux players sans entrée HDMI physique (gate au sync `_player_has_hdmi_input`) —
    l'UI prévient AVANT d'enregistrer. Rien n'est bloqué : le gate serveur reste l'autorité.
    ⚠️ Route FIXE déclarée avant les routes paramétrées /schedules/{id}."""
    from app.api.v1.endpoints.ws import _player_has_hdmi_input  # import tardif (évite cycle)

    # Playlist directe + ses sous-playlists imbriquées (rotation) : un item HDMI
    # peut vivre dans un enfant.
    from app.services.playlist_rotation import child_playlist_ids
    pl_ids = {body.id_playlist}
    for childs in (await child_playlist_ids(db, pl_ids)).values():
        pl_ids |= childs
    has_hdmi = (await db.execute(
        select(CmsPlaylistItem.id_item)
        .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media)
        .where(
            CmsPlaylistItem.id_playlist.in_(pl_ids),
            CmsMedia.media_type == "hdmi_input",
            CmsMedia.is_deleted == False,  # noqa: E712
        )
        .limit(1)
    )).first() is not None
    if not has_hdmi or not body.targets:
        return HdmiCompatCheckResult(has_hdmi_item=has_hdmi)
    targets = [Target(target_type=t.target_type, target_id=t.target_id) for t in body.targets]
    pids = await players_for_targets(db, current_user.id_org, targets)
    if not pids:
        return HdmiCompatCheckResult(has_hdmi_item=True)
    rows = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player.in_(pids))
    )).scalars().all()
    bad = sorted(
        (HdmiIncompatiblePlayer(id_player=p.id_player, player_name=p.player_name)
         for p in rows if not _player_has_hdmi_input(p)),
        key=lambda x: (x.player_name or "").lower(),
    )
    return HdmiCompatCheckResult(has_hdmi_item=True, incompatible_players=bad)


@router.post(
    "/schedules/governance-scope",
    response_model=GovernanceScopeResult,
    dependencies=[Depends(require_permission("schedules", "read"))],
)
async def governance_scope_check(
    body: GovernanceScopeRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GovernanceScopeResult:
    """D-b — « combien d'écrans mes cibles retiennent-elles dans ce nœud ? » (avant d'enregistrer).

    Même service que la validation D3 : l'aperçu ne peut pas annoncer autre chose que ce que
    l'enregistrement acceptera. Sans cible → tout le périmètre du nœud (D0).
    ⚠️ Route FIXE déclarée avant les routes paramétrées /schedules/{id}.
    """
    node = await _governed_node(db, current_user.id_org, body.id_governance_node)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Governance node not found")
    perimeter = await node_perimeter_players(db, current_user.id_org, node)
    tgts = [(t.target_type, t.target_id) for t in body.targets]
    kept = await resolved_players(db, current_user.id_org, node, tgts, perimeter=perimeter)
    bad = await out_of_scope_targets(
        db, current_user.id_org, node, tgts, perimeter=perimeter)
    names = dict((await db.execute(
        select(CmsPlayer.id_player, CmsPlayer.player_name)
        .where(CmsPlayer.id_player.in_(kept or {-1}))
    )).all())
    return GovernanceScopeResult(
        node_name=node.node_name,
        perimeter_count=len(perimeter),
        resolved_count=len(kept),
        target_count=len(tgts),
        players=sorted(
            (GovernanceScopePlayer(id_player=pid, player_name=names.get(pid)) for pid in kept),
            key=lambda p: (p.player_name or "").lower(),
        ),
        out_of_scope=[GovernanceScopeTarget(**b) for b in bad],
        # D4 : avertir quand le ciblage coupe un groupe de synchro LAN — jamais bloquer.
        lan_sync_split=[GovernanceScopeLanSplit(**g)
                        for g in await lan_sync_split(db, current_user.id_org, kept)],
    )


@router.post(
    "/schedules",
    response_model=ScheduleDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("schedules", "create"))],
)
async def create_schedule(
    body: ScheduleCreate,
    response: Response,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduleDetail:
    if body.schedule_kind == "audio":
        if not body.baskets:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="At least one basket is required for an audio schedule")
        ids = [b.id_basket for b in body.baskets]
        valid = set((await db.execute(
            select(CmsAudioBasket.id_basket).where(
                CmsAudioBasket.id_basket.in_(ids),
                CmsAudioBasket.id_org == current_user.id_org,
            )
        )).scalars().all())
        invalid = set(ids) - valid
        if invalid:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Baskets not found: {sorted(invalid)}")
    else:
        if body.id_playlist is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="id_playlist is required for a content schedule")
        # Existence + ORG + visibilité gouvernance (l'ancien contrôle ne vérifiait que
        # l'existence : la playlist d'une autre organisation passait en 201).
        await _validate_playlist_choice(db, current_user, body.id_playlist)

    # Chaque cible existe dans l'org — un identifiant inexistant ou étranger est refusé
    # en 422 nommé, plus jamais enregistré en silence.
    await _validate_targets_exist(db, current_user.id_org, body.targets)

    # Gouvernance §XVIII : valider le nœud + vérifier que l'utilisateur l'administre.
    await _validate_node_org(db, current_user, body.id_governance_node)
    await _enforce_node_edit(db, current_user, body.id_governance_node)
    # D-b/D3 : sous gouvernance, une cible qui ne retient aucun écran du nœud est refusée ici.
    await _enforce_targets_in_scope(
        db, current_user.id_org, body.id_governance_node, body.targets)

    sched = CmsSchedule(
        id_org=current_user.id_org,
        schedule_name=body.schedule_name,
        schedule_kind=body.schedule_kind,
        id_playlist=body.id_playlist if body.schedule_kind == "content" else None,
        id_governance_node=body.id_governance_node,
        priority=body.priority,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        created_by=current_user.id_user,
    )
    db.add(sched)
    await db.flush()

    if body.schedule_kind == "audio":
        for b in body.baskets:
            db.add(CmsScheduleBasket(id_schedule=sched.id_schedule,
                                     id_basket=b.id_basket, mix_ratio=b.mix_ratio))

    for rule_data in body.rules:
        db.add(
            CmsScheduleRule(
                id_schedule=sched.id_schedule,
                **rule_data.model_dump(),
            )
        )

    for target_data in body.targets:
        db.add(
            CmsScheduleTarget(
                id_schedule=sched.id_schedule,
                target_type=target_data.target_type,
                target_id=target_data.target_id,
            )
        )

    db.add(_audit(current_user, "create", sched,
                  {"targets": _targets_detail(body.targets)}))
    await db.commit()
    await db.refresh(sched)
    detail = await _get_schedule_detail(db, sched)
    if sched.schedule_kind == "content":
        await _attach_conformance_advisory(db, sched.id_org, sched.id_playlist, response)
    await _push_sync(
        await _resolve_player_ids(body.targets, sched.id_org, db)
        | await _governed_sync_ids(db, sched)
    )
    return detail


@router.get(
    "/schedules",
    response_model=list[ScheduleRead],
    dependencies=[Depends(require_permission("schedules", "read"))],
)
async def list_schedules(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    kind: Optional[str] = Query(None, description="Filter: content | audio"),
    target_type: Optional[str] = Query(
        None, pattern="^(player|player_group|group|site|site_group)$",
        description="Target filter: keep only schedules that reach this target",
    ),
    target_id: Optional[int] = Query(None),
    id_governance_node: Optional[int] = Query(
        None, description="Keep only schedules attached to this governance node",
    ),
) -> list[ScheduleRead]:
    stmt = (
        select(CmsSchedule)
        .where(CmsSchedule.is_deleted == False)  # noqa: E712
        .order_by(CmsSchedule.priority.desc(), CmsSchedule.created_at.desc())
    )
    if kind in ("content", "audio"):
        stmt = stmt.where(CmsSchedule.schedule_kind == kind)
    # G4 (audit 2026-07-27) : la liste des programmes n'avait AUCUN filtre de gouvernance —
    # un « programmeur de gouvernance » voyait, et pouvait cibler, tous les programmes de
    # l'org (défaut N3). Le filtre ne s'applique qu'aux porteurs d'un rôle de gouvernance :
    # les rôles classiques gardent leur vue d'organisation (décision G4 d'Ed).
    _gov_clause, _visible, _ = await content_visibility(db, current_user)
    _clause = _gov_clause(CmsSchedule.id_governance_node)
    if _clause is not None and await user_is_governance_scoped(db, current_user):
        stmt = stmt.where(_clause)
    if id_governance_node is not None:
        # Espace de travail du nœud : « qu'est-ce qui DIFFUSE sur ce nœud ? ». C'est la
        # question que l'écran doit savoir poser — rattacher une playlist à un nœud ne la
        # diffuse pas, c'est le PROGRAMME rattaché qui le fait (malentendu vécu le 25/07).
        stmt = stmt.where(CmsSchedule.id_governance_node == id_governance_node)
    schedules = list((await db.execute(stmt.offset(skip).limit(limit))).scalars().all())

    # Filtre cible (calendrier unifié) : programme retenu si l'ensemble de players
    # résolu de SES cibles intersecte celui de la cible-filtre. Résolutions mises en
    # cache par (type, id) — les mêmes groupes reviennent sur plusieurs programmes.
    if schedules and target_type is not None and target_id is not None:
        fset = await players_for_targets(db, current_user.id_org, [
            Target(target_type=target_type, target_id=target_id)
        ])
        trows = (await db.execute(
            select(CmsScheduleTarget).where(
                CmsScheduleTarget.id_schedule.in_([s.id_schedule for s in schedules])
            )
        )).scalars().all()
        cache: dict[tuple[str, int], set[int]] = {}
        keep: set[int] = set()
        for tg in trows:
            key = (tg.target_type, tg.target_id)
            if key not in cache:
                cache[key] = await players_for_targets(db, current_user.id_org, [
                    Target(target_type=tg.target_type, target_id=tg.target_id)
                ])
            if cache[key] & fset:
                keep.add(tg.id_schedule)
        schedules = [s for s in schedules if s.id_schedule in keep]

    if not schedules:
        return []

    # Règles batchées (1 requête) → peuplées sur chaque ScheduleRead pour la vue calendrier.
    ids = [s.id_schedule for s in schedules]
    rules_by_sched: dict[int, list[ScheduleRuleRead]] = {}
    for r in (await db.execute(
        select(CmsScheduleRule).where(CmsScheduleRule.id_schedule.in_(ids))
    )).scalars().all():
        rules_by_sched.setdefault(r.id_schedule, []).append(ScheduleRuleRead.model_validate(r))

    # Noms de playlist batchés (1 requête) — résolus SERVEUR, la liste client est paginée.
    pl_ids = {s.id_playlist for s in schedules if s.id_playlist is not None}
    pl_names: dict[int, str] = dict((await db.execute(
        select(CmsPlaylist.id_playlist, CmsPlaylist.playlist_name)
        .where(CmsPlaylist.id_playlist.in_(pl_ids))
    )).all()) if pl_ids else {}

    return [
        ScheduleRead(
            **{c.name: getattr(s, c.name) for c in s.__table__.columns},
            playlist_name=pl_names.get(s.id_playlist) if s.id_playlist is not None else None,
            rules=rules_by_sched.get(s.id_schedule, []),
        )
        for s in schedules
    ]


@router.get(
    "/schedules/{id_schedule}",
    response_model=ScheduleDetail,
    dependencies=[Depends(require_permission("schedules", "read"))],
)
async def get_schedule(
    id_schedule: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduleDetail:
    sched = await _get_visible_schedule(db, current_user, id_schedule)
    return await _get_schedule_detail(db, sched)


@router.put(
    "/schedules/{id_schedule}",
    response_model=ScheduleRead,
    dependencies=[Depends(require_permission("schedules", "edit"))],
)
async def update_schedule(
    id_schedule: int,
    body: ScheduleUpdate,
    response: Response,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsSchedule:
    sched = await _get_visible_schedule(db, current_user, id_schedule)

    # Gouvernance §XVIII : il faut administrer le nœud ACTUEL pour modifier ce programme.
    await _enforce_node_edit(db, current_user, sched.id_governance_node)
    # Périmètre d'AVANT : un déplacement de nœud doit aussi faire recompiler l'ancien.
    old_governed = await _governed_sync_ids(db, sched)
    # Changement de nœud : valider le nœud cible + l'administrer aussi (déplacement contrôlé).
    if "id_governance_node" in body.model_fields_set:
        await _validate_node_org(db, current_user, body.id_governance_node)
        await _enforce_node_edit(db, current_user, body.id_governance_node)
        # N2 : détacher (→ NULL) fait sortir le programme du contrôle EN GARDANT ses cibles
        # (il diffuserait hors gouvernance) — ancêtre STRICT ou org-admin seulement.
        if body.id_governance_node is None and not await user_can_detach_from_node(
            db, current_user, sched.id_governance_node
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an administrator of a parent node can detach "
                       "this schedule from governance",
            )
        sched.id_governance_node = body.id_governance_node

    if body.schedule_name is not None:
        sched.schedule_name = body.schedule_name
    if body.id_playlist is not None:
        # 🔴 Aucun contrôle jusqu'à l'audit menu ⑧ : inexistante, supprimée ou d'une
        # autre org, la playlist passait en 200 — l'écran cassait à la synchro suivante.
        await _validate_playlist_choice(db, current_user, body.id_playlist)
        sched.id_playlist = body.id_playlist
    if body.priority is not None:
        sched.priority = body.priority
    if body.is_active is not None:
        sched.is_active = body.is_active
    # Plage de validité : model_fields_set distingue « non fourni » de « effacé (null) ».
    if "valid_from" in body.model_fields_set:
        sched.valid_from = body.valid_from
    if "valid_until" in body.model_fields_set:
        sched.valid_until = body.valid_until

    # Remplace intégralement les règles de récurrence si fournies
    if body.rules is not None:
        await db.execute(
            delete(CmsScheduleRule).where(CmsScheduleRule.id_schedule == id_schedule)
        )
        for rule_data in body.rules:
            db.add(CmsScheduleRule(id_schedule=id_schedule, **rule_data.model_dump()))

    # Remplace intégralement les paniers du schedule audio si fournis
    if body.baskets is not None:
        await db.execute(
            delete(CmsScheduleBasket).where(CmsScheduleBasket.id_schedule == id_schedule)
        )
        for b in body.baskets:
            db.add(CmsScheduleBasket(id_schedule=id_schedule, id_basket=b.id_basket, mix_ratio=b.mix_ratio))

    targets = list((await db.execute(
        select(CmsScheduleTarget).where(CmsScheduleTarget.id_schedule == id_schedule)
    )).scalars().all())

    db.add(_audit(current_user, "update", sched,
                  {"changes": sorted(body.model_fields_set)}))
    await db.commit()
    await db.refresh(sched)
    if sched.schedule_kind == "content":
        await _attach_conformance_advisory(db, sched.id_org, sched.id_playlist, response)
    await _push_sync(
        await _resolve_player_ids(targets, sched.id_org, db)
        | old_governed | await _governed_sync_ids(db, sched)
    )
    return sched


@router.put(
    "/schedules/{id_schedule}/targets",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("schedules", "edit"))],
)
async def update_schedule_targets(
    id_schedule: int,
    body: list[ScheduleTargetCreate],
    response: Response,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    sched = await _get_visible_schedule(db, current_user, id_schedule)
    await _enforce_node_edit(db, current_user, sched.id_governance_node)
    await _validate_targets_exist(db, sched.id_org, body)
    await _enforce_targets_in_scope(db, sched.id_org, sched.id_governance_node, body)

    old_targets = list((await db.execute(
        select(CmsScheduleTarget).where(CmsScheduleTarget.id_schedule == id_schedule)
    )).scalars().all())
    old_pids = await _resolve_player_ids(old_targets, sched.id_org, db)
    # Périmètre du nœud AVANT commit : les écrans retirés du ciblage doivent recompiler.
    governed_pids = await _governed_sync_ids(db, sched)

    await db.execute(
        delete(CmsScheduleTarget).where(CmsScheduleTarget.id_schedule == id_schedule)
    )
    for t in body:
        db.add(CmsScheduleTarget(
            id_schedule=id_schedule,
            target_type=t.target_type,
            target_id=t.target_id,
        ))
    db.add(_audit(current_user, "update_targets", sched, {
        "old_targets": _targets_detail(old_targets),
        "new_targets": _targets_detail(body),
    }))
    await db.commit()
    if sched.schedule_kind == "content":
        await _attach_conformance_advisory(db, sched.id_org, sched.id_playlist, response)
    new_pids = await _resolve_player_ids(body, sched.id_org, db)
    await _push_sync(old_pids | new_pids | governed_pids)


@router.delete(
    "/schedules/{id_schedule}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("schedules", "delete"))],
)
async def delete_schedule(
    id_schedule: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    sched = await _get_visible_schedule(db, current_user, id_schedule)
    await _enforce_node_edit(db, current_user, sched.id_governance_node)

    targets = list((await db.execute(
        select(CmsScheduleTarget).where(CmsScheduleTarget.id_schedule == id_schedule)
    )).scalars().all())
    governed_pids = await _governed_sync_ids(db, sched)

    sched.is_deleted = True
    # La ligne survit (suppression logique) mais le journal porte QUI a supprimé, et
    # quels écrans étaient visés au moment du geste.
    db.add(_audit(current_user, "delete", sched,
                  {"targets": _targets_detail(targets)}))
    await db.commit()
    await _push_sync(await _resolve_player_ids(targets, sched.id_org, db) | governed_pids)


# ── résolution priorité player ─────────────────────────────────────────────────

async def _assert_player_readable(
    db: AsyncSession, user: CmsUser, id_player: int
) -> CmsPlayer:
    """Le player existe dans MON org ET dans mon périmètre de gouvernance — 404 sinon.

    🔴 Audit menu ⑧ : `/players/{id}/current-schedule` et `/simulate-day` vivent dans ce
    fichier et avaient échappé aux 7 surfaces de la Phase 2 (N8) — un admin de nœud
    lisait la programmation (noms de programmes, playlists, paniers) de n'importe quel
    écran de l'org ; et `simulate-day` travaillait avec l'org DU PLAYER, pas celle de
    l'appelant — un player étranger était simulé (la RLS le rattrape en prod, mais elle
    est un filet, pas la garde : leçon des installs client en superuser).
    Même service `user_player_scope()` que `/players/` — UNE seule définition du périmètre.
    """
    player = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_player == id_player,
            CmsPlayer.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    scope = await user_player_scope(db, user)
    if scope is not None and id_player not in scope:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return player


@router.get(
    "/players/{id_player}/current-schedule",
    response_model=CurrentScheduleResponse,
    dependencies=[Depends(require_permission("schedules", "read"))],
)
async def get_current_schedule(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentScheduleResponse:
    await _assert_player_readable(db, current_user, id_player)
    # Heure locale du SITE de l'écran, cohérente avec ws.py (P2.2). Avant : SIGNFLOW_TZ
    # pour tout le parc — cet endroit répondait donc « ce qui diffuse » d'après l'horloge
    # du serveur, pas celle de l'écran.
    tz_name = await player_tz_name(db, id_player)
    now = local_now(tz_name)

    # Schedules ciblant ce player directement, triés par priorité DESC
    targets_result = await db.execute(
        select(CmsScheduleTarget).where(
            CmsScheduleTarget.target_type == "player",
            CmsScheduleTarget.target_id == id_player,
        )
    )
    schedule_ids = [t.id_schedule for t in targets_result.scalars().all()]

    if not schedule_ids:
        return {
            "id_player": id_player,
            "id_schedule": None,
            "id_playlist": None,
            "playlist_name": None,
            "schedule_name": None,
            "priority": None,
            "resolved_at": now,
            "timezone": tz_name,
        }

    schedules_result = await db.execute(
        select(CmsSchedule).where(
            CmsSchedule.id_schedule.in_(schedule_ids),
            CmsSchedule.schedule_kind == "content",
            CmsSchedule.is_active == True,  # noqa: E712
            CmsSchedule.is_deleted == False,  # noqa: E712
        ).order_by(CmsSchedule.priority.desc())
    )
    schedules = list(schedules_result.scalars().all())

    for sched in schedules:
        rules_result = await db.execute(
            select(CmsScheduleRule).where(
                CmsScheduleRule.id_schedule == sched.id_schedule
            )
        )
        rules = list(rules_result.scalars().all())

        # Un schedule sans règle ne s'active pas
        if not rules:
            continue

        # Enveloppe de dates du programme, attribuée au jour de début (fenêtre nocturne).
        if any(schedule_active_at(time_rule_from_schedule(r), sched.valid_from, sched.valid_until, now)
               for r in rules):
            pl = (
                await db.execute(
                    select(CmsPlaylist).where(CmsPlaylist.id_playlist == sched.id_playlist)
                )
            ).scalar_one_or_none()
            return {
                "id_player": id_player,
                "id_schedule": sched.id_schedule,
                "id_playlist": sched.id_playlist,
                "playlist_name": pl.playlist_name if pl else None,
                "schedule_name": sched.schedule_name,
                "priority": sched.priority,
                "resolved_at": now,
                "timezone": tz_name,
            }

    return {
        "id_player": id_player,
        "id_schedule": None,
        "id_playlist": None,
        "playlist_name": None,
        "schedule_name": None,
        "priority": None,
        "resolved_at": now,
        "timezone": tz_name,
    }


@router.post(
    "/players/{id_player}/simulate-day",
    response_model=PlayerDaySimResult,
    dependencies=[Depends(require_permission("schedules", "read"))],
)
async def simulate_player_day(
    id_player: int,
    body: PlayerDaySimRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerDaySimResult:
    """Simulation de la programmation COMPLÈTE d'un player sur une journée.

    Rejoue la résolution réelle (event > programme par priorité, dates de validité,
    règles jours/horaires), heure locale SIGNFLOW_TZ, en deux pistes :
      - Vidéo : quel programme / event → quelle playlist est actif à chaque créneau ;
      - Audio : quels paniers sont mixés à chaque créneau.
    Vue « programmes » (niveau résolution) ; le détail des items se déroule via la
    simulation playlist (POST /playlists/{id}/simulate-timeline).
    """
    player = await _assert_player_readable(db, current_user, id_player)
    # L'org de l'APPELANT (== celle du player, garantie par la garde ci-dessus). L'ancien
    # code prenait `player.id_org` sans vérifier l'appelant : simulation cross-org.
    id_org = current_user.id_org

    try:
        day = date_cls.fromisoformat(body.date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid date")
    weekday = day.weekday()
    # Le jour simulé est celui de l'ÉCRAN (P2.2) : les bornes UTC des events se ramènent
    # au fuseau de son site, sinon la simulation décale toute la journée.
    tz = ZoneInfo(await player_tz_name(db, id_player))
    eod = datetime.combine(day, time_cls(23, 59, 59))

    def _utc_to_local_naive(dt: datetime) -> datetime:
        return dt.astimezone(tz).replace(tzinfo=None)

    group_ids = {row[0] for row in (await db.execute(
        select(CmsPlayerGroupMember.id_group).where(CmsPlayerGroupMember.id_player == id_player)
    )).all()}

    # Schedules ciblant le player (player + groupes + groupes de sites)
    tgt = [(CmsScheduleTarget.target_type == "player") & (CmsScheduleTarget.target_id == id_player)]
    if group_ids:
        tgt.append((CmsScheduleTarget.target_type == "player_group") & (CmsScheduleTarget.target_id.in_(group_ids)))
    id_site = await player_site_id(db, id_player)
    if id_site is not None:
        tgt.append((CmsScheduleTarget.target_type == "site") & (CmsScheduleTarget.target_id == id_site))
    site_group_ids = await player_site_group_ids(db, id_player)
    if site_group_ids:
        tgt.append((CmsScheduleTarget.target_type == "site_group") & (CmsScheduleTarget.target_id.in_(site_group_ids)))
    sched_ids = {row[0] for row in (await db.execute(
        select(CmsScheduleTarget.id_schedule).where(or_(*tgt))
    )).all()}

    content_scheds: list[dict] = []
    audio_scheds: list[dict] = []
    if sched_ids:
        rules_by: dict[int, list] = {}
        for r in (await db.execute(
            select(CmsScheduleRule).where(CmsScheduleRule.id_schedule.in_(sched_ids))
        )).scalars().all():
            rules_by.setdefault(r.id_schedule, []).append(time_rule_from_schedule(r))
        baskets_by: dict[int, list] = {}
        for sb, bname, btype, tmode, tval in (await db.execute(
            select(CmsScheduleBasket, CmsAudioBasket.basket_name, CmsAudioBasket.basket_type,
                   CmsAudioBasket.trigger_mode, CmsAudioBasket.trigger_value)
            .join(CmsAudioBasket, CmsAudioBasket.id_basket == CmsScheduleBasket.id_basket)
            .where(CmsScheduleBasket.id_schedule.in_(sched_ids))
        )).all():
            baskets_by.setdefault(sb.id_schedule, []).append(
                {"id_basket": sb.id_basket, "name": bname, "type": btype, "ratio": sb.mix_ratio,
                 "trigger_mode": tmode, "trigger_value": tval})
        for sched, plname in (await db.execute(
            select(CmsSchedule, CmsPlaylist.playlist_name)
            .join(CmsPlaylist, CmsPlaylist.id_playlist == CmsSchedule.id_playlist, isouter=True)
            .where(CmsSchedule.id_schedule.in_(sched_ids),
                   CmsSchedule.is_active == True,  # noqa: E712
                   CmsSchedule.is_deleted == False)  # noqa: E712
        )).all():
            entry = {"name": sched.schedule_name, "priority": sched.priority,
                     "vf": sched.valid_from, "vu": sched.valid_until,
                     "rules": rules_by.get(sched.id_schedule, [])}
            if sched.schedule_kind == "audio":
                entry["baskets"] = baskets_by.get(sched.id_schedule, [])
                audio_scheds.append(entry)
            else:
                entry["id_playlist"] = sched.id_playlist
                entry["playlist_name"] = plname
                content_scheds.append(entry)

    # Events programmés (content + audio) ciblant le player
    content_events: list[dict] = []
    audio_events: list[dict] = []
    ev_pl = aliased(CmsPlaylist)
    ev_basket = aliased(CmsAudioBasket)
    for ev in (await db.execute(
        select(
            CmsEvent.event_name, CmsEvent.priority, CmsEvent.event_kind,
            CmsEvent.trigger_config, CmsEvent.target_config,
            CmsEvent.valid_from, CmsEvent.valid_until, CmsEvent.not_before_utc,
            CmsEvent.id_playlist, ev_pl.playlist_name,
            CmsEvent.id_basket, ev_basket.basket_name, ev_basket.basket_type, CmsEvent.mix_ratio,
            ev_basket.trigger_mode, ev_basket.trigger_value,
        )
        .join(ev_pl, ev_pl.id_playlist == CmsEvent.id_playlist, isouter=True)
        .join(ev_basket, ev_basket.id_basket == CmsEvent.id_basket, isouter=True)
        .where(CmsEvent.id_org == id_org, CmsEvent.event_type == "scheduled",
               CmsEvent.status != "cancelled")
    )).all():
        # Matcher CANONIQUE (les deux formes de target_config, dont l'hétérogène) — ce
        # bloc était un matcher inline dupliqué, la classe de risque « couverture » : un
        # type ajouté au canonique mais pas ici aurait rendu le calendrier menteur.
        if not event_target_matches(
            ev.target_config, id_player, group_ids, site_group_ids, id_site=id_site
        ):
            continue
        lo_bounds = [_utc_to_local_naive(b) for b in (ev.valid_from, ev.not_before_utc) if b]
        lo = max(lo_bounds) if lo_bounds else None
        hi = _utc_to_local_naive(ev.valid_until) if ev.valid_until else None
        for cond in (ev.trigger_config or {}).get("conditions", []):
            if cond.get("source") != "schedule":
                continue
            dows = cond.get("days_of_week")
            if dows is not None and weekday not in dows:
                continue
            ts, te = cond.get("time_start"), cond.get("time_end")
            if ts and te:
                sh, sm = (int(x) for x in ts.split(":")[:2])
                eh, em = (int(x) for x in te.split(":")[:2])
                s_local = datetime.combine(day, time_cls(sh, sm))
                e_local = datetime.combine(day, time_cls(eh, em)) if (sh, sm) <= (eh, em) else eod
            else:
                s_local, e_local = datetime.combine(day, time_cls(0, 0)), eod
            if lo and s_local < lo:
                s_local = lo
            if hi and e_local > hi:
                e_local = hi
            if s_local >= e_local:
                continue
            if ev.event_kind == "audio":
                audio_events.append({"start": s_local, "end": e_local, "priority": ev.priority,
                                     "name": ev.event_name, "id_basket": ev.id_basket,
                                     "basket_name": ev.basket_name, "basket_type": ev.basket_type,
                                     "ratio": ev.mix_ratio or 50,
                                     "trigger_mode": ev.trigger_mode, "trigger_value": ev.trigger_value})
            else:
                content_events.append({"start": s_local, "end": e_local, "priority": ev.priority,
                                       "name": ev.event_name, "id_playlist": ev.id_playlist,
                                       "playlist_name": ev.playlist_name})

    # Échantillonnage minute par minute (1440), puis fusion des minutes contiguës
    video_keys: list = [None] * 1440
    audio_keys: list = [None] * 1440
    audio_meta: dict = {}
    for m in range(1440):
        t = datetime.combine(day, time_cls(m // 60, m % 60))
        active_ev = [e for e in content_events if e["start"] <= t < e["end"]]
        if active_ev:
            e = max(active_ev, key=lambda x: x["priority"])
            video_keys[m] = ("event", e["name"], e.get("id_playlist"), e.get("playlist_name"), e["priority"])
        else:
            cand = [s for s in content_scheds
                    if any(schedule_active_at(r, s["vf"], s["vu"], t) for r in s["rules"])]
            if cand:
                s = max(cand, key=lambda x: x["priority"])
                video_keys[m] = ("schedule", s["name"], s.get("id_playlist"), s.get("playlist_name"), s["priority"])

        # message/emergency = déclenchés sur site (imprévisibles) → EXCLUS de la sim audio.
        # On ne montre que les couches continues (music/ambiance) et les jingles (cadence).
        bmap: dict = {}
        for s in audio_scheds:
            if any(schedule_active_at(r, s["vf"], s["vu"], t) for r in s["rules"]):
                for b in s["baskets"]:
                    if b["type"] in ("message", "emergency"):
                        continue
                    prev = bmap.get(b["id_basket"])
                    if prev is None or s["priority"] > prev[1]:
                        bmap[b["id_basket"]] = (b["ratio"], s["priority"], "schedule", b["name"], b["type"],
                                                b.get("trigger_mode"), b.get("trigger_value"))
        # Events audio : priorité décalée (+1000) → priment sur les programmes par panier
        # (miroir de _resolve_audio_baskets).
        for e in audio_events:
            if e["start"] <= t < e["end"] and e["id_basket"]:
                if e["basket_type"] in ("message", "emergency"):
                    continue
                pr = 1000 + e["priority"]
                prev = bmap.get(e["id_basket"])
                if prev is None or pr > prev[1]:
                    bmap[e["id_basket"]] = (e["ratio"], pr, "event", e["basket_name"], e["basket_type"],
                                            e.get("trigger_mode"), e.get("trigger_value"))
        # Clamp du budget mix ≤100 % SÉPARÉ PAR FAMILLE : music et ambiance sont 2 couches
        # simultanées, chacune son budget propre (miroir de _resolve_audio_baskets). Trié
        # par priorité décroissante, on droppe les plus basses qui dépassent 100 % DANS LEUR
        # famille. message/jingle/emergency ne consomment aucun budget (jamais droppés).
        if bmap:
            final: dict = {}
            mix_total = {"music": 0, "ambiance": 0}
            for bid, v in sorted(bmap.items(), key=lambda kv: kv[1][1], reverse=True):
                if v[4] in mix_total:
                    if mix_total[v[4]] + v[0] > 100:
                        continue
                    mix_total[v[4]] += v[0]
                final[bid] = v
            if final:
                akey = tuple(sorted((bid, v[0], v[2]) for bid, v in final.items()))
                audio_keys[m] = akey
                audio_meta[akey] = final

    def _hm(m: int) -> str:
        return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"

    video: list[DaySimVideoSegment] = []
    i = 0
    while i < 1440:
        k = video_keys[i]
        j = i
        while j < 1440 and video_keys[j] == k:
            j += 1
        if k is not None:
            src, name, pid, pname, prio = k
            video.append(DaySimVideoSegment(start=_hm(i), end=_hm(j), source=src, name=name,
                                            id_playlist=pid, playlist_name=pname, priority=prio))
        i = j

    audio: list[DaySimAudioSegment] = []
    i = 0
    while i < 1440:
        k = audio_keys[i]
        j = i
        while j < 1440 and audio_keys[j] == k:
            j += 1
        if k is not None:
            bmap = audio_meta[k]
            baskets = [DaySimBasket(id_basket=bid, name=v[3], basket_type=v[4], ratio=v[0], source=v[2],
                                    trigger_mode=v[5], trigger_value=v[6])
                       for bid, v in sorted(bmap.items())]
            audio.append(DaySimAudioSegment(start=_hm(i), end=_hm(j), baskets=baskets))
        i = j

    return PlayerDaySimResult(
        id_player=id_player, date=body.date, timezone=str(tz), video=video, audio=audio
    )
