"""Connecteurs BI (§24.7) — flux CSV stables pour Power BI / Tableau / Looker / Metabase.

- GET /analytics/export/pop.csv              — preuves de diffusion brutes (période)
- GET /analytics/export/network-metrics.csv  — rollup quotidien (uptime, santé, alertes)
- GET /analytics/export/content.csv          — performance par contenu (période)
- GET/POST/DELETE /analytics/bi-keys         — clés BI `sfr_` (org-admin)

Auth des exports : JWT `reports:read` (démo refusée) OU clé BI `sfr_` (scope reports:export),
passée en Bearer ou en query `?api_key=` (les connecteurs BI web ne posent pas toujours de
header). Requêtes via get_db_worker (BYPASSRLS) filtrées par l'id_org de l'acteur — même
modèle que la vue publique share (§24.4). En-têtes de colonnes = snake_case anglais STABLES
(contrat d'intégration : ne pas renommer).
"""

import csv
import hashlib
import io
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import (
    REPORTS_EXPORT_SCOPE,
    ReportingActor,
    require_org_admin,
    require_reporting_actor,
)
from app.db.session import get_db_worker
from app.models.analytics import CmsNetworkMetricsDaily
from app.models.media import CmsMedia
from app.models.organization import CmsSite
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.user import CmsApiKey, CmsUser
from app.schemas.analytics import BiKeyCreate, BiKeyRead
from app.services import analytics, csv_safety, pop_counting

router = APIRouter(prefix="/analytics", tags=["analytics"])

_MAX_PERIOD_DAYS = 366


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


# Définition UNIQUE dans `services/csv_safety` (cf. commentaire dans pop.py).
_csv_safe = csv_safety.csv_safe


def _normalize_period(from_: date | None, until: date | None) -> tuple[date, date]:
    today = datetime.now(_tz()).date()
    if until is None:
        until = today
    if from_ is None:
        from_ = until - timedelta(days=29)
    if from_ > until:
        from_ = until
    if (until - from_).days + 1 > _MAX_PERIOD_DAYS:
        raise HTTPException(400, f"Period limited to {_MAX_PERIOD_DAYS} days.")
    return from_, until


def _csv_response(header: list[str], rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Flux CSV (§24.7) ─────────────────────────────────────────────────────────


async def _actor_is_org_admin(db, actor: ReportingActor) -> bool:
    """L'acteur UTILISATEUR est-il administrateur de son organisation ?

    Même vocabulaire de rôles que `governance_access._BYPASS_ROLES` — importé plutôt que
    recopié : deux listes de rôles d'administration finiraient par diverger.
    """
    from app.services import rbac
    from app.services.governance_access import _BYPASS_ROLES

    if actor.id_user is None:
        return False
    return bool(_BYPASS_ROLES.intersection(await rbac.get_user_role_keys(db, actor.id_user)))


async def _actor_scope(db, actor: ReportingActor) -> list[int] | None:
    """Périmètre de gouvernance de l'acteur (D1 étendue au menu Rapports, 31/07).

    Les exports BI sont un flux de PREUVES BRUTES : ils nomment l'écran, le site et le
    contenu. Un utilisateur JWT porteur d'un nœud doit y retrouver son périmètre, sinon
    l'export devient la porte de sortie que les écrans viennent de fermer.

    Une CLÉ machine (`sfr_`) n'a pas d'utilisateur : elle appartient à l'organisation et
    reste non restreinte — c'est un connecteur BI du retailer, pas un compte de nœud.
    """
    if actor.kind != "user" or actor.id_user is None:
        return None
    from sqlalchemy import select as _select

    from app.models.user import CmsUser as _CmsUser
    user = (await db.execute(
        _select(_CmsUser).where(_CmsUser.id_user == actor.id_user)
    )).scalar_one_or_none()
    if user is None:
        return None
    return await analytics.scoped_report_players(db, user)


@router.get("/export/pop.csv")
async def export_pop_csv(
    actor: Annotated[ReportingActor, Depends(require_reporting_actor)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
) -> Response:
    """Preuves de diffusion brutes de la période (défaut : 30 derniers jours)."""
    from_, until = _normalize_period(from_, until)
    start_utc, end_utc = analytics._day_bounds_utc(_tz(), from_, until)
    _scope = await _actor_scope(db, actor)
    rows = (await db.execute(
        select(
            CmsProofOfPlay.played_at, CmsProofOfPlay.id_player, CmsPlayer.player_name,
            CmsSite.site_name, CmsProofOfPlay.id_media, CmsMedia.media_name,
            CmsMedia.media_type, CmsProofOfPlay.duration_ms, CmsProofOfPlay.signature,
            CmsProofOfPlay.screen_off, CmsProofOfPlay.covered,
        )
        .join(CmsPlayer, CmsPlayer.id_player == CmsProofOfPlay.id_player, isouter=True)
        .join(CmsSite, CmsSite.id_site == CmsPlayer.id_site, isouter=True)
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media, isouter=True)
        .where(
            CmsProofOfPlay.id_org == actor.id_org,
            CmsProofOfPlay.played_at >= start_utc,
            CmsProofOfPlay.played_at < end_utc,
            *([CmsProofOfPlay.id_player.in_(_scope)] if _scope is not None else []),
        )
        .order_by(CmsProofOfPlay.played_at.asc(), CmsProofOfPlay.id_pop.asc())
    )).all()
    # `screen_off` (migr 0102) puis `covered` (migr 0125) ajoutés EN FIN de colonnes : un
    # connecteur BI existant qui lit par position n'est pas cassé. Les lignes marquées sont
    # exportées — c'est un flux de preuves brutes — mais l'outil aval doit les exclure de ses
    # totaux d'antenne. ⚠️ Règle à tenir : dans CE flux on n'insère jamais au milieu.
    return _csv_response(
        ["played_at_utc", "player_id", "player_name", "site_name",
         "media_id", "media_name", "media_type", "duration_sec", "signed",
         "screen_off", "covered"],
        [
            [
                r.played_at.astimezone(UTC).isoformat(), r.id_player,
                _csv_safe(r.player_name), _csv_safe(r.site_name), r.id_media,
                _csv_safe(r.media_name), _csv_safe(r.media_type),
                round((r.duration_ms or 0) / 1000), "true" if r.signature else "false",
                "true" if r.screen_off else "false",
                "true" if r.covered else "false",
            ]
            for r in rows
        ],
        f"signflow_pop_{from_.isoformat()}_{until.isoformat()}.csv",
    )


@router.get("/export/network-metrics.csv")
async def export_network_metrics_csv(
    actor: Annotated[ReportingActor, Depends(require_reporting_actor)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
) -> Response:
    """Rollup quotidien du réseau (uptime, conformité, alertes, health score).

    🔒 **Réservé à l'org-admin** (décision d'Ed, 2026-07-31). Ce flux est le seul des trois
    exports qui ne PEUT PAS être filtré par périmètre de gouvernance : il est agrégé au jour
    et à l'organisation, **sans dimension écran** — il n'y a rien à intersecter. Plutôt que
    de laisser un admin de nœud lire les indicateurs de tout le réseau (uptime, score de
    santé, alertes) par une porte que les autres surfaces viennent de fermer, on ferme la
    porte elle-même.

    ⚠️ La **clé machine `sfr_`** reste acceptée : elle appartient à l'ORGANISATION (créée par
    un org-admin, scope `reports:export`) et n'est pas un compte de nœud — c'est le connecteur
    BI du retailer. La restreindre casserait les intégrations sans rien protéger.
    """
    if actor.kind == "user":
        if not await _actor_is_org_admin(db, actor):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Export restricted to the organization administrator "
                       "(aggregated metrics for the whole network)",
            )
    from_, until = _normalize_period(from_, until)
    rows = (await db.execute(
        select(CmsNetworkMetricsDaily)
        .where(
            CmsNetworkMetricsDaily.id_org == actor.id_org,
            CmsNetworkMetricsDaily.metric_date >= from_,
            CmsNetworkMetricsDaily.metric_date <= until,
        )
        .order_by(CmsNetworkMetricsDaily.metric_date.asc())
    )).scalars().all()
    return _csv_response(
        ["metric_date", "players_total", "players_online", "players_warning",
         "players_offline", "players_maintenance", "conformity_pct", "alerts_active",
         "plays_count", "play_seconds", "health_score"],
        [
            [
                m.metric_date.isoformat(), m.players_total, m.players_online,
                m.players_warning, m.players_offline, m.players_maintenance,
                m.conformity_pct if m.conformity_pct is not None else "",
                m.alerts_active, m.plays_count, m.play_seconds,
                m.health_score if m.health_score is not None else "",
            ]
            for m in rows
        ],
        f"signflow_network_metrics_{from_.isoformat()}_{until.isoformat()}.csv",
    )


@router.get("/export/content.csv")
async def export_content_csv(
    actor: Annotated[ReportingActor, Depends(require_reporting_actor)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
    from_: date | None = Query(None, alias="from"),
    until: date | None = Query(None),
) -> Response:
    """Performance par contenu sur la période (diffusions, durée, portée players/sites)."""
    from_, until = _normalize_period(from_, until)
    start_utc, end_utc = analytics._day_bounds_utc(_tz(), from_, until)
    _scope = await _actor_scope(db, actor)
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
            CmsProofOfPlay.id_org == actor.id_org,
            CmsProofOfPlay.played_at >= start_utc,
            CmsProofOfPlay.played_at < end_utc,
            CmsProofOfPlay.id_media.isnot(None),
            # Flux AGRÉGÉ (performance par contenu) : ce qui n'a pas pu être vu est exclu —
            # écran coupé (migr 0102) ou zone recouverte (migr 0125).
            pop_counting.COUNTED,
            *([CmsProofOfPlay.id_player.in_(_scope)] if _scope is not None else []),
        )
        .group_by(CmsProofOfPlay.id_media, CmsMedia.media_name, CmsMedia.media_type)
        .order_by(func.count(CmsProofOfPlay.id_pop).desc())
    )).all()
    return _csv_response(
        ["media_id", "media_name", "media_type", "plays", "duration_sec",
         "players_reach", "sites_reach"],
        [
            [
                r.id_media, _csv_safe(r.media_name), _csv_safe(r.media_type),
                int(r.plays or 0), round((r.total_ms or 0) / 1000),
                int(r.players or 0), int(r.sites or 0),
            ]
            for r in rows
        ],
        f"signflow_content_{from_.isoformat()}_{until.isoformat()}.csv",
    )


# ── Clés BI (org-admin) ──────────────────────────────────────────────────────

def _key_read(k: CmsApiKey, raw: str | None = None) -> BiKeyRead:
    return BiKeyRead(
        id_key=k.id_key, key_prefix=k.key_prefix, label=k.label,
        last_used_at=k.last_used_at, expires_at=k.expires_at,
        created_at=k.created_at, key=raw,
    )


@router.get("/bi-keys", response_model=list[BiKeyRead])
async def list_bi_keys(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
):
    rows = (await db.execute(
        select(CmsApiKey).where(
            CmsApiKey.id_org == current_user.id_org,
            CmsApiKey.is_active == True,  # noqa: E712
        ).order_by(CmsApiKey.created_at.desc())
    )).scalars().all()
    return [_key_read(k) for k in rows if REPORTS_EXPORT_SCOPE in (k.scopes or [])]


@router.post("/bi-keys", response_model=BiKeyRead, status_code=201)
async def create_bi_key(
    body: BiKeyCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
):
    """Génère une clé BI `sfr_` — la valeur en clair n'est renvoyée QU'ICI (hash sha256 stocké)."""
    raw = f"sfr_{secrets.token_urlsafe(32)}"
    key = CmsApiKey(
        id_org=current_user.id_org, id_user=current_user.id_user,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        key_prefix=raw[:12], label=body.label,
        scopes=[REPORTS_EXPORT_SCOPE],
        expires_at=(
            datetime.now(UTC) + timedelta(days=body.expires_in_days)
            if body.expires_in_days else None),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return _key_read(key, raw=raw)


@router.delete("/bi-keys/{id_key}", status_code=204)
async def revoke_bi_key(
    id_key: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
):
    key = (await db.execute(
        select(CmsApiKey).where(
            CmsApiKey.id_key == id_key,
            CmsApiKey.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if key is None or REPORTS_EXPORT_SCOPE not in (key.scopes or []):
        raise HTTPException(404, "Key not found.")
    key.is_active = False
    await db.commit()
