"""Worker OTA (§XXX, slice 2) — rollup des déploiements + timeout des cibles bloquées.

Le noyau WS écrit les transitions de cible (async, simples UPDATE) et enqueue `ota_rollup` : ce
worker (psycopg2, comme event/feed) recalcule le statut agrégé d'un déploiement à partir de ses
cibles et, à la TRANSITION vers `failed`, émet l'alerte `deployment_failed` (edge-triggered, comme
`player_offline`/`feed_error`). `ota_timeout` (beat) échoue les cibles restées `downloading`/
`installing` trop longtemps (player qui a démarré l'apply mais ne confirme jamais — brick, coupure).
Les cibles `pending`/`queued` (jamais démarrées : player offline / pas encore de handler) ne
timeoutent PAS — elles s'appliqueront au retour du player, sans faux échec.
"""

from __future__ import annotations

import logging

import psycopg2

from app.core.config import settings
from app.services import notifications
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Une cible qui a DÉMARRÉ l'apply (downloading/installing) mais ne confirme pas dans ce délai est
# déclarée en échec (le déploiement peut alors se terminer et alerter). Pending/queued = épargnés.
_TARGET_TIMEOUT_S = 30 * 60

_TERMINAL = ("success", "failed", "cancelled", "rolled_back")


def _db_conn():
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def _rollup(conn, id_deployment: int) -> None:
    """Recalcule le statut d'un déploiement depuis ses cibles + émet `deployment_failed` à la
    transition vers `failed`. Verrouille la ligne déploiement (FOR UPDATE) → idempotent sous
    concurrence (plusieurs cibles peuvent enqueue un rollup en même temps)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id_org, status, strategy_params FROM cms_update_deployments "
        "WHERE id_deployment = %s FOR UPDATE",
        (id_deployment,),
    )
    dep = cur.fetchone()
    if not dep:
        conn.rollback()
        return
    id_org, old_status, strategy_params = dep
    stop_on_failure = bool((strategy_params or {}).get("stop_on_failure"))
    # Échecs déjà vus et acceptés par l'exploitant à la reprise (/resume) : on ne s'arrête
    # que sur un échec NOUVEAU, sinon la reprise serait aussitôt annulée par ce même rollup.
    try:
        acknowledged = int((strategy_params or {}).get("acknowledged_failures") or 0)
    except (TypeError, ValueError):
        acknowledged = 0
    if old_status == "cancelled":  # un déploiement annulé ne se recalcule pas
        conn.rollback()
        return

    cur.execute(
        "SELECT status, count(*) FROM cms_update_deployment_targets "
        "WHERE id_deployment = %s GROUP BY status",
        (id_deployment,),
    )
    counts = {row[0]: row[1] for row in cur.fetchall()}

    # Suspendu : on rafraîchit les compteurs (des retardataires peuvent finir) mais on NE touche
    # PAS au statut — seule la reprise (endpoint /resume) recalcule et sort de `paused`.
    if old_status == "paused":
        cur.execute(
            "UPDATE cms_update_deployments SET success_count = %s, failed_count = %s "
            "WHERE id_deployment = %s",
            (counts.get("success", 0),
             counts.get("failed", 0) + counts.get("rolled_back", 0), id_deployment),
        )
        conn.commit()
        return
    total = sum(counts.values())
    success = counts.get("success", 0)
    failed = counts.get("failed", 0) + counts.get("rolled_back", 0)
    terminal = sum(v for k, v in counts.items() if k in _TERMINAL)
    non_pending = sum(v for k, v in counts.items() if k != "pending")

    if total == 0:
        new_status = "completed"
    elif terminal >= total:
        new_status = "failed" if failed > 0 else "completed"
    elif stop_on_failure and failed > acknowledged:
        # Arrêt au PREMIER échec (moteur, vitrail#5 / D3) : le déploiement se suspend — plus
        # aucune directive n'est émise pour les cibles restantes (directive_for_player ne sert
        # que pending/in_progress). L'exploitant regarde, puis reprend (/resume) ou annule.
        new_status = "paused"
    elif non_pending > 0:
        new_status = "in_progress"
    else:
        new_status = "pending"

    set_started = new_status == "in_progress" and old_status == "pending"
    set_completed = new_status in ("completed", "failed")
    paused_on_failure = new_status == "paused"
    cur.execute(
        "UPDATE cms_update_deployments SET status = %s, success_count = %s, failed_count = %s, "
        "started_at = COALESCE(started_at, CASE WHEN %s THEN now() END), "
        "completed_at = CASE WHEN %s THEN now() ELSE completed_at END "
        "WHERE id_deployment = %s",
        (new_status, success, failed, set_started, set_completed, id_deployment),
    )
    conn.commit()

    # Alerte à la TRANSITION vers `failed` (§XXII) — une seule fois (old_status != 'failed').
    # Une suspension sur échec alerte aussi : c'est elle qui demande un humain.
    if (new_status == "failed" and old_status != "failed") or paused_on_failure:
        try:
            notifications.dispatch_sync(conn, id_org, "deployment_failed",
                                        {"id_deployment": id_deployment, "failed_count": failed})
        except Exception:  # noqa: BLE001 — best-effort, un webhook KO ne casse pas le rollup
            logger.exception("dispatch deployment_failed failed (deployment %s)", id_deployment)


@celery_app.task(name="app.workers.ota_worker.ota_rollup")
def ota_rollup(id_deployment: int) -> dict:
    """Recalcule un déploiement (enqueue par le noyau WS à chaque transition de cible)."""
    conn = _db_conn()
    try:
        _rollup(conn, id_deployment)
    finally:
        conn.close()
    return {"id_deployment": id_deployment}


@celery_app.task(name="app.workers.ota_worker.ota_timeout")
def ota_timeout() -> dict:
    """Échoue les cibles restées `downloading`/`installing` au-delà du délai, puis rollup les
    déploiements concernés (qui peuvent alors se terminer + alerter)."""
    conn = _db_conn()
    dep_ids: set[int] = set()
    try:
        cur = conn.cursor()
        # Slice 2c : seul `installing` timeoute (phase d'apply, rapide). `downloading`/`staged`
        # peuvent durer (fenêtre de download windowée, attente de la fenêtre de maintenance) → jamais
        # de faux échec sur une cible qui attend légitimement sa fenêtre.
        cur.execute(
            "UPDATE cms_update_deployment_targets "
            "SET status = 'failed', error_message = COALESCE(error_message, 'timeout : install non "
            "confirmée'), completed_at = now() "
            "WHERE status = 'installing' AND started_at IS NOT NULL "
            "AND started_at < now() - make_interval(secs => %s) "
            "RETURNING id_deployment",
            (_TARGET_TIMEOUT_S,),
        )
        dep_ids = {row[0] for row in cur.fetchall()}
        conn.commit()
        for id_deployment in dep_ids:
            _rollup(conn, id_deployment)
    finally:
        conn.close()
    return {"timed_out_deployments": len(dep_ids)}
