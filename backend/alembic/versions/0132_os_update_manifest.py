"""Mises à jour OS contrôlées : le manifeste relevé sur le banc (vitrail#6)

Revision ID: 0132
Revises: 0131
Create Date: 2026-11-15 00:00:00.000000

Une version de la piste `os` (catalogue `cms_player_update_versions`) est un MANIFESTE : la liste
exacte des paquets (nom, architecture, version) qu'un player de banc a installés. Le parc installe
ces paquets-là et rien d'autre. La liste vit dans une colonne JSONB, NULL pour les autres pistes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cms_player_update_versions", sa.Column("manifest", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("cms_player_update_versions", "manifest")
