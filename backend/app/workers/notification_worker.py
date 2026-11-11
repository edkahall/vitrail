"""Réessai des webhooks de notification (P1.7, 2026-08-05).

Le premier envoi reste SYNCHRONE, dans le chemin qui déclenche l'alerte : c'est lui qui
alimente `cms_notification_log` et qui rend le bouton « Tester » utile. Ce worker ne prend
la main que sur un échec **transitoire** — destinataire injoignable, 5xx, 429, délai
dépassé — et rejoue l'envoi trois fois de plus, sur ~15 minutes.

⛔ On ne réessaie jamais un 4xx (hors 408/429) : une URL fausse, une authentification
refusée ou un format rejeté ne se répareront pas en renvoyant la même chose. Cette règle
vit dans `services/notifications._is_retryable`, partagée avec le premier essai — deux
définitions de « ça vaut le coup de réessayer » finiraient par diverger.

CHAQUE TENTATIVE ÉCRIT SA PROPRE LIGNE DE JOURNAL, jamais une mise à jour de la
précédente. C'est cohérent avec la nature de ce journal (on y ajoute, on n'y réécrit pas)
et c'est plus utile en exploitation : « échoué à 14 h 02, réussi à 14 h 07 après 3
tentatives » raconte l'incident, là où une ligne mise à jour l'effacerait.
"""

from __future__ import annotations

import logging

import psycopg2

from app.core.config import settings
from app.services.notifications import RETRY_BACKOFF_S, webhook_post
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = len(RETRY_BACKOFF_S) + 1  # 1 essai synchrone + 4 différés


def _db_conn():
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def _log(id_org: int, id_rule: int | None, event_type: str, status: str, detail: str) -> None:
    """Trace best-effort : une panne de journalisation ne doit pas empêcher le réessai
    suivant de partir."""
    try:
        conn = _db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO cms_notification_log (id_org, id_rule, event_type, subject, body, "
                "status, detail, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s, now())",
                (id_org, id_rule, event_type, None, None, status, detail[:500]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — cf. docstring
        logger.warning("notification_worker: journalisation du reessai impossible", exc_info=True)


@celery_app.task(name="app.workers.notification_worker.retry_webhook")
def retry_webhook(url: str, payload: dict, secret: str, event_type: str,
                  id_org: int, id_rule: int | None, attempt: int) -> dict:
    """Rejoue un envoi webhook. `attempt` est le numéro de CETTE tentative (2 = premier
    réessai), il part dans l'en-tête `X-SignFlow-Attempt` pour que le destinataire puisse
    dédupliquer — un réessai n'est pas un nouvel événement."""
    ok, detail, retryable = webhook_post(
        url, payload, secret=secret, event_type=event_type, attempt=attempt)

    if ok:
        _log(id_org, id_rule, event_type, "sent", f"{detail} (attempt {attempt})")
        return {"ok": True, "attempt": attempt}

    if not retryable or attempt >= MAX_ATTEMPTS:
        reason = "gave up" if not retryable else "attempts exhausted"
        _log(id_org, id_rule, event_type, "failed",
             f"{detail} (attempt {attempt}/{MAX_ATTEMPTS}, {reason})")
        return {"ok": False, "attempt": attempt, "retryable": retryable}

    # `attempt` vaut 2 pour le premier réessai → l'attente suivante est à l'indice
    # attempt-1 dans la table des délais.
    countdown = RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)]
    _log(id_org, id_rule, event_type, "failed",
         f"{detail} (attempt {attempt}/{MAX_ATTEMPTS}, retrying in {countdown} s)")
    celery_app.send_task(
        "app.workers.notification_worker.retry_webhook",
        args=[url, payload, secret, event_type, id_org, id_rule, attempt + 1],
        countdown=countdown,
    )
    return {"ok": False, "attempt": attempt, "requeued": True}
