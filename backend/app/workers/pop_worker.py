"""
Proof of Play — maintenance des partitions + archivage/rotation (§XXIV, PLAN_POP_ARCHIVAL).

`cms_proof_of_play` est partitionnée par JOUR (migr 0060 ; auparavant mensuelle 0007). Deux
tâches beat quotidiennes :

- `ensure_pop_partitions` : crée les partitions JOUR à venir (aujourd'hui + N) AVANT que des
  données n'arrivent → aucune preuve ne tombe dans la partition DEFAULT.
- `archive_pop` : pour chaque partition JOUR plus vieille que la rétention chaude
  (`CMS_POP_HOT_RETENTION_DAYS`), exporte les preuves de chaque org vers MinIO (CSV.gz brut +
  PDF d'attestation), enregistre l'index permanent `cms_pop_archives` (merkle_root de la
  période), puis DROP la partition → disque rendu. Ordre strict export → INSERT (commit) →
  DROP : un crash au pire laisse une archive écrite mais partition non droppée, rejouée
  idempotemment (UNIQUE id_org/période) au run suivant.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone

import boto3
import psycopg2
from botocore.client import Config
from psycopg2.extras import Json

from app.core.config import settings
from app.services import pop_archive
from app.services.doc_i18n import tr
from app.services.pop_report import DOC_TEXTS as POP_TEXTS
from app.services.pop_report import build_pop_report_pdf
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _db_conn():
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=f"{'https' if settings.MINIO_USE_SSL else 'http'}://{settings.MINIO_ENDPOINT}",
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def _create_day_partition(conn, d: date) -> None:
    """Crée la partition du jour `d`, en rapatriant d'abord ce que la DEFAULT en détient.

    🔑 PostgreSQL REFUSE de créer une partition dont la DEFAULT contient déjà des lignes
    (« updated partition constraint for default partition would be violated by some row »,
    mesuré). Or dès qu'un jour a manqué sa partition, ses preuves sont tombées dans la
    DEFAULT — le simple fait d'avoir échoué une fois rend donc l'échec PERMANENT pour ce
    jour-là. On déplace les lignes avant de créer, puis on les réinsère : elles routent
    alors vers la nouvelle partition.

    Le tout dans UNE transaction (le DDL est transactionnel sous PostgreSQL) : soit le jour
    est réparé, soit rien n'a bougé. Aucune fenêtre où les preuves n'existent nulle part.
    """
    end = d + timedelta(days=1)
    tbl = pop_archive.day_partition_name(d)
    default_tbl = f"{pop_archive.PARTITION_PREFIX}default"
    with conn.cursor() as cur:
        moved = 0
        cur.execute("SELECT to_regclass(%s)", (default_tbl,))
        if cur.fetchone()[0] is not None:
            cur.execute(
                f"CREATE TEMP TABLE _pop_move ON COMMIT DROP AS "
                f"SELECT * FROM {default_tbl} WHERE played_at >= %s AND played_at < %s",
                (d, end),
            )
            moved = cur.rowcount or 0
            if moved:
                cur.execute(
                    f"DELETE FROM {default_tbl} WHERE played_at >= %s AND played_at < %s",
                    (d, end),
                )
        # Passe par la fonction SECURITY DEFINER de la migration 0127 : attacher une
        # partition modifie la table PARENTE, donc l'exige en propriété — ce que le rôle
        # worker n'a pas, et ne doit pas avoir. La fonction pose aussi le propriétaire de
        # la partition, sans quoi `archive_pop` ne pourrait jamais la DROP.
        cur.execute("SELECT cms_create_pop_partition(%s, %s, %s)", (tbl, d, end))
        if moved:
            cur.execute("INSERT INTO cms_proof_of_play SELECT * FROM _pop_move")
            logger.warning(
                "PoP : %d preuve(s) du %s récupérées de la partition DEFAULT vers %s",
                moved, d.isoformat(), tbl,
            )
    conn.commit()


@celery_app.task(name="app.workers.pop_worker.ensure_pop_partitions")
def ensure_pop_partitions(days_ahead: int = 7) -> dict:
    """Crée les partitions JOUR manquantes d'aujourd'hui + `days_ahead` jours.

    Chaque jour est traité INDÉPENDAMMENT. Auparavant la boucle était tout-ou-rien : un
    seul jour en échec emportait la création des sept suivants, qui étaient pourtant
    créables — c'est ce qui a transformé un incident de droits en panne permanente
    (mesuré en production : plus aucune partition depuis le 10/07, toutes les preuves
    accumulées dans la DEFAULT, donc rotation impossible).
    """
    created: list[str] = []
    failed: dict[str, str] = {}
    conn = _db_conn()
    try:
        today = date.today()
        # Jours PASSÉS déjà retombés dans la DEFAULT (un jour où la création a échoué). Sans
        # cette reprise, ils y resteraient définitivement : `archive_pop` ne connaît que les
        # partitions JOUR, et la DEFAULT n'est ni archivée ni droppable. On les traite d'abord,
        # dans l'ordre, avant les jours à venir.
        # ⚠️ Le jour est calculé en UTC, et les bornes des partitions sont des littéraux DATE
        # interprétés dans le fuseau de la SESSION. Les deux coïncident parce que le serveur
        # PostgreSQL est en `Etc/UTC` (vérifié en dev ET en prod), hypothèse que porte déjà la
        # migration 0060. Sur un serveur réglé autrement, les partitions couvriraient des
        # journées décalées : c'est le fuseau du SERVEUR qu'il faudrait corriger, pas ce calcul.
        stuck: list[date] = []
        default_tbl = f"{pop_archive.PARTITION_PREFIX}default"
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (default_tbl,))
            if cur.fetchone()[0] is not None:
                cur.execute(
                    f"SELECT DISTINCT (played_at AT TIME ZONE 'UTC')::date AS d "
                    f"FROM {default_tbl} ORDER BY d"
                )
                stuck = [r[0] for r in cur.fetchall()]
        conn.rollback()  # la lecture ci-dessus a ouvert une transaction

        upcoming = [today + timedelta(days=i) for i in range(days_ahead + 1)]
        for d in stuck + [d for d in upcoming if d not in stuck]:
            tbl = pop_archive.day_partition_name(d)
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (tbl,))
                if cur.fetchone()[0] is not None:
                    continue
            try:
                _create_day_partition(conn, d)
                created.append(tbl)
            except Exception as exc:
                conn.rollback()
                failed[d.isoformat()] = str(exc).strip().splitlines()[0]
    finally:
        conn.close()
    if created:
        logger.info("PoP : partitions jour créées %s", created)
    if failed:
        # Bruyant DÉLIBÉRÉMENT : sans partition pour le jour courant, les preuves tombent
        # dans la DEFAULT, qui n'est jamais archivée ni droppée. La table grossit alors sans
        # fin pendant que la tâche d'archivage, elle, continue de rendre « succès ».
        logger.error(
            "PoP : %d partition(s) jour NON créées %s — les preuves de ces jours tomberont "
            "dans la partition DEFAULT et échapperont à la rotation", len(failed), failed,
        )
    return {"created": created, "failed": failed}


# `screen_off` (migr 0102) et `covered` (migr 0125) suivent la ligne jusque dans le froid :
# une fois la partition DROPée, l'archive est la seule trace — sans ces colonnes, impossible
# de distinguer une diffusion réelle d'une lecture écran coupé ou masquée par un overlay
# plein écran dans les preuves de plus de N mois.
#
# 🔴 Ces colonnes étaient déclarées ICI mais ABSENTES du `SELECT` juste en dessous : deux
# listes, divergentes depuis l'ajout de `screen_off`. Elles n'en font plus qu'une, tenue par
# `pop_archive.ARCHIVE_COLUMNS`, elle-même dérivée de `pop_counting.NON_COUNTING_FLAGS`.


def _archive_day(conn, partition_regclass: str, day: date) -> dict:
    """Archive toutes les orgs d'un jour puis DROP la partition. Idempotent (skip les orgs
    déjà archivées). Ne DROP que si tout le jour a été traité sans erreur."""
    day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    period_end = day + timedelta(days=1)
    bucket = settings.MINIO_BUCKET_MEDIA
    prefix = settings.CMS_POP_ARCHIVE_PREFIX
    archived_orgs: list[int] = []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT id_org FROM cms_proof_of_play "
            "WHERE played_at >= %s AND played_at < %s",
            (day_start, day_end),
        )
        org_ids = [r[0] for r in cur.fetchall()]

    for id_org in org_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM cms_pop_archives "
                "WHERE id_org=%s AND period_start=%s AND period_end=%s",
                (id_org, day, period_end),
            )
            if cur.fetchone():
                continue  # déjà archivé (run précédent interrompu avant DROP)

            cur.execute(pop_archive.select_rows_sql(), (id_org, day_start, day_end))
            rows = [dict(zip(pop_archive.ARCHIVE_COLUMNS, r)) for r in cur.fetchall()]
            if not rows:
                continue

            cur.execute(
                "SELECT id_player, pop_public_key, player_name FROM cms_players WHERE id_org=%s",
                (id_org,),
            )
            keys, names = {}, {}
            for pid, key, name in cur.fetchall():
                keys[pid], names[pid] = key, name
            cur.execute("SELECT org_name FROM cms_organizations WHERE id_org=%s", (id_org,))
            org_row = cur.fetchone()
            org_name = (org_row[0] if org_row else None) or f"Org #{id_org}"

        players_report, global_root = pop_archive.aggregate(rows, keys, names)
        csv_gz = pop_archive.build_csv_gz(rows)
        csv_key = f"{prefix}/{id_org}/{day.isoformat()}.csv.gz"
        _s3().put_object(Bucket=bucket, Key=csv_key, Body=csv_gz, ContentType="application/gzip")

        report = {
            "org_name": org_name,
            "from_date": day.isoformat(),
            "until_date": day.isoformat(),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "total_plays": len(rows),
            "merkle_root": global_root,
            # Aucun utilisateur derrière l'archive journalière → langue par défaut (anglais).
            "scope_label": tr(POP_TEXTS, None, "scope_daily_archive"),
            "players": players_report,
        }
        pdf_key = f"{prefix}/{id_org}/{day.isoformat()}.pdf"
        try:
            pdf = build_pop_report_pdf(report)
            _s3().put_object(Bucket=bucket, Key=pdf_key, Body=pdf, ContentType="application/pdf")
        except Exception as exc:  # PDF regénérable depuis le CSV → non bloquant
            logger.warning("PoP archive : PDF %s échoué (%s) — CSV conservé", pdf_key, exc)
            pdf_key = None

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cms_pop_archives (id_org, period_start, period_end, row_count, "
                "merkle_root, csv_key, pdf_key, bytes_csv, players) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id_org, period_start, period_end) DO NOTHING",
                (id_org, day, period_end, len(rows), global_root, csv_key, pdf_key,
                 len(csv_gz), Json(players_report)),
            )
        conn.commit()
        archived_orgs.append(id_org)

    # Toutes les orgs du jour archivées (ou déjà présentes) → DROP la partition, disque rendu.
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {partition_regclass}")
    conn.commit()
    logger.info("PoP : jour %s archivé (%d org) + partition droppée", day, len(archived_orgs))
    return {"day": day.isoformat(), "orgs": archived_orgs, "dropped": True}


@celery_app.task(name="app.workers.pop_worker.archive_pop")
def archive_pop() -> dict:
    """Archive + DROP toutes les partitions JOUR plus vieilles que la rétention chaude."""
    cutoff = pop_archive.retention_cutoff(date.today(), settings.CMS_POP_HOT_RETENTION_DAYS)
    conn = _db_conn()
    processed: list[str] = []
    errors: list[str] = []
    stranded = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent = 'cms_proof_of_play'::regclass"
            )
            partitions = [r[0] for r in cur.fetchall()]

        candidates: list[tuple[str, date]] = []
        for regclass in partitions:
            bare = regclass.rsplit(".", 1)[-1].strip('"')
            d = pop_archive.parse_partition_day(bare)
            if d is not None and d < cutoff:
                candidates.append((regclass, d))

        for regclass, d in sorted(candidates, key=lambda x: x[1]):
            try:
                _archive_day(conn, regclass, d)
                processed.append(d.isoformat())
            except Exception as exc:
                conn.rollback()
                logger.exception("PoP archive : jour %s échoué", d)
                errors.append(f"{d.isoformat()}: {exc}")

        # Garde-fou : des preuves plus vieilles que la rétention encore dans la DEFAULT
        # signifient que la rotation NE PEUT PAS les sortir. Sans ce compte, la tâche
        # rendait `{'archived': [], 'errors': []}` — un succès indistinguable de « rien à
        # archiver », qui a laissé la panne invisible 28 jours en production.
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {pop_archive.PARTITION_PREFIX}default "
                "WHERE played_at < %s",
                (cutoff,),
            )
            stranded = int(cur.fetchone()[0] or 0)
        conn.rollback()
        if stranded:
            logger.error(
                "PoP archive : %d preuve(s) antérieures au %s sont dans la partition DEFAULT "
                "et échappent à la rotation — la partition du jour concerné n'a pas pu être "
                "créée (cf. ensure_pop_partitions)", stranded, cutoff.isoformat(),
            )
    finally:
        conn.close()
    return {"cutoff": cutoff.isoformat(), "archived": processed, "errors": errors,
            "stranded_in_default": stranded}
