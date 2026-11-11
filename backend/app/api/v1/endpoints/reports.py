"""
Reports — Droits d'auteur SACEM/SOCAN (§XIX.6).

GET /reports/rights — utilisation des œuvres audio sur une période, à partir
                      du Proof of Play. Format JSON (défaut) ou CSV.

Le rapport s'appuie sur cms_proof_of_play (jointure cms_media via id_media) —
aucune table supplémentaire requise.
"""

import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_permission
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.user import CmsUser
from app.schemas.report import RightsReportResponse, RightsReportRow
from app.services import csv_safety
from app.services.doc_i18n import tr

router = APIRouter(prefix="/reports", tags=["reports"])


# Neutralise l'injection de formule CSV (audit sécurité 2026-06-25) : un nom de
# média/compositeur contrôlé par un utilisateur du tenant peut commencer par =,+,-,@
# (ou tab/CR) → exécuté comme formule à l'ouverture dans Excel/Calc (HYPERLINK, DDE…).
# Définition UNIQUE dans `services/csv_safety` — elle vivait ici en troisième exemplaire.
_csv_safe = csv_safety.csv_safe

# Textes du CSV « droits d'auteur » dans la langue du profil du demandeur (défaut anglais).
# En-têtes séparés par des virgules, sans espace ; le français est conservé tel quel
# (historique), les autres langues sont en ASCII.
DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "csv_header": "ISRC,Title,Composer,Publisher,Rights_society,Plays,Total_duration_sec,"
                      "Players,Sites,Royalty_free",
        "yes": "yes",
        "no": "no",
    },
    "fr": {
        "csv_header": "ISRC,Titre,Compositeur,Editeur,Société,Nb_diffusions,Durée_totale_sec,"
                      "Players,Sites,Libre_de_droits",
        "yes": "oui",
        "no": "non",
    },
    "es": {
        "csv_header": "ISRC,Titulo,Compositor,Editor,Sociedad_gestion,Emisiones,"
                      "Duracion_total_seg,Players,Sitios,Libre_de_derechos",
        "yes": "si",
        "no": "no",
    },
    "pt": {
        "csv_header": "ISRC,Titulo,Compositor,Editora,Sociedade_gestao,Difusoes,"
                      "Duracao_total_seg,Players,Locais,Livre_de_direitos",
        "yes": "sim",
        "no": "nao",
    },
    "de": {
        "csv_header": "ISRC,Titel,Komponist,Verlag,Verwertungsgesellschaft,Wiedergaben,"
                      "Gesamtdauer_Sek,Player,Standorte,Lizenzfrei",
        "yes": "ja",
        "no": "nein",
    },
}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def _day_bounds_utc(from_d: date, until_d: date) -> tuple[datetime, datetime]:
    """Bornes [début from, fin until] en UTC, jours définis en SIGNFLOW_TZ (until inclus)."""
    tz = _tz()
    start_local = datetime.combine(from_d, time.min, tzinfo=tz)
    end_local = datetime.combine(until_d + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def _rights_rows(
    db: AsyncSession, id_org: int, from_d: date, until_d: date, rights_org: Optional[str]
) -> list[RightsReportRow]:
    start_utc, end_utc = _day_bounds_utc(from_d, until_d)

    stmt = (
        select(
            CmsMedia.id_media,
            CmsMedia.isrc,
            CmsMedia.media_name,
            CmsMedia.rights_metadata,
            func.count(CmsProofOfPlay.id_pop).label("plays"),
            func.coalesce(func.sum(CmsProofOfPlay.duration_ms), 0).label("total_ms"),
            func.count(distinct(CmsProofOfPlay.id_player)).label("players"),
            func.count(distinct(CmsPlayer.id_site)).label("sites"),
        )
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media)
        .join(CmsPlayer, CmsPlayer.id_player == CmsProofOfPlay.id_player, isouter=True)
        .where(
            CmsProofOfPlay.id_org == id_org,
            CmsProofOfPlay.played_at >= start_utc,
            CmsProofOfPlay.played_at < end_utc,
            CmsMedia.media_type == "audio",
        )
        .group_by(CmsMedia.id_media, CmsMedia.isrc, CmsMedia.media_name, CmsMedia.rights_metadata)
        .order_by(func.count(CmsProofOfPlay.id_pop).desc())
    )
    if rights_org:
        stmt = stmt.where(CmsMedia.rights_metadata["rights_org"].astext == rights_org)

    rows = (await db.execute(stmt)).all()
    result: list[RightsReportRow] = []
    for r in rows:
        meta = r.rights_metadata or {}
        result.append(
            RightsReportRow(
                id_media=r.id_media,
                isrc=r.isrc,
                title=meta.get("title") or r.media_name,
                composer=meta.get("composer"),
                publisher=meta.get("publisher"),
                rights_org=meta.get("rights_org"),
                rights_cleared=bool(meta.get("rights_cleared", False)),
                plays=r.plays,
                total_sec=round((r.total_ms or 0) / 1000),
                players=r.players,
                sites=r.sites,
            )
        )
    return result


@router.get("/rights")
async def rights_report(
    current_user: Annotated[CmsUser, Depends(require_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date = Query(..., alias="from"),
    until: date = Query(...),
    rights_org: Optional[str] = Query(None),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """Rapport d'utilisation des œuvres audio (déclaration SACEM/SOCAN) sur [from, until]."""
    rows = await _rights_rows(db, current_user.id_org, from_, until, rights_org)

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        loc = current_user.locale
        writer.writerow(tr(DOC_TEXTS, loc, "csv_header").split(","))
        yes, no = tr(DOC_TEXTS, loc, "yes"), tr(DOC_TEXTS, loc, "no")
        for row in rows:
            writer.writerow([
                _csv_safe(row.isrc), _csv_safe(row.title), _csv_safe(row.composer),
                _csv_safe(row.publisher), _csv_safe(row.rights_org),
                row.plays, row.total_sec, row.players, row.sites,
                yes if row.rights_cleared else no,
            ])
        filename = f"rights_{from_.isoformat()}_{until.isoformat()}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return RightsReportResponse(
        from_date=from_,
        until_date=until,
        rights_org=rights_org,
        total_plays=sum(r.plays for r in rows),
        total_sec=sum(r.total_sec for r in rows),
        rows=rows,
    )
