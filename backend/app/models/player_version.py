from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CmsPlayerUpdateVersion(Base):
    """Version de mise à jour disponible (§XXX) — structure Phase 1, logique Phase 2."""

    __tablename__ = "cms_player_update_versions"

    id_version: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # server | player | engine | firmware | os
    track: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    # stable | beta | lts
    channel: Mapped[str] = mapped_column(String(20), server_default="stable", nullable=False)
    release_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    release_notes_fr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    download_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sha256_checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    gpg_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    min_server_ver: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    eol_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # P1.1 — firmware BrightSign (track='firmware'). is_pinned = la SEULE version que l'UI pousse
    # (garde-fou anti-brick) ; un seul pinned par model_family (assuré côté service). storage_key =
    # objet MinIO du .bsfw. model_family = famille BrightSign cible (capabilities.bs_family), ex.
    # 'malibu' (XT4) → on ne propose que la version épinglée correspondant à la famille du player.
    is_pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False, nullable=False)
    storage_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_family: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Décision 2 du « canal unique » (2026-07-27) : un renderer BrightSign peut exiger un BOS
    # minimum. NULL = aucune exigence. Consommé par la garde de `ota.resolve_renderer_version`
    # (jamais servir un renderer à un device sous le minimum) et par l'orchestration de
    # `ota.create_deployment` (firmware épinglé poussé D'ABORD, ou signal à l'admin).
    min_firmware_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Mises à jour OS contrôlées (vitrail#6, track='os') : le MANIFESTE relevé sur le banc —
    # {"packages": [{"name","arch","version"}…], "source_player", "reboot_required", "requested_by"}.
    # Le parc installe exactement ces paquets et rien d'autre. NULL pour les autres pistes.
    manifest: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        # Unicité par (track, version, model_family) : une même version OS (ex. 9.1.132) existe pour
        # PLUSIEURS familles BrightSign (malibu/XT4, pagani/HD4...) = des binaires .bsfw distincts.
        # NULLS NOT DISTINCT (PG15+) préserve l'unicité (track, version) pour les pistes sans famille
        # (server/player, model_family NULL) = deux NULL comptent comme égaux.
        UniqueConstraint(
            "track", "version", "model_family",
            name="uq_cms_player_update_versions_track_version_family",
            postgresql_nulls_not_distinct=True,
        ),
    )


class CmsUpdateDeployment(Base):
    """Déploiement approuvé d'une version sur une organisation (§XXX)."""

    __tablename__ = "cms_update_deployments"

    id_deployment: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_org: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cms_organizations.id_org"), nullable=False
    )
    id_version: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cms_player_update_versions.id_version"),
        nullable=False,
    )
    approved_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cms_users.id_user"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(nullable=False)
    # immediate | gradual | scheduled
    strategy: Mapped[str] = mapped_column(
        String(20), server_default="immediate", nullable=False
    )
    strategy_params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"), nullable=False)
    # pending | in_progress | paused | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), server_default="pending", nullable=False)
    total_targets: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)


class CmsUpdateDeploymentTarget(Base):
    """Statut par player d'un déploiement (§XXX)."""

    __tablename__ = "cms_update_deployment_targets"

    id_target: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_deployment: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cms_update_deployments.id_deployment"),
        nullable=False,
    )
    id_player: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cms_players.id_player"), nullable=False
    )
    # pending | queued | downloading | installing | success | failed | rolled_back
    status: Mapped[str] = mapped_column(String(20), server_default="pending", nullable=False)
    previous_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
