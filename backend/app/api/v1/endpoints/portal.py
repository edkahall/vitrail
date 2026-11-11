"""Portail annonceur — self-service (S7 Phase C2, docs/PLAN_PORTAIL_ANNONCEUR.md).

L'annonceur (user lié `id_advertiser`, rôle CMS_ADVERTISER) crée SES campagnes en
BROUILLON depuis /portal : sites choisis sur la carte, créatifs UPLOADÉS (quotas §3.4
configurables par org — défauts 20 créatifs/campagne, 1 GiB/fichier ; migr 0084 —
nés `pending`, approbation obligatoire pour
tout créatif externe), devis + disponibilité AGRÉGÉE (décision §3.2 : jamais le détail
par player ni les campagnes concurrentes), puis SOUMET → Approval Workflow §21 (C1).
La publication reste au retailer (validation obligatoire, prix compris).

GET    /portal/sites                          — sites de l'org (nom + coords, carte)
GET    /portal/limits                         — quotas d'upload de l'org (UI)
POST   /portal/occupancy-check                — dispo agrégée + devis (réponse STRIPPÉE)
POST   /portal/campaigns                      — créer un brouillon (sites + SOV/Exclusif)
PUT    /portal/campaigns/{id}                 — modifier SON brouillon
POST   /portal/campaigns/{id}/creatives       — uploader un créatif (multipart, quotas)
DELETE /portal/campaigns/{id}/creatives/{id}  — retirer un créatif du brouillon
POST   /portal/campaigns/{id}/submit          — soumettre pour approbation (§21)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db
from app.models.audit_log import CmsAuditLog
from app.models.campaign import (
    CmsAdvertiser,
    CmsAdvertiserSiteList,
    CmsCampaign,
    CmsCampaignCreative,
    CmsCampaignTarget,
)
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsSite
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.user import CmsUser
from app.schemas.analytics import GeoSite
from app.schemas.campaign import CampaignRead, PriceEstimate
from app.services import approvals as approvals_svc
from app.services import notifications
from app.services import pop_counting
from app.services.analytics import effective_status
from app.services.campaign_feasibility import campaign_feasibility
from app.services.campaign_inventory import (
    advertiser_excluded_sites,
    campaign_eligible_players,
    check_audio_occupancy,
    check_campaign_occupancy,
    estimate_audio_price,
    estimate_campaign_price,
)
from app.services.media import delete_file, make_storage_key, stream_upload
from app.services.targeting import Target, players_for_targets

router = APIRouter(prefix="/portal", tags=["portal"])

# Quotas upload annonceur §3.4 — défauts si l'org n'a pas surchargé les siens
# (réglages GET/PUT /campaigns/settings, migr 0084 — retour Ed #6).
DEFAULT_MAX_CREATIVES = 20
DEFAULT_CREATIVE_MAX_MB = 1024
DEFAULT_IMAGE_DURATION_MS = 10_000


async def _org_upload_limits(db: AsyncSession, id_org: int) -> tuple[int, int]:
    """(max créatifs/campagne, taille max en MiB) de l'org."""
    row = (await db.execute(
        select(CmsOrganization.campaign_max_creatives,
               CmsOrganization.campaign_creative_max_mb)
        .where(CmsOrganization.id_org == id_org)
    )).one_or_none()
    if row is None:
        return DEFAULT_MAX_CREATIVES, DEFAULT_CREATIVE_MAX_MB
    return row[0], row[1]

# Types de médias uploadables en créatif (diffusion plein écran).
_PORTAL_MIME_TO_TYPE = {
    "image/jpeg": "image", "image/png": "image", "image/gif": "image",
    "image/webp": "image",
    "video/mp4": "video", "video/quicktime": "video", "video/webm": "video",
}
# Créatifs AUDIO (campagne kind='audio') — map EXCLUSIVE (un créatif audio ne peut pas
# entrer dans une campagne visuelle et réciproquement). Durée posée au transcodage OGG.
_PORTAL_AUDIO_MIME_TO_TYPE = {
    "audio/mpeg": "audio", "audio/mp3": "audio", "audio/ogg": "audio",
    "audio/wav": "audio", "audio/x-wav": "audio", "audio/wave": "audio",
    "audio/aac": "audio", "audio/mp4": "audio", "audio/x-m4a": "audio",
    "audio/webm": "audio",
}


async def _advertiser_allowed_kinds(db: AsyncSession, id_advertiser: int) -> list[str]:
    """Types de campagne que l'annonceur lié peut créer au PORTAIL (flags migr 0095)."""
    row = (await db.execute(
        select(CmsAdvertiser.allow_visual_campaigns, CmsAdvertiser.allow_audio_campaigns)
        .where(CmsAdvertiser.id_advertiser == id_advertiser)
    )).one_or_none()
    kinds: list[str] = []
    if row is None or row[0]:
        kinds.append("visual")
    if row is not None and row[1]:
        kinds.append("audio")
    return kinds


async def require_advertiser(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
) -> CmsUser:
    """Réservé aux comptes annonceur liés (miroir de require_retailer)."""
    if current_user.id_advertiser is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Reserved for advertiser accounts")
    return current_user


async def _own_campaign(
    db: AsyncSession, user: CmsUser, id_campaign: int, *, editable: bool = True
) -> CmsCampaign:
    """Campagne de SON annonceur (cross = 404). editable → brouillon requis (409)."""
    camp = (await db.execute(
        select(CmsCampaign).where(
            CmsCampaign.id_campaign == id_campaign,
            CmsCampaign.id_org == user.id_org,
            CmsCampaign.id_advertiser == user.id_advertiser,
            CmsCampaign.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if camp is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    if editable and camp.status != "draft":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Only a draft can be edited")
    return camp


async def _campaign_read(db: AsyncSession, camp: CmsCampaign) -> CampaignRead:
    from app.api.v1.endpoints.campaigns import _campaign_read as cms_campaign_read
    return await cms_campaign_read(db, camp)


# ── Sites (carte) ─────────────────────────────────────────────────────────────

class PortalSite(BaseModel):
    id_site: int
    site_name: str
    latitude: float | None = None
    longitude: float | None = None
    # Nb d'écrans VENDABLES du site (opt-out déduits) — info commerciale légitime
    # pour l'acheteur ; l'occupation détaillée reste réservée au retailer (§3.2).
    screen_count: int = 0
    # Exclusion concurrentielle (#10) : le site exclut la catégorie d'industrie de
    # CET annonceur — non sélectionnable (l'UI grise le chip/marqueur).
    excluded: bool = False
    # Sélection PAR ÉCRAN (migr 0089) : players vendables du site, exposés SEULEMENT
    # si l'org a activé campaign_portal_player_selection (liste nominative = interne).
    players: list["PortalSitePlayer"] = []


class PortalSitePlayer(BaseModel):
    id_player: int
    player_name: str


async def _org_portal_player_selection(db: AsyncSession, id_org: int) -> bool:
    return bool((await db.execute(
        select(CmsOrganization.campaign_portal_player_selection)
        .where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none())


@router.get("/sites", response_model=list[PortalSite])
async def portal_sites(
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
    kind: Literal["visual", "audio"] = Query("visual"),
) -> list[PortalSite]:
    """Sites de l'org (nom + coordonnées + nb d'écrans vendables) pour le sélecteur
    carte — pas de détail players ni d'occupation (§3.2). Les sites qui excluent la
    catégorie de l'annonceur sont marqués `excluded` (0 écran vendable pour lui).
    Si l'org autorise la sélection par écran, les players vendables sont listés.

    `kind` filtre les players/écrans par CAPACITÉ de diffusion (retour Ed 21/07) :
    une campagne VISUELLE ne compte que les players « Contenus » ou « Contenus +
    Audio » (on exclut l'audio-seul, incapable d'afficher un créatif) ; une campagne
    AUDIO ne compte que « Audio seul » ou « Contenus + Audio »."""
    rows = (await db.execute(
        select(CmsSite).where(CmsSite.id_org == current_user.id_org)
        .order_by(CmsSite.site_name)
    )).scalars().all()
    players_by_site: dict[int, list[PortalSitePlayer]] = {}
    for pid, sid, name, role in (await db.execute(
        select(CmsPlayer.id_player, CmsPlayer.id_site, CmsPlayer.player_name,
               CmsPlayer.player_role)
        .where(CmsPlayer.id_org == current_user.id_org,
               CmsPlayer.id_site.is_not(None),
               CmsPlayer.campaigns_optout == False)  # noqa: E712
        .order_by(CmsPlayer.player_name)
    )).all():
        # Capacité : audio → audio|mixed ; visuel → tout sauf audio-seul.
        capable = (role in ("audio", "mixed")) if kind == "audio" else (role != "audio")
        if not capable:
            continue
        players_by_site.setdefault(sid, []).append(
            PortalSitePlayer(id_player=pid, player_name=name))
    excluded_sites = await advertiser_excluded_sites(db, current_user.id_advertiser)
    expose_players = await _org_portal_player_selection(db, current_user.id_org)
    return [PortalSite(
        id_site=s.id_site, site_name=s.site_name,
        latitude=float(s.latitude) if s.latitude is not None else None,
        longitude=float(s.longitude) if s.longitude is not None else None,
        screen_count=0 if s.id_site in excluded_sites
        else len(players_by_site.get(s.id_site, [])),
        excluded=s.id_site in excluded_sites,
        players=(players_by_site.get(s.id_site, [])
                 if expose_players and s.id_site not in excluded_sites else []),
    ) for s in rows]


# ── Cloche des décisions (retour Ed #8) ──────────────────────────────────────

_DECIDED_STATUSES = ("approved", "rejected", "changes_requested", "published")


class PortalDecision(BaseModel):
    id_request: int
    id_campaign: int
    campaign_name: str | None = None
    status: str
    decided_at: datetime | None = None
    unseen: bool


class PortalDecisions(BaseModel):
    unseen_count: int
    items: list[PortalDecision]


@router.get("/decisions", response_model=PortalDecisions)
async def portal_decisions(
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortalDecisions:
    """Décisions du retailer sur SES demandes (cloche du header portail) — non-vu =
    serveur-side (`submitter_seen_at`, migr 0086), cohérent entre navigateurs."""
    from app.models.approval import CmsApprovalRequest

    rows = (await db.execute(
        select(CmsApprovalRequest, CmsCampaign.campaign_name)
        .join(CmsCampaign, CmsCampaign.id_campaign == CmsApprovalRequest.resource_id)
        .where(
            CmsApprovalRequest.id_org == current_user.id_org,
            CmsApprovalRequest.resource_type == "campaign",
            CmsApprovalRequest.status.in_(_DECIDED_STATUSES),
            CmsCampaign.id_advertiser == current_user.id_advertiser,
            CmsCampaign.is_deleted == False,  # noqa: E712
        )
        .order_by(CmsApprovalRequest.updated_at.desc(),
                  CmsApprovalRequest.id_request.desc())
        .limit(20)
    )).all()
    items = []
    unseen_count = 0
    for req, name in rows:
        decided = req.reviewed_at or req.published_at or req.updated_at
        unseen = req.submitter_seen_at is None or (
            decided is not None and req.submitter_seen_at < decided)
        if unseen:
            unseen_count += 1
        items.append(PortalDecision(
            id_request=req.id_request, id_campaign=req.resource_id,
            campaign_name=name, status=req.status, decided_at=decided, unseen=unseen))
    return PortalDecisions(unseen_count=unseen_count, items=items)


@router.post("/decisions/seen", status_code=status.HTTP_204_NO_CONTENT)
async def portal_decisions_seen(
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Marque toutes les décisions de SON annonceur comme vues (ouverture cloche)."""
    from sqlalchemy import update

    from app.models.approval import CmsApprovalRequest

    camp_ids = select(CmsCampaign.id_campaign).where(
        CmsCampaign.id_org == current_user.id_org,
        CmsCampaign.id_advertiser == current_user.id_advertiser)
    await db.execute(
        update(CmsApprovalRequest)
        .where(CmsApprovalRequest.id_org == current_user.id_org,
               CmsApprovalRequest.resource_type == "campaign",
               CmsApprovalRequest.resource_id.in_(camp_ids),
               CmsApprovalRequest.status.in_(_DECIDED_STATUSES))
        .values(submitter_seen_at=func.now()))
    await db.commit()


# ── Listes de sites enregistrées (retour Ed #11) ─────────────────────────────

class SiteListRead(BaseModel):
    id_list: int
    list_name: str
    site_ids: list[int]


class SiteListCreate(BaseModel):
    list_name: str = Field(min_length=1, max_length=255)
    site_ids: list[int] = Field(min_length=1)


@router.get("/site-lists", response_model=list[SiteListRead])
async def portal_list_site_lists(
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SiteListRead]:
    """Les listes de sites de SON annonceur (rappel de sélection au formulaire)."""
    rows = (await db.execute(
        select(CmsAdvertiserSiteList)
        .where(CmsAdvertiserSiteList.id_org == current_user.id_org,
               CmsAdvertiserSiteList.id_advertiser == current_user.id_advertiser)
        .order_by(CmsAdvertiserSiteList.list_name)
    )).scalars().all()
    return [SiteListRead(id_list=r.id_list, list_name=r.list_name,
                         site_ids=list(r.site_ids or [])) for r in rows]


@router.post("/site-lists", response_model=SiteListRead,
             status_code=status.HTTP_201_CREATED)
async def portal_save_site_list(
    body: SiteListCreate,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteListRead:
    """Enregistre la sélection courante sous un nom — même nom = REMPLACE (upsert,
    l'annonceur met sa liste à jour sans friction). Sites vérifiés dans l'org."""
    valid_ids = set((await db.execute(
        select(CmsSite.id_site).where(
            CmsSite.id_org == current_user.id_org,
            CmsSite.id_site.in_(set(body.site_ids)))
    )).scalars().all())
    unknown = set(body.site_ids) - valid_ids
    if unknown:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Unknown site in the selection")
    site_ids = sorted(valid_ids)

    existing = (await db.execute(
        select(CmsAdvertiserSiteList).where(
            CmsAdvertiserSiteList.id_org == current_user.id_org,
            CmsAdvertiserSiteList.id_advertiser == current_user.id_advertiser,
            CmsAdvertiserSiteList.list_name == body.list_name)
    )).scalar_one_or_none()
    if existing is not None:
        existing.site_ids = site_ids
        row = existing
    else:
        row = CmsAdvertiserSiteList(
            id_org=current_user.id_org, id_advertiser=current_user.id_advertiser,
            list_name=body.list_name, site_ids=site_ids)
        db.add(row)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="update" if existing is not None else "create",
        resource_type="advertiser_site_list", resource_id=body.list_name,
        detail={"sites": len(site_ids), "via": "portal"}))
    await db.commit()
    await db.refresh(row)
    return SiteListRead(id_list=row.id_list, list_name=row.list_name,
                        site_ids=list(row.site_ids or []))


@router.delete("/site-lists/{id_list}", status_code=status.HTTP_204_NO_CONTENT)
async def portal_delete_site_list(
    id_list: int,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    row = (await db.execute(
        select(CmsAdvertiserSiteList).where(
            CmsAdvertiserSiteList.id_list == id_list,
            CmsAdvertiserSiteList.id_org == current_user.id_org,
            CmsAdvertiserSiteList.id_advertiser == current_user.id_advertiser)
    )).scalar_one_or_none()
    if row is None:  # cross-annonceur = 404, jamais 403
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="List not found")
    await db.delete(row)
    await db.commit()


# ── Disponibilité agrégée + devis ─────────────────────────────────────────────

class PortalOccupancyRequest(BaseModel):
    starts_on: date
    ends_on: date
    sov_percent: float = Field(gt=0, le=100)
    site_ids: list[int] = []
    # Sélection par écran (migr 0089, si le réglage org l'autorise)
    player_ids: list[int] = []
    exclude_id_campaign: int | None = None
    # S3 audio : kind='audio' → projection en SPOTS/HEURE (spots_per_hour requis)
    campaign_kind: Literal["visual", "audio"] = "visual"
    spots_per_hour: int | None = Field(default=None, ge=1, le=60)


class PortalOccupancyResult(BaseModel):
    """Réponse STRIPPÉE (décision §3.2) : dispo agrégée, jamais le détail par player
    ni les campagnes concurrentes. availability: ok | tight | full.
    achievable_sov_pct = SOV max réalisable (visuel) ; achievable_spots_per_hour =
    équivalent audio (plafond restant du pire écran) — agrégats seuls (stripping)."""

    availability: str
    price: PriceEstimate | None = None
    achievable_sov_pct: float | None = None
    achievable_spots_per_hour: int | None = None


@router.post("/occupancy-check", response_model=PortalOccupancyResult)
async def portal_occupancy_check(
    body: PortalOccupancyRequest,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortalOccupancyResult:
    if body.ends_on < body.starts_on:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="ends_on must be ≥ starts_on")
    # Gate : l'annonceur ne peut sonder que les types qu'on lui a autorisés (migr 0095).
    if body.campaign_kind not in await _advertiser_allowed_kinds(db, current_user.id_advertiser):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Campaign type not allowed")
    # Les cibles sondées doivent exister DANS l'organisation (audit menu ⑨) : la création
    # les valide (`_check_portal_targets`), le SONDAGE ne le faisait pas. Un site étranger
    # résout à zéro écran ⇒ la réponse annonçait « disponible » et le devis portait sur du
    # vide — c'est précisément l'écran sur lequel l'annonceur décide d'acheter.
    await _check_sites(db, current_user.id_org, body.site_ids)
    await _check_portal_players(db, current_user, body.player_ids)
    targets = ([Target(target_type="site", target_id=i, tag_expr={}) for i in body.site_ids]
               + [Target(target_type="player", target_id=i, tag_expr={})
                  for i in body.player_ids])

    # ── Audio : projection en spots/heure (STRIPPÉE) + devis audio ────────────────
    if body.campaign_kind == "audio":
        if not body.spots_per_hour:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="spots_per_hour is required for an audio campaign")
        res = await check_audio_occupancy(
            db, current_user.id_org, starts_on=body.starts_on, ends_on=body.ends_on,
            spots_per_hour=body.spots_per_hour, targets=targets,
            exclude_id_campaign=body.exclude_id_campaign,
            id_advertiser=current_user.id_advertiser,
        )
        price = await estimate_audio_price(
            db, current_user.id_org, targets=targets, spots_per_hour=body.spots_per_hour,
            starts_on=body.starts_on, ends_on=body.ends_on,
            id_advertiser=current_user.id_advertiser,
        )
        availability = "full" if res["blocked"] else ("tight" if res["warning"] else "ok")
        return PortalOccupancyResult(
            availability=availability,
            price=PriceEstimate(**price) if price else None,
            achievable_spots_per_hour=res.get("achievable_spots_per_hour"),
        )

    # ── Visuel : occupation SOV + devis + SOV réalisable ──────────────────────────
    res = await check_campaign_occupancy(
        db, current_user.id_org, starts_on=body.starts_on, ends_on=body.ends_on,
        sov_percent=body.sov_percent, targets=targets,
        exclude_id_campaign=body.exclude_id_campaign,
        id_advertiser=current_user.id_advertiser,
    )
    price = await estimate_campaign_price(
        db, current_user.id_org, targets=targets, sov_percent=body.sov_percent,
        starts_on=body.starts_on, ends_on=body.ends_on,
        id_advertiser=current_user.id_advertiser,
    )
    # SOV réalisable (anti-martèlement) — agrégat STRIPPÉ (jamais le détail players).
    feas = await campaign_feasibility(
        db, current_user.id_org, targets=targets,
        id_campaign=body.exclude_id_campaign,
        id_advertiser=current_user.id_advertiser,
    )
    availability = "full" if res["blocked"] else ("tight" if res["warning"] else "ok")
    return PortalOccupancyResult(
        availability=availability,
        price=PriceEstimate(**price) if price else None,
        achievable_sov_pct=feas["max_sov_pct"] if feas else None,
    )


# ── Campagnes (brouillons annonceur) ──────────────────────────────────────────

class PortalCampaignBody(BaseModel):
    campaign_name: str = Field(min_length=1, max_length=255)
    starts_on: date
    ends_on: date
    # Exclusif = privatisation des écrans (décision cadrage : SOV 100 %) — VISUEL only
    exclusive: bool = False
    sov_percent: float = Field(default=10, gt=0, le=100)
    # Cibles : sites entiers et/ou écrans précis (player_ids exige le réglage org
    # campaign_portal_player_selection — migr 0089). Au moins une cible au total.
    site_ids: list[int] = []
    player_ids: list[int] = []
    # S3 audio : kind='audio' → vend des spots/heure (spots_per_hour) au lieu de la SOV.
    # Kind IMMUABLE après création (422 en update si différent).
    campaign_kind: Literal["visual", "audio"] = "visual"
    spots_per_hour: int | None = Field(default=None, ge=1, le=60)


async def _check_sites(db: AsyncSession, id_org: int, site_ids: list[int]) -> None:
    if not site_ids:
        return
    n = (await db.execute(
        select(func.count()).select_from(CmsSite).where(
            CmsSite.id_org == id_org, CmsSite.id_site.in_(site_ids))
    )).scalar_one()
    if n != len(set(site_ids)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")


async def _check_portal_players(
    db: AsyncSession, user: CmsUser, player_ids: list[int]
) -> None:
    """Écrans du self-service : réglage org actif + players VENDABLES de l'org (site
    rattaché, pas d'opt-out). Extrait de `_check_portal_targets` pour être appelé aussi
    par le SONDAGE de disponibilité, qui ne validait rien."""
    if not player_ids:
        return
    if not await _org_portal_player_selection(db, user.id_org):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Per-screen selection is not enabled")
    valid = set((await db.execute(
        select(CmsPlayer.id_player).where(
            CmsPlayer.id_org == user.id_org,
            CmsPlayer.id_player.in_(set(player_ids)),
            CmsPlayer.id_site.is_not(None),
            CmsPlayer.campaigns_optout == False,  # noqa: E712
        )
    )).scalars().all())
    if set(player_ids) - valid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Screen not found")


async def _check_portal_targets(
    db: AsyncSession, user: CmsUser, body: PortalCampaignBody
) -> None:
    """Valide les cibles du self-service : ≥ 1 cible, sites de l'org, et players
    vendables (helper partagé avec le sondage de disponibilité)."""
    if not body.site_ids and not body.player_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="At least one site or screen is required")
    await _check_sites(db, user.id_org, body.site_ids)
    await _check_portal_players(db, user, body.player_ids)


def _apply_body(camp: CmsCampaign, body: PortalCampaignBody) -> None:
    if body.ends_on < body.starts_on:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="ends_on must be ≥ starts_on")
    # Le TYPE de campagne est immuable après création (change l'inventaire, les créatifs
    # et la tarification — un basculement en cours de vie n'a pas de sens).
    if body.campaign_kind != camp.campaign_kind:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="The campaign type cannot be changed")
    camp.campaign_name = body.campaign_name
    camp.starts_on = body.starts_on
    camp.ends_on = body.ends_on
    if camp.campaign_kind == "audio":
        if not body.spots_per_hour:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="spots_per_hour is required for an audio campaign")
        camp.spots_per_hour = body.spots_per_hour
    else:
        camp.sov_percent = 100.0 if body.exclusive else body.sov_percent


async def _gate_kind(db: AsyncSession, user: CmsUser, kind: str) -> None:
    """403 si l'annonceur lié n'a pas le droit de créer ce type (flags migr 0095)."""
    if kind not in await _advertiser_allowed_kinds(db, user.id_advertiser):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Campaign type not allowed")


@router.post("/campaigns", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def portal_create_campaign(
    body: PortalCampaignBody,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    """Brouillon de campagne de l'annonceur lié — cibles = sites entiers et/ou
    écrans précis (sélection par écran si le réglage org l'autorise, migr 0089)."""
    if body.ends_on < body.starts_on:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="ends_on must be ≥ starts_on")
    await _gate_kind(db, current_user, body.campaign_kind)
    await _check_portal_targets(db, current_user, body)
    if body.campaign_kind == "audio":
        if not body.spots_per_hour:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="spots_per_hour is required for an audio campaign")
        camp = CmsCampaign(
            id_org=current_user.id_org, id_advertiser=current_user.id_advertiser,
            campaign_name=body.campaign_name, status="draft",
            starts_on=body.starts_on, ends_on=body.ends_on,
            campaign_kind="audio", spots_per_hour=body.spots_per_hour,
            sov_percent=body.sov_percent,  # ignoré en audio (colonne non-null)
            pricing_model="flat",
        )
    else:
        camp = CmsCampaign(
            id_org=current_user.id_org, id_advertiser=current_user.id_advertiser,
            campaign_name=body.campaign_name, status="draft",
            starts_on=body.starts_on, ends_on=body.ends_on,
            sov_percent=100.0 if body.exclusive else body.sov_percent,
            pricing_model="sov",
        )
    db.add(camp)
    await db.flush()
    for sid in dict.fromkeys(body.site_ids):
        db.add(CmsCampaignTarget(id_org=current_user.id_org, id_campaign=camp.id_campaign,
                                 target_type="site", target_id=sid))
    for pid in dict.fromkeys(body.player_ids):
        db.add(CmsCampaignTarget(id_org=current_user.id_org, id_campaign=camp.id_campaign,
                                 target_type="player", target_id=pid))
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="campaign", resource_id=str(camp.id_campaign),
        detail={"name": camp.campaign_name, "via": "portal"}))
    await db.commit()
    await db.refresh(camp)
    return await _campaign_read(db, camp)


@router.put("/campaigns/{id_campaign}", response_model=CampaignRead)
async def portal_update_campaign(
    id_campaign: int,
    body: PortalCampaignBody,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    camp = await _own_campaign(db, current_user, id_campaign)
    await _check_portal_targets(db, current_user, body)
    _apply_body(camp, body)
    # Remplace les cibles (sites entiers + écrans précis)
    existing = (await db.execute(
        select(CmsCampaignTarget).where(CmsCampaignTarget.id_campaign == id_campaign)
    )).scalars().all()
    for t in existing:
        await db.delete(t)
    for sid in dict.fromkeys(body.site_ids):
        db.add(CmsCampaignTarget(id_org=current_user.id_org, id_campaign=id_campaign,
                                 target_type="site", target_id=sid))
    for pid in dict.fromkeys(body.player_ids):
        db.add(CmsCampaignTarget(id_org=current_user.id_org, id_campaign=id_campaign,
                                 target_type="player", target_id=pid))
    camp.updated_at = func.now()
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="update",
        resource_type="campaign", resource_id=str(id_campaign),
        detail={"name": camp.campaign_name, "via": "portal"}))
    await db.commit()
    await db.refresh(camp)
    return await _campaign_read(db, camp)


@router.delete("/campaigns/{id_campaign}", status_code=status.HTTP_204_NO_CONTENT)
async def portal_delete_campaign(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Supprime SON brouillon (demande d'Ed, 2026-07-31 — le portail n'avait aucune route
    de suppression : une campagne créée par erreur obligeait à écrire au retailer).

    🔑 Ce qui décide, ce n'est pas seulement le statut de la campagne mais **à qui la main
    appartient** : une fois SOUMISE, la campagne est chez le valideur — la supprimer ferait
    disparaître une demande de SA file pendant qu'il l'examine. On refuse donc en 409 en
    nommant le geste (« retirez d'abord votre demande »), sauf si le retailer a rendu la
    main (`changes_requested`) : la demande est alors purgée avec la campagne, comme le
    fait la suppression côté CMS.

    Suppression LOGIQUE (`is_deleted`), cohérente avec `DELETE /campaigns/{id}` : les
    médias uploadés RESTENT dans le dossier « Annonceurs/<nom> » (ils appartiennent à la
    médiathèque du retailer et peuvent servir à une autre campagne).
    """
    camp = await _own_campaign(db, current_user, id_campaign)  # 404 cross · 409 non-brouillon
    req = await approvals_svc.get_active_request(
        db, current_user.id_org, "campaign", id_campaign)
    if req is not None and req.status != "changes_requested":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Campaign submitted for approval — it can no longer be deleted "
                   "from the portal until the retailer hands it back to you",
        )
    camp.is_deleted = True
    await approvals_svc.purge_for_resource(db, current_user, "campaign", id_campaign)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="delete",
        resource_type="campaign", resource_id=str(id_campaign),
        detail={"name": camp.campaign_name, "via": "portal"}))
    await db.commit()


# ── Créatifs (upload direct, quotas §3.4) ─────────────────────────────────────

class PortalLimitsRead(BaseModel):
    max_creatives: int
    creative_max_mb: int
    # Durée max d'un créatif (garde-fou boucle courte, migr 0089) — None = illimité
    creative_max_duration_s: int | None = None
    # Sélection par écran autorisée sur ce portail (réglage org, migr 0089)
    player_selection: bool = False
    # Types de campagne que l'annonceur lié peut créer (flags migr 0095)
    allowed_kinds: list[str] = ["visual"]


@router.get("/limits", response_model=PortalLimitsRead)
async def portal_limits(
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortalLimitsRead:
    """Quotas/réglages de l'org affichés par l'UI du portail (read-only)."""
    max_creatives, max_mb = await _org_upload_limits(db, current_user.id_org)
    row = (await db.execute(
        select(CmsOrganization.campaign_creative_max_duration_s,
               CmsOrganization.campaign_portal_player_selection)
        .where(CmsOrganization.id_org == current_user.id_org)
    )).one_or_none()
    return PortalLimitsRead(
        max_creatives=max_creatives, creative_max_mb=max_mb,
        creative_max_duration_s=row[0] if row else None,
        player_selection=bool(row[1]) if row else False,
        allowed_kinds=await _advertiser_allowed_kinds(db, current_user.id_advertiser),
    )


# ── Tableau de bord annonceur (accueil portail) ───────────────────────────────

class PortalOverviewCampaigns(BaseModel):
    total: int = 0
    active: int = 0  # approuvées et en cours (dates courantes)
    visual: int = 0
    audio: int = 0


class PortalOverviewPlayers(BaseModel):
    total: int = 0
    online: int = 0
    offline: int = 0
    warning: int = 0
    maintenance: int = 0


class PortalActivityItem(BaseModel):
    id: int
    action: str
    resource_type: str
    resource_id: str | None = None
    user_name: str | None = None
    created_at: datetime


class PortalOverview(BaseModel):
    """Synthèse de l'accueil portail, SCOPÉE à l'annonceur lié : ses campagnes, les
    players qu'elles ciblent (statut effectif), son Proof of Play, ses sites géolocalisés
    et son activité récente. Aucune fuite d'un autre annonceur ni de concurrent."""

    campaigns: PortalOverviewCampaigns
    players: PortalOverviewPlayers
    proof_of_play_24h: int = 0
    sites: list[GeoSite] = []
    activity: list[PortalActivityItem] = []


@router.get("/overview", response_model=PortalOverview)
async def portal_overview(
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortalOverview:
    org = current_user.id_org
    adv = current_user.id_advertiser
    now = datetime.now(UTC)
    today = date.today()

    camps = list((await db.execute(
        select(CmsCampaign).where(
            CmsCampaign.id_org == org, CmsCampaign.id_advertiser == adv,
            CmsCampaign.is_deleted == False,  # noqa: E712
        )
    )).scalars().all())
    camp_ids = [c.id_campaign for c in camps]
    counts = PortalOverviewCampaigns(
        total=len(camps),
        active=sum(1 for c in camps if c.status == "approved"
                   and c.starts_on <= today <= c.ends_on),
        visual=sum(1 for c in camps if (c.campaign_kind or "visual") != "audio"),
        audio=sum(1 for c in camps if c.campaign_kind == "audio"),
    )

    # ── Proof of Play 24 h : diffusions des créatifs de SES campagnes ─────────────
    media_ids: set[int] = set()
    if camp_ids:
        media_ids = set((await db.execute(
            select(CmsCampaignCreative.id_media).where(
                CmsCampaignCreative.id_campaign.in_(camp_ids))
        )).scalars().all())
    pop_24h = 0
    if media_ids:
        pop_24h = int((await db.execute(
            select(func.count()).select_from(CmsProofOfPlay).where(
                CmsProofOfPlay.id_org == org,
                CmsProofOfPlay.id_media.in_(media_ids),
                CmsProofOfPlay.played_at >= now - timedelta(hours=24),
                # Surface ANNONCEUR : ne jamais afficher comme diffusé un spot joué alors
                # que l'écran était coupé (migr 0102) ou recouvert par un autre overlay
                # plein écran — takeover VOD (migr 0125).
                pop_counting.COUNTED,
            )
        )).scalar() or 0)

    # ── Players ciblés par ses campagnes LIVE (approuvées, non expirées) ──────────
    targeted: set[int] = set()
    for c in camps:
        if c.status != "approved" or c.ends_on < today:
            continue
        trows = (await db.execute(
            select(CmsCampaignTarget).where(CmsCampaignTarget.id_campaign == c.id_campaign)
        )).scalars().all()
        if not trows:
            continue
        pids = await campaign_eligible_players(db, await players_for_targets(
            db, org,
            [Target(target_type=t.target_type, target_id=t.target_id, tag_expr=t.tag_expr or {})
             for t in trows],
        ), adv)
        targeted |= pids

    players = PortalOverviewPlayers()
    sites: list[GeoSite] = []
    if targeted:
        prows = (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.id_site,
                   CmsPlayer.status, CmsPlayer.last_seen_at)
            .where(CmsPlayer.id_player.in_(targeted))
        )).all()

        def bucket(status: str) -> str:
            if status == "online":
                return "online"
            if status == "warning":
                return "warning"
            if status == "maintenance":
                return "maintenance"
            return "offline"  # offline + critical

        by_site: dict[int, dict[str, int]] = {}
        for p in prows:
            b = bucket(effective_status(p.status, p.last_seen_at, now))
            players.total += 1
            setattr(players, b, getattr(players, b) + 1)
            if p.id_site is not None:
                c = by_site.setdefault(p.id_site, {"total": 0, "online": 0, "offline": 0,
                                                   "warning": 0, "maintenance": 0})
                c["total"] += 1
                c[b] += 1

        if by_site:
            site_rows = (await db.execute(
                select(CmsSite.id_site, CmsSite.site_name, CmsSite.country,
                       CmsSite.site_number, CmsSite.latitude, CmsSite.longitude)
                .where(CmsSite.id_site.in_(by_site.keys()))
            )).all()
            for s in site_rows:
                if s.latitude is None or s.longitude is None:
                    continue
                c = by_site[s.id_site]
                sites.append(GeoSite(
                    id_site=s.id_site, site_name=s.site_name, country=s.country,
                    site_number=s.site_number,
                    latitude=float(s.latitude), longitude=float(s.longitude),
                    players_total=c["total"], players_online=c["online"],
                    players_warning=c["warning"], players_offline=c["offline"],
                    players_maintenance=c["maintenance"],
                ))

    # ── Activité récente : audit des SES campagnes ───────────────────────────────
    activity: list[PortalActivityItem] = []
    if camp_ids:
        arows = list((await db.execute(
            select(CmsAuditLog, CmsUser.full_name, CmsUser.email)
            .join(CmsUser, CmsUser.id_user == CmsAuditLog.id_user, isouter=True)
            .where(
                CmsAuditLog.id_org == org,
                CmsAuditLog.resource_type == "campaign",
                CmsAuditLog.resource_id.in_([str(i) for i in camp_ids]),
            )
            .order_by(CmsAuditLog.created_at.desc())
            .limit(10)
        )).all())
        activity = [PortalActivityItem(
            id=a.id_audit, action=a.action, resource_type=a.resource_type,
            resource_id=a.resource_id, user_name=full or email, created_at=a.created_at,
        ) for a, full, email in arows]

    return PortalOverview(
        campaigns=counts, players=players, proof_of_play_24h=pop_24h,
        sites=sites, activity=activity,
    )


class PortalCreativeRead(BaseModel):
    id_creative: int
    id_media: int
    media_name: str
    media_type: str
    duration_ms: int | None = None
    approval_status: str


@router.get("/campaigns/{id_campaign}/creatives", response_model=list[PortalCreativeRead])
async def portal_list_creatives(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PortalCreativeRead]:
    await _own_campaign(db, current_user, id_campaign, editable=False)
    rows = (await db.execute(
        select(CmsCampaignCreative, CmsMedia.media_name, CmsMedia.media_type)
        .join(CmsMedia, CmsMedia.id_media == CmsCampaignCreative.id_media)
        .where(CmsCampaignCreative.id_campaign == id_campaign,
               CmsCampaignCreative.status == "active")
        .order_by(CmsCampaignCreative.sort_order, CmsCampaignCreative.id_creative)
    )).all()
    return [PortalCreativeRead(
        id_creative=cr.id_creative, id_media=cr.id_media, media_name=name,
        media_type=mtype, duration_ms=cr.duration_ms, approval_status=cr.approval_status,
    ) for cr, name, mtype in rows]


@router.post("/campaigns/{id_campaign}/creatives", response_model=PortalCreativeRead,
             status_code=status.HTTP_201_CREATED)
async def portal_upload_creative(
    id_campaign: int,
    file: UploadFile,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
    duration_ms: int | None = Form(None),
) -> PortalCreativeRead:
    """Upload direct d'un créatif (vidéo/image) dans le brouillon : quotas de l'org
    (défauts 20/campagne, 1 GiB/fichier) ; média rangé dans le dossier
    « Annonceurs/<nom> » ; créatif né `pending` (approbation OBLIGATOIRE pour tout
    créatif externe — l'approbation §21 de la campagne l'approuve). Transcodage =
    chaîne normale."""
    camp = await _own_campaign(db, current_user, id_campaign)
    max_creatives, max_mb = await _org_upload_limits(db, current_user.id_org)
    max_bytes = max_mb * 1024 * 1024

    n = (await db.execute(
        select(func.count()).select_from(CmsCampaignCreative).where(
            CmsCampaignCreative.id_campaign == id_campaign,
            CmsCampaignCreative.status == "active")
    )).scalar_one()
    if n >= max_creatives:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Quota reached: {max_creatives} creatives max")

    # Formats acceptés EXCLUSIFS par type de campagne : audio → fichiers audio only,
    # visuel → images/vidéos only (415 sinon).
    content_type = file.content_type or "application/octet-stream"
    is_audio = camp.campaign_kind == "audio"
    mime_map = _PORTAL_AUDIO_MIME_TO_TYPE if is_audio else _PORTAL_MIME_TO_TYPE
    media_type = mime_map.get(content_type)
    if media_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=("Accepted audio formats: MP3, OGG, WAV, AAC, M4A" if is_audio
                    else "Accepted formats: JPEG, PNG, GIF, WebP, MP4, MOV, WebM"))
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"File > {max_mb} MB")

    storage_key = make_storage_key(current_user.id_org, file.filename or "creative")
    sha256, file_size = await stream_upload(file.file, storage_key, content_type)
    if file_size > max_bytes:  # taille inconnue à l'entrée → garde post-upload
        await delete_file(storage_key)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"File > {max_mb} MB")

    # Dossier médiathèque de l'annonceur (créé si absent — même helper que le CMS).
    from app.api.v1.endpoints.campaigns import _ensure_advertiser_folder
    adv = (await db.execute(
        select(CmsAdvertiser).where(CmsAdvertiser.id_advertiser == current_user.id_advertiser)
    )).scalar_one()
    await _ensure_advertiser_folder(db, adv)

    media = CmsMedia(
        id_org=current_user.id_org,
        media_name=file.filename or storage_key,
        media_type=media_type,
        mime_type=content_type,
        storage_key=storage_key,
        file_size_bytes=file_size,
        checksum_sha256=sha256,
        source="upload",
        transcode_status="pending",
        encode_mode="auto",
        created_by=current_user.id_user,
        id_folder=adv.id_folder,
    )
    db.add(media)
    await db.flush()
    # Garde-fou boucle courte (migr 0089) : une image avec durée explicite se vérifie
    # ICI ; une vidéo n'a sa durée qu'après transcodage → re-vérifiée au SUBMIT.
    from app.api.v1.endpoints.campaigns import _check_creative_duration
    try:
        await _check_creative_duration(db, current_user.id_org, media, duration_ms)
    except HTTPException:
        await delete_file(storage_key)
        raise
    creative = CmsCampaignCreative(
        id_org=current_user.id_org, id_campaign=id_campaign, id_media=media.id_media,
        duration_ms=(duration_ms if duration_ms
                     else (DEFAULT_IMAGE_DURATION_MS if media_type == "image" else None)),
        approval_status="pending",  # créatif EXTERNE = revue obligatoire (décision §3.4)
    )
    db.add(creative)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="campaign_creative", resource_id=str(media.id_media),
        detail={"id_campaign": id_campaign, "via": "portal", "name": media.media_name}))
    await db.commit()
    await db.refresh(creative)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)

    return PortalCreativeRead(
        id_creative=creative.id_creative, id_media=media.id_media,
        media_name=media.media_name, media_type=media_type,
        duration_ms=creative.duration_ms, approval_status=creative.approval_status,
    )


@router.delete("/campaigns/{id_campaign}/creatives/{id_creative}",
               status_code=status.HTTP_204_NO_CONTENT)
async def portal_delete_creative(
    id_campaign: int,
    id_creative: int,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Retire un créatif du BROUILLON (le média reste dans le dossier annonceur)."""
    await _own_campaign(db, current_user, id_campaign)
    cr = (await db.execute(
        select(CmsCampaignCreative).where(
            CmsCampaignCreative.id_creative == id_creative,
            CmsCampaignCreative.id_campaign == id_campaign,
        )
    )).scalar_one_or_none()
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creative not found")
    await db.delete(cr)
    await db.commit()


# ── Soumission (§21) ──────────────────────────────────────────────────────────

class PortalSubmitResult(BaseModel):
    id_request: int
    status: str


@router.post("/campaigns/{id_campaign}/submit", response_model=PortalSubmitResult,
             status_code=status.HTTP_201_CREATED)
async def portal_submit_campaign(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(require_advertiser)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortalSubmitResult:
    """Soumet le brouillon à la validation du retailer (Approval Workflow §21 — même
    moteur que C1 : file /approvals côté CMS, previews, devis, breakdown). Exige au
    moins une cible ET un créatif (miroir du gate publish)."""
    await _own_campaign(db, current_user, id_campaign)
    n_creatives = (await db.execute(
        select(func.count()).select_from(CmsCampaignCreative).where(
            CmsCampaignCreative.id_campaign == id_campaign,
            CmsCampaignCreative.status == "active")
    )).scalar_one()
    n_targets = (await db.execute(
        select(func.count()).select_from(CmsCampaignTarget).where(
            CmsCampaignTarget.id_campaign == id_campaign)
    )).scalar_one()
    if not n_creatives or not n_targets:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one creative and one target are required before submitting")
    # Garde-fou boucle courte : les vidéos uploadées ont leur durée après transcodage
    # → le dépassement se détecte au plus tard ICI (avant d'atteindre le valideur).
    from app.api.v1.endpoints.campaigns import creative_duration_violations
    violations = await creative_duration_violations(db, current_user.id_org, id_campaign)
    if violations:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Creative(s) too long: " + " · ".join(violations))
    try:
        req, ctx = await approvals_svc.submit(
            db, current_user, "campaign", id_campaign)
    except approvals_svc.ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    await db.commit()
    await db.refresh(req)
    try:
        await notifications.dispatch_async(db, current_user.id_org, "approval_requested", ctx)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return PortalSubmitResult(id_request=req.id_request, status=req.status)
