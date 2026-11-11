"""Approval Workflow §XXI — service générique (S7 Phase C1).

Machine à états d'une demande (`cms_approval_requests`, migr 0081) :

    pending_review ──approve──▶ approved ──(publish du module ressource)──▶ published
          │  ▲                      │
          │  └──resubmit── changes_requested ◀──request_changes──┘ (depuis pending)
          └──reject──▶ rejected                │
                          ▲────────reject──────┘

`rejected` / `published` / `cancelled` = terminaux (une NOUVELLE soumission redevient
possible — l'index unique partiel ne retient que pending_review/changes_requested/approved).
`cancelled` (hors spec, assumé) = la ressource a été supprimée pendant le cycle.

La PUBLICATION reste dans le module ressource (un seul chemin d'enforcement — ex.
publish campagne = SOV/créatifs) : le module appelle `mark_published` quand il publie,
et bloque sa publication si `get_active_request` renvoie une demande non approuvée.

Reviewers v1 = RBAC `approvals:approve/reject` (CMS_APPROVER, org-admin) — pas de table
d'assignation par nœud de gouvernance (aucun consommateur tant que les notifications
§XXII sont des webhooks org-level). Registre de resource types : v1 = `campaign`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import CmsApprovalComment, CmsApprovalRequest
from app.models.audit_log import CmsAuditLog
from app.models.campaign import CmsAdvertiser, CmsCampaign, CmsCampaignCreative, CmsCampaignTarget
from app.models.media import CmsMedia
from app.models.user import CmsUser

# Types de ressources soumissibles. « playlist » ajouté pour la gouvernance §XVIII du
# contenu (R15) : une playlist gouvernée sous modération se soumet, et l'approbation
# publie son brouillon (cf. app/services/playlist_publish.py + endpoint submit).
RESOURCE_TYPES = ("campaign", "playlist")

# États dits ACTIFS = couverts par l'index unique partiel uq_car_active_per_resource.
ACTIVE_STATUSES = ("pending_review", "changes_requested", "approved")

# action → (statuts source acceptés, statut cible)
_TRANSITIONS = {
    "approve": (("pending_review",), "approved"),
    "reject": (("pending_review", "changes_requested"), "rejected"),
    "request_changes": (("pending_review",), "changes_requested"),
    "resubmit": (("changes_requested",), "pending_review"),
}

# action → event_type §XXII (resubmit ré-émet une demande)
ACTION_EVENTS = {
    "approve": "approval_approved",
    "reject": "approval_rejected",
    "request_changes": "approval_changes_requested",
    "resubmit": "approval_requested",
}


class ApprovalError(Exception):
    """Erreur métier (mappée 4xx par l'endpoint)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def resolve_resource(
    db: AsyncSession, id_org: int, resource_type: str, resource_id: int
) -> dict | None:
    """→ {name, can_submit} pour une ressource de l'org, None si introuvable."""
    if resource_type == "campaign":
        camp = (await db.execute(
            select(CmsCampaign).where(
                CmsCampaign.id_campaign == resource_id,
                CmsCampaign.id_org == id_org,
                CmsCampaign.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if camp is None:
            return None
        return {"name": camp.campaign_name, "can_submit": camp.status == "draft"}
    if resource_type == "playlist":
        from app.models.playlist import CmsPlaylist
        pl = (await db.execute(
            select(CmsPlaylist).where(
                CmsPlaylist.id_playlist == resource_id,
                CmsPlaylist.id_org == id_org,
                CmsPlaylist.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if pl is None:
            return None
        # Soumissible s'il y a un brouillon à publier (sinon rien à modérer) — contenus
        # OU réglages de rotation en attente (décision Ed 2026-07-25 ④).
        from app.services import playlist_publish
        return {"name": pl.playlist_name, "can_submit": playlist_publish.has_draft(pl)}
    return None


async def resource_details(
    db: AsyncSession, id_org: int, resource_type: str, resource_id: int
) -> dict:
    """Contexte montré au reviewer (GET /approvals/{id}) — il doit voir CE qu'il
    approuve (retour Ed 11/07 : « je valide à l'aveugle »). campaign → paramètres
    commerciaux (dates/SOV/prix/priorité/annonceur), cibles NOMMÉES + players résolus,
    **occupation projetée des cibles** (même moteur S2 que le publish : warning 80 /
    blocage 100), et créatifs avec URLs signées de PRÉVISUALISATION (lecture vidéo/image
    inline dans le drawer)."""
    if resource_type == "campaign":
        from app.services.campaign_inventory import (
            campaign_eligible_players,
            check_campaign_occupancy,
            estimate_campaign_price,
            occupancy_breakdown,
        )
        from app.services.media import get_signed_url
        from app.services.targeting import Target, players_for_targets

        camp = (await db.execute(
            select(CmsCampaign).where(
                CmsCampaign.id_campaign == resource_id,
                CmsCampaign.id_org == id_org,
                CmsCampaign.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        campaign_info = None
        if camp is not None:
            from app.models.campaign import (
                CmsAdvertiserIndustryCategory,
                CmsIndustryCategory,
            )

            adv_name = (await db.execute(
                select(CmsAdvertiser.advertiser_name).where(
                    CmsAdvertiser.id_advertiser == camp.id_advertiser)
            )).scalar_one_or_none()
            adv_categories = list((await db.execute(
                select(CmsIndustryCategory.category_name)
                .join(CmsAdvertiserIndustryCategory,
                      CmsAdvertiserIndustryCategory.id_category
                      == CmsIndustryCategory.id_category)
                .where(CmsAdvertiserIndustryCategory.id_advertiser == camp.id_advertiser)
                .order_by(CmsIndustryCategory.category_name)
            )).scalars().all())
            trows = (await db.execute(
                select(CmsCampaignTarget).where(
                    CmsCampaignTarget.id_campaign == resource_id)
                .order_by(CmsCampaignTarget.id_target)
            )).scalars().all()
            targets_out = [
                {"target_type": t.target_type,
                 "name": await _target_name(db, id_org, t.target_type, t.target_id)}
                for t in trows
            ]
            pids: set[int] = set()
            occupancy = None
            other_campaigns: list[dict] = []
            price = None
            achievable = None
            if trows:
                targets = [Target(target_type=t.target_type, target_id=t.target_id,
                                  tag_expr=t.tag_expr or {}) for t in trows]
                pids = await campaign_eligible_players(
                    db, await players_for_targets(db, id_org, targets),
                    camp.id_advertiser)
                occ = await check_campaign_occupancy(
                    db, id_org, starts_on=camp.starts_on, ends_on=camp.ends_on,
                    sov_percent=float(camp.sov_percent), targets=targets,
                    exclude_id_campaign=resource_id,
                    id_advertiser=camp.id_advertiser,
                )
                occupancy = {"warning": occ["warning"], "blocked": occ["blocked"],
                             "max_projected_pct": occ["max_projected_pct"]}
                # Le reviewer voit D'OÙ vient l'occupation (autres campagnes en place
                # sur les mêmes players) + le devis (tarifs par écran, v2).
                other_campaigns = await occupancy_breakdown(
                    db, id_org, starts_on=camp.starts_on, ends_on=camp.ends_on,
                    pids=pids, exclude_id_campaign=resource_id,
                )
                price = await estimate_campaign_price(
                    db, id_org, targets=targets, sov_percent=float(camp.sov_percent),
                    starts_on=camp.starts_on, ends_on=camp.ends_on,
                    id_advertiser=camp.id_advertiser,
                )
                # SOV réalisable (anti-martèlement) : le valideur voit si la SOV
                # demandée dépasse le plafond physique des boucles ciblées.
                from app.services.campaign_feasibility import campaign_feasibility
                feas = await campaign_feasibility(
                    db, id_org, targets=targets, id_campaign=resource_id,
                    id_advertiser=camp.id_advertiser,
                )
                if feas is not None:
                    worst = feas["players"][0] if feas["players"] else None
                    achievable = {
                        "max_sov_pct": feas["max_sov_pct"],
                        "worst_player_name": worst["player_name"] if worst else None,
                    }
            campaign_info = {
                "advertiser_name": adv_name,
                "industry_categories": adv_categories,
                "starts_on": camp.starts_on, "ends_on": camp.ends_on,
                "daily_hours": camp.daily_hours,
                "sov_percent": float(camp.sov_percent),
                "priority": camp.priority,
                "pricing_model": camp.pricing_model,
                "price_amount": (float(camp.price_amount)
                                 if camp.price_amount is not None else None),
                "price_currency": camp.price_currency,
                "cpm_rate": float(camp.cpm_rate) if camp.cpm_rate is not None else None,
                "targets": targets_out,
                "target_player_count": len(pids),
                "occupancy": occupancy,
                "other_campaigns": other_campaigns,
                "price": price,
                "achievable": achievable,
            }

        rows = (await db.execute(
            select(CmsCampaignCreative, CmsMedia)
            .join(CmsMedia, CmsMedia.id_media == CmsCampaignCreative.id_media)
            .where(
                CmsCampaignCreative.id_campaign == resource_id,
                CmsCampaignCreative.id_org == id_org,
                CmsCampaignCreative.status == "active",
            )
            .order_by(CmsCampaignCreative.sort_order, CmsCampaignCreative.id_creative)
        )).all()
        creatives = []
        for cr, media in rows:
            creatives.append({
                "id_creative": cr.id_creative,
                "media_name": media.media_name,
                "media_type": media.media_type,
                "duration_ms": cr.duration_ms or media.duration_ms or 0,
                "approval_status": cr.approval_status,
                "preview_url": (get_signed_url(media.storage_key)
                                if media.storage_key else None),
                "thumbnail_url": (get_signed_url(media.thumbnail_url, ttl=3600)
                                  if media.thumbnail_url else None),
            })
        return {"creatives": creatives, "campaign": campaign_info}
    if resource_type == "playlist":
        # Aperçu du BROUILLON à modérer : le reviewer doit voir ce qu'il approuve (items,
        # durées, vignettes). Le brouillon (draft_items) est la version soumise.
        from app.models.playlist import CmsPlaylist
        from app.services.media import get_signed_url
        pl = (await db.execute(
            select(CmsPlaylist).where(
                CmsPlaylist.id_playlist == resource_id,
                CmsPlaylist.id_org == id_org,
                CmsPlaylist.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if pl is None:
            return {}
        draft = pl.draft_items
        if draft is None:
            # Brouillon de RÉGLAGES seul (rotation) : le valideur doit quand même voir le
            # contenu concerné — donc les items PUBLIÉS, pas une liste vide qui laisserait
            # croire à une playlist sans contenu.
            from app.models.playlist import CmsPlaylistItem
            draft = [
                {"id_media": i.id_media, "duration_ms": i.duration_ms,
                 "id_child_playlist": i.id_child_playlist, "sort_order": i.sort_order}
                for i in (await db.execute(
                    select(CmsPlaylistItem)
                    .where(CmsPlaylistItem.id_playlist == pl.id_playlist)
                    .order_by(CmsPlaylistItem.sort_order)
                )).scalars().all()
            ]
        mids = [e["id_media"] for e in draft if e.get("id_media")]
        media_map: dict[int, CmsMedia] = {}
        if mids:
            media_map = {m.id_media: m for m in (await db.execute(
                select(CmsMedia).where(CmsMedia.id_media.in_(mids))
            )).scalars().all()}
        items = []
        for e in sorted(draft, key=lambda x: x.get("sort_order", 0)):
            m = media_map.get(e.get("id_media"))
            items.append({
                "media_name": m.media_name if m else None,
                "media_type": m.media_type if m else ("sub_playlist" if e.get("id_child_playlist") else None),
                "duration_ms": e.get("duration_ms") or (m.duration_ms if m else None),
                "thumbnail_url": (get_signed_url(m.thumbnail_url, ttl=3600)
                                  if (m and m.thumbnail_url) else None),
            })
        return {"playlist": {
            "id_playlist": pl.id_playlist,
            "playlist_name": pl.playlist_name,
            "item_count": len(items),
            "items": items,
            # Réglages en attente (rotation) : sans ça le valideur approuverait un
            # changement de boucle invisible à l'écran de validation.
            "pending_settings": [
                {"key": k, "from": getattr(pl, k), "to": v}
                for k, v in (pl.draft_settings or {}).items()
            ],
        }}
    return {}


async def _target_name(
    db: AsyncSession, id_org: int, target_type: str, target_id: int | None
) -> str | None:
    """Nom de l'entité ciblée (player/site/groupe…) — None pour `org` (label i18n UI)."""
    from app.models.organization import CmsSite
    from app.models.player import CmsPlayer
    from app.models.player_group import CmsPlayerGroup
    from app.models.site_group import CmsSiteGroup

    if target_id is None:
        return None
    lookups = {
        "player": (CmsPlayer.player_name, CmsPlayer.id_player, CmsPlayer.id_org),
        "site": (CmsSite.site_name, CmsSite.id_site, CmsSite.id_org),
        "group": (CmsPlayerGroup.group_name, CmsPlayerGroup.id_group, CmsPlayerGroup.id_org),
        "site_group": (CmsSiteGroup.group_name, CmsSiteGroup.id_site_group, CmsSiteGroup.id_org),
    }
    if target_type not in lookups:
        return None
    name_col, id_col, org_col = lookups[target_type]
    return (await db.execute(
        select(name_col).where(id_col == target_id, org_col == id_org)
    )).scalar_one_or_none()


async def _approve_campaign_creatives(
    db: AsyncSession, req: CmsApprovalRequest, user: CmsUser
) -> int:
    """Approuver la campagne (§21) approuve ses créatifs EN ATTENTE (décision Ed
    2026-07-11) : le reviewer valide la campagne comme un tout — pas de double saisie.
    Un créatif ajouté APRÈS l'approbation naît toujours `pending` (flag org S5) : du
    contenu nouveau ne profite pas d'un feu vert passé. Le reject inline reste possible
    avant/après pour écarter un créatif précis. Sans commit."""
    rows = (await db.execute(
        select(CmsCampaignCreative).where(
            CmsCampaignCreative.id_campaign == req.resource_id,
            CmsCampaignCreative.id_org == req.id_org,
            CmsCampaignCreative.approval_status == "pending",
        )
    )).scalars().all()
    now = datetime.now(UTC)
    for cr in rows:
        cr.approval_status = "approved"
        cr.reviewed_by = user.id_user
        cr.reviewed_at = now
        db.add(CmsAuditLog(
            id_org=req.id_org, id_user=user.id_user, action="approve",
            resource_type="campaign_creative", resource_id=str(cr.id_creative),
            detail={"id_campaign": req.resource_id, "via_approval_request": req.id_request}))
    return len(rows)


async def get_active_request(
    db: AsyncSession, id_org: int, resource_type: str, resource_id: int
) -> CmsApprovalRequest | None:
    return (await db.execute(
        select(CmsApprovalRequest).where(
            CmsApprovalRequest.id_org == id_org,
            CmsApprovalRequest.resource_type == resource_type,
            CmsApprovalRequest.resource_id == resource_id,
            CmsApprovalRequest.status.in_(ACTIVE_STATUSES),
        )
    )).scalar_one_or_none()


def _add_comment(
    db: AsyncSession, req: CmsApprovalRequest, user: CmsUser, text_: str, action: str | None
) -> None:
    db.add(CmsApprovalComment(
        id_request=req.id_request, id_org=req.id_org, id_user=user.id_user,
        comment_text=text_, action=action,
    ))


def _add_audit(db: AsyncSession, req: CmsApprovalRequest, user: CmsUser, action: str) -> None:
    db.add(CmsAuditLog(
        id_org=req.id_org, id_user=user.id_user, action=f"approval_{action}",
        resource_type=req.resource_type, resource_id=str(req.resource_id),
        detail={"id_request": req.id_request, "status": req.status},
    ))


def _notify_ctx(req: CmsApprovalRequest, user: CmsUser, resource_name: str | None,
                comment: str | None = None) -> dict:
    return {
        "id_request": req.id_request,
        "resource_type": req.resource_type,
        "resource_id": req.resource_id,
        "resource_name": resource_name or f"#{req.resource_id}",
        "actor_name": user.full_name or user.email,
        "comment": comment,
        "due_date": req.due_date.isoformat() if req.due_date else None,
    }


async def submit(
    db: AsyncSession, user: CmsUser, resource_type: str, resource_id: int,
    notes: str | None = None, due_date: datetime | None = None,
) -> tuple[CmsApprovalRequest, dict]:
    """Crée la demande (pending_review) + commentaire 'submitted' + audit.
    → (request, ctx notification). L'appelant commit puis dispatch."""
    if resource_type not in RESOURCE_TYPES:
        raise ApprovalError(422, f"Resource type cannot be submitted: {resource_type}")
    info = await resolve_resource(db, user.id_org, resource_type, resource_id)
    if info is None:
        raise ApprovalError(404, "Resource not found")
    # Isolation portail (C2) : un compte annonceur ne soumet que les campagnes de
    # SON annonceur — cross-annonceur = 404 (même règle que _get_campaign B1).
    if user.id_advertiser is not None and resource_type == "campaign":
        camp_adv = (await db.execute(
            select(CmsCampaign.id_advertiser).where(CmsCampaign.id_campaign == resource_id)
        )).scalar_one_or_none()
        if camp_adv != user.id_advertiser:
            raise ApprovalError(404, "Resource not found")
    if not info["can_submit"]:
        raise ApprovalError(
            409, "The resource is not in a submittable state (draft required)")
    if await get_active_request(db, user.id_org, resource_type, resource_id) is not None:
        raise ApprovalError(409, "An approval request is already active for this resource")

    req = CmsApprovalRequest(
        id_org=user.id_org, resource_type=resource_type, resource_id=resource_id,
        submitted_by=user.id_user, status="pending_review",
        notes=notes, due_date=due_date,
        submitted_at=datetime.now(UTC),
    )
    db.add(req)
    await db.flush()  # id_request pour le commentaire/l'audit
    _add_comment(db, req, user, notes or "Submitted for approval", "submitted")
    _add_audit(db, req, user, "submitted")
    return req, _notify_ctx(req, user, info["name"], notes)


async def apply_transition(
    db: AsyncSession, req: CmsApprovalRequest, user: CmsUser, action: str,
    comment: str | None = None,
) -> tuple[str, dict]:
    """Applique approve/reject/request_changes/resubmit + commentaire + audit (sans commit).
    → (event_type §XXII, ctx notification)."""
    allowed_from, to_status = _TRANSITIONS[action]
    if req.status not in allowed_from:
        raise ApprovalError(409, f"Action {action} not allowed from status {req.status}")
    if action in ("reject", "request_changes") and not (comment or "").strip():
        raise ApprovalError(422, "A comment is required for this action")

    req.status = to_status
    req.updated_at = datetime.now(UTC)
    if action in ("approve", "reject"):
        req.reviewed_by = user.id_user
        req.reviewed_at = datetime.now(UTC)
    if action == "approve" and req.resource_type == "campaign":
        await _approve_campaign_creatives(db, req, user)
    if action == "resubmit":
        req.reviewed_by = None
        req.reviewed_at = None
        req.submitted_at = datetime.now(UTC)

    default_text = {
        "approve": "Approved", "reject": "Rejected",
        "request_changes": "Changes requested", "resubmit": "Resubmitted",
    }[action]
    comment_action = {
        "approve": "approved", "reject": "rejected",
        "request_changes": "changes_requested", "resubmit": "resubmitted",
    }[action]
    _add_comment(db, req, user, (comment or "").strip() or default_text, comment_action)
    _add_audit(db, req, user, action)

    info = await resolve_resource(db, req.id_org, req.resource_type, req.resource_id)
    ctx = _notify_ctx(req, user, info["name"] if info else None, comment)
    return ACTION_EVENTS[action], ctx


async def mark_published(
    db: AsyncSession, user: CmsUser, resource_type: str, resource_id: int
) -> CmsApprovalRequest | None:
    """Appelé par le module ressource au moment de SA publication : la demande approuvée
    (s'il y en a une) passe en `published`. Sans commit — s'insère dans la transaction
    du module. Renvoie la demande marquée (ou None si aucune)."""
    req = await get_active_request(db, user.id_org, resource_type, resource_id)
    if req is None or req.status != "approved":
        return None
    req.status = "published"
    req.published_at = datetime.now(UTC)
    req.updated_at = datetime.now(UTC)
    _add_comment(db, req, user, "Published", "published")
    _add_audit(db, req, user, "published")
    return req


async def cancel_for_resource(
    db: AsyncSession, user: CmsUser, resource_type: str, resource_id: int
) -> None:
    """La ressource sort du cycle (transition cancel) → la demande active passe en
    `cancelled` (terminal, hors index actif — la ressource existe toujours). Sans commit."""
    req = await get_active_request(db, user.id_org, resource_type, resource_id)
    if req is None:
        return
    req.status = "cancelled"
    req.updated_at = datetime.now(UTC)
    _add_comment(db, req, user, "Resource deleted — request cancelled", "cancelled")
    _add_audit(db, req, user, "cancelled")


async def purge_for_resource(
    db: AsyncSession, user: CmsUser, resource_type: str, resource_id: int
) -> None:
    """La ressource est SUPPRIMÉE → toutes ses demandes disparaissent de la file
    (retour Ed 11/07 : une campagne supprimée restait affichée « #7 » — nettoyage
    complet, même règle que les liens annonceur). Les commentaires suivent (FK CASCADE) ;
    la trace subsiste dans cms_audit_log. Sans commit."""
    from sqlalchemy import delete as sa_delete

    ids = (await db.execute(
        select(CmsApprovalRequest.id_request).where(
            CmsApprovalRequest.id_org == user.id_org,
            CmsApprovalRequest.resource_type == resource_type,
            CmsApprovalRequest.resource_id == resource_id,
        )
    )).scalars().all()
    if not ids:
        return
    await db.execute(sa_delete(CmsApprovalComment).where(
        CmsApprovalComment.id_request.in_(ids)))
    await db.execute(sa_delete(CmsApprovalRequest).where(
        CmsApprovalRequest.id_request.in_(ids)))
    db.add(CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action="approval_purged",
        resource_type=resource_type, resource_id=str(resource_id),
        detail={"purged_requests": list(ids)}))
