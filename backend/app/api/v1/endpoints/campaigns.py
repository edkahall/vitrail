"""Retail Media §2.8 — annonceurs & campagnes (S1 socle, migr 0074).

CRUD annonceurs/campagnes/créatifs/cibles. RBAC = module `campaigns` (seedé 0002,
rôle CMS_CAMPAIGN_MANAGER). Cross-tenant = 404. Les cibles reprennent le contrat
canonique des schedules (services/targeting.py) — la diffusion (S3) et le calcul
d'occupation SOV (S2) se grefferont dessus sans changer ce contrat.
Statut : `status` stocké (draft/approved/paused/cancelled/completed) ; le statut
EFFECTIF (live = approved + aujourd'hui dans [starts_on, ends_on]) est calculé à
la lecture — pas de worker de bascule en S1.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import (
    CAMPAIGN_IMPORT_SCOPE,
    CampaignImportActor,
    get_current_user,
    get_db,
    get_db_worker,
    require_campaign_import_actor,
    require_org_admin,
    require_permission,
)
from app.core.security import hash_password
from app.core.ssrf import url_is_safe
from app.models.audit_log import CmsAuditLog
from app.models.campaign import (
    CmsAdvertiser,
    CmsAdvertiserIndustryCategory,
    CmsCampaign,
    CmsCampaignCreative,
    CmsCampaignTarget,
    CmsIndustryCategory,
    CmsSiteExcludedCategory,
)
from app.models.media import CmsMedia
from app.models.media_folder import CmsMediaFolder
from app.models.organization import CmsOrganization
from app.models.player import CmsPlayer
from app.models.report import CmsReportShareLink
from app.models.user import CmsApiKey, CmsUser
from app.schemas.analytics import BiKeyCreate, BiKeyRead, ShareLinkCreate, ShareLinkRead
from app.schemas.campaign import (
    AdvertiserCreate,
    AdvertiserRead,
    AdvertiserUpdate,
    AdvertiserUserInvite,
    AdvertiserUserInviteResult,
    AdvertiserUserRead,
    CampaignApprovalInfo,
    CampaignCreate,
    CampaignImportOrder,
    CampaignRead,
    CampaignSettingsRead,
    CampaignSettingsUpdate,
    CampaignStatusUpdate,
    CampaignTargetRead,
    CampaignUpdate,
    CreativeCreate,
    CreativeRead,
    CreativeReview,
    CreativeUpdate,
    ImportDryRunResult,
    ImportResolvedCreative,
    ImportResolvedTarget,
    IndustryCategoryCreate,
    IndustryCategoryRead,
    InventoryRow,
    OccupancyCheckRequest,
    OccupancyCheckResult,
    PortalBranding,
)
from app.services import approvals as approvals_svc
from app.services import campaign_report, campaign_report_pdf, rbac, report_render
from app.services.campaign_feasibility import campaign_feasibility
from app.services.campaign_inventory import (
    campaign_eligible_players,
    check_audio_occupancy,
    check_campaign_occupancy,
    estimate_audio_price,
    estimate_campaign_price,
    occupancy_by_day,
    occupancy_by_player,
)
from app.services.session_revocation import revocation_bound
from app.services.targeting import Target, players_for_targets

router = APIRouter(tags=["campaigns"])

# Types de médias diffusables en créatif de campagne, PAR TYPE de campagne :
# visual = boucle plein écran (PoP natif) ; audio = spot dans la rotation musicale §XIX.
_CREATIVE_MEDIA_TYPES = ("video", "image")
_CREATIVE_MEDIA_TYPES_BY_KIND = {"visual": ("video", "image"), "audio": ("audio",)}

# Transitions autorisées : action → (statuts source acceptés, statut cible)
_STATUS_ACTIONS = {
    "publish": (("draft",), "approved"),
    "pause": (("approved",), "paused"),
    "resume": (("paused",), "approved"),
    "cancel": (("draft", "approved", "paused"), "cancelled"),
    "complete": (("approved", "paused"), "completed"),
}


def _effective_status(c: CmsCampaign, today: date | None = None) -> str:
    """Statut effectif : `approved` devient `scheduled`/`live`/`completed` selon les dates."""
    today = today or date.today()
    if c.status != "approved":
        return c.status
    if today < c.starts_on:
        return "scheduled"
    if today > c.ends_on:
        return "completed"
    return "live"


async def _get_campaign(db: AsyncSession, user: CmsUser, id_campaign: int) -> CmsCampaign:
    """Campagne de l'org du user, 404 sinon. Portail annonceur (B1) : un user lié à un
    annonceur ne voit QUE les campagnes de SON annonceur — cross-annonceur = 404,
    jamais 403 (même règle que le multi-tenant)."""
    camp = (await db.execute(
        select(CmsCampaign).where(
            CmsCampaign.id_campaign == id_campaign,
            CmsCampaign.id_org == user.id_org,
            CmsCampaign.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if camp is None or (
        user.id_advertiser is not None and camp.id_advertiser != user.id_advertiser
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return camp


async def require_retailer(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
) -> CmsUser:
    """Bloque les comptes annonceur (portail B1) sur les endpoints org-wide : inventaire,
    occupation, réglages… — leur contenu fuiterait la pression commerciale des concurrents."""
    if current_user.id_advertiser is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Reserved for the retailer")
    return current_user


async def _campaign_read(db: AsyncSession, camp: CmsCampaign) -> CampaignRead:
    adv = (await db.execute(
        select(CmsAdvertiser.advertiser_name).where(
            CmsAdvertiser.id_advertiser == camp.id_advertiser)
    )).scalar_one_or_none()
    n_creatives = (await db.execute(
        select(func.count()).select_from(CmsCampaignCreative).where(
            CmsCampaignCreative.id_campaign == camp.id_campaign,
            CmsCampaignCreative.status == "active",
        )
    )).scalar() or 0
    targets = (await db.execute(
        select(CmsCampaignTarget).where(CmsCampaignTarget.id_campaign == camp.id_campaign)
        .order_by(CmsCampaignTarget.id_target)
    )).scalars().all()
    # Nb de players ciblés RÉSOLUS (même résolution que la diffusion : union des
    # cibles canoniques, moins les players opt-out) — pastille de comptage du drawer.
    pids: set[int] = set()
    if targets:
        pids = await players_for_targets(db, camp.id_org, [
            Target(target_type=t.target_type, target_id=t.target_id, tag_expr=t.tag_expr or {})
            for t in targets
        ])
        pids = await campaign_eligible_players(db, pids, camp.id_advertiser)
    out = CampaignRead.model_validate(camp)
    out.status = _effective_status(camp)
    out.advertiser_name = adv
    out.creative_count = n_creatives
    out.target_player_count = len(pids)
    out.targets = [CampaignTargetRead.model_validate(t) for t in targets]
    await _fill_target_names(db, camp.id_org, out.targets)
    # Demande d'approbation §XXI active (C1) — badge UI + le publish la vérifie.
    appr = await approvals_svc.get_active_request(
        db, camp.id_org, "campaign", camp.id_campaign)
    if appr is not None:
        out.approval_request = CampaignApprovalInfo(
            id_request=appr.id_request, status=appr.status)
    return out


async def _fill_target_names(db: AsyncSession, id_org: int, targets: list) -> None:
    """Nomme les cibles CÔTÉ SERVEUR, en lot (une requête par type présent).

    🔴 Sans cela, l'écran des campagnes ne peut nommer une cible qu'en ayant chargé la
    liste complète de l'entité — dont **tout le parc d'écrans**, à l'ouverture de la page
    (≈1,9 Ko par écran : le défaut d'échelle relevé par Ed sur les Événements le 29/07).
    Les noms résolus ici libèrent la page de cette dépendance.
    """
    from app.models.organization import CmsSite
    from app.models.player_group import CmsPlayerGroup
    from app.models.site_group import CmsSiteGroup

    models = {
        "player": (CmsPlayer, CmsPlayer.id_player, CmsPlayer.player_name),
        "group": (CmsPlayerGroup, CmsPlayerGroup.id_group, CmsPlayerGroup.group_name),
        "site": (CmsSite, CmsSite.id_site, CmsSite.site_name),
        "site_group": (CmsSiteGroup, CmsSiteGroup.id_site_group, CmsSiteGroup.group_name),
    }
    by_type: dict[str, set[int]] = {}
    for t in targets:
        if t.target_type in models and t.target_id is not None:
            by_type.setdefault(t.target_type, set()).add(t.target_id)
    names: dict[tuple[str, int], str] = {}
    for t_type, ids in by_type.items():
        model, id_col, name_col = models[t_type]
        for tid, name in (await db.execute(
            select(id_col, name_col).where(id_col.in_(ids), model.id_org == id_org)
        )).all():
            names[(t_type, tid)] = name
    for t in targets:
        if t.target_id is not None:
            t.target_name = names.get((t.target_type, t.target_id))


# Racine des dossiers d'annonceurs : repérée par sa clé (migr 0131), jamais par son nom —
# le nom affiché est traduit par l'interface.
ADVERTISERS_FOLDER_KEY = "advertisers"


async def _ensure_advertiser_folder(db: AsyncSession, adv: CmsAdvertiser) -> None:
    """Dossier médiathèque « Advertisers/<nom> » de l'annonceur (créé si absent, renommé
    si l'annonceur change de nom). Le picker de créatifs filtre sur son sous-arbre ;
    l'utilisateur peut créer librement des sous-dossiers dedans. Ne commit pas."""
    if adv.id_folder is not None:
        folder = (await db.execute(
            select(CmsMediaFolder).where(CmsMediaFolder.id_folder == adv.id_folder)
        )).scalar_one_or_none()
        if folder is not None:
            if folder.folder_name != adv.advertiser_name:
                folder.folder_name = adv.advertiser_name
            return
        adv.id_folder = None  # dossier supprimé à la main → on le recrée
    root = (await db.execute(
        select(CmsMediaFolder).where(
            CmsMediaFolder.id_org == adv.id_org,
            CmsMediaFolder.parent_id.is_(None),
            CmsMediaFolder.system_key == ADVERTISERS_FOLDER_KEY,
        ).limit(1)
    )).scalar_one_or_none()
    if root is None:
        root = CmsMediaFolder(id_org=adv.id_org, parent_id=None,
                              folder_name="Advertisers", depth=0, path="",
                              system_key=ADVERTISERS_FOLDER_KEY)
        db.add(root)
        await db.flush()
        root.path = str(root.id_folder)
    folder = CmsMediaFolder(id_org=adv.id_org, parent_id=root.id_folder,
                            folder_name=adv.advertiser_name, depth=root.depth + 1, path="")
    db.add(folder)
    await db.flush()
    folder.path = f"{root.path}.{folder.id_folder}"
    adv.id_folder = folder.id_folder


async def _force_sync_campaign_players(db: AsyncSession, camp: CmsCampaign) -> None:
    """player:force_sync aux players ciblés par la campagne (règle d'or : une diffusion
    publiée/modifiée/suspendue doit se refléter à l'écran sans attendre un resync naturel).
    S3 : les players re-résolvent leur payload → créatifs injectés/retirés dans la boucle."""
    trows = (await db.execute(
        select(CmsCampaignTarget).where(CmsCampaignTarget.id_campaign == camp.id_campaign)
    )).scalars().all()
    if not trows:
        return
    pids = await players_for_targets(db, camp.id_org, [
        Target(target_type=t.target_type, target_id=t.target_id, tag_expr=t.tag_expr or {})
        for t in trows
    ])
    if not pids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        payload = json.dumps({"type": "player:force_sync"})
        for pid in pids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)
    finally:
        await r.aclose()


async def validate_campaign_targets(db: AsyncSession, id_org: int, specs: list) -> None:
    """Chaque identifiant de cible existe-t-il, DANS cette organisation ? (audit menu ⑨)

    🔴 Rien ne le vérifiait : le schéma valide le TYPE et la présence d'un identifiant,
    jamais son existence. Une campagne pouvait donc naître (201) en visant l'écran d'une
    AUTRE organisation ou un identifiant mort — enregistrée, comptée dans l'aperçu de
    ciblage, **facturée sur une base fausse**, et jamais diffusée. C'est la classe de
    défaut de l'audit du 18/07 (« cible acceptée à l'écriture, ignorée à la lecture »),
    ici sur l'existence — déjà fermée sur les événements (29/07) et les programmes (31/07).

    ⛔ `org` et `tags` n'ont pas d'identifiant à vérifier (l'un vise toute l'organisation,
    l'autre s'exprime en expression de tags) — ils passent.
    """
    from app.models.player_group import CmsPlayerGroup
    from app.models.site_group import CmsSiteGroup
    from app.models.organization import CmsSite

    models = {
        "player": (CmsPlayer, CmsPlayer.id_player),
        "group": (CmsPlayerGroup, CmsPlayerGroup.id_group),
        "site": (CmsSite, CmsSite.id_site),
        "site_group": (CmsSiteGroup, CmsSiteGroup.id_site_group),
    }
    by_type: dict[str, set[int]] = {}
    for s in specs:
        if s.target_type in models and s.target_id is not None:
            by_type.setdefault(s.target_type, set()).add(s.target_id)
    for t_type, ids in by_type.items():
        model, column = models[t_type]
        found = set((await db.execute(
            select(column).where(column.in_(ids), model.id_org == id_org)
        )).scalars().all())
        missing = sorted(ids - found)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Target not found in the organization ({t_type}): {missing}",
            )


async def _replace_targets(
    db: AsyncSession, id_org: int, id_campaign: int, specs: list,
) -> None:
    existing = (await db.execute(
        select(CmsCampaignTarget).where(CmsCampaignTarget.id_campaign == id_campaign)
    )).scalars().all()
    for t in existing:
        await db.delete(t)
    for s in specs:
        db.add(CmsCampaignTarget(
            id_org=id_org, id_campaign=id_campaign,
            target_type=s.target_type, target_id=s.target_id,
            tag_expr=s.tag_expr or {},
        ))


# ── Catégories d'industrie (exclusion concurrentielle #10, migr 0087) ────────
# Liste gérée par l'org : un annonceur porte une catégorie, un site en exclut —
# les players du site sortent de l'inventaire de ces annonceurs (tuyau opt-out).

@router.get(
    "/campaigns/industry-categories",
    response_model=list[IndustryCategoryRead],
    dependencies=[Depends(require_permission("campaigns", "read")), Depends(require_retailer)],
)
async def list_industry_categories(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[IndustryCategoryRead]:
    rows = (await db.execute(
        select(CmsIndustryCategory).where(
            CmsIndustryCategory.id_org == current_user.id_org)
        .order_by(CmsIndustryCategory.category_name)
    )).scalars().all()
    adv_counts = dict((await db.execute(
        select(CmsAdvertiserIndustryCategory.id_category, func.count())
        .join(CmsAdvertiser,
              CmsAdvertiser.id_advertiser == CmsAdvertiserIndustryCategory.id_advertiser)
        .where(CmsAdvertiserIndustryCategory.id_org == current_user.id_org,
               CmsAdvertiser.is_deleted == False)  # noqa: E712
        .group_by(CmsAdvertiserIndustryCategory.id_category)
    )).all())
    site_counts = dict((await db.execute(
        select(CmsSiteExcludedCategory.id_category, func.count())
        .where(CmsSiteExcludedCategory.id_org == current_user.id_org)
        .group_by(CmsSiteExcludedCategory.id_category)
    )).all())
    out = []
    for c in rows:
        item = IndustryCategoryRead.model_validate(c)
        item.advertiser_count = adv_counts.get(c.id_category, 0)
        item.excluded_site_count = site_counts.get(c.id_category, 0)
        out.append(item)
    return out


@router.post(
    "/campaigns/industry-categories",
    response_model=IndustryCategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("campaigns", "edit")), Depends(require_retailer)],
)
async def create_industry_category(
    body: IndustryCategoryCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IndustryCategoryRead:
    name = body.category_name.strip()
    dup = (await db.execute(
        select(CmsIndustryCategory.id_category).where(
            CmsIndustryCategory.id_org == current_user.id_org,
            func.lower(CmsIndustryCategory.category_name) == name.lower(),
        )
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This category already exists")
    cat = CmsIndustryCategory(id_org=current_user.id_org, category_name=name)
    db.add(cat)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="industry_category", resource_id=name, detail={}))
    await db.commit()
    await db.refresh(cat)
    return IndustryCategoryRead.model_validate(cat)


@router.delete(
    "/campaigns/industry-categories/{id_category}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("campaigns", "edit")), Depends(require_retailer)],
)
async def delete_industry_category(
    id_category: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Supprime la catégorie — les annonceurs repassent sans catégorie (SET NULL) et
    les exclusions de sites disparaissent (CASCADE) : plus aucune exclusion active."""
    cat = (await db.execute(
        select(CmsIndustryCategory).where(
            CmsIndustryCategory.id_category == id_category,
            CmsIndustryCategory.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Category not found")
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="delete",
        resource_type="industry_category", resource_id=str(id_category),
        detail={"name": cat.category_name}))
    await db.delete(cat)
    await db.commit()


async def _check_industry_categories(
    db: AsyncSession, id_org: int, ids: list[int]
) -> None:
    """Les catégories référencées doivent être de l'org (cross-tenant = 404)."""
    if not ids:
        return
    valid = set((await db.execute(
        select(CmsIndustryCategory.id_category).where(
            CmsIndustryCategory.id_category.in_(set(ids)),
            CmsIndustryCategory.id_org == id_org,
        )
    )).scalars().all())
    if set(ids) - valid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Category not found")


async def _replace_advertiser_categories(
    db: AsyncSession, id_org: int, id_advertiser: int, ids: list[int]
) -> bool:
    """Remplace l'ensemble des catégories de l'annonceur (migr 0088).
    → True si l'ensemble a changé (l'appelant force_sync). Sans commit."""
    wanted = set(ids)
    existing = (await db.execute(
        select(CmsAdvertiserIndustryCategory).where(
            CmsAdvertiserIndustryCategory.id_advertiser == id_advertiser)
    )).scalars().all()
    have = {row.id_category for row in existing}
    for row in existing:
        if row.id_category not in wanted:
            await db.delete(row)
    for cid in wanted - have:
        db.add(CmsAdvertiserIndustryCategory(
            id_advertiser=id_advertiser, id_category=cid, id_org=id_org))
    return wanted != have


async def _advertiser_read(db: AsyncSession, adv: CmsAdvertiser) -> AdvertiserRead:
    """AdvertiserRead avec ses catégories peuplées (réponse create/update)."""
    item = AdvertiserRead.model_validate(adv)
    cats = (await _advertiser_categories(db, adv.id_org)).get(adv.id_advertiser, [])
    item.industry_categories = [cid for cid, _ in cats]
    item.industry_category_names = [name for _, name in cats]
    return item


async def _advertiser_categories(
    db: AsyncSession, id_org: int
) -> dict[int, list[tuple[int, str]]]:
    """{id_advertiser: [(id_category, nom) triés par nom]} pour toute l'org."""
    rows = (await db.execute(
        select(CmsAdvertiserIndustryCategory.id_advertiser,
               CmsIndustryCategory.id_category, CmsIndustryCategory.category_name)
        .join(CmsIndustryCategory,
              CmsIndustryCategory.id_category == CmsAdvertiserIndustryCategory.id_category)
        .where(CmsAdvertiserIndustryCategory.id_org == id_org)
        .order_by(CmsIndustryCategory.category_name)
    )).all()
    out: dict[int, list[tuple[int, str]]] = {}
    for aid, cid, name in rows:
        out.setdefault(aid, []).append((cid, name))
    return out


# ── Annonceurs ───────────────────────────────────────────────────────────────

@router.get(
    "/advertisers/",
    response_model=list[AdvertiserRead],
    dependencies=[Depends(require_permission("campaigns", "read"))],
)
async def list_advertisers(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AdvertiserRead]:
    rows = (await db.execute(
        select(
            CmsAdvertiser,
            func.count(CmsCampaign.id_campaign).filter(
                CmsCampaign.is_deleted == False  # noqa: E712
            ).label("n"),
        )
        .outerjoin(CmsCampaign, CmsCampaign.id_advertiser == CmsAdvertiser.id_advertiser)
        .where(
            CmsAdvertiser.id_org == current_user.id_org,
            CmsAdvertiser.is_deleted == False,  # noqa: E712
            # Portail annonceur (B1) : un user externe ne voit que SON annonceur
            *([CmsAdvertiser.id_advertiser == current_user.id_advertiser]
              if current_user.id_advertiser is not None else []),
        )
        .group_by(CmsAdvertiser.id_advertiser)
        .order_by(CmsAdvertiser.advertiser_name)
    )).all()
    adv_cats = await _advertiser_categories(db, current_user.id_org)
    out = []
    for adv, n in rows:
        item = AdvertiserRead.model_validate(adv)
        item.campaign_count = n or 0
        cats = adv_cats.get(adv.id_advertiser, [])
        item.industry_categories = [cid for cid, _ in cats]
        item.industry_category_names = [name for _, name in cats]
        out.append(item)
    return out


@router.post(
    "/advertisers/",
    response_model=AdvertiserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("campaigns", "create"))],
)
async def create_advertiser(
    body: AdvertiserCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdvertiserRead:
    await _check_industry_categories(db, current_user.id_org, body.industry_categories)
    adv = CmsAdvertiser(
        id_org=current_user.id_org,
        **body.model_dump(exclude={"industry_categories"}))
    db.add(adv)
    await db.flush()
    await _replace_advertiser_categories(
        db, current_user.id_org, adv.id_advertiser, body.industry_categories)
    await _ensure_advertiser_folder(db, adv)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="advertiser", resource_id=body.advertiser_name,
        detail={"name": body.advertiser_name}))
    await db.commit()
    await db.refresh(adv)
    return await _advertiser_read(db, adv)


@router.put(
    "/advertisers/{id_advertiser}",
    response_model=AdvertiserRead,
    dependencies=[Depends(require_permission("campaigns", "edit"))],
)
async def update_advertiser(
    id_advertiser: int,
    body: AdvertiserUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdvertiserRead:
    adv = (await db.execute(
        select(CmsAdvertiser).where(
            CmsAdvertiser.id_advertiser == id_advertiser,
            CmsAdvertiser.id_org == current_user.id_org,
            CmsAdvertiser.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if adv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Advertiser not found")
    category_changed = False
    if body.industry_categories is not None:
        await _check_industry_categories(db, current_user.id_org, body.industry_categories)
        category_changed = await _replace_advertiser_categories(
            db, current_user.id_org, id_advertiser, body.industry_categories)
    for field in body.model_fields_set - {"industry_categories"}:
        setattr(adv, field, getattr(body, field))
    await _ensure_advertiser_folder(db, adv)  # crée si absent, renomme si nom changé
    # 🔴 Créer et supprimer un annonceur étaient journalisés, MODIFIER ne l'était pas
    # (0 ligne en prod) — or c'est la mutation qui change le nom commercial, les
    # catégories d'exclusion concurrentielle et les droits de campagne du portail.
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="update",
        resource_type="advertiser", resource_id=str(id_advertiser),
        detail={"name": adv.advertiser_name,
                "changes": sorted(body.model_fields_set),
                "categories_changed": category_changed}))
    await db.commit()
    await db.refresh(adv)
    # Catégorie changée → l'exclusion concurrentielle peut sortir/rentrer ses créatifs
    # des sites exclus : force_sync des campagnes actives (règle d'or diffusion).
    if category_changed:
        camps = (await db.execute(
            select(CmsCampaign).where(
                CmsCampaign.id_advertiser == id_advertiser,
                CmsCampaign.is_deleted == False,  # noqa: E712
                CmsCampaign.status == "approved",
                CmsCampaign.ends_on >= date.today(),
            )
        )).scalars().all()
        for camp in camps:
            await _force_sync_campaign_players(db, camp)
    return await _advertiser_read(db, adv)


@router.delete(
    "/advertisers/{id_advertiser}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("campaigns", "delete"))],
)
async def delete_advertiser(
    id_advertiser: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    adv = (await db.execute(
        select(CmsAdvertiser).where(
            CmsAdvertiser.id_advertiser == id_advertiser,
            CmsAdvertiser.id_org == current_user.id_org,
            CmsAdvertiser.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if adv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Advertiser not found")
    active = (await db.execute(
        select(func.count()).select_from(CmsCampaign).where(
            CmsCampaign.id_advertiser == id_advertiser,
            CmsCampaign.is_deleted == False,  # noqa: E712
            CmsCampaign.status.in_(("draft", "approved", "paused")),
        )
    )).scalar() or 0
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{active} unfinished campaign(s) — cancel or end them first",
        )
    adv.is_deleted = True
    # Coupe aussi les accès portail de l'annonceur supprimé (comptes externes désactivés)
    portal_users = (await db.execute(
        select(CmsUser).where(
            CmsUser.id_org == current_user.id_org,
            CmsUser.id_advertiser == id_advertiser,
        )
    )).scalars().all()
    for u in portal_users:
        u.is_active = False
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="delete",
        resource_type="advertiser", resource_id=str(id_advertiser),
        detail={"name": adv.advertiser_name, "portal_users_disabled": len(portal_users)}))
    await db.commit()


# ── Accès portail annonceur (S7 B2) — comptes EXTERNES lecture seule ─────────
# Org-admin uniquement. Invitation = user CMS_ADVERTISER lié (1 user = 1 annonceur,
# décision Ed 2026-07-10) avec mot de passe initial généré, affiché UNE fois.
# Révocation = désactivation (le login filtre is_active) — l'audit est conservé.

async def _get_advertiser_or_404(
    db: AsyncSession, id_org: int, id_advertiser: int,
) -> CmsAdvertiser:
    adv = (await db.execute(
        select(CmsAdvertiser).where(
            CmsAdvertiser.id_advertiser == id_advertiser,
            CmsAdvertiser.id_org == id_org,
            CmsAdvertiser.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if adv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Advertiser not found")
    return adv


async def _advertiser_role_id(db: AsyncSession) -> int:
    """Rôle système CMS_ADVERTISER (seedé migr 0080) — get-or-create idempotent
    (permissions campaigns:read + campaigns:report ré-attachées si manquantes)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.role import CmsPermission, CmsRole, CmsRolePermission

    await db.execute(
        pg_insert(CmsRole)
        .values(role_key="CMS_ADVERTISER", label="Advertiser (portal)", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    role_id = (await db.execute(
        select(CmsRole.id_role).where(CmsRole.role_key == "CMS_ADVERTISER")
    )).scalar_one()
    for op_ in ("read", "report"):
        pid = (await db.execute(
            select(CmsPermission.id_permission).where(
                CmsPermission.module == "campaigns", CmsPermission.operation == op_)
        )).scalar_one_or_none()
        if pid is not None:
            await db.execute(
                pg_insert(CmsRolePermission)
                .values(id_role=role_id, id_permission=pid)
                .on_conflict_do_nothing()
            )
    return role_id


@router.get(
    "/advertisers/{id_advertiser}/users",
    response_model=list[AdvertiserUserRead],
)
async def list_advertiser_users(
    id_advertiser: int,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsUser]:
    await _get_advertiser_or_404(db, admin.id_org, id_advertiser)
    return list((await db.execute(
        select(CmsUser).where(
            CmsUser.id_org == admin.id_org,
            CmsUser.id_advertiser == id_advertiser,
        ).order_by(CmsUser.email)
    )).scalars().all())


@router.post(
    "/advertisers/{id_advertiser}/users",
    response_model=AdvertiserUserInviteResult,
    status_code=status.HTTP_201_CREATED,
)
async def invite_advertiser_user(
    id_advertiser: int,
    body: AdvertiserUserInvite,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> AdvertiserUserInviteResult:
    """Invite un compte portail. GET-OR-LINK (retour Ed 11/07) : si l'email existe déjà
    dans l'org (compte créé dans /settings/iam, ou révoqué/« supprimé » — la suppression
    IAM est une DÉSACTIVATION logique, l'email reste pris), on RATTACHE le compte à
    l'annonceur : réactivation, rôles internes remplacés par CMS_ADVERTISER (un externe
    ne garde pas de droits CMS), mot de passe régénéré SEULEMENT si le compte était
    désactivé (un compte actif garde le sien). Refus : email d'une autre org (404-like,
    pas de fuite), compte déjà lié à un AUTRE annonceur (§3.1 : 1 user = 1 annonceur),
    compte administrateur."""
    adv = await _get_advertiser_or_404(db, admin.id_org, id_advertiser)
    email = body.email.strip().lower()
    # Lookup via worker : l'email est unique GLOBAL (toutes orgs) — la session RLS ne
    # verrait pas un homonyme d'une autre org et l'INSERT casserait en 500.
    existing = (await db_worker.execute(
        select(CmsUser).where(func.lower(CmsUser.email) == email)
    )).scalar_one_or_none()
    if existing is not None:
        if existing.id_org != admin.id_org:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="An account already exists with this email")
        if existing.id_advertiser is not None and existing.id_advertiser != id_advertiser:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is already linked to another advertiser (1 account = 1 advertiser)")
        role_keys = await rbac.get_user_role_keys(db_worker, existing.id_user)
        if {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is an administrator — remove its roles first "
                       "in Users & roles")
        from app.models.role import CmsUserRoleAssignment
        was_inactive = not existing.is_active
        existing.id_advertiser = id_advertiser
        existing.is_active = True
        if was_inactive:
            # P1.1 — RÉACTIVATION : un compte désactivé voyait ses jetons refusés par le
            # contrôle `is_active`, mais ces jetons n'étaient pas MORTS pour autant. En
            # rendant le compte actif, on les ressuscitait : le jeton de rafraîchissement
            # d'un compte révoqué il y a trois semaines rouvrait la session, sous une
            # ancienne identité de rôles, sans que personne ne se soit reconnecté. La
            # borne clôt la vie d'avant — l'invitation en ouvre une neuve.
            existing.sessions_valid_from = revocation_bound()
        # Ne JAMAIS effacer silencieusement un flag démo existant à la ré-invitation
        # (retour Ed 12/07 #1) — le retrait du flag se fait dans /settings/iam.
        if body.is_demo:
            existing.is_demo = True
        if (body.full_name or "").strip():
            existing.full_name = body.full_name.strip()
        # Un compte annonceur est un EXTERNE : ses assignations de rôle internes sont
        # remplacées par CMS_ADVERTISER seul.
        await db_worker.execute(sa_delete(CmsUserRoleAssignment).where(
            CmsUserRoleAssignment.id_user == existing.id_user))
        db_worker.add(CmsUserRoleAssignment(
            id_user=existing.id_user, id_role=await _advertiser_role_id(db)))
        initial_password = None
        if was_inactive:  # compte révoqué/« supprimé » ré-invité → nouveau mdp (une fois)
            initial_password = secrets.token_urlsafe(9)
            existing.password_hash = hash_password(initial_password)
        db_worker.add(CmsAuditLog(
            id_org=admin.id_org, id_user=admin.id_user, action="link",
            resource_type="advertiser_user", resource_id=email,
            detail={"advertiser": adv.advertiser_name, "id_advertiser": id_advertiser,
                    "reactivated": was_inactive}))
        await db_worker.commit()
        await db_worker.refresh(existing)
        return AdvertiserUserInviteResult(
            user=AdvertiserUserRead.model_validate(existing),
            initial_password=initial_password,
            linked_existing=True,
        )
    initial_password = secrets.token_urlsafe(9)
    user = CmsUser(
        id_org=admin.id_org,
        email=email,
        full_name=(body.full_name or "").strip() or None,
        password_hash=hash_password(initial_password),
        locale=admin.locale,
        is_active=True,
        is_demo=body.is_demo,   # annonceur DÉMO (prospect) : garde démo + bulles portail
        id_advertiser=id_advertiser,
    )
    db.add(user)
    await db.flush()
    from app.models.role import CmsUserRoleAssignment
    db.add(CmsUserRoleAssignment(
        id_user=user.id_user, id_role=await _advertiser_role_id(db)))
    db.add(CmsAuditLog(
        id_org=admin.id_org, id_user=admin.id_user, action="create",
        resource_type="advertiser_user", resource_id=email,
        detail={"advertiser": adv.advertiser_name, "id_advertiser": id_advertiser}))
    await db.commit()
    await db.refresh(user)
    return AdvertiserUserInviteResult(
        user=AdvertiserUserRead.model_validate(user),
        initial_password=initial_password,
    )


@router.delete(
    "/advertisers/{id_advertiser}/users/{id_user}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_advertiser_user(
    id_advertiser: int,
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    purge: bool = False,
) -> None:
    """Révoque un compte portail (désactivation — la ligne reste listée « révoqué »,
    ré-invitation en 1 clic). `?purge=true` (retour Ed 11/07) : DÉLIE aussi le compte
    de l'annonceur → il disparaît de la liste (l'email reste pris, get-or-link le
    récupère au besoin)."""
    await _get_advertiser_or_404(db, admin.id_org, id_advertiser)
    user = (await db.execute(
        select(CmsUser).where(
            CmsUser.id_user == id_user,
            CmsUser.id_org == admin.id_org,
            CmsUser.id_advertiser == id_advertiser,
        )
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    user.is_active = False
    if purge:
        user.id_advertiser = None
    db.add(CmsAuditLog(
        id_org=admin.id_org, id_user=admin.id_user, action="delete",
        resource_type="advertiser_user", resource_id=user.email,
        detail={"id_advertiser": id_advertiser, "purged": purge}))
    await db.commit()


@router.get(
    "/portal/branding",
    response_model=PortalBranding,
    dependencies=[Depends(require_permission("campaigns", "read"))],
)
async def portal_branding(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortalBranding:
    """En-tête white-label du portail annonceur (réutilise le branding rapports §24.3)."""
    from app.models.report import CmsReportBranding
    from app.services.media import get_signed_url

    org_name = (await db.execute(
        select(CmsOrganization.org_name).where(CmsOrganization.id_org == current_user.id_org)
    )).scalar_one()
    adv_name = None
    if current_user.id_advertiser is not None:
        adv_name = (await db.execute(
            select(CmsAdvertiser.advertiser_name).where(
                CmsAdvertiser.id_advertiser == current_user.id_advertiser)
        )).scalar_one_or_none()
    b = (await db.execute(
        select(CmsReportBranding).where(CmsReportBranding.id_org == current_user.id_org)
    )).scalar_one_or_none()
    return PortalBranding(
        org_name=org_name,
        advertiser_name=adv_name,
        primary_color=(b.primary_color if b and b.primary_color else "#4875B4"),
        report_title=(b.report_title if b else None),
        logo_url=(get_signed_url(b.logo_key) if b and b.logo_key else None),
    )


# ── Campagnes ────────────────────────────────────────────────────────────────

@router.get(
    "/campaigns/",
    response_model=list[CampaignRead],
    dependencies=[Depends(require_permission("campaigns", "read"))],
)
async def list_campaigns(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    id_advertiser: int | None = Query(default=None),
    target_type: str | None = Query(
        default=None, pattern="^(player|player_group|group|site|site_group)$",
        description="Target filter: keep only the campaigns reaching this target",
    ),
    target_id: int | None = Query(default=None),
) -> list[CampaignRead]:
    q = select(CmsCampaign).where(
        CmsCampaign.id_org == current_user.id_org,
        CmsCampaign.is_deleted == False,  # noqa: E712
    )
    # Portail annonceur (B1) : scope forcé sur SON annonceur (le filtre client est ignoré)
    if current_user.id_advertiser is not None:
        q = q.where(CmsCampaign.id_advertiser == current_user.id_advertiser)
    elif id_advertiser is not None:
        q = q.where(CmsCampaign.id_advertiser == id_advertiser)
    rows = list((await db.execute(q.order_by(CmsCampaign.starts_on.desc()))).scalars().all())

    # Filtre cible (calendrier unifié /schedules) : campagne retenue si ses players
    # résolus (même tuyau que la diffusion : cibles canoniques → éligibilité opt-out
    # + exclusion concurrentielle) intersectent la cible-filtre.
    if rows and target_type is not None and target_id is not None:
        fset = await players_for_targets(db, current_user.id_org, [
            Target(target_type=target_type, target_id=target_id)
        ])
        kept: list[CmsCampaign] = []
        for camp in rows:
            trows = (await db.execute(
                select(CmsCampaignTarget).where(
                    CmsCampaignTarget.id_campaign == camp.id_campaign)
            )).scalars().all()
            if not trows:
                continue
            pids = await players_for_targets(db, camp.id_org, [
                Target(target_type=t.target_type, target_id=t.target_id,
                       tag_expr=t.tag_expr or {})
                for t in trows
            ])
            pids = await campaign_eligible_players(db, pids, camp.id_advertiser)
            if pids & fset:
                kept.append(camp)
        rows = kept

    return [await _campaign_read(db, c) for c in rows]


# ── Inventaire / occupation SOV (S2) — routes FIXES avant /campaigns/{id} ────

@router.post(
    "/campaigns/occupancy-check",
    response_model=OccupancyCheckResult,
    dependencies=[Depends(require_permission("campaigns", "read")), Depends(require_retailer)],
)
async def occupancy_check(
    body: OccupancyCheckRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OccupancyCheckResult:
    """Projection d'occupation si la campagne candidate était approuvée — l'UI prévient
    AVANT d'enregistrer (warning ≥ 80 %, blocage > 100 %). L'autorité reste le publish.
    S2 audio : `campaign_kind='audio'` → projection en SPOTS/HEURE (plafond par écran) +
    devis au tarif audio + `achievable_spots_per_hour` ; pas de feasibility SOV."""
    targets = [Target(target_type=t.target_type, target_id=t.target_id,
                      tag_expr=t.tag_expr or {}) for t in body.targets]
    if body.campaign_kind == "audio":
        if not body.spots_per_hour:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="spots_per_hour is required for an audio campaign")
        res = await check_audio_occupancy(
            db, current_user.id_org,
            starts_on=body.starts_on, ends_on=body.ends_on,
            spots_per_hour=body.spots_per_hour,
            targets=targets,
            exclude_id_campaign=body.exclude_id_campaign,
            id_advertiser=body.id_advertiser,
        )
    else:
        res = await check_campaign_occupancy(
            db, current_user.id_org,
            starts_on=body.starts_on, ends_on=body.ends_on,
            sov_percent=body.sov_percent,
            targets=targets,
            exclude_id_campaign=body.exclude_id_campaign,
            id_advertiser=body.id_advertiser,
        )
    pids = [r["id_player"] for r in res["players"]]
    names = {}
    if pids:
        names = {r.id_player: r.player_name for r in (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name)
            .where(CmsPlayer.id_player.in_(pids))
        )).all()}
    for r in res["players"]:
        r["player_name"] = names.get(r["id_player"])
    if body.campaign_kind == "audio":
        res["price"] = await estimate_audio_price(
            db, current_user.id_org,
            targets=targets, spots_per_hour=body.spots_per_hour,
            starts_on=body.starts_on, ends_on=body.ends_on,
            id_advertiser=body.id_advertiser,
        )
        return OccupancyCheckResult(**res)
    # Devis v2 : prix indicatif si un tarif SOV est configuré (org/site/player).
    res["price"] = await estimate_campaign_price(
        db, current_user.id_org,
        targets=targets,
        sov_percent=body.sov_percent,
        starts_on=body.starts_on, ends_on=body.ends_on,
        id_advertiser=body.id_advertiser,
    )
    # SOV réalisable (anti-martèlement) : plafond physique de la SOV sur les boucles
    # courantes des players ciblés — l'UI avertit + suggère si sov_percent le dépasse.
    res["achievable"] = await campaign_feasibility(
        db, current_user.id_org,
        targets=targets,
        id_campaign=body.exclude_id_campaign,
        id_advertiser=body.id_advertiser,
    )
    return OccupancyCheckResult(**res)


@router.get(
    "/campaigns/inventory",
    response_model=list[InventoryRow],
    dependencies=[Depends(require_permission("campaigns", "read")), Depends(require_retailer)],
)
async def campaign_inventory(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> list[InventoryRow]:
    """Occupation vendue par player (campagnes approuvées) sur la fenêtre — défaut 30 jours."""
    start = date_from or date.today()
    end = date_to or (start + timedelta(days=30))
    occ = await occupancy_by_player(db, current_user.id_org, start, end)
    # Les players exclus des campagnes (campaigns_optout) ne sont pas de l'inventaire.
    rows = (await db.execute(
        select(CmsPlayer.id_player, CmsPlayer.player_name)
        .where(CmsPlayer.id_org == current_user.id_org,
               CmsPlayer.campaigns_optout == False)  # noqa: E712
        .order_by(CmsPlayer.player_name)
    )).all()
    return [
        InventoryRow(id_player=r.id_player, player_name=r.player_name,
                     occupied_pct=round(occ.get(r.id_player, 0.0), 2))
        for r in rows
    ]


@router.get(
    "/campaigns/occupancy-daily",
    dependencies=[Depends(require_permission("campaigns", "read")), Depends(require_retailer)],
)
async def campaign_occupancy_daily(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: date = Query(...),
    date_to: date = Query(...),
) -> dict:
    """Charge d'inventaire par JOUR (max sur les players de Σ SOV vendue) — vue
    calendrier S6. Fenêtre bornée à 92 jours (≈ un trimestre affichable)."""
    if date_to < date_from:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="date_to < date_from")
    if (date_to - date_from).days > 92:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Maximum window is 92 days")
    days = await occupancy_by_day(db, current_user.id_org, date_from, date_to)
    return {"days": [{"date": d, "max_pct": pct} for d, pct in days.items()]}


# ── Import de campagne par fichier JSON (S6 — ordre d'insertion annonceur) ───
# Format documenté : docs/FORMAT_IMPORT_CAMPAGNE.md. Flux : dry-run (aperçu +
# occupancy-check) → import (création en DRAFT + téléchargement des créatifs en
# tâche Celery, gardes anti-SSRF core/ssrf.py) → publish MANUEL (enforcement
# surbooking existant).

_IMPORT_VIDEO_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "m4v", "mpg", "mpeg", "ts", "m2ts", "wmv"}
_IMPORT_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}


def _import_creative_filename(url: str) -> str:
    from urllib.parse import unquote, urlparse

    path = unquote(urlparse(url).path or "")
    return (path.rsplit("/", 1)[-1] or "").strip() or "creative"


def _import_creative_check(spec) -> ImportResolvedCreative:
    """Déduit nom + type du créatif depuis l'URL, sans réseau (le download est en worker)."""
    filename = _import_creative_filename(spec.url)
    name = spec.name or filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _IMPORT_VIDEO_EXTS:
        mt = "video"
    elif ext in _IMPORT_IMAGE_EXTS:
        mt = "image"
    else:
        return ImportResolvedCreative(
            url=spec.url, media_name=name, media_type=None,
            error=f"Extension '{ext or '∅'}' cannot be played (video/image only)")
    if mt == "image" and not spec.duration_ms:
        return ImportResolvedCreative(url=spec.url, media_name=name, media_type=mt,
                                      error="duration_ms is required for an image")
    if not url_is_safe(spec.url, schemes=("http", "https")):
        return ImportResolvedCreative(url=spec.url, media_name=name, media_type=mt,
                                      error="URL not allowed (scheme/host)")
    return ImportResolvedCreative(url=spec.url, media_name=name, media_type=mt)


async def _import_resolve(
    db: AsyncSession, id_org: int, order: CampaignImportOrder,
) -> tuple[CmsAdvertiser | None, list[ImportResolvedTarget], list[ImportResolvedCreative], list[str]]:
    """Résout annonceur (match par nom) + cibles par NOM + créatifs. Ne crée rien."""
    from app.models.organization import CmsSite
    from app.models.player_group import CmsPlayerGroup
    from app.models.site_group import CmsSiteGroup

    errors: list[str] = []
    adv = (await db.execute(
        select(CmsAdvertiser).where(
            CmsAdvertiser.id_org == id_org,
            func.lower(CmsAdvertiser.advertiser_name) == order.advertiser.name.strip().lower(),
            CmsAdvertiser.is_deleted == False,  # noqa: E712
        ).limit(1)
    )).scalars().first()

    _LOOKUPS = {
        "site": (CmsSite, CmsSite.id_site, CmsSite.site_name),
        "site_group": (CmsSiteGroup, CmsSiteGroup.id_site_group, CmsSiteGroup.group_name),
        "group": (CmsPlayerGroup, CmsPlayerGroup.id_group, CmsPlayerGroup.group_name),
        "player": (CmsPlayer, CmsPlayer.id_player, CmsPlayer.player_name),
    }
    targets: list[ImportResolvedTarget] = []
    for t in order.targets:
        rt = ImportResolvedTarget(type=t.type, name=t.name)
        if t.type != "org":
            model, id_col, name_col = _LOOKUPS[t.type]
            rows = (await db.execute(
                select(id_col).where(
                    model.id_org == id_org,
                    func.lower(name_col) == (t.name or "").strip().lower(),
                )
            )).scalars().all()
            if not rows:
                rt.error = f"{t.type} '{t.name}' not found"
                errors.append(rt.error)
            elif len(rows) > 1:
                rt.error = f"{t.type} '{t.name}' is ambiguous ({len(rows)} matches)"
                errors.append(rt.error)
            else:
                rt.target_id = rows[0]
        targets.append(rt)

    creatives = [
        await run_in_threadpool(_import_creative_check, spec) for spec in order.creatives
    ]
    errors.extend(f"creative {c.media_name}: {c.error}" for c in creatives if c.error)
    return adv, targets, creatives, errors


def _import_target_specs(targets: list[ImportResolvedTarget]) -> list[Target]:
    return [Target(target_type=t.type, target_id=t.target_id) for t in targets]


@router.post(
    "/campaigns/import/dry-run",
    response_model=ImportDryRunResult,
)
async def campaign_import_dry_run(
    order: CampaignImportOrder,
    actor: Annotated[CampaignImportActor, Depends(require_campaign_import_actor)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ImportDryRunResult:
    """Vérifie l'ordre d'insertion SANS rien créer : annonceur matché/nouveau, cibles
    résolues par nom, créatifs (type/URL), projection d'occupation (seuils S2).
    Auth : JWT `campaigns:create` (drawer) OU clé machine `sfi_` (v2 API — l'agence
    pousse ses ordres sans humain ; session worker + filtres id_org explicites,
    même modèle que les exports BI §24.7)."""
    adv, targets, creatives, errors = await _import_resolve(db, actor.id_org, order)
    occupancy = None
    target_player_count = 0
    if not any(t.error for t in targets):
        specs = _import_target_specs(targets)
        pids = await campaign_eligible_players(
            db, await players_for_targets(db, actor.id_org, specs),
            adv.id_advertiser if adv is not None else None)
        target_player_count = len(pids)
        if not pids:
            errors.append("No player targeted (no targets, or all opted out)")
        occ = await check_campaign_occupancy(
            db, actor.id_org,
            starts_on=order.campaign.starts_on, ends_on=order.campaign.ends_on,
            sov_percent=order.campaign.sov_percent, targets=specs,
            id_advertiser=adv.id_advertiser if adv is not None else None,
        )
        pid_names = {r["id_player"]: None for r in occ["players"]}
        if pid_names:
            names = {r.id_player: r.player_name for r in (await db.execute(
                select(CmsPlayer.id_player, CmsPlayer.player_name)
                .where(CmsPlayer.id_player.in_(list(pid_names)))
            )).all()}
            for r in occ["players"]:
                r["player_name"] = names.get(r["id_player"])
        # SOV réalisable : l'agence voit AVANT d'importer si la SOV demandée dépasse
        # le plafond physique des boucles ciblées (anti-martèlement).
        occ["achievable"] = await campaign_feasibility(
            db, actor.id_org, targets=specs,
            id_advertiser=adv.id_advertiser if adv is not None else None,
        )
        occupancy = OccupancyCheckResult(**occ)
    return ImportDryRunResult(
        ok=not errors,
        errors=errors,
        advertiser_name=order.advertiser.name.strip(),
        advertiser_matched=adv is not None,
        targets=targets,
        target_player_count=target_player_count,
        creatives=creatives,
        occupancy=occupancy,
    )


@router.post(
    "/campaigns/import",
    response_model=CampaignRead,
    status_code=status.HTTP_201_CREATED,
)
async def campaign_import(
    order: CampaignImportOrder,
    actor: Annotated[CampaignImportActor, Depends(require_campaign_import_actor)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CampaignRead:
    """Importe l'ordre d'insertion : annonceur créé si nouveau, campagne en DRAFT,
    créatifs = médias `pending` téléchargés en tâche Celery (pipeline transcode
    normal). Publish MANUEL ensuite (l'enforcement surbooking s'applique)."""
    from app.services.media import make_storage_key

    adv, targets, creatives, errors = await _import_resolve(db, actor.id_org, order)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"errors": errors})

    if adv is None:
        adv = CmsAdvertiser(
            id_org=actor.id_org,
            advertiser_name=order.advertiser.name.strip(),
            contact_name=order.advertiser.contact_name,
            contact_email=order.advertiser.contact_email,
        )
        db.add(adv)
        await db.flush()
        await _ensure_advertiser_folder(db, adv)
    else:
        await _ensure_advertiser_folder(db, adv)  # recréé si supprimé à la main

    camp = CmsCampaign(
        id_org=actor.id_org,
        id_advertiser=adv.id_advertiser,
        campaign_name=order.campaign.name.strip(),
        starts_on=order.campaign.starts_on,
        ends_on=order.campaign.ends_on,
        daily_hours=order.campaign.daily_hours,
        sov_percent=order.campaign.sov_percent,
        pricing_model=order.campaign.pricing_model,
        price_amount=order.campaign.price_amount,
        price_currency=(order.campaign.price_currency or "EUR").upper(),
        cpm_rate=order.campaign.cpm_rate,
        priority=order.campaign.priority,
        created_by=actor.id_user,
    )
    db.add(camp)
    await db.flush()
    for t in targets:
        db.add(CmsCampaignTarget(
            id_org=actor.id_org, id_campaign=camp.id_campaign,
            target_type=t.type, target_id=t.target_id, tag_expr={},
        ))

    approval_on = await _org_creative_approval(db, actor.id_org)
    media_ids: list[int] = []
    for spec, info in zip(order.creatives, creatives):
        media = CmsMedia(
            id_org=actor.id_org,
            id_folder=adv.id_folder,
            media_name=info.media_name,
            media_type=info.media_type,
            storage_key=make_storage_key(actor.id_org, _import_creative_filename(spec.url)),
            checksum_sha256="",              # posé par le worker après download
            source="import",
            transcode_status="pending",
            created_by=actor.id_user,
            media_metadata={"download_url": spec.url, "campaign_import": camp.id_campaign},
        )
        db.add(media)
        await db.flush()
        media_ids.append(media.id_media)
        cr = CmsCampaignCreative(
            id_org=actor.id_org, id_campaign=camp.id_campaign,
            id_media=media.id_media, duration_ms=spec.duration_ms,
            weight=spec.weight, sort_order=len(media_ids) - 1,
        )
        if approval_on:
            cr.approval_status = "pending"
        db.add(cr)

    db.add(CmsAuditLog(
        id_org=actor.id_org, id_user=actor.id_user, action="create",
        resource_type="campaign", resource_id=str(camp.id_campaign),
        detail={"name": camp.campaign_name, "advertiser": adv.advertiser_name,
                "sov": float(camp.sov_percent), "imported": True,
                "creatives": len(media_ids)}))
    await db.commit()
    await db.refresh(camp)

    from app.workers.media_worker import download_remote_media
    for mid in media_ids:
        download_remote_media.delay(mid)

    return await _campaign_read(db, camp)


# ── Clés d'import de campagne `sfi_` (v2 API — org-admin, miroir des clés BI) ─

@router.get("/campaigns/import-keys", response_model=list[BiKeyRead])
async def list_import_keys(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[BiKeyRead]:
    rows = (await db.execute(
        select(CmsApiKey).where(
            CmsApiKey.id_org == current_user.id_org,
            CmsApiKey.is_active == True,  # noqa: E712
        ).order_by(CmsApiKey.created_at.desc())
    )).scalars().all()
    return [BiKeyRead(
        id_key=k.id_key, key_prefix=k.key_prefix, label=k.label,
        last_used_at=k.last_used_at, expires_at=k.expires_at, created_at=k.created_at,
    ) for k in rows if CAMPAIGN_IMPORT_SCOPE in (k.scopes or [])]


@router.post("/campaigns/import-keys", response_model=BiKeyRead, status_code=201)
async def create_import_key(
    body: BiKeyCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> BiKeyRead:
    """Clé machine `sfi_` (scope campaigns:import) — une agence/un annonceur pousse ses
    ordres d'insertion sur POST /campaigns/import[/dry-run] sans compte humain. La
    valeur en clair n'est renvoyée QU'ICI (hash sha256 stocké)."""
    import hashlib

    raw = f"sfi_{secrets.token_urlsafe(32)}"
    key = CmsApiKey(
        id_org=current_user.id_org, id_user=current_user.id_user,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        key_prefix=raw[:12], label=body.label,
        scopes=[CAMPAIGN_IMPORT_SCOPE],
        expires_at=(datetime.now(UTC) + timedelta(days=body.expires_in_days)
                    if body.expires_in_days else None),
    )
    db.add(key)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="campaign_import_key", resource_id=raw[:12],
        detail={"label": body.label}))
    await db.commit()
    await db.refresh(key)
    return BiKeyRead(
        id_key=key.id_key, key_prefix=key.key_prefix, label=key.label,
        last_used_at=key.last_used_at, expires_at=key.expires_at,
        created_at=key.created_at, key=raw,
    )


@router.delete("/campaigns/import-keys/{id_key}", status_code=204)
async def revoke_import_key(
    id_key: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    key = (await db.execute(
        select(CmsApiKey).where(
            CmsApiKey.id_key == id_key,
            CmsApiKey.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if key is None or CAMPAIGN_IMPORT_SCOPE not in (key.scopes or []):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found.")
    key.is_active = False
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="delete",
        resource_type="campaign_import_key", resource_id=key.key_prefix, detail={}))
    await db.commit()


# ── Réglages retail media de l'org (S5) — routes FIXES avant /campaigns/{id} ─

async def _org_creative_approval(db: AsyncSession, id_org: int) -> bool:
    """Flag org : les créatifs doivent-ils être approuvés avant diffusion ?"""
    return bool((await db.execute(
        select(CmsOrganization.campaign_creative_approval)
        .where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none())


async def _org_creative_max_duration_s(db: AsyncSession, id_org: int) -> int | None:
    """Durée MAX d'un créatif (garde-fou boucle courte, migr 0089). None = illimité."""
    return (await db.execute(
        select(CmsOrganization.campaign_creative_max_duration_s)
        .where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none()


def _effective_creative_ms(media: CmsMedia, duration_ms: int | None) -> int | None:
    """Durée réellement diffusée : une VIDÉO (ou un SPOT AUDIO) joue sa durée native
    (l'override n'ampute pas la lecture) ; une image = l'override (défaut 10 s au sync)."""
    if media.media_type in ("video", "audio"):
        return media.duration_ms or duration_ms
    return duration_ms or media.duration_ms


async def _check_creative_duration(
    db: AsyncSession, id_org: int, media: CmsMedia, duration_ms: int | None
) -> None:
    """Garde-fou « créatif long × boucle courte » (décision Ed 12/07 : limite fixe
    réglable par org, défaut 30 s — le cas Corvane 90 s sur boucle 80 s donnait 46 %
    servis pour 15 % vendus). 422 si dépassement. Ne bloque pas si la durée est
    encore inconnue (vidéo pas transcodée) — re-vérifié au publish/submit."""
    limit_s = await _org_creative_max_duration_s(db, id_org)
    if limit_s is None:
        return
    eff = _effective_creative_ms(media, duration_ms)
    if eff is not None and eff > limit_s * 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Creative too long: {round(eff / 1000)} s > the organization's "
                   f"{limit_s} s limit (configurable in Campaigns)",
        )


async def creative_duration_violations(
    db: AsyncSession, id_org: int, id_campaign: int
) -> list[str]:
    """Créatifs ACTIFS d'une campagne au-dessus de la limite org — utilisé au
    publish (CMS) et au submit portail (les vidéos uploadées au portail n'ont leur
    durée qu'après transcodage). → [descriptions] vide si OK."""
    limit_s = await _org_creative_max_duration_s(db, id_org)
    if limit_s is None:
        return []
    rows = (await db.execute(
        select(CmsCampaignCreative, CmsMedia)
        .join(CmsMedia, CmsMedia.id_media == CmsCampaignCreative.id_media)
        .where(CmsCampaignCreative.id_campaign == id_campaign,
               CmsCampaignCreative.status == "active")
    )).all()
    out = []
    for cr, media in rows:
        eff = _effective_creative_ms(media, cr.duration_ms)
        if eff is not None and eff > limit_s * 1000:
            out.append(f"{media.media_name} ({round(eff / 1000)} s > {limit_s} s)")
    return out


@router.get(
    "/campaigns/settings",
    response_model=CampaignSettingsRead,
    # 🔴 `require_retailer` MANQUAIT (audit menu ⑨, 2026-07-31) : le rôle système
    # CMS_ADVERTISER porte `campaigns:read`, si bien que l'ACHETEUR lisait la GRILLE
    # TARIFAIRE du VENDEUR (`sov_daily_rate`, `audio_spot_daily_rate`) et ses quotas
    # internes. Toutes les autres surfaces org-wide de ce fichier (inventaire,
    # occupation, catégories d'industrie) l'ont : c'est la seule qui l'avait oublié.
    # Le portail a son propre endpoint pour ce dont il a besoin (`GET /portal/limits`),
    # qui n'expose PAS les tarifs.
    dependencies=[Depends(require_permission("campaigns", "read")), Depends(require_retailer)],
)
async def get_campaign_settings(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignSettingsRead:
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
    )).scalar_one()
    return _settings_read(org)


def _settings_read(org: CmsOrganization) -> CampaignSettingsRead:
    return CampaignSettingsRead(
        creative_approval=org.campaign_creative_approval,
        sov_daily_rate=(float(org.campaign_sov_daily_rate)
                        if org.campaign_sov_daily_rate is not None else None),
        max_creatives=org.campaign_max_creatives,
        creative_max_mb=org.campaign_creative_max_mb,
        portal_player_selection=org.campaign_portal_player_selection,
        creative_max_duration_s=org.campaign_creative_max_duration_s,
        audio_spots_cap=org.campaign_audio_spots_cap,
        audio_spot_daily_rate=(float(org.campaign_audio_spot_daily_rate)
                               if org.campaign_audio_spot_daily_rate is not None else None))


@router.put("/campaigns/settings", response_model=CampaignSettingsRead)
async def update_campaign_settings(
    body: CampaignSettingsUpdate,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CampaignSettingsRead:
    """Org-admin : approbation des créatifs (OFF n'altère pas les statuts existants —
    un créatif resté `pending` redevient simplement diffusable) + tarif SOV par défaut
    (v2 : € / 1 % SOV / jour ; null explicite = plus de devis). Champs partiels."""
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == admin.id_org)
    )).scalar_one()
    changed: dict = {}
    if body.creative_approval is not None:
        org.campaign_creative_approval = body.creative_approval
        changed["creative_approval"] = body.creative_approval
    if "sov_daily_rate" in body.model_fields_set:
        org.campaign_sov_daily_rate = body.sov_daily_rate
        changed["sov_daily_rate"] = body.sov_daily_rate
    if body.max_creatives is not None:
        org.campaign_max_creatives = body.max_creatives
        changed["max_creatives"] = body.max_creatives
    if body.creative_max_mb is not None:
        org.campaign_creative_max_mb = body.creative_max_mb
        changed["creative_max_mb"] = body.creative_max_mb
    if body.portal_player_selection is not None:
        org.campaign_portal_player_selection = body.portal_player_selection
        changed["portal_player_selection"] = body.portal_player_selection
    if "creative_max_duration_s" in body.model_fields_set:
        org.campaign_creative_max_duration_s = body.creative_max_duration_s
        changed["creative_max_duration_s"] = body.creative_max_duration_s
    if body.audio_spots_cap is not None:
        org.campaign_audio_spots_cap = body.audio_spots_cap
        changed["audio_spots_cap"] = body.audio_spots_cap
    if "audio_spot_daily_rate" in body.model_fields_set:
        org.campaign_audio_spot_daily_rate = body.audio_spot_daily_rate
        changed["audio_spot_daily_rate"] = body.audio_spot_daily_rate
    db.add(CmsAuditLog(
        id_org=admin.id_org, id_user=admin.id_user, action="update",
        resource_type="campaign_settings", resource_id=str(admin.id_org),
        detail=changed))
    await db.commit()
    return _settings_read(org)


@router.post(
    "/campaigns/",
    response_model=CampaignRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("campaigns", "create"))],
)
async def create_campaign(
    body: CampaignCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    adv = (await db.execute(
        select(CmsAdvertiser).where(
            CmsAdvertiser.id_advertiser == body.id_advertiser,
            CmsAdvertiser.id_org == current_user.id_org,
            CmsAdvertiser.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if adv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Advertiser not found")
    await validate_campaign_targets(db, current_user.id_org, body.targets)
    camp = CmsCampaign(
        id_org=current_user.id_org,
        id_advertiser=body.id_advertiser,
        campaign_name=body.campaign_name,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        daily_hours=body.daily_hours.model_dump() if body.daily_hours else None,
        sov_percent=body.sov_percent,
        max_plays_per_hour=body.max_plays_per_hour,
        campaign_kind=body.campaign_kind,
        spots_per_hour=body.spots_per_hour if body.campaign_kind == "audio" else None,
        pricing_model=body.pricing_model,
        price_amount=body.price_amount,
        price_currency=body.price_currency.upper(),
        cpm_rate=body.cpm_rate,
        priority=body.priority,
        created_by=current_user.id_user,
    )
    db.add(camp)
    await db.flush()
    await _replace_targets(db, current_user.id_org, camp.id_campaign, body.targets)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="campaign", resource_id=str(camp.id_campaign),
        detail={"name": body.campaign_name, "advertiser": adv.advertiser_name,
                "kind": body.campaign_kind,
                "sov": float(body.sov_percent), "spots_per_hour": body.spots_per_hour}))
    await db.commit()
    await db.refresh(camp)
    return await _campaign_read(db, camp)


@router.get(
    "/campaigns/{id_campaign}",
    response_model=CampaignRead,
    dependencies=[Depends(require_permission("campaigns", "read"))],
)
async def get_campaign(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    camp = await _get_campaign(db, current_user, id_campaign)
    return await _campaign_read(db, camp)


@router.put(
    "/campaigns/{id_campaign}",
    response_model=CampaignRead,
    dependencies=[Depends(require_permission("campaigns", "edit"))],
)
async def update_campaign(
    id_campaign: int,
    body: CampaignUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    camp = await _get_campaign(db, current_user, id_campaign)
    if camp.status in ("cancelled", "completed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Campaign ended — cannot be modified")
    # Édition d'une campagne EN DIFFUSION : si les cibles changent, les players retirés
    # doivent aussi re-syncer (perdre les créatifs) → on capture les anciens avant.
    was_approved = camp.status == "approved"
    old_pids: set[int] = set()
    if was_approved and body.targets is not None:
        old_trows = (await db.execute(
            select(CmsCampaignTarget).where(CmsCampaignTarget.id_campaign == id_campaign)
        )).scalars().all()
        old_pids = await players_for_targets(db, current_user.id_org, [
            Target(target_type=t.target_type, target_id=t.target_id, tag_expr=t.tag_expr or {})
            for t in old_trows
        ])
    if "id_advertiser" in body.model_fields_set and body.id_advertiser is not None:
        adv = (await db.execute(
            select(CmsAdvertiser).where(
                CmsAdvertiser.id_advertiser == body.id_advertiser,
                CmsAdvertiser.id_org == current_user.id_org,
                CmsAdvertiser.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if adv is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Advertiser not found")
    for field in body.model_fields_set:
        if field == "targets":
            continue
        if field == "daily_hours":
            camp.daily_hours = body.daily_hours.model_dump() if body.daily_hours else None
        elif field == "price_currency" and body.price_currency:
            camp.price_currency = body.price_currency.upper()
        else:
            setattr(camp, field, getattr(body, field))
    if body.targets is not None:
        await validate_campaign_targets(db, current_user.id_org, body.targets)
        await _replace_targets(db, current_user.id_org, camp.id_campaign, body.targets)
    camp.updated_at = func.now()
    await db.commit()
    await db.refresh(camp)
    if was_approved or camp.status == "approved":
        await _force_sync_campaign_players(db, camp)
        if old_pids:  # anciens ciblés sortis du périmètre → retirer leurs créatifs
            r = aioredis.from_url(settings.REDIS_URL)
            try:
                for pid in old_pids:
                    await r.publish(f"signflow:player:{pid}:cmds",
                                    json.dumps({"type": "player:force_sync"}))
            finally:
                await r.aclose()
    return await _campaign_read(db, camp)


@router.delete(
    "/campaigns/{id_campaign}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("campaigns", "delete"))],
)
async def delete_campaign(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    camp = await _get_campaign(db, current_user, id_campaign)
    was_approved = camp.status == "approved"
    camp.is_deleted = True
    # Demandes d'approbation §XXI → PURGÉES (la ressource disparaît, nettoyage complet —
    # sinon la file affiche « #id » à vie ; la trace reste dans l'audit log).
    await approvals_svc.purge_for_resource(db, current_user, "campaign", id_campaign)
    # Nettoyage complet : liens annonceur révoqués (le /share public 404 déjà via le
    # filtre is_deleted — double verrou + les liens sortent des listes/exports).
    await db.execute(
        update(CmsReportShareLink)
        .where(CmsReportShareLink.id_campaign == id_campaign,
               CmsReportShareLink.id_org == current_user.id_org)
        .values(revoked=True))
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="delete",
        resource_type="campaign", resource_id=str(id_campaign),
        detail={"name": camp.campaign_name}))
    await db.commit()
    if was_approved:  # campagne en diffusion supprimée → retirer les créatifs des boucles
        await _force_sync_campaign_players(db, camp)


@router.post(
    "/campaigns/{id_campaign}/status",
    response_model=CampaignRead,
    dependencies=[Depends(require_permission("campaigns", "publish"))],
)
async def change_campaign_status(
    id_campaign: int,
    body: CampaignStatusUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    camp = await _get_campaign(db, current_user, id_campaign)
    allowed_from, to_status = _STATUS_ACTIONS[body.action]
    if camp.status not in allowed_from:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action {body.action} not allowed from status {camp.status}",
        )
    # Approval Workflow §XXI (C1) : une demande en cours de revue bloque la publication ;
    # une demande approuvée est marquée `published` avec la transition (même transaction).
    approval_req = await approvals_svc.get_active_request(
        db, current_user.id_org, "campaign", id_campaign)
    if (body.action == "publish" and approval_req is not None
            and approval_req.status in ("pending_review", "changes_requested")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Campaign awaiting approval — publishing is blocked "
                   "until the request is approved",
        )
    if body.action == "publish":
        creative_statuses = (await db.execute(
            select(CmsCampaignCreative.approval_status).where(
                CmsCampaignCreative.id_campaign == id_campaign,
                CmsCampaignCreative.status == "active",
            )
        )).scalars().all()
        trows = (await db.execute(
            select(CmsCampaignTarget).where(
                CmsCampaignTarget.id_campaign == id_campaign)
        )).scalars().all()
        if not creative_statuses or not trows:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot publish: at least one active creative and one target are required",
            )
        # S5 : org avec approbation des créatifs → au moins un créatif approuvé, sinon
        # la campagne serait « live » sans rien à diffuser.
        if (not any(s == "approved" for s in creative_statuses)
                and await _org_creative_approval(db, current_user.id_org)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot publish: no approved creative "
                       "(approval required by your organization)",
            )
        # Garde-fou boucle courte : re-vérifié ici car la durée d'une vidéo uploadée
        # au portail / importée par URL n'est connue qu'après transcodage.
        violations = await creative_duration_violations(
            db, current_user.id_org, id_campaign)
        if violations:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Creative(s) over the organization's "
                       "duration limit: " + " · ".join(violations),
            )
        # Surbooking (S2, décision Ed) : > 100 % d'occupation sur AU MOINS un player ciblé
        # = publication refusée, sauf override org-admin explicite (overbook_override).
        # Campagne AUDIO : même politique, mais l'inventaire est en SPOTS/HEURE (plafond
        # par écran, héritage player > site > org — migr 0094).
        pub_targets = [Target(target_type=t.target_type, target_id=t.target_id,
                              tag_expr=t.tag_expr or {}) for t in trows]
        if camp.campaign_kind == "audio":
            if not camp.spots_per_hour:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Cannot publish: spots per hour not set",
                )
            occ = await check_audio_occupancy(
                db, current_user.id_org,
                starts_on=camp.starts_on, ends_on=camp.ends_on,
                spots_per_hour=int(camp.spots_per_hour),
                targets=pub_targets,
                exclude_id_campaign=camp.id_campaign,
                id_advertiser=camp.id_advertiser,
            )
            if occ["blocked"] and not camp.overbook_override:
                worst = occ["players"][0] if occ["players"] else {}
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Audio overbooking: the spots-per-hour cap would be exceeded "
                        f"({worst.get('projected_spots', '?')}/{worst.get('cap_spots', '?')} "
                        f"spots/h on player #{worst.get('id_player', '?')}). "
                        "Reduce the rate / the period, or force it (org admin, "
                        "overbook_override)."
                    ),
                )
        else:
            occ = await check_campaign_occupancy(
                db, current_user.id_org,
                starts_on=camp.starts_on, ends_on=camp.ends_on,
                sov_percent=float(camp.sov_percent),
                targets=pub_targets,
                exclude_id_campaign=camp.id_campaign,
                id_advertiser=camp.id_advertiser,
            )
            if occ["blocked"] and not camp.overbook_override:
                worst = occ["players"][0] if occ["players"] else {}
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Overbooking: inventory would exceed "
                        f"{occ['max_projected_pct']}% (player #{worst.get('id_player', '?')}). "
                        "Reduce the share of voice / the period, or force it (org admin, "
                        "overbook_override)."
                    ),
                )
    camp.status = to_status
    camp.updated_at = func.now()
    if body.action == "publish" and approval_req is not None:
        await approvals_svc.mark_published(db, current_user, "campaign", id_campaign)
    elif body.action == "cancel" and approval_req is not None:
        await approvals_svc.cancel_for_resource(db, current_user, "campaign", id_campaign)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action=body.action,
        resource_type="campaign", resource_id=str(id_campaign),
        detail={"name": camp.campaign_name, "to": to_status}))
    await db.commit()
    await db.refresh(camp)
    # Toute transition change la diffusion (publish = injecter, pause/cancel/complete =
    # retirer) → les players ciblés re-syncent immédiatement.
    await _force_sync_campaign_players(db, camp)
    return await _campaign_read(db, camp)


# ── Créatifs ─────────────────────────────────────────────────────────────────

def _creative_audit(
    user: CmsUser, action: str, cr: CmsCampaignCreative,
    media: CmsMedia | None = None, detail: dict | None = None,
) -> CmsAuditLog:
    """Trace d'une mutation de créatif — le nom du média est conservé dans le détail :
    après retrait, le journal dit encore CE QUI a été diffusé (ou retiré de l'antenne)."""
    base = {"id_campaign": cr.id_campaign, "id_media": cr.id_media}
    if media is not None:
        base["media_name"] = media.media_name
    base.update(detail or {})
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="campaign_creative", resource_id=str(cr.id_creative),
        source="cms", detail=base,
    )


async def _creative_read(db: AsyncSession, cr: CmsCampaignCreative) -> CreativeRead:
    media = (await db.execute(
        select(CmsMedia).where(CmsMedia.id_media == cr.id_media)
    )).scalar_one_or_none()
    out = CreativeRead.model_validate(cr)
    if media is not None:
        out.media_name = media.media_name
        out.media_type = media.media_type
        out.media_duration_ms = media.duration_ms
    return out


@router.get(
    "/campaigns/{id_campaign}/creatives",
    response_model=list[CreativeRead],
    dependencies=[Depends(require_permission("campaigns", "read"))],
)
async def list_creatives(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CreativeRead]:
    await _get_campaign(db, current_user, id_campaign)
    rows = (await db.execute(
        select(CmsCampaignCreative).where(
            CmsCampaignCreative.id_campaign == id_campaign)
        .order_by(CmsCampaignCreative.sort_order, CmsCampaignCreative.id_creative)
    )).scalars().all()
    return [await _creative_read(db, cr) for cr in rows]


@router.post(
    "/campaigns/{id_campaign}/creatives",
    response_model=CreativeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("campaigns", "edit"))],
)
async def add_creative(
    id_campaign: int,
    body: CreativeCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreativeRead:
    camp = await _get_campaign(db, current_user, id_campaign)
    if camp.status in ("cancelled", "completed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Campaign ended — cannot be modified")
    media = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media == body.id_media,
            CmsMedia.id_org == current_user.id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    allowed = _CREATIVE_MEDIA_TYPES_BY_KIND.get(camp.campaign_kind, _CREATIVE_MEDIA_TYPES)
    if media.media_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Type {media.media_type} cannot be used as a creative in a "
                    f"{camp.campaign_kind} campaign ({'/'.join(allowed)} only)"),
        )
    if media.media_type == "image" and not body.duration_ms and not media.duration_ms:
        # une image sans durée native exige une durée d'affichage explicite
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="duration_ms is required for an image")
    # Garde-fou boucle courte (migr 0089) : limite de durée org (défaut 30 s)
    await _check_creative_duration(db, current_user.id_org, media, body.duration_ms)
    cr = CmsCampaignCreative(
        id_org=current_user.id_org, id_campaign=id_campaign, **body.model_dump()
    )
    if await _org_creative_approval(db, current_user.id_org):
        cr.approval_status = "pending"  # S5 : revue requise avant diffusion
    db.add(cr)
    await db.flush()
    # Traçabilité (audit menu ⑨) : seule la REVUE d'un créatif était journalisée. Or
    # ajouter/retirer un créatif change ce qui passe à l'antenne — et ce qui est facturé.
    db.add(_creative_audit(current_user, "create", cr, media))
    await db.commit()
    await db.refresh(cr)
    if camp.status == "approved":  # campagne en diffusion → rotation mise à jour à l'écran
        await _force_sync_campaign_players(db, camp)
    return await _creative_read(db, cr)


@router.put(
    "/campaigns/creatives/{id_creative}",
    response_model=CreativeRead,
    dependencies=[Depends(require_permission("campaigns", "edit"))],
)
async def update_creative(
    id_creative: int,
    body: CreativeUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreativeRead:
    cr = (await db.execute(
        select(CmsCampaignCreative).where(
            CmsCampaignCreative.id_creative == id_creative,
            CmsCampaignCreative.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creative not found")
    # La limite de durée de l'organisation était vérifiée à l'AJOUT et au publish, pas
    # ICI : on ajoutait une image à 10 s puis on la passait à 300 s. Le publish rattrapait,
    # mais entre-temps une campagne DÉJÀ publiée diffusait une durée interdite, et le
    # portail affichait un créatif « conforme ».
    if "duration_ms" in body.model_fields_set:
        media = (await db.execute(
            select(CmsMedia).where(CmsMedia.id_media == cr.id_media)
        )).scalar_one_or_none()
        if media is not None:
            await _check_creative_duration(
                db, current_user.id_org, media, body.duration_ms)
    for field in body.model_fields_set:
        setattr(cr, field, getattr(body, field))
    db.add(_creative_audit(current_user, "update", cr,
                           detail={"changes": sorted(body.model_fields_set)}))
    await db.commit()
    await db.refresh(cr)
    camp = await _get_campaign(db, current_user, cr.id_campaign)
    if camp.status == "approved":
        await _force_sync_campaign_players(db, camp)
    return await _creative_read(db, cr)


@router.delete(
    "/campaigns/creatives/{id_creative}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("campaigns", "edit"))],
)
async def delete_creative(
    id_creative: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    cr = (await db.execute(
        select(CmsCampaignCreative).where(
            CmsCampaignCreative.id_creative == id_creative,
            CmsCampaignCreative.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creative not found")
    id_campaign = cr.id_campaign
    media = (await db.execute(
        select(CmsMedia).where(CmsMedia.id_media == cr.id_media)
    )).scalar_one_or_none()
    db.add(_creative_audit(current_user, "delete", cr, media))
    await db.delete(cr)
    await db.commit()
    camp = await _get_campaign(db, current_user, id_campaign)
    if camp.status == "approved":
        await _force_sync_campaign_players(db, camp)


# ── Revue des créatifs (S5, approbation optionnelle par org) ─────────────────
# 1er consommateur réel du RBAC §21 seedé (approvals:approve/reject, CMS_APPROVER).

async def _review_creative(
    db: AsyncSession, current_user: CmsUser, id_creative: int,
    new_status: str, note: str | None,
) -> CreativeRead:
    cr = (await db.execute(
        select(CmsCampaignCreative).where(
            CmsCampaignCreative.id_creative == id_creative,
            CmsCampaignCreative.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creative not found")
    cr.approval_status = new_status
    cr.reviewed_by = current_user.id_user
    cr.reviewed_at = datetime.now(UTC)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="approve" if new_status == "approved" else "reject",
        resource_type="campaign_creative", resource_id=str(id_creative),
        detail={"id_campaign": cr.id_campaign, **({"note": note} if note else {})}))
    await db.commit()
    await db.refresh(cr)
    camp = await _get_campaign(db, current_user, cr.id_campaign)
    if camp.status == "approved":  # campagne en diffusion → l'écran suit la revue
        await _force_sync_campaign_players(db, camp)
    return await _creative_read(db, cr)


@router.post(
    "/campaigns/creatives/{id_creative}/approve",
    response_model=CreativeRead,
    dependencies=[Depends(require_permission("approvals", "approve"))],
)
async def approve_creative(
    id_creative: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: CreativeReview | None = None,
) -> CreativeRead:
    return await _review_creative(db, current_user, id_creative, "approved",
                                  body.note if body else None)


@router.post(
    "/campaigns/creatives/{id_creative}/reject",
    response_model=CreativeRead,
    dependencies=[Depends(require_permission("approvals", "reject"))],
)
async def reject_creative(
    id_creative: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: CreativeReview | None = None,
) -> CreativeRead:
    return await _review_creative(db, current_user, id_creative, "rejected",
                                  body.note if body else None)


# ── Rapport annonceur (S4) ────────────────────────────────────────────────────

def _report_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


@router.get(
    "/campaigns/{id_campaign}/report",
    dependencies=[Depends(require_permission("campaigns", "report"))],
)
async def campaign_report_json(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
) -> dict:
    """Rapport annonceur : PoP agrégés (passages/signés/antenne), SOV servi vs vendu,
    montant, répartitions par écran/jour/créatif. Période par défaut = celle de la campagne."""
    camp = await _get_campaign(db, current_user, id_campaign)
    return await campaign_report.build_campaign_report(db, camp, _report_tz(), from_, until)


@router.get(
    "/campaigns/{id_campaign}/report.pdf",
    dependencies=[Depends(require_permission("campaigns", "report"))],
)
async def campaign_report_pdf_endpoint(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
    level: Literal["player", "site", "site_group"] = Query("player"),
):
    """Rapport annonceur PDF white-label (branding §24.3 réutilisé) — envoyable tel quel.
    `level` = granularité de la répartition (écran / site / groupe de sites, Ed #3)."""
    camp = await _get_campaign(db, current_user, id_campaign)
    tz = _report_tz()
    payload = await campaign_report.build_campaign_report(db, camp, tz, from_, until)
    b = await report_render.branding_or_default(db, current_user.id_org)
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
    )).scalar_one()
    logo_bytes = None
    if b.logo_key and not b.logo_key.endswith(".svg"):  # reportlab ne lit pas le SVG
        try:
            logo_bytes = await run_in_threadpool(report_render._fetch_logo_bytes, b.logo_key)
        except Exception:  # noqa: BLE001
            logo_bytes = None
    pdf = await run_in_threadpool(
        campaign_report_pdf.build_campaign_pdf,
        org_name=org.org_name, payload=payload,
        branding={"primary_color": b.primary_color},
        logo_bytes=logo_bytes,
        generated_at=campaign_report_pdf.fmt_generated_at(
            datetime.now(tz), current_user.locale),
        level=level,
        locale=current_user.locale,
    )
    filename = campaign_report_pdf.report_filename(
        id_campaign, payload["period"]["from"], payload["period"]["until"],
        current_user.locale)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _campaign_share_read(s: CmsReportShareLink) -> ShareLinkRead:
    return ShareLinkRead(
        id_share=s.id_share, token=s.token, url=f"/share/{s.token}",
        label=s.label, period_days=s.period_days, expires_at=s.expires_at,
        revoked=s.revoked, access_count=s.access_count,
        last_accessed_at=s.last_accessed_at,
        has_password=s.password_hash is not None,
        ip_allowlist=list(s.ip_allowlist) if s.ip_allowlist else None,
        created_at=s.created_at,
    )


@router.post(
    "/campaigns/{id_campaign}/share",
    response_model=ShareLinkRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_campaign_share(
    id_campaign: int,
    body: ShareLinkCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShareLinkRead:
    """Lien annonceur sans login vers le rapport de campagne (page /share/{token} publique,
    mêmes protections §24.4 : mot de passe bcrypt + IP allowlist + expiration + révocation)."""
    camp = await _get_campaign(db, current_user, id_campaign)
    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_in_days)
    s = CmsReportShareLink(
        id_org=current_user.id_org, token=secrets.token_urlsafe(24),
        label=body.label or camp.campaign_name,
        report_type="campaign", id_campaign=camp.id_campaign,
        expires_at=expires_at, created_by=current_user.id_user,
        password_hash=(
            await run_in_threadpool(hash_password, body.password) if body.password else None),
        ip_allowlist=body.ip_allowlist,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _campaign_share_read(s)


@router.get(
    "/campaigns/{id_campaign}/shares",
    response_model=list[ShareLinkRead],
)
async def list_campaign_shares(
    id_campaign: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ShareLinkRead]:
    """Liens annonceur de la campagne (révocation = DELETE /analytics/shares/{id} existant)."""
    await _get_campaign(db, current_user, id_campaign)
    rows = (await db.execute(
        select(CmsReportShareLink)
        .where(CmsReportShareLink.id_org == current_user.id_org,
               CmsReportShareLink.id_campaign == id_campaign)
        .order_by(CmsReportShareLink.created_at.desc())
    )).scalars().all()
    return [_campaign_share_read(s) for s in rows]
