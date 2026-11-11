"""
Endpoints Playlists (§XIII).

POST   /playlists                       — créer
GET    /playlists                       — liste
GET    /playlists/{id}                  — détail + items
PUT    /playlists/{id}                  — mise à jour
DELETE /playlists/{id}                  — suppression logique
POST   /playlists/{id}/items            — ajouter un item
PUT    /playlists/{id}/items/reorder    — réordonner les items
DELETE /playlists/{id}/items/{id_item}  — retirer un item
GET    /playlists/{id}/items/{id_item}/rules — règles de diffusion de l'item
PUT    /playlists/{id}/items/{id_item}/rules — remplacer les règles (vide = perpétuel)
"""

import json
from datetime import UTC, date as date_cls, datetime, time as time_cls, timedelta
from typing import Annotated, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_permission
from app.models.audit_log import CmsAuditLog
from app.models.governance import CmsGovernanceNode
from app.models.media import CmsMedia, CmsMediaRendition
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroupMember
from app.models.template import CmsTemplate
from app.models.playlist import CmsPlaylist, CmsPlaylistItem, CmsPlaylistItemRule
from app.models.schedule import CmsSchedule, CmsScheduleTarget
from app.models.user import CmsUser
from app.services import approvals as approvals_svc
from app.services import document_media
from app.services import playlist_publish
from app.services import targeting
from app.services import governance_moderation
from app.services.governance_access import (
    content_visibility,
    media_visibility,
    editable_node_ids,
    effective_max_item_sec,
    node_requires_approval as _node_requires_approval_chain,
    user_can_detach_from_node,
    user_can_edit_node,
    user_is_governance_scoped,
    visible_node_ids,
)
from app.services.playlist_rotation import (
    rotation_seed,
    unroll_slots,
    validate_child_for_nesting,
)
from app.services.renditions import player_target_height
from app.schemas.playlist import (
    ItemRuleCreate,
    ItemRuleRead,
    PlaylistCreate,
    PlaylistDetail,
    PlaylistItemCreate,
    PlaylistItemRead,
    PlaylistItemsBulkCreate,
    PlaylistItemsReorder,
    PlaylistItemUpdate,
    PlaylistRead,
    PlaylistUpdate,
    SimulateTimelineRequest,
    SimulateTimelineResult,
    TimelineSegment,
)
from app.services.scheduling import TimeRule, rule_active_at

router = APIRouter(prefix="/playlists", tags=["playlists"])


# ── Gouvernance §XVIII du CONTENU (R14/R15) ──────────────────────────────────────

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


async def _enforce_playlist_node_edit(db: AsyncSession, current_user: CmsUser, pl: CmsPlaylist) -> None:
    """403 si l'utilisateur n'administre pas le nœud de la playlist (NULL = libre)."""
    if not await user_can_edit_node(db, current_user, pl.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer this playlist's governance node",
        )


async def _node_requires_approval(db: AsyncSession, id_node: int | None) -> bool:
    """True si le nœud exige la modération du contenu (D7).

    Délègue au helper partagé : la règle est **monotone vers le bas** depuis le 2026-07-26 —
    un ancêtre qui exige la modération l'impose à tout son sous-arbre, décocher plus bas n'y
    change rien. Sans ça, la modération du national ne tenait que si personne en dessous ne
    la retirait.
    """
    return await _node_requires_approval_chain(db, id_node)


async def _assert_playlist_visible(
    db: AsyncSession, user: CmsUser, pl: CmsPlaylist
) -> tuple[bool, set[int] | None]:
    """404 si `pl` est hors du périmètre de lecture de `user` (D6/G4).

    **Définition unique** de « cette playlist existe-t-elle pour moi ? ». Le détail portait
    déjà cette règle en dur ; trois routes satellites ne la portaient PAS (mesuré sur la
    prod le 2026-07-29) — `simulate-timeline` rendait la timeline complète, NOMS DES MÉDIAS
    COMPRIS, d'une playlist d'un nœud frère répondant 404 au détail, et les règles d'item
    de la même playlist se lisaient en 200. Le cloisonnement ne vaut que si toutes les
    portes portent la même règle.

    Renvoie (governed_readonly, editable) pour éviter un second appel aux appelants qui
    en ont besoin.
    """
    gov = pl.id_governance_node
    _clause, visible, editable = await content_visibility(db, user)
    if gov is not None and visible is not None and gov not in visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    if gov is None and await user_is_governance_scoped(db, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    return bool(gov is not None and editable is not None and gov not in editable), editable


async def _get_visible_playlist(
    db: AsyncSession, user: CmsUser, id_playlist: int
) -> CmsPlaylist:
    """Charge une playlist de l'org en appliquant le périmètre de LECTURE (404 sinon)."""
    pl = (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    await _assert_playlist_visible(db, user, pl)
    return pl


async def get_editable_playlist(
    id_playlist: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlaylist:
    """Dépendance : charge la playlist de l'org + enforce la garde de nœud (édition).

    Utilisée par toutes les mutations de brouillon (items, règles, rename…) pour qu'un
    responsable local ne modifie pas le contenu d'un nœud qu'il n'administre pas.
    """
    pl = (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == current_user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    await _enforce_playlist_node_edit(db, current_user, pl)
    return pl


async def _push_sync_org(db: AsyncSession, preemptive: bool = False) -> None:
    """Force-sync tous les players de l'org (RLS filtre automatiquement par org).

    `preemptive=False` (défaut) = bascule à la frontière d'item (graceful, pas
    d'interruption) — c'est ce qu'on veut à la publication d'une playlist."""
    result = await db.execute(select(CmsPlayer.id_player))
    player_ids = [row[0] for row in result.all()]
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    payload = json.dumps({"type": "player:force_sync", "preemptive": preemptive})
    async with r:
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)


async def _playlist_audience_min_height(db: AsyncSession, id_org: int, id_playlist: int) -> int | None:
    """Hauteur cible MIN des players qui jouent cette playlist (via schedules → cibles).

    Sert l'advisory de conformité §XVI.2 : si un média dépasse cette hauteur, le player
    le plus faible de l'audience devra le downscaler (4K qui rame). None = audience non
    résoluble (aucun schedule ne référence la playlist, ou players sans cible connue)
    → pas de contrôle.
    """
    sched_ids = (await db.execute(
        select(CmsSchedule.id_schedule).where(
            CmsSchedule.id_playlist == id_playlist,
            CmsSchedule.is_deleted == False,  # noqa: E712
        )
    )).scalars().all()
    if not sched_ids:
        return None
    target_rows = (await db.execute(
        select(CmsScheduleTarget.target_type, CmsScheduleTarget.target_id)
        .where(CmsScheduleTarget.id_schedule.in_(sched_ids))
    )).all()
    if not target_rows:
        return None
    targets = [targeting.Target(target_type=t, target_id=i) for t, i in target_rows]
    player_ids = await targeting.players_for_targets(db, id_org, targets)
    if not player_ids:
        return None
    players = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player.in_(player_ids))
    )).scalars().all()
    heights = [h for h in (player_target_height(p) for p in players) if h is not None]
    return min(heights) if heights else None


async def _autogen_media_fhd(db: AsyncSession, media: CmsMedia, min_h: int) -> dict | None:
    """Cœur de la conformité §XVI.2 pour UN média contre une cible déjà résolue : si le
    média vidéo dépasse `min_h`, lance la rendition FHD (si absente) + renvoie l'advisory.
    None si conforme ou non-vidéo. Idempotent (pas de double génération)."""
    if media.media_type != "video" or not media.height or media.height <= min_h:
        return None
    existing = (await db.execute(
        select(CmsMediaRendition.status).where(
            CmsMediaRendition.id_parent == media.id_media,
            CmsMediaRendition.profile == "fhd",
        )
    )).scalar_one_or_none()
    action = "exists"
    if existing not in ("ready", "processing"):
        from app.workers.media_worker import generate_rendition
        generate_rendition.delay(media.id_media, "fhd")
        action = "generating"
    return {
        "id_media": media.id_media,
        "media_height": media.height,
        "target_height": min_h,
        "action": action,  # "generating" (rendition lancée) | "exists" (déjà prête/en cours)
    }


async def _conformance_autogen(db: AsyncSession, id_org: int, id_playlist: int, media: CmsMedia) -> dict | None:
    """À l'affectation d'un média : si sa hauteur dépasse la cible min de l'audience de
    la playlist, lance auto une rendition FHD (si absente) et renvoie un advisory pour
    alerter l'UI (§XVI.2, décision Ed 2026-06-07 : alerte + auto-génération)."""
    if media.media_type != "video" or not media.height:
        return None
    min_h = await _playlist_audience_min_height(db, id_org, id_playlist)
    if min_h is None:
        return None
    return await _autogen_media_fhd(db, media, min_h)


async def conformance_check_playlist(db: AsyncSession, id_org: int, id_playlist: int) -> list[dict]:
    """Conformité §XVI.2 sur TOUTE une playlist contre son audience GLOBALE courante.

    Câblé à l'AFFECTATION (schedules create/update/targets) en plus de la publication :
    affecter une playlist 4K déjà construite à des players FHD doit déclencher l'alerte +
    l'auto-rendition, comme l'ajout d'un média 4K à une playlist déjà affectée. La cible
    est recalculée APRÈS commit (état d'affectation à jour). [] si audience non résoluble.
    """
    min_h = await _playlist_audience_min_height(db, id_org, id_playlist)
    if min_h is None:
        return []
    medias = (await db.execute(
        select(CmsMedia)
        .join(CmsPlaylistItem, CmsPlaylistItem.id_media == CmsMedia.id_media)
        .where(
            CmsPlaylistItem.id_playlist == id_playlist,
            CmsMedia.media_type == "video",
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalars().unique().all()
    advisories: list[dict] = []
    for media in medias:
        adv = await _autogen_media_fhd(db, media, min_h)
        if adv:
            advisories.append(adv)
    return advisories


# ── Staging / brouillon d'édition (#1, archi 2) ─────────────────────────────────
# cms_playlist_items = version PUBLIÉE (servie aux players, chemin de sync INCHANGÉ).
# draft_items (JSONB) = brouillon. Les endpoints d'édition écrivent le brouillon SANS
# pousser ; « Publier » applique le brouillon → items live (par DIFF, pour préserver
# les id_item existants donc leurs tags) + push graceful ; « Annuler » efface le brouillon.
# Forme d'une entrée : {uid, id_media, sort_order, duration_ms, zone_index,
# transition_type, rules:[...]} (uid = id réel si item existant, négatif si nouveau).

def _rule_to_draft(r: CmsPlaylistItemRule) -> dict:
    return {
        "rule_type": r.rule_type, "days_of_week": r.days_of_week,
        "time_start": r.time_start.isoformat() if r.time_start else None,
        "time_end": r.time_end.isoformat() if r.time_end else None,
        "date_from": r.date_from.isoformat() if r.date_from else None,
        "date_to": r.date_to.isoformat() if r.date_to else None,
        "scope": r.scope, "target_id": r.target_id,
    }


async def _serialize_live_to_draft(db: AsyncSession, id_playlist: int) -> list[dict]:
    """Items publiés (+ règles) → forme brouillon (init au 1er edit)."""
    items = list((await db.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == id_playlist)
        .order_by(CmsPlaylistItem.sort_order)
    )).scalars().all())
    rules_by_item: dict[int, list[dict]] = {}
    if items:
        for r in (await db.execute(
            select(CmsPlaylistItemRule)
            .where(CmsPlaylistItemRule.id_item.in_([i.id_item for i in items]))
            .order_by(CmsPlaylistItemRule.id_item_rule)
        )).scalars().all():
            rules_by_item.setdefault(r.id_item, []).append(_rule_to_draft(r))
    return [{
        "uid": it.id_item, "id_media": it.id_media, "sort_order": it.sort_order,
        "item_type": it.item_type, "id_child_playlist": it.id_child_playlist,
        "duration_ms": it.duration_ms, "zone_index": it.zone_index,
        "transition_type": it.transition_type, "doc_pages": it.doc_pages,
        "touch_navigation": it.touch_navigation,
        "touch_resume_after_s": it.touch_resume_after_s,
        "rules": rules_by_item.get(it.id_item, []),
    } for it in items]


async def _ensure_draft(db: AsyncSession, pl: CmsPlaylist) -> list[dict]:
    """Copie du brouillon (init depuis les items publiés si aucun brouillon)."""
    if pl.draft_items is None:
        return await _serialize_live_to_draft(db, pl.id_playlist)
    return [dict(e) for e in pl.draft_items]


def _next_uid(draft: list[dict]) -> int:
    return min((e["uid"] for e in draft), default=0) - 1


async def _child_info(db: AsyncSession, child_ids: set[int]) -> dict[int, dict]:
    """Enrichissement des items sub_playlist : nom, nb d'items, réglages rotation,
    durées agrégées de l'enfant (total connu + manquantes + nonconformes)."""
    if not child_ids:
        return {}
    out: dict[int, dict] = {}
    for pl in (await db.execute(
        select(CmsPlaylist).where(CmsPlaylist.id_playlist.in_(child_ids))
    )).scalars().all():
        out[pl.id_playlist] = {
            "name": pl.playlist_name, "rotation_mode": pl.rotation_mode,
            "items_per_turn": pl.items_per_turn, "is_deleted": pl.is_deleted,
            "count": 0, "total_ms": 0, "missing": 0, "nonconform": 0,
        }
    for pid, cnt, total, missing, nonconform in (await db.execute(
        select(
            CmsPlaylistItem.id_playlist,
            func.count(),
            # Durée effective document-aware (par page × pages) — cf. services/document_media.
            func.sum(document_media.item_duration_sql()),
            func.sum(document_media.item_duration_missing_sql()),
            func.sum(case((CmsMedia.conformance_status.in_(["playable", "nonconform"]), 1),
                          else_=0)),
        )
        .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media, isouter=True)
        .where(
            CmsPlaylistItem.id_playlist.in_(child_ids),
            CmsPlaylistItem.item_type == "media",
        )
        .group_by(CmsPlaylistItem.id_playlist)
    )).all():
        if pid in out:
            out[pid].update({
                "count": int(cnt or 0), "total_ms": int(total or 0),
                "missing": int(missing or 0), "nonconform": int(nonconform or 0),
            })
    return out


def _sub_flag_row(info: dict | None) -> tuple:
    """Ligne _sync_flags d'un item sub_playlist : durée = contribution moyenne par tour
    (K × durée moyenne de l'enfant) si toutes les durées enfant sont connues, sinon
    None (→ sync_ready False, comme un média sans durée)."""
    if not info or not info["count"] or info["missing"]:
        return (None, "nonconform" if info and info["nonconform"] else None, False, False)
    if info["rotation_mode"] in ("one_sequential", "one_random"):
        k = max(1, min(info["items_per_turn"], info["count"]))
        dur = int(info["total_ms"] * k / info["count"])
    else:
        dur = info["total_ms"]
    return (dur, "nonconform" if info["nonconform"] else None, False, False)


async def _draft_items_read(db: AsyncSession, id_playlist: int, draft: list[dict]) -> list[PlaylistItemRead]:
    media_ids = [e["id_media"] for e in draft if e.get("id_media")]
    media_map: dict[int, CmsMedia] = {}
    if media_ids:
        for m in (await db.execute(
            select(CmsMedia).where(CmsMedia.id_media.in_(media_ids))
        )).scalars().all():
            media_map[m.id_media] = m
    children = await _child_info(
        db, {e["id_child_playlist"] for e in draft if e.get("id_child_playlist")})
    now = datetime.now(UTC)
    out: list[PlaylistItemRead] = []
    for e in sorted(draft, key=lambda x: x["sort_order"]):
        ci = children.get(e.get("id_child_playlist") or 0)
        m = media_map.get(e["id_media"]) if e.get("id_media") else None
        out.append(PlaylistItemRead(
            id_item=e["uid"], id_playlist=id_playlist, id_media=e.get("id_media"),
            item_type=e.get("item_type", "media"),
            id_child_playlist=e.get("id_child_playlist"),
            sort_order=e["sort_order"], duration_ms=e.get("duration_ms"),
            zone_index=e.get("zone_index", 0), transition_type=e.get("transition_type", "cut"),
            doc_pages=e.get("doc_pages"),
            touch_navigation=bool(e.get("touch_navigation", False)),
            touch_resume_after_s=e.get("touch_resume_after_s"),
            media_page_count=m.page_count if m else None,
            created_at=now, updated_at=now,
            media_name=m.media_name if m else None,
            media_type=m.media_type if m else None,
            media_duration_ms=m.duration_ms if m else None,
            conformance_status=m.conformance_status if m else None,
            has_rules=bool(e.get("rules")),
            child_name=ci["name"] if ci else None,
            child_item_count=ci["count"] if ci else None,
            child_rotation_mode=ci["rotation_mode"] if ci else None,
            child_items_per_turn=ci["items_per_turn"] if ci else None,
        ))
    return out


def _settings_overlay(pl: CmsPlaylist) -> dict:
    """Réglages (rotation) tels que l'ÉDITEUR doit les voir : brouillon superposé au publié.

    Les LISTES, elles, montrent volontairement les colonnes publiées (= ce que jouent les
    écrans) et signalent l'écart par `has_draft_changes`."""
    return {
        k: playlist_publish.draft_setting(pl, k)
        for k in playlist_publish.DRAFT_SETTING_KEYS
    }


def _draft_find(draft: list[dict], uid: int) -> dict | None:
    return next((e for e in draft if e["uid"] == uid), None)


def _sync_flags(sync_mode: str, rows: list[tuple]) -> tuple[int, bool, bool, bool]:
    """rows = [(resolved_duration_ms|None, media_conf|None, has_rule, has_override)].
    Renvoie (total_ms, sync_ready, has_item_rules, sync_windowed). Logique commune au
    chemin publié et au brouillon."""
    total = 0
    missing = conf_blocks = any_rule = any_override = False
    for dur, conf, has_rule, has_override in rows:
        if dur is None:
            missing = True
        if conf in ("playable", "nonconform"):
            conf_blocks = True
        any_rule = any_rule or has_rule
        any_override = any_override or has_override
        total += dur or 10000
    sync_ready = (
        sync_mode == "wall_clock" and len(rows) > 0
        and not missing and not conf_blocks and not any_override
    )
    return total, sync_ready, any_rule, (sync_ready and any_rule)


async def _assert_template_visible(
    db: AsyncSession, user: CmsUser, id_template: int | None
) -> None:
    """Le template choisi doit être VISIBLE de l'utilisateur (D6/G4).

    Sans ça, le filtre de visibilité des templates n'existait que dans le sélecteur, donc
    côté client : mesuré le 2026-07-28, un responsable de site pouvait lier à sa playlist,
    en devinant l'identifiant, un template d'un nœud frère que `GET /templates/{id}` lui
    refusait en 404. Même famille que le défaut trouvé par Ed le 27/07 sur la gouvernance
    (« pointer son programme vers une playlist qu'il reçoit en 404 »).
    """
    if id_template is None:
        return
    tmpl = (await db.execute(
        select(CmsTemplate).where(
            CmsTemplate.id_template == id_template,
            CmsTemplate.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    _clause, visible, _editable = await content_visibility(db, user)
    node = tmpl.id_governance_node
    hidden = (
        (node is not None and visible is not None and node not in visible)
        or (node is None and await user_is_governance_scoped(db, user))
    )
    if hidden:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")


def _audit(user: CmsUser, action: str, id_playlist: int, detail: dict) -> CmsAuditLog:
    """Trace une opération de playlist (`cms_audit_log`).

    Aucune mutation directe n'était tracée : mesuré le 2026-07-29, les 111 lignes de type
    « playlist » de la prod viennent TOUTES des approbations §XXI et de l'assistant IA —
    créer, renommer, publier ou supprimer une playlist depuis l'écran ne laissait rien.
    Même constat que sur les templates la veille, et même conséquence : « qui a mis ça à
    l'antenne ? » restait sans réponse.

    🔑 On trace la PUBLICATION, pas chaque geste d'édition : le brouillon est un espace de
    travail privé, la publication est le moment où le contenu part sur les écrans — c'est
    celui-là qui engage.
    """
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="playlist", resource_id=str(id_playlist), source="cms", detail=detail,
    )


async def _assert_media_addable(
    db: AsyncSession, user: CmsUser, id_media: int
) -> CmsMedia:
    """Le média ajouté doit EXISTER, ne pas être supprimé, et être VISIBLE de l'utilisateur.

    Deux défauts mesurés en prod le 2026-07-29, tous deux invisibles depuis l'écran (le
    sélecteur ne propose que du bon) mais atteignables en postant l'identifiant :

    * un média **supprimé** était accepté (201) — l'item devenait un fantôme qui ne
      diffuserait jamais rien ; ⚠️ asymétrie révélatrice, l'ajout EN LOT filtrait déjà
      `is_deleted`, l'ajout unitaire non ;
    * la **visibilité de gouvernance** n'était pas vérifiée — même famille que le défaut
      trouvé par Ed le 27/07 (« pointer son programme vers une playlist qu'il reçoit en
      404 ») et que le template refermé le 28/07. Sans effet sur le parc d'aujourd'hui
      (0 média gouverné, mesuré), la garde est là pour que ça le reste.

    ⚠️ La visibilité suit celle de la MÉDIATHÈQUE (`media_visibility`), pas celle des
    playlists : depuis la décision d'Ed du 2026-07-29, un rôle de gouvernance LIT la
    médiathèque commune et peut donc la programmer, sans pouvoir la modifier. Utiliser
    `content_visibility` ici rendrait l'ajout plus strict que le sélecteur qui le propose.
    """
    gov_clause, _visible, _editable = await media_visibility(db, user)
    stmt = select(CmsMedia).where(
        CmsMedia.id_media == id_media,
        CmsMedia.id_org == user.id_org,
        CmsMedia.is_deleted == False,  # noqa: E712
    )
    clause = gov_clause(CmsMedia.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    media = (await db.execute(stmt)).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    return media


@router.post(
    "",
    response_model=PlaylistRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("playlists", "create"))],
)
async def create_playlist(
    body: PlaylistCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlaylist:
    # Gouvernance : on ne crée pas une playlist sous un nœud qu'on n'administre pas.
    await _validate_node_org(db, current_user, body.id_governance_node)
    if not await user_can_edit_node(db, current_user, body.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer this governance node",
        )
    await _assert_template_visible(db, current_user, body.id_template)
    pl = CmsPlaylist(
        id_org=current_user.id_org,
        playlist_name=body.playlist_name,
        description=body.description,
        id_template=body.id_template,
        id_governance_node=body.id_governance_node,
        created_by=current_user.id_user,
    )
    db.add(pl)
    await db.flush()
    db.add(_audit(current_user, "create", pl.id_playlist, {
        "playlist_name": pl.playlist_name,
        "id_governance_node": pl.id_governance_node,
        "id_template": pl.id_template,
    }))
    await db.commit()
    await db.refresh(pl)
    return pl


@router.get(
    "/",
    response_model=list[PlaylistRead],
    dependencies=[Depends(require_permission("playlists", "read"))],
)
async def list_playlists(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None, description="Search by name (server-side)"),
    id_governance_node: Optional[int] = Query(
        None, description="Keep only playlists attached to this governance node",
    ),
) -> list[PlaylistRead]:
    """Liste des playlists — `q` cherche par NOM, côté serveur.

    🔴 Pourquoi `q` a été ajouté (audit des menus Événements/VOD, 2026-07-29) : l'écran des
    événements alimentait son menu déroulant de playlists avec un appel SANS paramètre, donc
    plafonné au défaut de **50**, et **sans aucune recherche**. Au-delà de 50 playlists, les
    suivantes étaient **invisibles ET introuvables** — exactement le défaut corrigé le 20/07
    sur le tiroir « Ajouter un média » (filtrage dans le navigateur d'une liste déjà
    tronquée). Chercher doit se faire là où sont les données.
    """
    # Gouvernance §XVIII (D6) : lecture filtrée. On voit les playlists d'organisation
    # (nœud NULL) + celles des nœuds visibles (éditables + ancêtres en lecture seule).
    # visible/editable None = org-admin → aucun filtre. G4 : un porteur de rôle de
    # gouvernance ne voit PAS le non-gouverné (clause sans la branche NULL).
    gov_clause, visible, editable = await content_visibility(db, current_user)
    # Sous-requête : durée totale par playlist.
    # COALESCE(item.duration_ms, media.duration_ms, 10000) reproduit le même
    # fallback que ws.py pour que total_duration_ms soit non-null même si les
    # items n'ont pas de duration_ms explicite (durée extraite par le worker Celery
    # mais non propagée vers l'item au moment de l'ajout).
    # item_count + missing_dur servent à calculer sync_ready : une playlist est
    # « synchronisable » si elle a des items ET qu'aucun n'a une durée nulle
    # (ni item.duration_ms, ni media.duration_ms) — sinon le wall-clock retombe
    # sur un fallback 10s/item, qui produit une désync silencieuse en LAN sync.
    dur_subq = (
        select(
            CmsPlaylistItem.id_playlist,
            # Durée effective document-aware (par page × pages sélectionnées).
            func.sum(document_media.item_duration_sql()).label("total_duration_ms"),
            func.count().label("item_count"),
            func.sum(
                case(
                    (CmsMedia.page_count.isnot(None), 0),  # document : durée toujours résolue
                    (
                        func.coalesce(
                            CmsPlaylistItem.duration_ms, CmsMedia.duration_ms
                        ).is_(None),
                        1,
                    ),
                    else_=0,
                )
            ).label("missing_dur"),
            # Un média passthrough non sync-ready (playable/nonconform) casse la
            # synchro LAN de la playlist entière (§XVI.2 → propagation sync_ready).
            func.sum(
                case(
                    (CmsMedia.conformance_status.in_(["playable", "nonconform"]), 1),
                    else_=0,
                )
            ).label("nonconform_count"),
        )
        .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media, isouter=True)
        .group_by(CmsPlaylistItem.id_playlist)
        .subquery()
    )
    # Règles de diffusion par item : total (pour le badge) + overrides de cible
    # (scope=player/player_group) qui, seuls, cassent la synchro de groupe.
    rule_subq = (
        select(
            CmsPlaylistItem.id_playlist,
            func.count(CmsPlaylistItemRule.id_item_rule).label("rule_count"),
            func.sum(
                case((CmsPlaylistItemRule.scope != "default", 1), else_=0)
            ).label("override_count"),
        )
        .join(CmsPlaylistItemRule, CmsPlaylistItemRule.id_item == CmsPlaylistItem.id_item)
        .group_by(CmsPlaylistItem.id_playlist)
        .subquery()
    )
    stmt = (
        select(
            CmsPlaylist,
            dur_subq.c.total_duration_ms,
            dur_subq.c.item_count,
            dur_subq.c.missing_dur,
            dur_subq.c.nonconform_count,
            rule_subq.c.rule_count,
            rule_subq.c.override_count,
        )
        .outerjoin(dur_subq, CmsPlaylist.id_playlist == dur_subq.c.id_playlist)
        .outerjoin(rule_subq, CmsPlaylist.id_playlist == rule_subq.c.id_playlist)
        .where(CmsPlaylist.is_deleted == False)  # noqa: E712
    )
    if q and q.strip():
        stmt = stmt.where(CmsPlaylist.playlist_name.ilike(f"%{q.strip()}%"))
    _clause = gov_clause(CmsPlaylist.id_governance_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    if id_governance_node is not None:
        # Espace de travail du nœud : « quelles playlists ce nœud diffuse-t-il ? ».
        # ⚠️ Ce filtre s'AJOUTE au filtre de visibilité D6 ci-dessus, il ne le remplace pas :
        # demander un nœud qu'on n'a pas le droit de voir doit rendre une liste vide, pas
        # ouvrir une porte dérobée sur le contenu d'un nœud frère.
        stmt = stmt.where(CmsPlaylist.id_governance_node == id_governance_node)
    stmt = stmt.order_by(CmsPlaylist.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    rows_all = result.all()
    page_ids = [pl.id_playlist for pl, *_ in rows_all]

    # Sous-playlists (rotation) : les agrégats SQL comptent un item sub_playlist comme
    # « durée manquante » (pas de média joint) → on le remplace par la contribution
    # réelle de l'enfant. + flag used_as_child (référencée par ≥1 parent).
    subs_by_parent: dict[int, list[int]] = {}
    used_as_child: set[int] = set()
    if page_ids:
        for parent, child in (await db.execute(
            select(CmsPlaylistItem.id_playlist, CmsPlaylistItem.id_child_playlist)
            .where(
                CmsPlaylistItem.id_playlist.in_(page_ids),
                CmsPlaylistItem.id_child_playlist.isnot(None),
            )
        )).all():
            subs_by_parent.setdefault(parent, []).append(child)
        used_as_child = {r[0] for r in (await db.execute(
            select(CmsPlaylistItem.id_child_playlist)
            .where(CmsPlaylistItem.id_child_playlist.in_(page_ids)).distinct()
        )).all()}
    child_infos = await _child_info(
        db, {c for v in subs_by_parent.values() for c in v})

    # Gouvernance : requires_approval des nœuds gouvernant les playlists de la page.
    gov_node_ids = {pl.id_governance_node for pl, *_ in rows_all if pl.id_governance_node}
    node_req: dict[int, bool] = {}
    if gov_node_ids:
        node_req = {nid: bool(ra) for nid, ra in (await db.execute(
            select(CmsGovernanceNode.id_node, CmsGovernanceNode.requires_approval)
            .where(CmsGovernanceNode.id_node.in_(gov_node_ids))
        )).all()}

    playlists = []
    for pl, total_dur, item_count, missing_dur, nonconform_count, rule_count, override_count in rows_all:
        data = {c.name: getattr(pl, c.name) for c in pl.__table__.columns}
        for child in subs_by_parent.get(pl.id_playlist, []):
            dur, conf, _, _ = _sub_flag_row(child_infos.get(child))
            total_dur = (total_dur or 0) - 10000 + (dur or 0)
            missing_dur = (missing_dur or 0) - 1 + (0 if dur is not None else 1)
            if conf:
                nonconform_count = (nonconform_count or 0) + 1
        data["used_as_child"] = pl.id_playlist in used_as_child
        data["total_duration_ms"] = total_dur
        data["item_count"] = int(item_count or 0)
        has_item_rules = (rule_count or 0) > 0
        data["has_item_rules"] = has_item_rules
        # Règles default → sync de groupe préservée ; seuls les overrides cassent la sync.
        sync_ready = (
            pl.sync_mode == "wall_clock"
            and (item_count or 0) > 0
            and (missing_dur or 0) == 0
            and (nonconform_count or 0) == 0
            and (override_count or 0) == 0
        )
        data["sync_ready"] = sync_ready
        data["sync_windowed"] = sync_ready and has_item_rules
        # Staging : la liste reflète l'état PUBLIÉ (ce que voient les players) + un badge
        # « modifications non publiées ». L'éditeur (détail) montre le brouillon.
        data["has_draft_changes"] = playlist_publish.has_draft(pl)
        # Gouvernance : lecture seule si gouvernée par un nœud non éditable (D6) + drapeau
        # modération (editable None = org-admin → jamais lecture seule).
        gov = pl.id_governance_node
        data["governed_readonly"] = bool(
            gov is not None and editable is not None and gov not in editable
        )
        data["requires_approval"] = node_req.get(gov, False) if gov else False
        playlists.append(PlaylistRead.model_validate(data))
    return playlists



def _too_long_names(cap_sec, durations, enriched) -> list[str]:
    """Contenus dépassant la durée maximale par contenu imposée par la gouvernance.

    Calculé ICI et pas dans le navigateur : l'éditeur ne reçoit que l'override `duration_ms`
    de l'item (souvent NULL), jamais la durée du média ni le nombre de pages d'un document —
    il ne PEUT pas appliquer la règle correctement. On réutilise donc la durée effective déjà
    calculée pour la synchro (`effective_duration_py`), seule définition de « combien de temps
    cet item occupe l'écran ».

    ⛔ Les sous-playlists sont ignorées : elles sont déroulées en items à la compilation, et
    chacun de ces items est plafonné pour son compte. Avertir sur leur durée cumulée
    signalerait un problème qui n'existe pas.
    """
    if not cap_sec:
        return []
    limit = cap_sec * 1000
    names: list[str] = []
    for dur, it in zip(durations, enriched):
        if getattr(it, "id_child_playlist", None):
            continue
        if dur and dur > limit:
            names.append(it.media_name or f"#{it.id_media}")
    return names


@router.get(
    "/{id_playlist}",
    response_model=PlaylistDetail,
    dependencies=[Depends(require_permission("playlists", "read"))],
)
async def get_playlist(
    id_playlist: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlaylistDetail:
    result = await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == current_user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )
    pl = result.scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    # Gouvernance D6 : une playlist gouvernée hors des nœuds visibles → 404 (cross-nœud
    # invisible comme le cross-tenant). governed_readonly/requires_approval pour le front.
    gov = pl.id_governance_node
    _gov_readonly, _editable = await _assert_playlist_visible(db, current_user, pl)
    _gov_requires_approval = await _node_requires_approval(db, gov)
    # Durée maximale par contenu imposée par la chaîne de gouvernance : l'éditeur doit
    # AVERTIR au chargement (décision Ed 2026-07-26). Avant, on ne découvrait le contenu
    # écarté qu'à l'aperçu du programme compilé, une fois la playlist déjà construite.
    # ⚠️ Avertissement seulement : l'exclusion réelle reste à la compilation, qui seule sait
    # quel nœud diffuse quoi et à quel instant.
    _gov_max_item_sec = await effective_max_item_sec(db, gov)

    # Demande d'approbation active (§XXI) : l'éditeur doit pouvoir dire « en cours de
    # validation » après un simple rechargement de page — sinon le seul retour possible
    # est le 409 « une demande est déjà active » au 2ᵉ clic sur Soumettre.
    _approval = await approvals_svc.get_active_request(
        db, current_user.id_org, "playlist", id_playlist)

    # Rotation : référencée comme sous-playlist par ≥1 parent non supprimé ?
    used_as_child = (await db.execute(
        select(CmsPlaylistItem.id_item)
        .join(CmsPlaylist, CmsPlaylist.id_playlist == CmsPlaylistItem.id_playlist)
        .where(
            CmsPlaylistItem.id_child_playlist == id_playlist,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        ).limit(1)
    )).scalar_one_or_none() is not None

    # Staging : si un brouillon existe, l'éditeur travaille dessus (les players, eux,
    # voient toujours cms_playlist_items via _resolve_sync_payload, inchangé).
    if pl.draft_items is not None:
        draft = pl.draft_items
        enriched = await _draft_items_read(db, id_playlist, draft)
        mmap: dict[int, tuple] = {}
        mids = [e["id_media"] for e in draft if e.get("id_media")]
        if mids:
            for mid, mdur, mconf, mpages in (await db.execute(
                select(CmsMedia.id_media, CmsMedia.duration_ms, CmsMedia.conformance_status,
                       CmsMedia.page_count)
                .where(CmsMedia.id_media.in_(mids))
            )).all():
                mmap[mid] = (mdur, mconf, mpages)
        children = await _child_info(
            db, {e["id_child_playlist"] for e in draft if e.get("id_child_playlist")})
        rows = [
            _sub_flag_row(children.get(e["id_child_playlist"]))
            if e.get("id_child_playlist") else
            (document_media.effective_duration_py(
                e.get("duration_ms"),
                mmap.get(e["id_media"], (None, None, None))[0],
                mmap.get(e["id_media"], (None, None, None))[2],
                doc_pages=e.get("doc_pages"),
             ),
             mmap.get(e["id_media"], (None, None, None))[1],
             bool(e.get("rules")),
             any(r.get("scope", "default") != "default" for r in e.get("rules", [])))
            for e in draft
        ]
        total_ms, sync_ready, has_item_rules, sync_windowed = _sync_flags(pl.sync_mode, rows)
        _too_long = _too_long_names(_gov_max_item_sec, [r[0] for r in rows], enriched)
        return PlaylistDetail(
            **{**{c.name: getattr(pl, c.name) for c in pl.__table__.columns},
               **_settings_overlay(pl)},
            total_duration_ms=total_ms if enriched else None,
            sync_ready=sync_ready, has_item_rules=has_item_rules, sync_windowed=sync_windowed,
            has_draft_changes=True, used_as_child=used_as_child, items=enriched,
            governed_readonly=_gov_readonly, requires_approval=_gov_requires_approval,
            approval_status=_approval.status if _approval else None,
            approval_request_id=_approval.id_request if _approval else None,
            governance_max_item_sec=_gov_max_item_sec,
            governance_too_long=_too_long,
        )

    rows = list(
        (await db.execute(
            select(
                CmsPlaylistItem,
                CmsMedia.media_name,
                CmsMedia.media_type,
                CmsMedia.duration_ms.label("media_duration_ms"),
                CmsMedia.conformance_status.label("media_conformance"),
                CmsMedia.page_count.label("media_page_count"),
            )
            .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media, isouter=True)
            .where(CmsPlaylistItem.id_playlist == id_playlist)
            .order_by(CmsPlaylistItem.sort_order)
        )).all()
    )

    # Items porteurs d'une règle de diffusion (set d'id_item) + ceux avec un override
    # de cible (scope=player/player_group) qui casse la synchro de groupe.
    item_ids = [item.id_item for item, *_ in rows]
    items_with_rules: set[int] = set()
    items_with_override: set[int] = set()
    if item_ids:
        for iid, scope in (await db.execute(
            select(CmsPlaylistItemRule.id_item, CmsPlaylistItemRule.scope)
            .where(CmsPlaylistItemRule.id_item.in_(item_ids))
        )).all():
            items_with_rules.add(iid)
            if scope != "default":
                items_with_override.add(iid)

    children = await _child_info(
        db, {item.id_child_playlist for item, *_ in rows if item.id_child_playlist})
    enriched = []
    sf_rows: list[tuple] = []
    for item, media_name, media_type, media_dur, media_conf, media_pages in rows:
        r = PlaylistItemRead.model_validate(item)
        r.media_name = media_name
        r.media_type = media_type
        r.media_duration_ms = media_dur
        r.conformance_status = media_conf
        r.media_page_count = media_pages
        r.has_rules = item.id_item in items_with_rules
        if item.id_child_playlist:
            ci = children.get(item.id_child_playlist)
            if ci:
                r.child_name = ci["name"]
                r.child_item_count = ci["count"]
                r.child_rotation_mode = ci["rotation_mode"]
                r.child_items_per_turn = ci["items_per_turn"]
            enriched.append(r)
            sf_rows.append(_sub_flag_row(ci))
            continue
        enriched.append(r)
        sf_rows.append((
            document_media.effective_duration_py(
                item.duration_ms, media_dur, media_pages,
                doc_pages=item.doc_pages, doc_page_count=item.doc_page_count,
            ),
            media_conf,
            item.id_item in items_with_rules, item.id_item in items_with_override))

    total_ms, sync_ready, has_item_rules, sync_windowed = _sync_flags(pl.sync_mode, sf_rows)
    _too_long = _too_long_names(_gov_max_item_sec, [r[0] for r in sf_rows], enriched)

    return PlaylistDetail(
        **{**{c.name: getattr(pl, c.name) for c in pl.__table__.columns},
           **_settings_overlay(pl)},
        total_duration_ms=total_ms if enriched else None,
        sync_ready=sync_ready,
        has_item_rules=has_item_rules,
        sync_windowed=sync_windowed,
        # Pas d'items en brouillon, mais peut-être des réglages de rotation en attente.
        has_draft_changes=playlist_publish.has_draft(pl),
        used_as_child=used_as_child,
        items=enriched,
        governed_readonly=_gov_readonly,
        requires_approval=_gov_requires_approval,
        approval_status=_approval.status if _approval else None,
        approval_request_id=_approval.id_request if _approval else None,
        governance_max_item_sec=_gov_max_item_sec,
        governance_too_long=_too_long,
    )


@router.put(
    "/{id_playlist}",
    response_model=PlaylistRead,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def update_playlist(
    id_playlist: int,
    body: PlaylistUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlaylist:
    result = await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == current_user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )
    pl = result.scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    # Gouvernance : édition gardée par le nœud ACTUEL ; un changement de nœud exige aussi
    # d'administrer le nœud CIBLE (miroir des programmes).
    await _enforce_playlist_node_edit(db, current_user, pl)
    if "id_governance_node" in body.model_fields_set:
        await _validate_node_org(db, current_user, body.id_governance_node)
        if not await user_can_edit_node(db, current_user, body.id_governance_node):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not administer the target governance node",
            )
        # N2 : détacher (→ NULL) sort la playlist de la gouvernance (fin de la modération
        # D7) — réservé à un ancêtre STRICT ou à l'org-admin, jamais l'admin du nœud même.
        if body.id_governance_node is None and not await user_can_detach_from_node(
            db, current_user, pl.id_governance_node
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an administrator of a parent node can detach "
                       "this playlist from governance",
            )
        pl.id_governance_node = body.id_governance_node

    _old_name = pl.playlist_name
    if body.playlist_name is not None:
        pl.playlist_name = body.playlist_name
    if body.description is not None:
        pl.description = body.description
    if body.id_template is not None:
        await _assert_template_visible(db, current_user, body.id_template)
        pl.id_template = body.id_template
    if body.sync_mode is not None:
        pl.sync_mode = body.sync_mode
    # Rotation : le réglage vit sur la playlist ENFANT et change la boucle des parents qui
    # l'imbriquent. Il passe donc par le BROUILLON comme les items (décision Ed 2026-07-25 ④) :
    # rien ne part aux players avant « Publier » — et sous modération, rien avant approbation.
    # ⚠️ Ne PAS réintroduire d'écriture directe des colonnes ici : elles sont ce que lisent
    # les players (ws.py), donc les toucher = diffuser un réglage non publié.
    settings = dict(pl.draft_settings or {})
    for key in playlist_publish.DRAFT_SETTING_KEYS:
        value = getattr(body, key)
        if value is None:
            continue
        if value == getattr(pl, key):
            settings.pop(key, None)   # retour à la valeur publiée = plus rien en attente
        else:
            settings[key] = value
    pl.draft_settings = settings or None

    # Le journal ne retient que ce qui a CHANGÉ (un PATCH de réglage ne doit pas ressembler
    # à un renommage), et nomme l'ancien nom : c'est lui qu'on cherche après coup.
    _changed = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if _changed:
        db.add(_audit(current_user, "update", id_playlist, {
            "playlist_name": pl.playlist_name,
            **({"renamed_from": _old_name} if pl.playlist_name != _old_name else {}),
            "fields": sorted(_changed),
        }))
    await db.commit()
    await db.refresh(pl)
    return pl


@router.delete(
    "/{id_playlist}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("playlists", "delete"))],
)
async def delete_playlist(
    id_playlist: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    result = await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == current_user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )
    pl = result.scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    await _enforce_playlist_node_edit(db, current_user, pl)
    # Référencée comme sous-playlist par un parent non supprimé → refus explicite
    # (la retirer des parents d'abord) plutôt qu'un slot silencieusement ignoré.
    parent = (await db.execute(
        select(CmsPlaylist.playlist_name)
        .join(CmsPlaylistItem, CmsPlaylistItem.id_playlist == CmsPlaylist.id_playlist)
        .where(
            CmsPlaylistItem.id_child_playlist == id_playlist,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        ).limit(1)
    )).scalar_one_or_none()
    if parent is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Playlist used as a sub-playlist by '{parent}' — remove it from there first",
        )
    pl.is_deleted = True
    # La ligne disparaît de toutes les vues, le journal reste : c'est la seule façon de
    # répondre plus tard à « qui a supprimé cette playlist, et quand ? ».
    db.add(_audit(current_user, "delete", id_playlist, {
        "playlist_name": pl.playlist_name,
        "id_governance_node": pl.id_governance_node,
    }))
    # Gouvernance §XXI : purge des demandes d'approbation liées (comme pour les campagnes).
    await approvals_svc.purge_for_resource(db, current_user, "playlist", id_playlist)
    await db.commit()


@router.post(
    "/{id_playlist}/items",
    response_model=PlaylistItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def add_playlist_item(
    id_playlist: int,
    body: PlaylistItemCreate,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlaylistItemRead:
    """Ajoute un item au BROUILLON (non publié). Pas de push : invisible des players
    jusqu'à « Publier »."""
    if body.id_child_playlist is not None:
        # 🔑 La sous-playlist doit être VISIBLE de l'utilisateur. Mesuré en prod le
        # 2026-07-29 : un responsable de Site-01 imbriquait (201) une playlist de Site-02
        # que le détail lui refusait en 404 — il ne pouvait ni la voir, ni la lire, mais il
        # la faisait diffuser sur ses écrans, et toute modification faite chez le voisin
        # partait chez lui en silence. `validate_child_for_nesting` ne vérifiait que l'ORG.
        await _get_visible_playlist(db, current_user, body.id_child_playlist)
        # Puis les gardes d'imbrication : 1 seul niveau, même org, pas d'auto-référence.
        # La rotation se règle sur la playlist ENFANT (rotation_mode).
        try:
            await validate_child_for_nesting(
                db, pl.id_org, id_playlist, body.id_child_playlist)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        draft = await _ensure_draft(db, pl)
        uid = _next_uid(draft)
        draft.append({
            "uid": uid, "id_media": None, "item_type": "sub_playlist",
            "id_child_playlist": body.id_child_playlist, "sort_order": body.sort_order,
            "duration_ms": None, "zone_index": body.zone_index,
            "transition_type": body.transition_type, "rules": [],
        })
        pl.draft_items = draft
        await db.commit()
        reads = await _draft_items_read(db, id_playlist, draft)
        return next(r for r in reads if r.id_item == uid)

    media = await _assert_media_addable(db, current_user, body.id_media)
    # Une playlist standard est visuelle : image ou vidéo (vidéo = visuel + son OK).
    # Le son pur (audio) n'a pas de rendu écran → il appartient aux paniers audio (§XIX).
    if media.media_type == "audio":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Audio media cannot be added to a standard playlist (reserved for audio baskets)",
        )

    draft = await _ensure_draft(db, pl)
    uid = _next_uid(draft)
    draft.append({
        "uid": uid, "id_media": body.id_media, "sort_order": body.sort_order,
        "duration_ms": body.duration_ms, "zone_index": body.zone_index,
        "transition_type": body.transition_type,
        "doc_pages": (body.doc_pages or "").strip() or None,
        "touch_navigation": body.touch_navigation,
        "touch_resume_after_s": body.touch_resume_after_s, "rules": [],
    })
    pl.draft_items = draft
    await db.commit()
    reads = await _draft_items_read(db, id_playlist, draft)
    return next(r for r in reads if r.id_item == uid)


@router.post(
    "/{id_playlist}/items/bulk",
    response_model=list[PlaylistItemRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def add_playlist_items_bulk(
    id_playlist: int,
    body: PlaylistItemsBulkCreate,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlaylistItemRead]:
    """Ajoute PLUSIEURS contenus au BROUILLON en une fois (multi-sélection). Pas de push :
    invisible des players jusqu'à « Publier ». Les médias audio (réservés aux paniers §XIX),
    les ids introuvables et **ceux hors du périmètre de gouvernance** sont ignorés
    silencieusement. L'ordre de `id_medias` est conservé.

    ⚠️ Ignorer plutôt que refuser est le contrat de CETTE route (multi-sélection : un id
    périmé ne doit pas faire échouer les 20 autres) — mais le filtre de visibilité doit y
    être quand même, sinon l'ajout en lot serait le contournement de la garde unitaire.
    """
    # Médias éligibles (existants, non supprimés, VISIBLES), en préservant l'ordre demandé.
    # `media_visibility` et pas `content_visibility` : même règle que l'ajout unitaire.
    gov_clause, _visible, _editable = await media_visibility(db, current_user)
    stmt = select(CmsMedia).where(
        CmsMedia.id_media.in_(body.id_medias),
        CmsMedia.id_org == current_user.id_org,
        CmsMedia.is_deleted == False,  # noqa: E712
    )
    _clause = gov_clause(CmsMedia.id_governance_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    media_map = {m.id_media: m for m in (await db.execute(stmt)).scalars().all()}

    draft = await _ensure_draft(db, pl)
    uid = _next_uid(draft)
    sort_order = len(draft)
    for id_media in body.id_medias:
        m = media_map.get(id_media)
        if m is None or m.media_type == "audio":
            continue
        draft.append({
            "uid": uid, "id_media": id_media, "sort_order": sort_order,
            "duration_ms": None, "zone_index": 0, "transition_type": "cut", "rules": [],
        })
        uid -= 1
        sort_order += 1
    pl.draft_items = draft
    await db.commit()
    return await _draft_items_read(db, id_playlist, draft)


@router.put(
    "/{id_playlist}/items/reorder",
    response_model=list[PlaylistItemRead],
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def reorder_playlist_items(
    id_playlist: int,
    body: PlaylistItemsReorder,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlaylistItemRead]:
    """Réordonne les items du BROUILLON (id_item = uid). Pas de push."""
    order_map = {entry.id_item: entry.sort_order for entry in body.items}
    draft = await _ensure_draft(db, pl)
    for e in draft:
        if e["uid"] in order_map:
            e["sort_order"] = order_map[e["uid"]]
    pl.draft_items = draft
    await db.commit()
    return await _draft_items_read(db, id_playlist, draft)


@router.delete(
    "/{id_playlist}/items/{id_item}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def remove_playlist_item(
    id_playlist: int,
    id_item: int,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Retire un item du BROUILLON (id_item = uid). Pas de push."""
    draft = await _ensure_draft(db, pl)
    new = [e for e in draft if e["uid"] != id_item]
    if len(new) == len(draft):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    pl.draft_items = new
    await db.commit()


@router.patch(
    "/{id_playlist}/items/{id_item}",
    response_model=list[PlaylistItemRead],
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def update_playlist_item(
    id_playlist: int,
    id_item: int,
    body: PlaylistItemUpdate,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlaylistItemRead]:
    """Met à jour un item du BROUILLON : durée d'exposition (utile pour image/PDF sans durée
    native) + transition. id_item = uid. Pas de push (l'édition va au brouillon ; la
    publication la propage). `model_fields_set` distingue « non fourni » de « null »."""
    draft = await _ensure_draft(db, pl)
    entry = next((e for e in draft if e["uid"] == id_item), None)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    if "duration_ms" in body.model_fields_set:
        entry["duration_ms"] = body.duration_ms          # None = repli durée par défaut player
    if "transition_type" in body.model_fields_set and body.transition_type is not None:
        entry["transition_type"] = body.transition_type
    if "zone_index" in body.model_fields_set and body.zone_index is not None:
        entry["zone_index"] = body.zone_index            # §25.4 : zone du template
    if "doc_pages" in body.model_fields_set:
        # Documents multipages : sélection "2-5,8" (None = toutes les pages).
        entry["doc_pages"] = (body.doc_pages or "").strip() or None
    if "touch_navigation" in body.model_fields_set and body.touch_navigation is not None:
        # Documents multipages : feuilletage au doigt (inerte hors document / ecran non tactile).
        entry["touch_navigation"] = body.touch_navigation
    if "touch_resume_after_s" in body.model_fields_set:
        entry["touch_resume_after_s"] = body.touch_resume_after_s   # None = defaut du renderer
    pl.draft_items = draft
    await db.commit()
    return await _draft_items_read(db, id_playlist, draft)


@router.post(
    "/{id_playlist}/items/{id_item}/duplicate",
    response_model=list[PlaylistItemRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def duplicate_playlist_item(
    id_playlist: int,
    id_item: int,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlaylistItemRead]:
    """Duplique un item du BROUILLON : insère une copie (même média + durée + zone +
    transition) JUSTE APRÈS l'original, puis renumérote l'ordre. Les règles de diffusion
    ne sont PAS copiées (position/temps-spécifiques). Marche pour tous les types (vidéo,
    image, feed_display, url_display…). Pas de push (va au brouillon)."""
    draft = await _ensure_draft(db, pl)
    ordered = sorted(draft, key=lambda x: x["sort_order"])
    idx = next((i for i, e in enumerate(ordered) if e["uid"] == id_item), None)
    if idx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    src = ordered[idx]
    copy = {
        "uid": _next_uid(draft), "id_media": src.get("id_media"), "sort_order": 0,
        "item_type": src.get("item_type", "media"),
        "id_child_playlist": src.get("id_child_playlist"),
        "duration_ms": src.get("duration_ms"), "zone_index": src.get("zone_index", 0),
        "transition_type": src.get("transition_type", "cut"),
        "doc_pages": src.get("doc_pages"),
        "touch_navigation": src.get("touch_navigation", False),
        "touch_resume_after_s": src.get("touch_resume_after_s"), "rules": [],
    }
    ordered.insert(idx + 1, copy)
    for i, e in enumerate(ordered):
        e["sort_order"] = i
    pl.draft_items = ordered
    await db.commit()
    return await _draft_items_read(db, id_playlist, ordered)


# ── Règles de diffusion par item (§ retours clients item 4) ─────────────────────

async def _get_item(db: AsyncSession, id_playlist: int, id_item: int) -> CmsPlaylistItem:
    item = (await db.execute(
        select(CmsPlaylistItem).where(
            CmsPlaylistItem.id_item == id_item,
            CmsPlaylistItem.id_playlist == id_playlist,
        )
    )).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.get(
    "/{id_playlist}/items/{id_item}/rules",
    response_model=list[ItemRuleRead],
    dependencies=[Depends(require_permission("playlists", "read"))],
)
async def list_item_rules(
    id_playlist: int,
    id_item: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Règles de diffusion d'un item (vide = item perpétuel). Lit le brouillon s'il
    existe (id_item = uid), sinon les règles publiées. Ne crée PAS de brouillon.

    Périmètre de gouvernance appliqué (D6) : cette route se lisait sans aucun filtre, donc
    un responsable de site lisait les règles d'un item d'une playlist d'un nœud frère que
    le détail lui refusait en 404 (mesuré en prod le 2026-07-29).
    """
    pl = await _get_visible_playlist(db, current_user, id_playlist)
    if pl.draft_items is not None:
        e = _draft_find(pl.draft_items, id_item)
        if e is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        return [ItemRuleRead(id_item_rule=-(i + 1), id_item=id_item, **r)
                for i, r in enumerate(e.get("rules", []))]
    await _get_item(db, id_playlist, id_item)
    return list((await db.execute(
        select(CmsPlaylistItemRule)
        .where(CmsPlaylistItemRule.id_item == id_item)
        .order_by(CmsPlaylistItemRule.id_item_rule)
    )).scalars().all())


@router.put(
    "/{id_playlist}/items/{id_item}/rules",
    response_model=list[ItemRuleRead],
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def set_item_rules(
    id_playlist: int,
    id_item: int,
    body: list[ItemRuleCreate],
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remplace les règles d'un item dans le BROUILLON (id_item = uid). Liste vide =
    item perpétuel. Pas de push (visible des players à la publication)."""
    draft = await _ensure_draft(db, pl)
    e = _draft_find(draft, id_item)
    if e is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    e["rules"] = [r.model_dump(mode="json") for r in body]
    pl.draft_items = draft
    await db.commit()
    return [ItemRuleRead(id_item_rule=-(i + 1), id_item=id_item, **r)
            for i, r in enumerate(e["rules"])]


# ── Publication / abandon du brouillon (#1 staging) ────────────────────────────

@router.post(
    "/{id_playlist}/publish",
    response_model=PlaylistDetail,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def publish_playlist(
    id_playlist: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> PlaylistDetail:
    """Applique le brouillon → items publiés (par DIFF : update existants, insert
    nouveaux, delete retirés → préserve les id_item donc les tags) puis push graceful.
    No-op si aucun brouillon.

    Gouvernance §XVIII : une playlist rattachée à un nœud n'est publiable que par un admin
    de ce nœud, et si le nœud EXIGE la modération, la publication directe est refusée (409) —
    le seul chemin est submit → approve (l'approbation publie le brouillon)."""
    pl = (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.id_org == current_user.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if pl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    await _enforce_playlist_node_edit(db, current_user, pl)
    if await _node_requires_approval(db, pl.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Governed content under moderation: submit the playlist for approval "
                   "(it is published on approval).",
        )
    if not playlist_publish.has_draft(pl):
        return await get_playlist(id_playlist, current_user, db)

    try:
        draft = await playlist_publish.apply_draft(db, pl)
    except playlist_publish.DraftApplyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sub-playlist: {exc}") from exc
    # Le geste qui engage : ce contenu part sur les écrans.
    db.add(_audit(current_user, "publish", id_playlist, {
        "playlist_name": pl.playlist_name,
        "items": len(draft),
        "id_governance_node": pl.id_governance_node,
    }))
    await db.commit()

    # Conformité §XVI.2 : à la publication, alerte + auto-rendition pour les médias trop
    # lourds pour les players cibles (idempotent : pas de double génération).
    advisories: list[dict] = []
    for e in draft:
        if not e.get("id_media"):
            continue  # item sub_playlist : la conformité vit sur la playlist enfant
        media = (await db.execute(
            select(CmsMedia).where(CmsMedia.id_media == e["id_media"])
        )).scalar_one_or_none()
        if media:
            adv = await _conformance_autogen(db, pl.id_org, id_playlist, media)
            if adv:
                advisories.append(adv)
    if advisories:
        response.headers["X-Rendition-Advisory"] = json.dumps(advisories)

    await _push_sync_org(db, preemptive=False)
    return await get_playlist(id_playlist, current_user, db)


@router.post(
    "/{id_playlist}/discard",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def discard_draft(
    id_playlist: int,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Abandonne le brouillon — contenus ET réglages (revient à la version publiée)."""
    pl.draft_items = None
    pl.draft_settings = None
    await db.commit()


@router.post(
    "/{id_playlist}/submit-approval",
    dependencies=[Depends(require_permission("playlists", "edit"))],
)
async def submit_playlist_approval(
    id_playlist: int,
    pl: Annotated[CmsPlaylist, Depends(get_editable_playlist)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Soumet le brouillon d'une playlist gouvernée SOUS MODÉRATION à l'approbation (§XXI).

    Seul chemin de publication pour un nœud qui exige la modération (R15/C) : l'approbation
    publiera le brouillon. Prévient les modérateurs par e-mail (D8). L'accès est déjà gardé
    par le nœud (dépendance get_editable_playlist)."""
    if not await _node_requires_approval(db, pl.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This playlist is not under moderation: publish it directly.",
        )
    try:
        req, _ctx = await approvals_svc.submit(db, current_user, "playlist", id_playlist)
    except approvals_svc.ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    await db.refresh(req)
    # Alerte modérateurs (best-effort) + événement §XXII org-level (règles d'alerte).
    await governance_moderation.notify_moderators_submitted(db, pl, current_user)
    return {"id_request": req.id_request, "status": req.status}


# ── Simulation timeline déterministe (§ retours clients — Simulation v2) ──────────
# Couvre une journée complète pour du contenu de durée raisonnable (≥ ~30 s en moyenne).
# Plafond de garde contre les playlists d'items très courts qui boucleraient des milliers
# de fois (le drapeau `truncated` prévient alors l'utilisateur).
_TIMELINE_MAX_SEGMENTS = 3000


def _item_timerule(r: CmsPlaylistItemRule) -> TimeRule:
    return TimeRule(
        rule_type=r.rule_type,
        days_of_week=r.days_of_week,
        time_start=r.time_start,
        time_end=r.time_end,
        date_from=r.date_from,
        date_to=r.date_to,
    )


@router.post(
    "/{id_playlist}/simulate-timeline",
    response_model=SimulateTimelineResult,
    dependencies=[Depends(require_permission("playlists", "read"))],
)
async def simulate_timeline(
    id_playlist: int,
    body: SimulateTimelineRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SimulateTimelineResult:
    """Timeline prévisionnelle d'une journée : ordre et heure de passage de chaque item
    en honorant les règles de diffusion (heure locale SIGNFLOW_TZ). Modèle round-robin
    fidèle au player wall-clock : on parcourt les items dans l'ordre, en sautant ceux hors
    de leur fenêtre à l'instant courant, jusqu'à la fin de journée ou la limite de segments.
    `player_id` applique les overrides de règle scope=player / player_group.

    🔑 Périmètre de gouvernance appliqué (D6). La simulation ne se contentait d'aucun filtre :
    mesuré en prod le 2026-07-29, un responsable de Site-01 obtenait 1789 segments — donc le
    déroulé complet et **les noms des médias** — d'une playlist de Site-02 que le détail lui
    refusait en 404. C'est précisément le contenu que le cloisonnement protège.
    """
    pl = await _get_visible_playlist(db, current_user, id_playlist)

    def _sim_select(pid: int):
        return (
            select(
                CmsPlaylistItem.id_item,
                CmsPlaylistItem.id_media,
                CmsPlaylistItem.item_type,
                CmsPlaylistItem.id_child_playlist,
                CmsMedia.media_name,
                CmsMedia.media_type,
                # Document multipage : par page × pages sélectionnées (1 segment = le doc entier).
                document_media.item_duration_sql().label("dur"),
            )
            .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media, isouter=True)
            .where(CmsPlaylistItem.id_playlist == pid)
            .order_by(CmsPlaylistItem.sort_order)
        )

    rows = (await db.execute(_sim_select(id_playlist))).all()

    # Slots (média / sous-playlist) puis déroulé du cycle de rotation — même moteur
    # que le payload player (unroll_slots) : la timeline simulée = la vraie boucle.
    slots: list[dict] = []
    for r in rows:
        if r.item_type == "sub_playlist" and r.id_child_playlist:
            child = (await db.execute(
                select(CmsPlaylist).where(
                    CmsPlaylist.id_playlist == r.id_child_playlist,
                    CmsPlaylist.is_deleted == False,  # noqa: E712
                )
            )).scalar_one_or_none()
            if child is None:
                continue
            crows = (await db.execute(
                _sim_select(child.id_playlist)
                .where(CmsPlaylistItem.item_type == "media")
            )).all()
            citems = [
                {"id_item": c.id_item, "id_media": c.id_media, "media_name": c.media_name,
                 "media_type": c.media_type, "duration_ms": int(c.dur)}
                for c in crows
            ]
            if not citems:
                continue
            if child.rotation_mode in ("one_sequential", "one_random") and len(citems) > 1:
                slots.append({
                    "kind": "rotation", "items": citems, "mode": child.rotation_mode,
                    "k": max(1, min(child.items_per_turn or 1, len(citems))),
                    "seed": rotation_seed(child.id_playlist, [c["id_item"] for c in citems]),
                })
            else:
                slots.append({"kind": "inline", "items": citems})
        else:
            slots.append({"kind": "inline", "items": [
                {"id_item": r.id_item, "id_media": r.id_media, "media_name": r.media_name,
                 "media_type": r.media_type, "duration_ms": int(r.dur)}]})
    items = unroll_slots(slots)
    if not items:
        return SimulateTimelineResult(sync_mode=pl.sync_mode, segments=[], truncated=False)

    # Filtrage par tags du player (même logique runtime que le player) : on ne garde
    # que les items éligibles au profil de réception du player ciblé. Statique (≠ heure),
    # donc appliqué une fois avant la simulation temporelle.
    if body.player_id is not None:
        from app.api.v1.endpoints.ws import _filter_items_by_tags
        shaped = [{"id_item": it["id_item"], "media": {"id_media": it["id_media"]}} for it in items]
        eligible_ids = {e["id_item"] for e in await _filter_items_by_tags(db, body.player_id, shaped)}
        items = [it for it in items if it["id_item"] in eligible_ids]
        if not items:
            return SimulateTimelineResult(sync_mode=pl.sync_mode, segments=[], truncated=False)

    # Groupes du player ciblé (pour les overrides scope=player_group)
    group_ids: set[int] = set()
    if body.player_id is not None:
        group_ids = {
            row[0] for row in (await db.execute(
                select(CmsPlayerGroupMember.id_group)
                .where(CmsPlayerGroupMember.id_player == body.player_id)
            )).all()
        }

    # Règles par item, classées par scope (le plus spécifique l'emporte)
    item_ids = [it["id_item"] for it in items]
    player_rules: dict[int, list[TimeRule]] = {}
    group_rules: dict[int, list[TimeRule]] = {}
    default_rules: dict[int, list[TimeRule]] = {}
    for r in (await db.execute(
        select(CmsPlaylistItemRule).where(CmsPlaylistItemRule.id_item.in_(item_ids))
    )).scalars().all():
        tr = _item_timerule(r)
        if r.scope == "player":
            if body.player_id is not None and r.target_id == body.player_id:
                player_rules.setdefault(r.id_item, []).append(tr)
        elif r.scope == "player_group":
            if r.target_id in group_ids:
                group_rules.setdefault(r.id_item, []).append(tr)
        else:
            default_rules.setdefault(r.id_item, []).append(tr)

    def allowed_at(iid: int, when: datetime) -> bool:
        eff = player_rules.get(iid) or group_rules.get(iid) or default_rules.get(iid)
        return (not eff) or any(rule_active_at(rr, when) for rr in eff)

    # Datetime de départ (heure locale naïve, cohérente avec rule_active_at)
    try:
        day = date_cls.fromisoformat(body.date)
        hh, mm = (int(x) for x in body.start_time.split(":")[:2])
        t = datetime.combine(day, time_cls(hh, mm))
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date/start_time")
    eod = datetime.combine(day, time_cls(23, 59, 59))

    # Simulation de la PLAYLIST SEULE (round-robin wall-clock honorant les item-rules).
    # Les events / programmes / priorités ne sont PAS simulés ici : c'est le rôle de la
    # simulation « journée du player » (POST /players/{id}/simulate-day).
    segments: list[TimelineSegment] = []
    n = len(items)
    cursor = 0
    guard = 0
    while t <= eod and len(segments) < _TIMELINE_MAX_SEGMENTS:
        guard += 1
        if guard > _TIMELINE_MAX_SEGMENTS + 1500:
            break

        found = None
        for _ in range(n):
            cand = items[cursor % n]
            cursor += 1
            if allowed_at(cand["id_item"], t):
                found = cand
                break
        if found is None:
            # Aucun item dans sa fenêtre à cet instant → on avance jusqu'au prochain créneau.
            t += timedelta(minutes=1)
            continue
        end = t + timedelta(milliseconds=found["duration_ms"])
        segments.append(TimelineSegment(
            id_item=found["id_item"], id_media=found["id_media"],
            media_name=found["media_name"], media_type=found["media_type"],
            start=t.strftime("%H:%M:%S"),
            end=min(end, eod).strftime("%H:%M:%S"),
            duration_ms=int((min(end, eod) - t).total_seconds() * 1000),
        ))
        t = end if end > t else t + timedelta(milliseconds=max(found["duration_ms"], 1000))

    return SimulateTimelineResult(
        sync_mode=pl.sync_mode,
        segments=segments,
        truncated=len(segments) >= _TIMELINE_MAX_SEGMENTS,
    )
