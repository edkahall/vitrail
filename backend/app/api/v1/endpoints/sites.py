"""
Endpoints Sites + heures d'ouverture + auto-power écrans (§ heures d'ouverture).

Allumage/extinction automatique des écrans calé sur les heures d'ouverture du
magasin. Le pilotage runtime (envoi power_on/power_off) est assuré par la tâche
beat `event_worker.apply_site_power`. Ici : configuration (sites, horaires hebdo,
exceptions datées) + lecture des players rattachés.

GET    /sites                          — liste des sites de l'org (+ player_count, hours)
POST   /sites                          — créer un site
GET    /sites/{id}                     — détail (avec horaires)
PUT    /sites/{id}                     — modifier (réglages auto-power)
DELETE /sites/{id}                     — supprimer (players détachés, FK SET NULL implicite via id_site nullable)
PUT    /sites/{id}/hours               — remplacer les horaires hebdo
GET    /sites/{id}/exceptions          — lister les exceptions datées
POST   /sites/{id}/exceptions          — créer une exception
DELETE /sites/{id}/exceptions/{id_exc} — supprimer une exception
GET    /sites/{id}/players             — players rattachés au site

Permissions : lecture = players:read ; écriture = players:edit (pas de module
`sites` dédié dans le catalogue RBAC ; les sites pilotent le parc d'écrans).
"""

import json
from datetime import date, timedelta
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_permission
from app.models.campaign import CmsIndustryCategory, CmsSiteExcludedCategory
from app.models.organization import CmsSite, CmsSiteException, CmsSiteHours
from app.models.player import CmsPlayer
from app.models.user import CmsUser
from app.schemas.site import (
    SiteCreate,
    SiteExceptionCreate,
    SiteExceptionRead,
    SiteHoursItem,
    SiteHoursReplace,
    SitePlayerRead,
    SiteRead,
    SiteUpdate,
)
from app.services.player_timezone import align_player_timezone

router = APIRouter(prefix="/sites", tags=["sites"])


async def _get_site_or_404(db: AsyncSession, id_org: int, id_site: int) -> CmsSite:
    site = (await db.execute(
        select(CmsSite).where(CmsSite.id_site == id_site, CmsSite.id_org == id_org)
    )).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")
    return site


async def _hours_for(db: AsyncSession, id_site: int) -> list[SiteHoursItem]:
    rows = (await db.execute(
        select(CmsSiteHours).where(CmsSiteHours.id_site == id_site).order_by(CmsSiteHours.weekday)
    )).scalars().all()
    return [SiteHoursItem.model_validate(r) for r in rows]


async def _excluded_categories_for(db: AsyncSession, id_site: int) -> list[int]:
    """Catégories d'industrie exclues du site (exclusion concurrentielle, migr 0087)."""
    return list((await db.execute(
        select(CmsSiteExcludedCategory.id_category)
        .where(CmsSiteExcludedCategory.id_site == id_site)
        .order_by(CmsSiteExcludedCategory.id_category)
    )).scalars().all())


@router.get("", response_model=list[SiteRead])
async def list_sites(
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SiteRead]:
    sites = (await db.execute(
        select(CmsSite).where(CmsSite.id_org == current_user.id_org).order_by(CmsSite.site_name)
    )).scalars().all()

    # Comptes de players par site (un seul aller-retour).
    counts = dict((await db.execute(
        select(CmsPlayer.id_site, func.count())
        .where(CmsPlayer.id_org == current_user.id_org, CmsPlayer.id_site.is_not(None))
        .group_by(CmsPlayer.id_site)
    )).all())

    excluded: dict[int, list[int]] = {}
    for sid, cid in (await db.execute(
        select(CmsSiteExcludedCategory.id_site, CmsSiteExcludedCategory.id_category)
        .where(CmsSiteExcludedCategory.id_org == current_user.id_org)
    )).all():
        excluded.setdefault(sid, []).append(cid)

    out: list[SiteRead] = []
    for s in sites:
        r = SiteRead.model_validate(s)
        r.player_count = counts.get(s.id_site, 0)
        r.hours = await _hours_for(db, s.id_site)
        r.campaign_excluded_categories = sorted(excluded.get(s.id_site, []))
        out.append(r)
    return out


# ── Stats campagnes par site (tooltip, retour Ed #4) ─────────────────────────
# ⚠️ route FIXE — doit rester déclarée AVANT GET /sites/{id_site}.

class SiteOccupancyDay(BaseModel):
    date: str
    avg_pct: float
    max_pct: float


class SiteCampaignStatsRead(BaseModel):
    id_site: int
    player_count: int
    eligible_count: int
    days: list[SiteOccupancyDay]


@router.get("/campaign-stats", response_model=list[SiteCampaignStatsRead])
async def sites_campaign_stats(
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(7, ge=1, le=31),
) -> list[SiteCampaignStatsRead]:
    """Tooltip des sites : players rattachés / éligibles campagnes (opt-out déduits)
    + occupation SOV par jour (moyenne + pire player) sur `days` jours à partir
    d'aujourd'hui. Gate players:read = jamais exposé au portail annonceur."""
    from app.services.campaign_inventory import site_campaign_stats

    today = date.today()
    stats = await site_campaign_stats(
        db, current_user.id_org, today, today + timedelta(days=days - 1))
    return [SiteCampaignStatsRead(id_site=sid, **v) for sid, v in stats.items()]


@router.post("", response_model=SiteRead, status_code=status.HTTP_201_CREATED)
async def create_site(
    body: SiteCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteRead:
    site = CmsSite(
        id_org=current_user.id_org,
        site_name=body.site_name,
        address=body.address,
        country=body.country,
        site_number=body.site_number,
        latitude=body.latitude,
        longitude=body.longitude,
        timezone=body.timezone,
        auto_power_enabled=body.auto_power_enabled,
        power_lead_min=body.power_lead_min,
        power_lag_min=body.power_lag_min,
        respect_holidays=body.respect_holidays,
        holiday_region=body.holiday_region,
    )
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return SiteRead.model_validate(site)


@router.get("/{id_site}", response_model=SiteRead)
async def get_site(
    id_site: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteRead:
    site = await _get_site_or_404(db, current_user.id_org, id_site)
    count = (await db.execute(
        select(func.count()).select_from(CmsPlayer).where(CmsPlayer.id_site == id_site)
    )).scalar_one()
    r = SiteRead.model_validate(site)
    r.player_count = count
    r.hours = await _hours_for(db, id_site)
    r.campaign_excluded_categories = await _excluded_categories_for(db, id_site)
    return r


@router.put("/{id_site}", response_model=SiteRead)
async def update_site(
    id_site: int,
    body: SiteUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteRead:
    site = await _get_site_or_404(db, current_user.id_org, id_site)
    data = body.model_dump(exclude_unset=True)
    # Exclusion concurrentielle : liste fournie = REMPLACE les exclusions du site
    # (catégories vérifiées dans l'org — cross-tenant = 404).
    excluded = data.pop("campaign_excluded_categories", None)
    excluded_changed = False
    if excluded is not None:
        cat_ids = set(excluded)
        valid = set((await db.execute(
            select(CmsIndustryCategory.id_category).where(
                CmsIndustryCategory.id_org == current_user.id_org,
                CmsIndustryCategory.id_category.in_(cat_ids),
            )
        )).scalars().all()) if cat_ids else set()
        if cat_ids - valid:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Category not found")
        existing = (await db.execute(
            select(CmsSiteExcludedCategory).where(
                CmsSiteExcludedCategory.id_site == id_site)
        )).scalars().all()
        for row in existing:
            if row.id_category not in cat_ids:
                await db.delete(row)
        have = {row.id_category for row in existing}
        for cid in cat_ids - have:
            db.add(CmsSiteExcludedCategory(
                id_site=id_site, id_category=cid, id_org=current_user.id_org))
        excluded_changed = cat_ids != have
    tz_changed = bool(data.get("timezone")) and data["timezone"] != site.timezone
    for k, v in data.items():
        setattr(site, k, v)
    await db.commit()
    await db.refresh(site)
    # Nouveau fuseau de site → réaligner le fuseau OS de tous ses players (best-effort,
    # one-shot ; seuls les players ONLINE reçoivent la commande — les autres via un futur
    # ré-attachement ou le drawer, le badge de mismatch les signale).
    if tz_changed:
        site_players = (await db.execute(
            select(CmsPlayer).where(CmsPlayer.id_site == id_site)
        )).scalars().all()
        for p in site_players:
            await align_player_timezone(db, p, site)
    # Exclusions concurrentielles modifiées → force_sync des players du site (règle
    # d'or : les créatifs exclus sortent/entrent de la boucle immédiatement).
    if excluded_changed:
        pids = (await db.execute(
            select(CmsPlayer.id_player).where(CmsPlayer.id_site == id_site)
        )).scalars().all()
        if pids:
            rds = aioredis.from_url(settings.REDIS_URL)
            try:
                payload = json.dumps({"type": "player:force_sync"})
                for pid in pids:
                    await rds.publish(f"signflow:player:{pid}:cmds", payload)
            finally:
                await rds.aclose()
    r = SiteRead.model_validate(site)
    r.hours = await _hours_for(db, id_site)
    r.campaign_excluded_categories = await _excluded_categories_for(db, id_site)
    return r


@router.delete("/{id_site}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(
    id_site: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    site = await _get_site_or_404(db, current_user.id_org, id_site)
    # Détacher les players (id_site nullable, pas de cascade côté players).
    await db.execute(
        update(CmsPlayer).where(CmsPlayer.id_site == id_site).values(id_site=None)
    )
    # Les horaires/exceptions partent en cascade (FK ondelete=CASCADE).
    await db.delete(site)
    await db.commit()


@router.put("/{id_site}/hours", response_model=list[SiteHoursItem])
async def replace_hours(
    id_site: int,
    body: SiteHoursReplace,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SiteHoursItem]:
    """Remplace l'intégralité des horaires hebdo du site."""
    await _get_site_or_404(db, current_user.id_org, id_site)
    # Dédoublonnage défensif des jours (la contrainte UNIQUE le refuserait sinon).
    seen: dict[int, SiteHoursItem] = {}
    for item in body.hours:
        seen[item.weekday] = item

    existing = (await db.execute(
        select(CmsSiteHours).where(CmsSiteHours.id_site == id_site)
    )).scalars().all()
    for row in existing:
        await db.delete(row)
    await db.flush()

    for item in seen.values():
        db.add(CmsSiteHours(
            id_site=id_site,
            weekday=item.weekday,
            is_closed=item.is_closed,
            open_time=None if item.is_closed else item.open_time,
            close_time=None if item.is_closed else item.close_time,
        ))
    await db.commit()
    return await _hours_for(db, id_site)


@router.get("/{id_site}/exceptions", response_model=list[SiteExceptionRead])
async def list_exceptions(
    id_site: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SiteExceptionRead]:
    await _get_site_or_404(db, current_user.id_org, id_site)
    rows = (await db.execute(
        select(CmsSiteException)
        .where(CmsSiteException.id_site == id_site)
        .order_by(CmsSiteException.date_from)
    )).scalars().all()
    return [SiteExceptionRead.model_validate(r) for r in rows]


@router.post("/{id_site}/exceptions", response_model=SiteExceptionRead, status_code=status.HTTP_201_CREATED)
async def create_exception(
    id_site: int,
    body: SiteExceptionCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteExceptionRead:
    await _get_site_or_404(db, current_user.id_org, id_site)
    exc = CmsSiteException(
        id_site=id_site,
        date_from=body.date_from,
        date_to=body.date_to,
        kind=body.kind,
        open_time=body.open_time if body.kind == "hours" else None,
        close_time=body.close_time if body.kind == "hours" else None,
        label=body.label,
    )
    db.add(exc)
    await db.commit()
    await db.refresh(exc)
    return SiteExceptionRead.model_validate(exc)


@router.delete("/{id_site}/exceptions/{id_exception}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_exception(
    id_site: int,
    id_exception: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_site_or_404(db, current_user.id_org, id_site)
    exc = (await db.execute(
        select(CmsSiteException).where(
            CmsSiteException.id_exception == id_exception,
            CmsSiteException.id_site == id_site,
        )
    )).scalar_one_or_none()
    if exc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exception not found")
    await db.delete(exc)
    await db.commit()


@router.get("/{id_site}/players", response_model=list[SitePlayerRead])
async def list_site_players(
    id_site: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SitePlayerRead]:
    await _get_site_or_404(db, current_user.id_org, id_site)
    players = (await db.execute(
        select(CmsPlayer)
        .where(CmsPlayer.id_site == id_site, CmsPlayer.id_org == current_user.id_org)
        .order_by(CmsPlayer.player_name)
    )).scalars().all()
    out: list[SitePlayerRead] = []
    for p in players:
        r = SitePlayerRead.model_validate(p)
        cfg = (p.display_config or {}).get("power_schedule") or {}
        r.power_mode = cfg.get("mode", "follow_site")
        out.append(r)
    return out


async def _get_player_in_org(db: AsyncSession, id_org: int, id_player: int) -> CmsPlayer:
    p = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player, CmsPlayer.id_org == id_org)
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return p


@router.post("/{id_site}/players/{id_player}", status_code=status.HTTP_204_NO_CONTENT)
async def attach_player(
    id_site: int,
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Rattache un player au site (déplace depuis son site précédent le cas échéant)."""
    site = await _get_site_or_404(db, current_user.id_org, id_site)
    player = await _get_player_in_org(db, current_user.id_org, id_player)
    player.id_site = id_site
    await db.commit()
    # Alignement auto du fuseau OS du player sur celui du site (best-effort, one-shot ;
    # le player relance son app — moment idéal : il vient d'être posé/déplacé).
    await align_player_timezone(db, player, site)


@router.delete("/{id_site}/players/{id_player}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_player(
    id_site: int,
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Détache un player du site (id_site → NULL, uniquement s'il est bien sur ce site)."""
    await _get_site_or_404(db, current_user.id_org, id_site)
    player = await _get_player_in_org(db, current_user.id_org, id_player)
    if player.id_site == id_site:
        player.id_site = None
        await db.commit()
