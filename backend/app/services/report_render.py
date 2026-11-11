"""Rendu du rapport exécutif PDF (§24.3) — partagé par l'endpoint et le planificateur.

Assemble : payload dashboard (analytics) + branding white-label + ROI + logo MinIO → PDF reportlab.
Fonctions pures pour le calcul du prochain envoi d'une planification.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.organization import CmsOrganization
from app.models.report import CmsReportBranding
from app.models.user import CmsUser
from app.services import analytics, media, report_pdf
from app.services.doc_i18n import tr

# E-mail d'envoi du rapport + nom du fichier joint/téléchargé. Le PDF lui-même porte ses
# textes dans report_pdf.DOC_TEXTS. Langue = profil du destinataire du document (doc_i18n).
DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "email_subject": "{org} — Executive report ({from_d} → {until_d})",
        "email_body": (
            "Hello,\n\nPlease find attached the Vitrail executive report for {org} "
            "for the period from {from_d} to {until_d}.\n\n"
            "This report is generated automatically.\n\n— Vitrail"),
        "email_body_test": (
            "Vitrail executive report for {org} "
            "({from_d} → {until_d}).\n\n— Vitrail"),
        "pdf_filename": "executive_report_{from_d}_{until_d}.pdf",
    },
    "fr": {
        "email_subject": "{org} — Rapport exécutif ({from_d} → {until_d})",
        "email_body": (
            "Bonjour,\n\nVeuillez trouver ci-joint le rapport exécutif Vitrail de "
            "{org} pour la période du {from_d} au {until_d}.\n\n"
            "Ce rapport est généré automatiquement.\n\n— Vitrail"),
        "email_body_test": (
            "Rapport exécutif Vitrail de {org} "
            "({from_d} → {until_d}).\n\n— Vitrail"),
        "pdf_filename": "rapport_executif_{from_d}_{until_d}.pdf",
    },
    "es": {
        "email_subject": "{org} — Informe ejecutivo ({from_d} → {until_d})",
        "email_body": (
            "Hola,\n\nAdjunto encontrará el informe ejecutivo Vitrail de {org} "
            "para el período del {from_d} al {until_d}.\n\n"
            "Este informe se genera automáticamente.\n\n— Vitrail"),
        "email_body_test": (
            "Informe ejecutivo Vitrail de {org} "
            "({from_d} → {until_d}).\n\n— Vitrail"),
        "pdf_filename": "informe_ejecutivo_{from_d}_{until_d}.pdf",
    },
    "pt": {
        "email_subject": "{org} — Relatório executivo ({from_d} → {until_d})",
        "email_body": (
            "Olá,\n\nSegue em anexo o relatório executivo Vitrail de {org} "
            "para o período de {from_d} a {until_d}.\n\n"
            "Este relatório é gerado automaticamente.\n\n— Vitrail"),
        "email_body_test": (
            "Relatório executivo Vitrail de {org} "
            "({from_d} → {until_d}).\n\n— Vitrail"),
        "pdf_filename": "relatorio_executivo_{from_d}_{until_d}.pdf",
    },
    "de": {
        "email_subject": "{org} — Management-Bericht ({from_d} → {until_d})",
        "email_body": (
            "Guten Tag,\n\nanbei erhalten Sie den Vitrail-Management-Bericht von {org} "
            "für den Zeitraum vom {from_d} bis {until_d}.\n\n"
            "Dieser Bericht wird automatisch erstellt.\n\n— Vitrail"),
        "email_body_test": (
            "Vitrail-Management-Bericht von {org} "
            "({from_d} → {until_d}).\n\n— Vitrail"),
        "pdf_filename": "management_bericht_{from_d}_{until_d}.pdf",
    },
}


def pdf_filename(from_d: date, until_d: date, locale: str | None) -> str:
    """Nom du fichier PDF (ASCII, dates ISO) dans la langue du document."""
    return tr(DOC_TEXTS, locale, "pdf_filename",
              from_d=from_d.isoformat(), until_d=until_d.isoformat())


def executive_email(org_name: str, from_d: date, until_d: date, locale: str | None,
                    *, test: bool = False) -> tuple[str, str, str]:
    """(sujet, corps, nom du fichier joint) de l'e-mail du rapport exécutif.
    `test=True` = envoi immédiat (« Envoyer maintenant ») : corps court."""
    fd = report_pdf.fmt_date(from_d, locale)
    ud = report_pdf.fmt_date(until_d, locale)
    subject = tr(DOC_TEXTS, locale, "email_subject", org=org_name, from_d=fd, until_d=ud)
    body = tr(DOC_TEXTS, locale, "email_body_test" if test else "email_body",
              org=org_name, from_d=fd, until_d=ud)
    return subject, body, pdf_filename(from_d, until_d, locale)


async def user_locale(db: AsyncSession, id_user: int | None) -> str | None:
    """Langue de profil d'un utilisateur (créateur d'une planification) — None si inconnu
    (supprimé, `created_by` NULL) → le document retombe sur l'anglais."""
    if id_user is None:
        return None
    return (await db.execute(
        select(CmsUser.locale).where(CmsUser.id_user == id_user)
    )).scalar_one_or_none()


def default_branding(id_org: int) -> CmsReportBranding:
    """Branding par défaut TRANSITOIRE (non persisté) — valeurs Python-side = server_default DB."""
    return CmsReportBranding(
        id_org=id_org, primary_color="#4875B4", roi_hours_per_day=12,
        roi_contacts_hour=50, roi_cpm_eur=5, roi_paper_eur=50)


async def branding_or_default(db: AsyncSession, id_org: int) -> CmsReportBranding:
    """Lecture PURE : ligne existante ou défaut transitoire (aucune écriture)."""
    b = (await db.execute(
        select(CmsReportBranding).where(CmsReportBranding.id_org == id_org)
    )).scalar_one_or_none()
    return b if b is not None else default_branding(id_org)


def roi_from_branding(b: CmsReportBranding, screens: int, uptime_pct: float | None,
                      days: int) -> dict:
    return analytics.roi_estimate(
        screens=screens, uptime_pct=uptime_pct, days=days,
        hours_per_day=float(b.roi_hours_per_day), contacts_per_hour=float(b.roi_contacts_hour),
        cpm_eur=float(b.roi_cpm_eur), paper_eur_per_month=float(b.roi_paper_eur))


def _fetch_logo_bytes(key: str) -> bytes:
    obj = media._s3().get_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=key)
    return obj["Body"].read()


async def render_executive_pdf(
    db: AsyncSession, id_org: int, from_d: date, until_d: date, tz: ZoneInfo,
    *, with_roi: bool = True, fetch_logo=None, locale: str | None = None,
) -> tuple[bytes, str]:
    """Construit le PDF exécutif → (bytes, nom_org). `fetch_logo` = callable(key)->bytes injecté
    (async endpoint : run_in_threadpool ; worker sync : appel direct). Défaut = lecture directe."""
    payload = await analytics.build_executive_payload(db, id_org, from_d, until_d, None, tz)
    b = await branding_or_default(db, id_org)
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )).scalar_one()

    roi = None
    if with_roi:
        days = (until_d - from_d).days + 1
        roi = roi_from_branding(b, payload["fleet"]["total"], payload["uptime_pct"], days)

    logo_bytes = None
    if b.logo_key and not b.logo_key.endswith(".svg"):
        try:
            logo_bytes = (fetch_logo or _fetch_logo_bytes)(b.logo_key)
            if hasattr(logo_bytes, "__await__"):
                logo_bytes = await logo_bytes
        except Exception:
            logo_bytes = None

    generated = report_pdf.fmt_datetime(datetime.now(tz), locale)
    pdf = report_pdf.build_executive_pdf(
        org_name=org.org_name, payload=payload,
        branding={"primary_color": b.primary_color, "report_title": b.report_title,
                  "report_subtitle": b.report_subtitle},
        roi=roi, logo_bytes=logo_bytes, generated_at=generated, locale=locale)
    return pdf, org.org_name


# ── Planification : calcul du prochain envoi (fonction pure) ──────────────────

def compute_next_run(
    *, frequency: str, send_hour: int, send_dow: int, send_dom: int,
    tz: ZoneInfo, after: datetime,
) -> datetime:
    """Prochain instant d'envoi (UTC) strictement APRÈS `after`, à `send_hour` locale.

    weekly : le jour de semaine `send_dow` (0=lundi). monthly : le jour `send_dom` du mois
    (borné à 28). Renvoie un datetime aware UTC.
    """
    local_after = after.astimezone(tz)
    # candidat = aujourd'hui à send_hour locale
    cand = local_after.replace(hour=int(send_hour), minute=0, second=0, microsecond=0)

    if frequency == "weekly":
        # avance jusqu'au bon jour de semaine, strictement après `after`
        for _ in range(0, 8):
            if cand.weekday() == int(send_dow) and cand > local_after:
                break
            cand = cand + timedelta(days=1)
            cand = cand.replace(hour=int(send_hour), minute=0, second=0, microsecond=0)
    else:  # monthly
        dom = max(1, min(28, int(send_dom)))
        cand = cand.replace(day=dom)
        if cand <= local_after:
            # mois suivant
            year = cand.year + (1 if cand.month == 12 else 0)
            month = 1 if cand.month == 12 else cand.month + 1
            cand = cand.replace(year=year, month=month, day=dom,
                                hour=int(send_hour), minute=0, second=0, microsecond=0)

    return cand.astimezone(UTC)
