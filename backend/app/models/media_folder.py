from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CmsMediaFolder(Base):
    """Dossier de classement des médias (arborescence, § retours clients).

    `path` matérialisé ("1.5.23", réutilise le pattern Gouvernance §XVIII) pour les
    requêtes de sous-arbre. `depth` = profondeur (racine=0). Complémentaire aux Tags.
    """

    __tablename__ = "cms_media_folders"

    id_folder: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_org: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cms_organizations.id_org"), nullable=False, index=True
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("cms_media_folders.id_folder", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    folder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, server_default="0", default=0, nullable=False)
    path: Mapped[str] = mapped_column(Text, server_default="", default="", nullable=False)
    # Dossier créé par le produit (migr 0131) : `advertisers` = racine des dossiers
    # d'annonceurs. Repéré par cette clé, jamais par son nom ; l'interface traduit le nom.
    system_key: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
