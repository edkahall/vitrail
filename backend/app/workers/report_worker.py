"""Rapports planifiés (§24.3) — worker beat.

`run_due_report_schedules` (beat horaire) : pour chaque planification échue (enabled +
next_run_at <= now), génère le PDF exécutif, l'envoie par email aux destinataires, journalise
dans cms_report_history, et recalcule next_run_at. Engine async frais (rôle worker BYPASSRLS,
NullPool) créé par exécution — les agrégats filtrent par id_org explicite ; on pose quand même le
contexte org pour les tables RLS écrites.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.report import CmsReportHistory, CmsReportSchedule
from app.services import email_delivery, report_render
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


async def _deliver(db, sched: CmsReportSchedule, tz: ZoneInfo) -> str:
    """Génère + envoie un rapport pour une planification. Journalise + recalcule next_run.
    Renvoie le statut ('sent'|'failed')."""
    now = datetime.now(UTC)
    until = datetime.now(tz).date()
    from_d = until - timedelta(days=sched.period_days - 1)
    recipients = [r for r in (sched.recipients or []) if isinstance(r, str)]

    status, detail = "sent", None
    try:
        if not recipients:
            raise email_delivery.EmailError("no recipient configured")
        # Langue du document = profil du créateur de la planification (absent → anglais).
        locale = await report_render.user_locale(db, sched.created_by)
        pdf, org_name = await report_render.render_executive_pdf(
            db, sched.id_org, from_d, until, tz, with_roi=sched.include_roi, locale=locale)
        subject, body, fname = report_render.executive_email(org_name, from_d, until, locale)
        email_delivery.send_email(
            to=recipients, subject=subject, body=body,
            attachments=[(fname, pdf, "pdf")])
    except Exception as e:  # noqa: BLE001
        status, detail = "failed", str(e)[:500]
        logger.warning("Rapport planifié %s échoué : %s", sched.id_schedule, e)

    db.add(CmsReportHistory(
        id_org=sched.id_org, id_schedule=sched.id_schedule, report_type=sched.report_type,
        period_from=from_d, period_until=until, recipients=recipients,
        status=status, detail=detail))
    sched.last_run_at = now
    sched.next_run_at = report_render.compute_next_run(
        frequency=sched.frequency, send_hour=sched.send_hour, send_dow=sched.send_dow,
        send_dom=sched.send_dom, tz=tz, after=now)
    return status


async def _process_due() -> dict:
    tz = _tz()
    now = datetime.now(UTC)
    sent = failed = 0
    due: list = []
    # Engine FRAIS + NullPool : la tâche Celery appelle asyncio.run() à chaque exécution (nouvelle
    # boucle) → un engine async partagé (module-level) déclencherait « Future attached to a
    # different loop » sur asyncpg. On crée/dispose l'engine dans la boucle courante.
    engine = create_async_engine(settings.db_worker_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            due = (await db.execute(
                select(CmsReportSchedule).where(
                    CmsReportSchedule.enabled.is_(True),
                    CmsReportSchedule.next_run_at.isnot(None),
                    CmsReportSchedule.next_run_at <= now,
                )
            )).scalars().all()
            for sched in due:
                await db.execute(
                    text("SELECT set_config('app.current_org_id', :o, true)"),
                    {"o": str(sched.id_org)})
                status = await _deliver(db, sched, tz)
                await db.commit()
                sent += 1 if status == "sent" else 0
                failed += 1 if status == "failed" else 0
    finally:
        await engine.dispose()
    return {"processed": len(due), "sent": sent, "failed": failed}


@celery_app.task(name="app.workers.report_worker.run_due_report_schedules")
def run_due_report_schedules() -> dict:
    return asyncio.run(_process_due())
