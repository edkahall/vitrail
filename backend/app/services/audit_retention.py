"""Rétention du journal d'audit : ce qui peut être archivé, purgé, ou anonymisé (P3.5).

`cms_audit_log` est en AJOUT SEUL depuis la migration 0121, qui retire `UPDATE`, `DELETE`
et `TRUNCATE` à `signflow_app` ET `signflow_worker`. Rien ici ne revient là-dessus : ces
fonctions tournent sur une connexion ouverte en PROPRIÉTAIRE, depuis le script de
maintenance `backend/scripts/audit_retention.py`, jamais depuis l'application. Aucun
administrateur n'a de bouton qui efface l'histoire, et c'est le but — un journal que ses
lecteurs peuvent élaguer n'atteste rien.

⚠️ Corollaire de la 0121, et raison d'être de ce module : depuis cette migration, PLUS
AUCUN rôle applicatif ne peut retirer une ligne. Sans outil de maintenance, la table ne
peut que croître, et le droit à l'effacement n'a aucun chemin d'exécution.

Trois opérations, délibérément de natures différentes :

  archive       lit et écrit un fichier ; ne change RIEN en base
  purge         supprime des lignes déjà archivées, au-delà de la rétention
  pseudonymise  garde toutes les lignes et efface QUI a agi (effacement RGPD)

Seule `archive` est sûre isolément. `purge` refuse de s'exécuter sans une archive LISIBLE
des lignes qu'elle s'apprête à supprimer : une politique de rétention capable de perdre
une histoire qu'elle n'a jamais écrite n'est pas une politique, c'est une perte de données
à retardement.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.services.csv_safety import csv_safe

# Plancher de la fenêtre de rétention. Six mois, c'est déjà court pour un journal qui porte
# des autorisations de diffusion, des attributions de rôles et des dépenses ; en dessous,
# c'est presque toujours une faute de frappe (`--months 3` pour `--months 30`), et cette
# faute-là ne se rattrape pas.
MIN_RETENTION_MONTHS = 6

# Marqueur écrit à la place de la référence utilisateur par un effacement RGPD.
# Reconnaissable dans un export, et impossible à confondre avec un compte simplement
# supprimé — celui-ci laisse NULL, via le ON DELETE SET NULL du modèle.
ANONYMISED = "anonymised"

CSV_HEADER = [
    "id_audit", "created_at_utc", "id_org", "org_name", "id_user", "user_email",
    "action", "resource_type", "resource_id", "source", "detail",
]


class RetentionRefused(RuntimeError):
    """L'opération ferait perdre de l'histoire. Levée plutôt que signalée."""


def cutoff(months: int, now: datetime | None = None) -> datetime:
    """Instant le plus ancien conservé. Tout ce qui est strictement avant peut être archivé.

    Compté en jours pleins (30 par mois) depuis l'instant courant plutôt qu'en mois
    calendaires : une purge est un acte de maintenance, pas une période de facturation, et
    une borne approximative qui ne surprend jamais vaut mieux qu'une borne exacte qui se
    décale de trois jours selon le mois où on la lance.
    """
    if months < MIN_RETENTION_MONTHS:
        raise RetentionRefused(
            f"retention of {months} months refused: the minimum is "
            f"{MIN_RETENTION_MONTHS} months. A purged log cannot be rebuilt."
        )
    now = now or datetime.now(UTC)
    return now - timedelta(days=30 * months)


@dataclass
class Plan:
    """Ce qu'une opération ferait, avant qu'elle ne fasse quoi que ce soit."""

    cutoff: datetime
    rows: int
    oldest: datetime | None = None
    newest: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.rows:
            return f"No entries before {self.cutoff.date().isoformat()}."
        span = ""
        if self.oldest and self.newest:
            span = f" ({self.oldest.date().isoformat()} → {self.newest.date().isoformat()})"
        return (f"{self.rows} entry(ies) before "
                f"{self.cutoff.date().isoformat()}{span}.")


# ── Lecture de ce qui est dû ─────────────────────────────────────────────────

_SELECT = (
    "SELECT a.id_audit, a.created_at, a.id_org, o.org_name, a.id_user, u.email, "
    "       a.action, a.resource_type, a.resource_id, a.source, a.detail "
    "FROM cms_audit_log a "
    "LEFT JOIN cms_users u ON u.id_user = a.id_user "
    "LEFT JOIN cms_organizations o ON o.id_org = a.id_org "
    "WHERE a.created_at < %s "
)


def survey(conn, cutoff_at: datetime) -> Plan:
    """Combien d'histoire se trouve avant la borne, et jusqu'où elle remonte."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(created_at), max(created_at) "
            "FROM cms_audit_log WHERE created_at < %s",
            (cutoff_at,),
        )
        rows, oldest, newest = cur.fetchone()
    return Plan(cutoff=cutoff_at, rows=int(rows or 0), oldest=oldest, newest=newest)


def read_due(conn, cutoff_at: datetime, batch: int = 1_000):
    """Rend les entrées à archiver, de la plus ancienne à la plus récente.

    Ordre croissant volontairement, à l'inverse de l'écran : une archive se lit en avançant,
    et un fichier qui commence par la ligne la plus récente est illisible.

    Paginé par CLÉ plutôt que par curseur serveur : un curseur nommé exige une transaction
    ouverte, et `OFFSET` dériverait sous les lignes qui continuent de s'ajouter pendant que
    l'archive s'écrit — le journal reste en service pendant la maintenance.
    """
    after = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT + "AND a.id_audit > %s ORDER BY a.id_audit ASC LIMIT %s",
                (cutoff_at, after, batch),
            )
            rows = cur.fetchall()
        if not rows:
            return
        yield from rows
        after = rows[-1][0]
        if len(rows) < batch:
            return


def to_csv_row(row: tuple) -> list[str]:
    """Une ligne de journal en cellules de CSV, neutralisées contre l'injection de formule.

    `action`, `resource_id` et `detail` portent des valeurs qui viennent des utilisateurs :
    un export d'audit ouvert dans un tableur est exactement le contexte où une formule
    injectée s'exécute.
    """
    (id_audit, created_at, id_org, org_name, id_user, email,
     action, resource_type, resource_id, source, detail) = row
    return [
        str(id_audit),
        created_at.astimezone(UTC).isoformat(),
        "" if id_org is None else str(id_org),
        csv_safe(org_name),
        "" if id_user is None else str(id_user),
        csv_safe(email),
        csv_safe(action),
        csv_safe(resource_type),
        csv_safe(resource_id),
        csv_safe(source),
        csv_safe("" if detail is None else detail),
    ]


def write_csv(handle, rows) -> int:
    """Écrit l'en-tête puis les lignes. Rend le nombre d'entrées RÉELLEMENT écrites —
    c'est ce nombre, et pas celui qu'on espérait, qui autorisera ensuite la purge."""
    writer = csv.writer(handle)
    writer.writerow(CSV_HEADER)
    written = 0
    for row in rows:
        writer.writerow(to_csv_row(row))
        written += 1
    return written


def count_archive(text: str) -> int:
    """Entrées d'une archive, en-tête exclu. RELUE plutôt que crue sur parole : c'est ce
    compte qui autorise la suppression."""
    rows = sum(1 for _ in csv.reader(io.StringIO(text)))
    return max(0, rows - 1)


# ── Purge ────────────────────────────────────────────────────────────────────

def purge(conn, cutoff_at: datetime, *, archived: int) -> int:
    """Supprime ce qui a été archivé. Rend le nombre de lignes retirées.

    `archived` est le compte que l'étape d'archivage a RÉELLEMENT écrit. Il est comparé à
    ce qui va être supprimé au lieu d'être cru : une archive interrompue à mi-chemin
    autoriserait sinon la purge de tout ce qu'elle n'a pas sauvé.
    """
    plan = survey(conn, cutoff_at)
    if plan.rows == 0:
        return 0
    if archived < plan.rows:
        raise RetentionRefused(
            f"purge refused: {plan.rows} entry(ies) to delete but only "
            f"{archived} archived. Re-run the archiving before purging."
        )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM cms_audit_log WHERE created_at < %s", (cutoff_at,))
        return cur.rowcount


# ── Effacement RGPD ──────────────────────────────────────────────────────────

def pseudonymise(conn, *, id_user: int) -> int:
    """Efface QUI a agi, garde CE QUI a été fait. Rend le nombre de lignes touchées.

    Supprimer les lignes emporterait les actions avec elles — dont des autorisations de
    diffusion, des attributions de rôles et des dépenses dont les enregistrements d'autres
    personnes dépendent. Ce que le règlement demande, c'est que la personne ne soit plus
    identifiable ; ce que le journal sert à faire, c'est que l'action reste attestée. Les
    deux tiennent ici.

    La ligne du compte elle-même n'est PAS touchée : désactiver ou supprimer le compte est
    une décision distincte, prise sur l'écran d'administration (cf. P3.3).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cms_audit_log SET id_user = NULL, "
            "    detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object('subject', %s) "
            "WHERE id_user = %s",
            (ANONYMISED, id_user),
        )
        return cur.rowcount


def trace(conn, action: str, detail: dict) -> None:
    """Chaque opération de maintenance laisse SA propre ligne dans le journal.

    L'ajout reste toujours permis, et un journal en ajout seul dont la maintenance est
    invisible ne vaut pas mieux qu'un journal qu'on peut éditer.
    """
    from psycopg2.extras import Json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cms_audit_log (id_org, id_user, action, resource_type, source, detail) "
            "VALUES (NULL, NULL, %s, 'audit', 'maintenance', %s)",
            (action, Json(detail)),
        )
