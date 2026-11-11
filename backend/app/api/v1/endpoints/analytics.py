"""Analytics & Reporting Exécutif (§XXIV) — Executive Dashboard + PDF white-label,
ROI estimator et liens de partage sans login.

- GET  /analytics/executive              — KPIs + graphiques (gate reports:read → CMS_REPORT_VIEWER)
- GET  /analytics/executive/report.pdf   — rapport PDF brandé (§24.3)
- GET/PUT /analytics/branding            — white-label + hypothèses ROI (org-admin)
- POST/DELETE /analytics/branding/logo   — logo du rapport (org-admin)
- GET/POST/DELETE /analytics/shares      — liens de partage (§24.4, org-admin)
- GET  /public/share/{token}             — vue lecture seule sans login (route publique)

RLS-scopé à l'org du compte connecté ; la route publique résout l'org via le token (BYPASSRLS).
"""

import ipaddress
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_org_admin, require_permission
from app.core.security import hash_password, verify_password
from app.db.session import get_db_worker
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsSite
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.report import (
    CmsReportBranding,
    CmsReportHistory,
    CmsReportSchedule,
    CmsReportShareLink,
)
from app.models.user import CmsUser
from app.schemas.analytics import (
    AudienceHourRow,
    AudienceResponse,
    AudienceSiteRow,
    ContentPerfResponse,
    ContentPerfRow,
    ExecutiveDashboard,
    GeoResponse,
    GeoSite,
    NocResponse,
    NocRow,
    PublicShareView,
    ReportBranding,
    ReportBrandingUpdate,
    ReportHistoryRow,
    ReportScheduleCreate,
    ReportScheduleRead,
    ReportScheduleUpdate,
    RoiEstimate,
    ShareLinkCreate,
    ShareLinkRead,
    SmtpStatus,
)
from app.services import (
    analytics, email_delivery, media, pop_counting, report_pdf, report_render,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Ce qui n'a pas pu être vu n'est pas de la diffusion : écran coupé (migr 0102) ou zone
# recouverte par un overlay plein écran (migr 0125) sont exclus des performances de contenu
# ET de l'estimation d'audience — créditer des impressions sur un écran éteint ou sur une
# zone masquée fausserait le CPM du pDOOH. Définition unique : services/pop_counting.
_COUNTED = pop_counting.COUNTED
public_router = APIRouter(prefix="/public", tags=["public"])

_LOGO_PREFIX = "report-logos/"
_LOGO_MAX = 2 * 1024 * 1024  # 2 Mo
_LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/svg+xml": "svg",
               "image/webp": "webp"}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def _normalize_period(from_: date | None, until: date | None, tz: ZoneInfo) -> tuple[date, date]:
    today = datetime.now(tz).date()
    if until is None:
        until = today
    if from_ is None:
        from_ = until - timedelta(days=29)
    if from_ > until:
        from_ = until
    return from_, until


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/executive", response_model=ExecutiveDashboard)
async def executive_dashboard(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
    id_players: Annotated[list[int] | None, Query()] = None,
    id_group: int | None = Query(None),
    id_site: int | None = Query(None),
    id_site_group: int | None = Query(None),
):
    """Executive Dashboard (§24.1) sur [from, until] (défaut : 30 derniers jours).

    **Périmètre de gouvernance (D1 étendue au menu Rapports)** : le filtre demandé est
    INTERSECTÉ avec le périmètre de l'utilisateur — `id_players` vient du client, sans
    l'intersection il suffisait de nommer l'écran d'un autre nœud pour lire ses chiffres.
    """
    tz = _tz()
    from_, until = _normalize_period(from_, until, tz)
    player_ids = await analytics.scoped_report_players(
        db, current_user, id_players, id_group, id_site, id_site_group)
    payload = await analytics.build_executive_payload(
        db, current_user.id_org, from_, until, player_ids, tz)
    return ExecutiveDashboard(**payload)


# ── Dashboards spécialisés (§24.2) ───────────────────────────────────────────

# Nombre maximal de LIGNES détaillées renvoyées par le NOC. Les compteurs, eux, sont
# calculés sur le parc ENTIER (cf. `_NOC_ROW_CAP` plus bas) : plafonner la charge utile
# ne doit jamais fausser un chiffre affiché.
_NOC_ROW_CAP = 2000


@router.get("/noc", response_model=NocResponse)
async def noc_dashboard(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Dashboard NOC / Opérations (§24.2) : flotte enrichie (site résolu) + résumé statuts.
    Gate reports:read → accessible au CMS_REPORT_VIEWER sans droit opérationnel players:read.

    **Périmètre de gouvernance (N8/G1)** : un admin de nœud ne voit que les écrans de ses
    sous-arbres — ici comme dans `/players/`. Sans ce filtre, il suffisait de détenir
    `reports:read` (5 rôles système le portent, dont CMS_VIEWER) pour lire l'inventaire
    COMPLET du parc — nom, OS, version, site, disque — alors que `/players/` le restreignait
    correctement. Deux surfaces portant la même donnée avec deux règles de périmètre
    divergent toujours : c'est l'écart mesuré à l'audit du Dashboard (2026-07-28).

    **Plafond non silencieux** : les compteurs portent sur tout le périmètre, seules les
    lignes sont plafonnées, et `rows_truncated` le DIT. Avant, `total` valait `len(rows)`
    après un `LIMIT 2000` : au-delà, le nombre d'écrans annoncé était faux sans aucun signal.
    """
    from app.services.governance_scope import user_player_scope

    scope = await user_player_scope(db, current_user)
    if scope is not None and not scope:  # périmètre vide : il ne voit rien (décision Ed)
        return NocResponse(total=0, online=0, warning=0, offline=0, maintenance=0,
                           rows=[], rows_truncated=False)

    def _scoped(stmt):
        stmt = stmt.where(CmsPlayer.id_org == current_user.id_org)
        return stmt if scope is None else stmt.where(CmsPlayer.id_player.in_(scope))

    now = datetime.now(UTC)

    # ① Compteurs sur TOUT le périmètre (3 colonnes seulement : reste léger à 10 000 écrans).
    # Le statut « effectif » se calcule en Python (il dépend de last_seen_at), d'où ce
    # passage plutôt qu'un COUNT(*) FILTER en SQL.
    tally_rows = (await db.execute(
        _scoped(select(CmsPlayer.status, CmsPlayer.last_seen_at))
    )).all()
    effective = [analytics.effective_status(s, ls, now) for s, ls in tally_rows]

    # ② Lignes détaillées, plafonnées.
    rows = (await db.execute(
        _scoped(
            select(
                CmsPlayer.id_player, CmsPlayer.player_name, CmsPlayer.id_site,
                CmsSite.site_name, CmsPlayer.player_os, CmsPlayer.player_version,
                CmsPlayer.status, CmsPlayer.last_seen_at, CmsPlayer.disk_usage,
                CmsPlayer.alert_reasons,
            ).join(CmsSite, CmsSite.id_site == CmsPlayer.id_site, isouter=True)
        )
        .order_by((CmsPlayer.status == "online"), CmsPlayer.player_name)
        .limit(_NOC_ROW_CAP)
    )).all()
    out = [
        NocRow(
            id_player=r.id_player, player_name=r.player_name, id_site=r.id_site,
            site_name=r.site_name,
            player_os=r.player_os, player_version=r.player_version,
            status=analytics.effective_status(r.status, r.last_seen_at, now),
            last_seen_at=r.last_seen_at, disk_usage=r.disk_usage,
            alert_reasons=list(r.alert_reasons or []),
        ) for r in rows
    ]
    return NocResponse(
        total=len(effective),
        online=sum(1 for s in effective if s == "online"),
        warning=sum(1 for s in effective if s == "warning"),
        offline=sum(1 for s in effective if s in ("offline", "critical")),
        maintenance=sum(1 for s in effective if s == "maintenance"),
        rows=out,
        rows_truncated=len(effective) > len(out),
    )


@router.get("/content", response_model=ContentPerfResponse)
async def content_performance(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
):
    """Dashboard Contenu (§24.2) : performance par contenu sur la période (Proof of Play,
    tous types) — diffusions, durée cumulée, portée (players/sites distincts).

    **Périmètre de gouvernance (D1 étendue)** : le classement était calculé sur TOUTE
    l'organisation — un responsable de nœud y lisait donc les contenus (et les campagnes)
    diffusés ailleurs, avec leurs volumes. Hors périmètre = rien.
    """
    tz = _tz()
    from_, until = _normalize_period(from_, until, tz)
    start_utc, end_utc = analytics._day_bounds_utc(tz, from_, until)
    scope = await analytics.scoped_report_players(db, current_user)
    rows = (await db.execute(
        select(
            CmsProofOfPlay.id_media, CmsMedia.media_name, CmsMedia.media_type,
            func.count(CmsProofOfPlay.id_pop).label("plays"),
            func.coalesce(func.sum(CmsProofOfPlay.duration_ms), 0).label("total_ms"),
            func.count(distinct(CmsProofOfPlay.id_player)).label("players"),
            func.count(distinct(CmsPlayer.id_site)).label("sites"),
        )
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media, isouter=True)
        .join(CmsPlayer, CmsPlayer.id_player == CmsProofOfPlay.id_player, isouter=True)
        .where(
            CmsProofOfPlay.id_org == current_user.id_org,
            CmsProofOfPlay.played_at >= start_utc,
            CmsProofOfPlay.played_at < end_utc,
            CmsProofOfPlay.id_media.isnot(None),
            _COUNTED,
            *([CmsProofOfPlay.id_player.in_(scope)] if scope is not None else []),
        )
        .group_by(CmsProofOfPlay.id_media, CmsMedia.media_name, CmsMedia.media_type)
        .order_by(func.count(CmsProofOfPlay.id_pop).desc())
        .limit(500)
    )).all()
    out = [
        ContentPerfRow(
            id_media=r.id_media, media_name=r.media_name, media_type=r.media_type,
            plays=int(r.plays or 0), seconds=round((r.total_ms or 0) / 1000),
            players=int(r.players or 0), sites=int(r.sites or 0),
        ) for r in rows
    ]
    return ContentPerfResponse(
        from_date=from_, until_date=until,
        total_plays=sum(r.plays for r in out),
        total_seconds=sum(r.seconds for r in out),
        rows=out,
    )


# ── Carte géographique du parc (§24.2) ───────────────────────────────────────

@router.get("/geo", response_model=GeoResponse)
async def geo_dashboard(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Sites géolocalisés + agrégat de statut des players par site (§24.2).

    Agrégation en Python (pas de FILTER SQL) pour appliquer le MÊME statut « effectif » que
    la table NOC — online/warning périmé → offline — et rester cohérent avec elle (sinon la
    carte et la table divergeraient, cf. l'écart Q4 corrigé le 18/07).

    **Périmètre de gouvernance (N8/G1)** : mêmes règles que le NOC. Décision d'Ed
    (2026-07-28) : hors périmètre, il ne voit RIEN — un site dont aucun écran ne lui
    appartient DISPARAÎT de la carte (il n'apparaît pas non plus « à 0 »), et ses
    coordonnées ne sont donc jamais révélées. `unplaced` suit la même règle : on ne
    compte que les sites du périmètre.
    """
    from app.services.governance_scope import user_player_scope

    org = current_user.id_org
    now = datetime.now(UTC)
    scope = await user_player_scope(db, current_user)
    if scope is not None and not scope:
        return GeoResponse(sites=[], unplaced=0)

    site_rows = (await db.execute(
        select(
            CmsSite.id_site, CmsSite.site_name, CmsSite.country, CmsSite.site_number,
            CmsSite.latitude, CmsSite.longitude,
        ).where(CmsSite.id_org == org)
    )).all()
    player_stmt = (
        select(CmsPlayer.id_site, CmsPlayer.status, CmsPlayer.last_seen_at)
        .where(CmsPlayer.id_org == org, CmsPlayer.id_site.isnot(None))
    )
    if scope is not None:
        player_stmt = player_stmt.where(CmsPlayer.id_player.in_(scope))
    player_rows = (await db.execute(player_stmt)).all()

    if scope is not None:
        # Un site n'existe pour lui que s'il y héberge au moins un de SES écrans.
        visible_sites = {p.id_site for p in player_rows}
        site_rows = [r for r in site_rows if r.id_site in visible_sites]

    # Compteurs par site (statut effectif ; critical compté offline — bucketing NOC/summary).
    counts: dict[int, dict[str, int]] = {}
    for p in player_rows:
        eff = analytics.effective_status(p.status, p.last_seen_at, now)
        c = counts.setdefault(p.id_site, {"total": 0, "online": 0, "warning": 0, "offline": 0, "maintenance": 0})
        c["total"] += 1
        if eff == "online":
            c["online"] += 1
        elif eff == "warning":
            c["warning"] += 1
        elif eff == "maintenance":
            c["maintenance"] += 1
        else:  # offline + critical
            c["offline"] += 1

    placed: list[GeoSite] = []
    for r in site_rows:
        if r.latitude is None or r.longitude is None:
            continue
        c = counts.get(r.id_site, {})
        placed.append(GeoSite(
            id_site=r.id_site, site_name=r.site_name, country=r.country,
            site_number=r.site_number,
            latitude=float(r.latitude), longitude=float(r.longitude),
            players_total=c.get("total", 0), players_online=c.get("online", 0),
            players_warning=c.get("warning", 0), players_offline=c.get("offline", 0),
            players_maintenance=c.get("maintenance", 0),
        ))
    unplaced = sum(1 for r in site_rows if r.latitude is None or r.longitude is None)
    return GeoResponse(sites=placed, unplaced=unplaced)


# ── Audience estimée (§24.2, sans Quividi) ───────────────────────────────────

@router.get("/audience", response_model=AudienceResponse)
async def audience_dashboard(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
):
    """Audience ESTIMÉE (§24.2) — impressions dérivées du Proof of Play × une hypothèse de
    fréquentation (contacts/h/écran, paramètre ROI). Pas de mesure réelle (Quividi = matériel).
    impressions ≈ heures de diffusion × contacts/h. Par site + par heure de la journée.

    **Périmètre de gouvernance (D1 étendue)** : la ventilation PAR SITE révélait la
    fréquentation estimée de magasins qu'on n'administre pas. Hors périmètre = rien.
    """
    tz = _tz()
    from_, until = _normalize_period(from_, until, tz)
    start_utc, end_utc = analytics._day_bounds_utc(tz, from_, until)
    org = current_user.id_org
    scope = await analytics.scoped_report_players(db, current_user)
    _scope_cond = ([CmsProofOfPlay.id_player.in_(scope)] if scope is not None else [])

    branding = await report_render.branding_or_default(db, org)
    cph = int(branding.roi_contacts_hour)

    def est(seconds: float) -> int:
        return round((seconds / 3600.0) * cph)

    # Par site
    site_rows = (await db.execute(
        select(
            CmsPlayer.id_site, CmsSite.site_name,
            func.count(CmsProofOfPlay.id_pop).label("plays"),
            func.coalesce(func.sum(CmsProofOfPlay.duration_ms), 0).label("total_ms"),
        )
        .join(CmsPlayer, CmsPlayer.id_player == CmsProofOfPlay.id_player, isouter=True)
        .join(CmsSite, CmsSite.id_site == CmsPlayer.id_site, isouter=True)
        .where(
            CmsProofOfPlay.id_org == org,
            CmsProofOfPlay.played_at >= start_utc,
            CmsProofOfPlay.played_at < end_utc,
            _COUNTED,
            *_scope_cond,
        )
        .group_by(CmsPlayer.id_site, CmsSite.site_name)
        .order_by(func.sum(CmsProofOfPlay.duration_ms).desc())
    )).all()
    by_site = [
        AudienceSiteRow(
            id_site=r.id_site, site_name=r.site_name, plays=int(r.plays or 0),
            seconds=round((r.total_ms or 0) / 1000), est_impressions=est((r.total_ms or 0) / 1000),
        ) for r in site_rows
    ]

    # Par heure locale de la journée
    local_hour = func.extract("hour", func.timezone(settings.SIGNFLOW_TZ, CmsProofOfPlay.played_at))
    hour_rows = (await db.execute(
        select(
            local_hour.label("hour"),
            func.count(CmsProofOfPlay.id_pop).label("plays"),
            func.coalesce(func.sum(CmsProofOfPlay.duration_ms), 0).label("total_ms"),
        )
        .where(
            CmsProofOfPlay.id_org == org,
            CmsProofOfPlay.played_at >= start_utc,
            CmsProofOfPlay.played_at < end_utc,
            _COUNTED,
            *_scope_cond,
        )
        .group_by(local_hour)
    )).all()
    hmap = {int(r.hour): r for r in hour_rows}
    by_hour = [
        AudienceHourRow(
            hour=h,
            plays=int(hmap[h].plays or 0) if h in hmap else 0,
            est_impressions=est((hmap[h].total_ms or 0) / 1000) if h in hmap else 0,
        ) for h in range(24)
    ]

    total = sum(s.est_impressions for s in by_site)
    return AudienceResponse(
        from_date=from_, until_date=until, contacts_per_hour=cph,
        total_est_impressions=total, by_site=by_site, by_hour=by_hour)


# ── Branding white-label + ROI ───────────────────────────────────────────────

def _default_branding(id_org: int) -> CmsReportBranding:
    """Branding par défaut TRANSITOIRE (non persisté) — valeurs Python-side alignées sur les
    server_default DB (un objet non inséré n'a pas ces défauts)."""
    return CmsReportBranding(
        id_org=id_org, primary_color="#4875B4", roi_hours_per_day=12,
        roi_contacts_hour=50, roi_cpm_eur=5, roi_paper_eur=50)


async def _branding_or_default(db: AsyncSession, id_org: int) -> CmsReportBranding:
    """Lecture PURE : renvoie la ligne si elle existe, sinon un défaut transitoire (aucune
    écriture). Utilisé par les GET/PDF → un compte démo (lecture seule) ne crée rien."""
    b = (await db.execute(
        select(CmsReportBranding).where(CmsReportBranding.id_org == id_org)
    )).scalar_one_or_none()
    return b if b is not None else _default_branding(id_org)


async def _get_branding(db: AsyncSession, id_org: int) -> CmsReportBranding:
    """Crée la ligne si absente — réservé aux chemins d'ÉCRITURE (PUT/logo, gate org-admin)."""
    b = (await db.execute(
        select(CmsReportBranding).where(CmsReportBranding.id_org == id_org)
    )).scalar_one_or_none()
    if b is None:
        b = CmsReportBranding(id_org=id_org)
        db.add(b)
        await db.commit()
        await db.refresh(b)
    return b


def _branding_read(b: CmsReportBranding) -> ReportBranding:
    return ReportBranding(
        primary_color=b.primary_color,
        report_title=b.report_title,
        report_subtitle=b.report_subtitle,
        roi_hours_per_day=float(b.roi_hours_per_day),
        roi_contacts_hour=b.roi_contacts_hour,
        roi_cpm_eur=float(b.roi_cpm_eur),
        roi_paper_eur=float(b.roi_paper_eur),
        logo_url=media.get_signed_url(b.logo_key) if b.logo_key else None,
        has_logo=bool(b.logo_key),
    )


def _roi_from_branding(b: CmsReportBranding, screens: int, uptime_pct: float | None,
                       days: int) -> dict:
    return analytics.roi_estimate(
        screens=screens, uptime_pct=uptime_pct, days=days,
        hours_per_day=float(b.roi_hours_per_day), contacts_per_hour=float(b.roi_contacts_hour),
        cpm_eur=float(b.roi_cpm_eur), paper_eur_per_month=float(b.roi_paper_eur),
    )


@router.get("/branding", response_model=ReportBranding)
async def get_branding(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _branding_read(await _branding_or_default(db, current_user.id_org))


@router.put("/branding", response_model=ReportBranding)
async def update_branding(
    body: ReportBrandingUpdate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    b = await _get_branding(db, current_user.id_org)
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(b, field, val)
    await db.commit()
    await db.refresh(b)
    return _branding_read(b)


@router.post("/branding/logo", response_model=ReportBranding)
async def upload_branding_logo(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
):
    ext = _LOGO_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(400, "Unsupported logo format (PNG/JPEG/SVG/WebP).")
    data = await file.read()
    if len(data) > _LOGO_MAX:
        raise HTTPException(400, "Logo too large (max 2 MB).")
    import io
    key = f"{_LOGO_PREFIX}{current_user.id_org}/logo.{ext}"
    await run_in_threadpool(
        media._stream_upload_sync, io.BytesIO(data), key, file.content_type)
    b = await _get_branding(db, current_user.id_org)
    b.logo_key = key
    await db.commit()
    await db.refresh(b)
    return _branding_read(b)


@router.delete("/branding/logo", response_model=ReportBranding)
async def delete_branding_logo(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    b = await _get_branding(db, current_user.id_org)
    b.logo_key = None
    await db.commit()
    await db.refresh(b)
    return _branding_read(b)


# ── PDF (§24.3) ──────────────────────────────────────────────────────────────

async def _build_pdf(db: AsyncSession, id_org: int, org_name: str, from_: date, until: date,
                     player_ids: list[int] | None, tz: ZoneInfo, with_roi: bool,
                     locale: str | None = None) -> bytes:
    payload = await analytics.build_executive_payload(db, id_org, from_, until, player_ids, tz)
    b = await _branding_or_default(db, id_org)
    roi = None
    if with_roi:
        days = (until - from_).days + 1
        roi = _roi_from_branding(b, payload["fleet"]["total"], payload["uptime_pct"], days)
    logo_bytes = None
    if b.logo_key and not b.logo_key.endswith(".svg"):  # reportlab ne lit pas le SVG
        try:
            logo_bytes = await run_in_threadpool(_fetch_logo_bytes, b.logo_key)
        except Exception:
            logo_bytes = None
    generated = report_pdf.fmt_datetime(datetime.now(tz), locale)
    return await run_in_threadpool(
        report_pdf.build_executive_pdf,
        org_name=org_name, payload=payload,
        branding={"primary_color": b.primary_color, "report_title": b.report_title,
                  "report_subtitle": b.report_subtitle},
        roi=roi, logo_bytes=logo_bytes, generated_at=generated, locale=locale,
    )


def _fetch_logo_bytes(key: str) -> bytes:
    obj = media._s3().get_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=key)
    return obj["Body"].read()


@router.get("/executive/report.pdf")
async def executive_report_pdf(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
    roi: bool = Query(True),
    id_group: int | None = Query(None),
    id_site: int | None = Query(None),
    id_site_group: int | None = Query(None),
):
    """Rapport exécutif PDF white-label (§24.3) — mêmes filtres de périmètre que le dashboard."""
    tz = _tz()
    from_, until = _normalize_period(from_, until, tz)
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
    )).scalar_one()
    # Même périmètre que le dashboard : un PDF est un document qui SORT de l'entreprise,
    # il ne doit pas contenir ce que l'écran refuse d'afficher.
    player_ids = await analytics.scoped_report_players(
        db, current_user, None, id_group, id_site, id_site_group)
    # Langue du document = profil de la personne qui le télécharge (défaut anglais).
    pdf = await _build_pdf(
        db, current_user.id_org, org.org_name, from_, until, player_ids, tz, roi,
        locale=current_user.locale)
    filename = report_render.pdf_filename(from_, until, current_user.locale)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Share links (§24.4) ──────────────────────────────────────────────────────

def _share_read(s: CmsReportShareLink) -> ShareLinkRead:
    # Chemin relatif — le frontend préfixe avec son origine pour l'URL partageable.
    return ShareLinkRead(
        id_share=s.id_share, token=s.token,
        url=f"/share/{s.token}",
        label=s.label, period_days=s.period_days, expires_at=s.expires_at,
        revoked=s.revoked, access_count=s.access_count,
        last_accessed_at=s.last_accessed_at,
        has_password=s.password_hash is not None,
        ip_allowlist=list(s.ip_allowlist) if s.ip_allowlist else None,
        created_at=s.created_at,
    )


@router.get("/shares", response_model=list[ShareLinkRead])
async def list_shares(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(
        select(CmsReportShareLink)
        .where(CmsReportShareLink.id_org == current_user.id_org)
        .order_by(CmsReportShareLink.created_at.desc())
    )).scalars().all()
    return [_share_read(s) for s in rows]


@router.post("/shares", response_model=ShareLinkRead, status_code=201)
async def create_share(
    body: ShareLinkCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_in_days)
    s = CmsReportShareLink(
        id_org=current_user.id_org, token=secrets.token_urlsafe(24),
        label=body.label, period_days=body.period_days, expires_at=expires_at,
        created_by=current_user.id_user,
        password_hash=(
            await run_in_threadpool(hash_password, body.password) if body.password else None),
        ip_allowlist=body.ip_allowlist,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _share_read(s)


@router.delete("/shares/{id_share}", response_model=ShareLinkRead)
async def revoke_share(
    id_share: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    s = (await db.execute(
        select(CmsReportShareLink).where(
            CmsReportShareLink.id_share == id_share,
            CmsReportShareLink.id_org == current_user.id_org)
    )).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "Link not found.")
    s.revoked = True
    await db.commit()
    await db.refresh(s)
    return _share_read(s)


# ── Rapports planifiés par email (§24.3) ─────────────────────────────────────

def _schedule_read(s: CmsReportSchedule) -> ReportScheduleRead:
    return ReportScheduleRead(
        id_schedule=s.id_schedule, label=s.label, frequency=s.frequency,
        send_hour=s.send_hour, send_dow=s.send_dow, send_dom=s.send_dom,
        period_days=s.period_days, language=s.language,
        recipients=list(s.recipients or []), include_roi=s.include_roi, enabled=s.enabled,
        last_run_at=s.last_run_at, next_run_at=s.next_run_at, created_at=s.created_at)


@router.get("/smtp-status", response_model=SmtpStatus)
async def smtp_status(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
):
    return SmtpStatus(configured=email_delivery.smtp_configured())


@router.get("/report-schedules", response_model=list[ReportScheduleRead])
async def list_report_schedules(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(
        select(CmsReportSchedule)
        .where(CmsReportSchedule.id_org == current_user.id_org)
        .order_by(CmsReportSchedule.created_at.desc())
    )).scalars().all()
    return [_schedule_read(s) for s in rows]


@router.post("/report-schedules", response_model=ReportScheduleRead, status_code=201)
async def create_report_schedule(
    body: ReportScheduleCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    tz = _tz()
    s = CmsReportSchedule(
        id_org=current_user.id_org, label=body.label, frequency=body.frequency,
        send_hour=body.send_hour, send_dow=body.send_dow, send_dom=body.send_dom,
        period_days=body.period_days, language=body.language,
        recipients=body.recipients, include_roi=body.include_roi, enabled=body.enabled,
        created_by=current_user.id_user)
    s.next_run_at = report_render.compute_next_run(
        frequency=s.frequency, send_hour=s.send_hour, send_dow=s.send_dow,
        send_dom=s.send_dom, tz=tz, after=datetime.now(UTC))
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _schedule_read(s)


async def _get_schedule_or_404(db: AsyncSession, id_org: int, id_schedule: int
                               ) -> CmsReportSchedule:
    s = (await db.execute(
        select(CmsReportSchedule).where(
            CmsReportSchedule.id_schedule == id_schedule,
            CmsReportSchedule.id_org == id_org)
    )).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "Schedule not found.")
    return s


@router.put("/report-schedules/{id_schedule}", response_model=ReportScheduleRead)
async def update_report_schedule(
    id_schedule: int,
    body: ReportScheduleUpdate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    s = await _get_schedule_or_404(db, current_user.id_org, id_schedule)
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(s, field, val)
    # recalcule la prochaine échéance sur toute modif de cadence
    s.next_run_at = report_render.compute_next_run(
        frequency=s.frequency, send_hour=s.send_hour, send_dow=s.send_dow,
        send_dom=s.send_dom, tz=_tz(), after=datetime.now(UTC))
    await db.commit()
    await db.refresh(s)
    return _schedule_read(s)


@router.delete("/report-schedules/{id_schedule}", status_code=204)
async def delete_report_schedule(
    id_schedule: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    s = await _get_schedule_or_404(db, current_user.id_org, id_schedule)
    await db.delete(s)
    await db.commit()


@router.post("/report-schedules/{id_schedule}/send-now", response_model=ReportHistoryRow)
async def send_report_now(
    id_schedule: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Envoie immédiatement le rapport (test) — ne modifie pas la prochaine échéance."""
    s = await _get_schedule_or_404(db, current_user.id_org, id_schedule)
    tz = _tz()
    until = datetime.now(tz).date()
    from_d = until - timedelta(days=s.period_days - 1)
    recipients = [r for r in (s.recipients or []) if isinstance(r, str) and r.strip()]
    status, detail = "sent", None
    try:
        if not recipients:
            raise email_delivery.EmailError("no recipient configured")
        # Même langue que l'envoi planifié : profil du créateur de la planification.
        locale = await report_render.user_locale(db, s.created_by)
        pdf, org_name = await report_render.render_executive_pdf(
            db, s.id_org, from_d, until, tz, with_roi=s.include_roi,
            fetch_logo=lambda k: run_in_threadpool(report_render._fetch_logo_bytes, k),
            locale=locale)
        subject, body, fname = report_render.executive_email(
            org_name, from_d, until, locale, test=True)
        email_delivery.send_email(
            to=recipients, subject=subject, body=body, attachments=[(fname, pdf, "pdf")])
    except Exception as e:  # noqa: BLE001
        status, detail = "failed", str(e)[:500]
    h = CmsReportHistory(
        id_org=s.id_org, id_schedule=s.id_schedule, report_type=s.report_type,
        period_from=from_d, period_until=until, recipients=recipients,
        status=status, detail=detail)
    db.add(h)
    await db.commit()
    await db.refresh(h)
    if status == "failed":
        raise HTTPException(502, detail or "Sending failed.")
    return ReportHistoryRow(
        id_history=h.id_history, id_schedule=h.id_schedule, report_type=h.report_type,
        period_from=h.period_from, period_until=h.period_until, recipients=recipients,
        status=h.status, detail=h.detail, created_at=h.created_at)


@router.get("/report-history", response_model=list[ReportHistoryRow])
async def list_report_history(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),
):
    rows = (await db.execute(
        select(CmsReportHistory)
        .where(CmsReportHistory.id_org == current_user.id_org)
        .order_by(CmsReportHistory.id_history.desc()).limit(limit)
    )).scalars().all()
    return [
        ReportHistoryRow(
            id_history=h.id_history, id_schedule=h.id_schedule, report_type=h.report_type,
            period_from=h.period_from, period_until=h.period_until,
            recipients=list(h.recipients or []), status=h.status, detail=h.detail,
            created_at=h.created_at)
        for h in rows
    ]


# ── Vue publique (sans login, §24.4) ─────────────────────────────────────────

def _public_client_ip(request: Request) -> str:
    """IP cliente réelle derrière Caddy (1re valeur de X-Forwarded-For), comme le login."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _ip_allowed(client_ip: str, allowlist: list) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False  # IP inconnue/invalide → refus (fail-closed)
    for cidr in allowlist:
        try:
            if addr in ipaddress.ip_network(str(cidr), strict=False):
                return True
        except ValueError:
            continue
    return False


@public_router.get("/share/{token}", response_model=PublicShareView)
async def public_share(
    token: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_worker)],
    x_share_password: Annotated[str | None, Header(alias="X-Share-Password")] = None,
):
    """Vue lecture seule d'un dashboard partagé. Résout l'org via le token (BYPASSRLS) —
    les agrégats analytics filtrent par id_org explicite, aucune donnée cross-org n'est exposée.

    Options avancées (§24.4) : IP hors allowlist → 404 (ne révèle pas l'existence du lien) ;
    mot de passe requis → 401 `password_required` sans header, 403 `invalid_password` si faux
    (bcrypt ~100 ms = frein brute-force naturel)."""
    s = (await db.execute(
        select(CmsReportShareLink).where(CmsReportShareLink.token == token)
    )).scalar_one_or_none()
    now = datetime.now(UTC)
    if s is None or s.revoked or (s.expires_at and s.expires_at < now):
        raise HTTPException(404, "Invalid or expired link.")

    if s.ip_allowlist and not _ip_allowed(_public_client_ip(request), s.ip_allowlist):
        raise HTTPException(404, "Invalid or expired link.")

    if s.password_hash:
        if not x_share_password:
            raise HTTPException(401, "password_required")
        ok = await run_in_threadpool(verify_password, x_share_password, s.password_hash)
        if not ok:
            raise HTTPException(403, "invalid_password")

    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == s.id_org)
    )).scalar_one_or_none()
    if org is None or not org.is_active:
        raise HTTPException(404, "Invalid or expired link.")

    tz = _tz()
    b = (await db.execute(
        select(CmsReportBranding).where(CmsReportBranding.id_org == s.id_org)
    )).scalar_one_or_none()
    if b is not None:
        color, title, subtitle = b.primary_color, b.report_title, b.report_subtitle
        logo_url = media.get_signed_url(b.logo_key) if b.logo_key else None
    else:
        color, title, subtitle, logo_url = "#4875B4", None, None, None

    async def _log_access() -> None:
        await db.execute(
            update(CmsReportShareLink).where(CmsReportShareLink.id_share == s.id_share)
            .values(access_count=CmsReportShareLink.access_count + 1, last_accessed_at=now)
        )
        await db.commit()

    # ── Lien « campagne » (S4 Retail Media) : rapport annonceur au lieu du dashboard ──
    if s.report_type == "campaign" and s.id_campaign:
        from app.models.campaign import CmsCampaign
        from app.services import campaign_report
        camp = (await db.execute(
            select(CmsCampaign).where(
                CmsCampaign.id_campaign == s.id_campaign,
                CmsCampaign.id_org == s.id_org,
                CmsCampaign.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if camp is None:
            raise HTTPException(404, "Invalid or expired link.")
        report = await campaign_report.build_campaign_report(db, camp, tz)
        await _log_access()
        return PublicShareView(
            org_name=org.org_name, primary_color=color, report_title=title,
            report_subtitle=subtitle, logo_url=logo_url,
            report_type="campaign", campaign_report=report,
        )

    until = datetime.now(tz).date()
    from_ = until - timedelta(days=s.period_days - 1)
    payload = await analytics.build_executive_payload(db, s.id_org, from_, until, None, tz)
    days = (until - from_).days + 1
    if b is not None:
        roi = _roi_from_branding(b, payload["fleet"]["total"], payload["uptime_pct"], days)
    else:
        roi = analytics.roi_estimate(
            screens=payload["fleet"]["total"], uptime_pct=payload["uptime_pct"], days=days,
            hours_per_day=12, contacts_per_hour=50, cpm_eur=5, paper_eur_per_month=50)

    await _log_access()

    return PublicShareView(
        org_name=org.org_name, primary_color=color, report_title=title, report_subtitle=subtitle,
        logo_url=logo_url, dashboard=ExecutiveDashboard(**payload), roi=RoiEstimate(**roi),
    )
