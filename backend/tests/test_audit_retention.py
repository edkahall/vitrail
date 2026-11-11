"""Rétention du journal d'audit (P3.5) — archive, purge, effacement RGPD.

Le journal est en ajout seul depuis la migration 0121. Ces tests exercent le chemin réel :
une connexion PROPRIÉTAIRE sur la base de test, les vraies requêtes du service.

Ce qu'ils défendent, dans l'ordre d'importance :
  · une purge ne peut pas supprimer plus que ce qu'une archive relue contient ;
  · une rétention absurdement courte est REFUSÉE, pas exécutée ;
  · l'effacement RGPD conserve l'action et retire la personne ;
  · un export du journal ne peut pas devenir une formule chez qui l'ouvre.
"""

import io
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from app.services import audit_retention
from app.services.audit_retention import RetentionRefused

TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://signflow:signflow@postgres:5432/signflow_test",
).replace("+asyncpg", "")

ACTION = "retention_probe"


@pytest.fixture
def conn(engine):
    """Connexion propriétaire sur la base de TEST (`engine` garantit le schéma migré)."""
    c = psycopg2.connect(TEST_DSN)
    _clear(c)
    yield c
    _clear(c)
    c.close()


def _clear(c) -> None:
    # Nettoyage par `resource_type`, PAS par `action` : le test d'injection CSV insère une
    # action bien à lui, qui survivait au nettoyage et faussait les tests de purge suivants
    # (« 1 entrée à supprimer, 0 archivée » sur une base censée être vide).
    c.rollback()
    with c.cursor() as cur:
        cur.execute("DELETE FROM cms_audit_log WHERE resource_type IN ('probe', 'audit')")
    c.commit()


def _add(c, *, days_ago: int, id_user=None, action: str = ACTION) -> None:
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO cms_audit_log (id_org, id_user, action, resource_type, created_at) "
            "VALUES (NULL, %s, %s, 'probe', %s)",
            (id_user, action, at),
        )
    c.commit()


# ── la borne ─────────────────────────────────────────────────────────────────

def test_retention_below_the_floor_is_refused():
    """Le garde-fou qui compte le plus : `--months 3` au lieu de `--months 30` est une
    faute de frappe irrattrapable une fois exécutée."""
    with pytest.raises(RetentionRefused, match="the minimum is"):
        audit_retention.cutoff(audit_retention.MIN_RETENTION_MONTHS - 1)
    # la borne juste au plancher est acceptée
    assert audit_retention.cutoff(audit_retention.MIN_RETENTION_MONTHS) is not None


def test_cutoff_counts_backwards_from_now():
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    assert audit_retention.cutoff(6, now=now) == now - timedelta(days=180)


# ── archive ──────────────────────────────────────────────────────────────────

async def test_survey_counts_only_what_is_past_the_cutoff(conn):
    _add(conn, days_ago=400)
    _add(conn, days_ago=1)
    at = audit_retention.cutoff(6)
    plan = audit_retention.survey(conn, at)
    assert plan.rows == 1, "seule l'entrée au-delà de la rétention est concernée"
    assert "1 entry" in plan.describe()


async def test_archive_writes_every_due_entry(conn):
    for d in (400, 401, 402):
        _add(conn, days_ago=d)
    at = audit_retention.cutoff(6)
    buf = io.StringIO()
    written = audit_retention.write_csv(buf, audit_retention.read_due(conn, at, batch=2))
    assert written == 3, "la pagination par clé ne doit oublier aucune entrée"
    assert audit_retention.count_archive(buf.getvalue()) == 3


async def test_export_neutralises_spreadsheet_formulas(conn):
    """Une action contrôlée par un utilisateur ne doit pas s'exécuter chez qui ouvre l'export."""
    _add(conn, days_ago=400, action="=HYPERLINK(0)")
    at = audit_retention.cutoff(6)
    buf = io.StringIO()
    audit_retention.write_csv(buf, audit_retention.read_due(conn, at))
    assert "'=HYPERLINK(0)" in buf.getvalue()
    assert "\n=HYPERLINK" not in buf.getvalue()


# ── purge ────────────────────────────────────────────────────────────────────

async def test_purge_refuses_when_the_archive_holds_less_than_it_deletes(conn):
    """Le cœur du dispositif : une archive interrompue à mi-chemin ne doit PAS autoriser
    la suppression de ce qu'elle n'a pas sauvé."""
    for d in (400, 401, 402):
        _add(conn, days_ago=d)
    at = audit_retention.cutoff(6)
    with pytest.raises(RetentionRefused, match="but only 2"):
        audit_retention.purge(conn, at, archived=2)
    conn.rollback()
    assert audit_retention.survey(conn, at).rows == 3, "rien ne doit avoir été supprimé"


async def test_purge_removes_exactly_what_was_archived(conn):
    for d in (400, 401):
        _add(conn, days_ago=d)
    _add(conn, days_ago=2)  # récente : hors périmètre, doit survivre
    at = audit_retention.cutoff(6)

    removed = audit_retention.purge(conn, at, archived=2)
    conn.commit()

    assert removed == 2
    assert audit_retention.survey(conn, at).rows == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cms_audit_log WHERE action = %s", (ACTION,))
        assert cur.fetchone()[0] == 1, "l'entrée récente a été emportée"


async def test_purge_of_nothing_is_not_an_error(conn):
    assert audit_retention.purge(conn, audit_retention.cutoff(6), archived=0) == 0


# ── effacement RGPD ──────────────────────────────────────────────────────────

async def test_erasure_keeps_the_action_and_drops_the_person(conn):
    """Supprimer les lignes emporterait des autorisations et des dépenses dont d'autres
    enregistrements dépendent. La ligne reste, la personne s'efface."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cms_organizations (org_name, org_slug) "
            "VALUES ('RGPD probe', 'rgpd-probe') RETURNING id_org"
        )
        id_org = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO cms_users (id_org, email, password_hash, full_name, is_active) "
            "VALUES (%s, 'rgpd-probe@example.test', 'x', 'Probe', true) RETURNING id_user",
            (id_org,),
        )
        id_user = cur.fetchone()[0]
    conn.commit()
    try:
        _add(conn, days_ago=10, id_user=id_user)
        _add(conn, days_ago=11, id_user=id_user)

        touched = audit_retention.pseudonymise(conn, id_user=id_user)
        conn.commit()

        assert touched == 2
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM cms_audit_log WHERE action = %s", (ACTION,))
            assert cur.fetchone()[0] == 2, "les actions doivent rester attestées"
            cur.execute("SELECT count(*) FROM cms_audit_log WHERE id_user = %s", (id_user,))
            assert cur.fetchone()[0] == 0, "la personne ne doit plus être identifiable"
            cur.execute(
                "SELECT count(*) FROM cms_audit_log WHERE detail->>'subject' = %s",
                (audit_retention.ANONYMISED,),
            )
            assert cur.fetchone()[0] == 2, (
                "le marqueur distingue un effacement demandé d'un compte simplement supprimé"
            )
            # Le compte lui-même n'est pas touché : le supprimer est une décision distincte.
            cur.execute("SELECT count(*) FROM cms_users WHERE id_user = %s", (id_user,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cms_audit_log WHERE id_user = %s", (id_user,))
            cur.execute("DELETE FROM cms_users WHERE id_user = %s", (id_user,))
            cur.execute("DELETE FROM cms_organizations WHERE id_org = %s", (id_org,))
        conn.commit()


# ── traçabilité de la maintenance ────────────────────────────────────────────

async def test_maintenance_leaves_its_own_trace(conn):
    """Un journal en ajout seul dont la maintenance est invisible ne vaut pas mieux qu'un
    journal qu'on peut éditer."""
    audit_retention.trace(conn, "audit_purged", {"rows": 7})
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, detail->>'rows' FROM cms_audit_log "
            "WHERE action = 'audit_purged' ORDER BY id_audit DESC LIMIT 1"
        )
        source, rows = cur.fetchone()
    assert source == "maintenance"
    assert rows == "7"
