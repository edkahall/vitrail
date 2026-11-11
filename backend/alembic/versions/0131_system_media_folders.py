"""Dossiers système de la médiathèque : le dossier des annonceurs repéré par une CLÉ

Revision ID: 0131
Revises: 0130
Create Date: 2026-11-11 00:00:00.000000

Le dossier racine des annonceurs (migr 0075) était retrouvé PAR SON NOM, « Annonceurs » :
une interface anglaise affichait donc un nom français, et le traduire côté serveur aurait
fait naître un second dossier dans chaque organisation qui avait déjà le premier.

`system_key` le repère désormais ; le nom affiché est traduit par l'interface. Reprise :
dans chaque org, la racine « Annonceurs » qui porte des dossiers d'annonceurs (à défaut la
plus ancienne) reçoit la clé `advertisers` et le nom de repli anglais « Advertisers ».
Une seule racine par clé et par org (index unique partiel).
"""

import sqlalchemy as sa
from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cms_media_folders", sa.Column("system_key", sa.String(32), nullable=True))
    op.execute(
        """
        WITH ranked AS (
            SELECT f.id_folder,
                   row_number() OVER (
                       PARTITION BY f.id_org
                       ORDER BY (EXISTS (
                           SELECT 1 FROM cms_media_folders c
                             JOIN cms_advertisers a ON a.id_folder = c.id_folder
                            WHERE c.parent_id = f.id_folder)) DESC,
                                f.id_folder
                   ) AS rn
              FROM cms_media_folders f
             WHERE f.parent_id IS NULL AND f.folder_name = 'Annonceurs'
        )
        UPDATE cms_media_folders m
           SET system_key = 'advertisers', folder_name = 'Advertisers'
          FROM ranked r
         WHERE m.id_folder = r.id_folder AND r.rn = 1
        """
    )
    op.create_index(
        "uq_cms_media_folders_system_key", "cms_media_folders", ["id_org", "system_key"],
        unique=True, postgresql_where=sa.text("system_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE cms_media_folders SET folder_name = 'Annonceurs' WHERE system_key = 'advertisers'"
    )
    op.drop_index("uq_cms_media_folders_system_key", table_name="cms_media_folders")
    op.drop_column("cms_media_folders", "system_key")
