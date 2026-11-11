from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession

from app.core.config import settings

# ── Session applicative (signflow_app) — SOUMISE à la RLS PostgreSQL ──────────
# Connexion via le rôle non-superuser signflow_app : la RLS (set_config
# app.current_org_id dans get_db) est réellement appliquée. NE PAS remettre
# settings.DATABASE_URL ici (= superuser signflow → bypasse toute la RLS).


def assert_app_url_is_isolated(app_url: str, database_url: str) -> None:
    """Refuse de démarrer si l'URL applicative dégénère en URL SUPERUSER.

    Garde-fou du chantier RLS (2026-07-30) : sans `DATABASE_APP_URL`, `db_app_url`
    dérive le rôle applicatif par un `.replace("signflow:signflow@", …)` qui ne matche
    QUE les identifiants de dev. Sur toute installation à mot de passe généré (= les
    installs CLIENT), le repli échouait **en silence** et renvoyait l'URL superuser
    telle quelle : le backend contournait alors TOUTES les politiques RLS, `FORCE`
    comprise — un superuser n'est jamais soumis à la RLS. Mesuré le 2026-07-30 : le
    compose client ne définissait pas `DATABASE_APP_URL` alors que le rôle et son mot
    de passe étaient provisionnés depuis toujours par postgres-init.sh.

    Même philosophie que la garde CMS_JWT_SECRET du pentest : mieux vaut arrêté que
    compromis, et l'erreur NOMME le geste qui répare.
    """
    if app_url == database_url:
        raise RuntimeError(
            "DATABASE_APP_URL missing: the application session would fall back to the "
            "SUPERUSER URL of DATABASE_URL and bypass all RLS isolation. "
            "Set DATABASE_APP_URL (role signflow_app — created by postgres-init.sh, "
            "password POSTGRES_APP_PASSWORD from .env) in the environment of the backend "
            "and the workers."
        )


def connections_per_process(
    pool_size: int, max_overflow: int, worker_pool_size: int, worker_max_overflow: int
) -> int:
    """Connexions PostgreSQL qu'UN processus backend peut ouvrir au maximum.

    🔑 Les DEUX pools comptent. Un processus backend ouvre l'applicatif (soumis RLS) ET
    celui du rôle BYPASSRLS : c'est 45 avec les valeurs par défaut, pas 30. Le cadrage de
    P3.12 annonçait « 4 workers = 120 connexions » en ne comptant que le premier — le
    vrai chiffre est 180, pour un budget réel de 97.
    """
    return pool_size + max_overflow + worker_pool_size + worker_max_overflow


def assert_pool_budget_fits(workers: int, per_process: int, budget: int) -> None:
    """Refuse de démarrer si les pools cumulés peuvent dépasser le budget de connexions.

    Le mode de défaillance qu'on évite est le pire qui soit : la configuration paraît
    saine, tout fonctionne au repos (le pool ne garde presque rien — mesuré en production,
    `signflow_app` tient **0** connexion au repos), et c'est **la rafale de reconnexions**
    — le moment précis où l'on avait augmenté le parallélisme pour tenir — qui épuise la
    réserve. PostgreSQL refuse alors les connexions : le serveur ne dégrade pas, il casse.

    La garde est déclarative et sans I/O : elle compare des réglages entre eux, elle
    n'interroge pas la base. Un `max_connections` relevé côté serveur se déclare ici.
    """
    demanded = workers * per_process
    if demanded > budget:
        raise RuntimeError(
            f"Connection budget exceeded: {workers} worker(s) × {per_process} "
            f"connections = {demanded}, for a budget of {budget}. "
            "Reduce WEB_CONCURRENCY, or lower CMS_DB_POOL_SIZE / CMS_DB_MAX_OVERFLOW / "
            "CMS_DB_WORKER_POOL_SIZE / CMS_DB_WORKER_MAX_OVERFLOW, or raise "
            "CMS_DB_CONNECTION_BUDGET after increasing max_connections on PostgreSQL "
            "(keeping headroom for the Celery workers and maintenance)."
        )


assert_app_url_is_isolated(settings.db_app_url, settings.DATABASE_URL)

# Tailles RÉELLEMENT appliquées : recalculées à partir du nombre de processus et du budget
# tant que l'exploitant n'a pas repris la main (cf. Settings.effective_pool_sizes). La garde
# reste appelée sur ces valeurs-là : elle n'est pas devenue décorative, elle attrape
# désormais les configurations EXPLICITES intenables — le seul cas qui reste possible.
_POOL_SIZE, _MAX_OVERFLOW, _WORKER_POOL_SIZE, _WORKER_MAX_OVERFLOW = (
    settings.effective_pool_sizes
)
assert_pool_budget_fits(
    settings.WEB_CONCURRENCY,
    connections_per_process(
        _POOL_SIZE, _MAX_OVERFLOW, _WORKER_POOL_SIZE, _WORKER_MAX_OVERFLOW
    ),
    settings.CMS_DB_CONNECTION_BUDGET,
)

engine = create_async_engine(
    settings.db_app_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


# ── Contexte RLS robuste : SET LOCAL à CHAQUE transaction ─────────────────────
# Le GUC `app.current_org_id` était posé une fois par get_db en `is_local=false`
# (niveau session) — mais ce mode PERSISTE sur la connexion poolée : une requête
# pouvait hériter d'un contexte vide laissé par une précédente (ex. un flux SSE qui
# garde sa session ouverte), cassant la policy RLS (`""::bigint`). Ici on (re)pose le
# contexte avec `SET LOCAL` (portée TRANSACTION → zéro fuite vers le pool) à chaque
# début de transaction. Comme le listener re-tire à chaque nouvelle transaction, le
# contexte SURVIT aux commits intermédiaires d'un endpoint (multi-commit) sans is_local.
# L'org cible est rangée dans `session.info['current_org_id']` par get_db ; les sessions
# worker (BYPASSRLS) ne la posent pas → no-op pour elles.
@event.listens_for(SyncSession, "after_begin")
def _apply_org_rls(session, transaction, connection) -> None:
    org_id = session.info.get("current_org_id")
    if org_id is None:
        return
    # int() garantit l'absence d'injection (org_id vient de nos données, jamais d'un input).
    connection.exec_driver_sql(f"SET LOCAL app.current_org_id = '{int(org_id)}'")

# ── Session worker (signflow_worker) — BYPASSRLS ──────────────────────────────
worker_engine = create_async_engine(
    settings.db_worker_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=_WORKER_POOL_SIZE,
    max_overflow=_WORKER_MAX_OVERFLOW,
)

WorkerSessionLocal = async_sessionmaker(
    worker_engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db_worker() -> AsyncSession:
    """Session worker BYPASSRLS — auth, lookups cross-tenant, Celery."""
    async with WorkerSessionLocal() as session:
        yield session
