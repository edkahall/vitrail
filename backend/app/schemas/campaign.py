"""Schémas Retail Media §2.8 — annonceurs, campagnes, créatifs, cibles (migr 0074).

Vente v1 = SOV % + forfait (CPM indicatif). Les cibles reprennent le contrat des
schedules (target_type/target_id/tag_expr → services/targeting.py).
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Annonceurs ───────────────────────────────────────────────────────────────
class AdvertiserCreate(BaseModel):
    advertiser_name: str
    contact_name: str | None = None
    contact_email: str | None = None
    billing_info: dict = {}
    notes: str | None = None
    is_active: bool = True
    # Types de campagne autorisés au portail (migr 0095) — audio OFF par défaut
    allow_visual_campaigns: bool = True
    allow_audio_campaigns: bool = False
    # Multi-catégories (migr 0088) : une agence de pub porte plusieurs industries
    industry_categories: list[int] = []


class AdvertiserUpdate(BaseModel):
    advertiser_name: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    billing_info: dict | None = None
    notes: str | None = None
    is_active: bool | None = None
    allow_visual_campaigns: bool | None = None
    allow_audio_campaigns: bool | None = None
    # None = intouché ; liste = REMPLACE l'ensemble des catégories
    industry_categories: list[int] | None = None


class AdvertiserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_advertiser: int
    advertiser_name: str
    contact_name: str | None = None
    contact_email: str | None = None
    billing_info: dict = {}
    notes: str | None = None
    is_active: bool = True
    allow_visual_campaigns: bool = True
    allow_audio_campaigns: bool = False
    id_folder: int | None = None
    created_at: datetime
    # Agrégats peuplés par l'endpoint
    campaign_count: int = 0
    industry_categories: list[int] = []
    industry_category_names: list[str] = []


# ── Catégories d'industrie (exclusion concurrentielle, migr 0087) ────────────
class IndustryCategoryCreate(BaseModel):
    category_name: str = Field(min_length=1, max_length=100)


class IndustryCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_category: int
    category_name: str
    # Agrégats peuplés par l'endpoint (annonceurs de la catégorie / sites qui l'excluent)
    advertiser_count: int = 0
    excluded_site_count: int = 0


# ── Cibles (contrat canonique, même forme que les schedules) ─────────────────
class CampaignTargetSpec(BaseModel):
    target_type: Literal["org", "site", "site_group", "group", "player", "tags"]
    target_id: int | None = None
    tag_expr: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _check(self) -> CampaignTargetSpec:
        if self.target_type in ("site", "site_group", "group", "player") and self.target_id is None:
            raise ValueError(f"target_id required for target_type={self.target_type}")
        if self.target_type == "tags" and not self.tag_expr:
            raise ValueError("tag_expr required for target_type=tags")
        return self


class CampaignTargetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_target: int
    target_type: str
    target_id: int | None = None
    tag_expr: dict = {}
    # Nom résolu par le SERVEUR (audit menu ⑨) : la liste des campagnes nommait ses
    # cibles depuis les listes complètes chargées par la page — dont TOUT le parc
    # d'écrans, à l'ouverture (le défaut d'échelle relevé sur les Événements le 29/07).
    target_name: str | None = None


# ── Créatifs ─────────────────────────────────────────────────────────────────
class CreativeCreate(BaseModel):
    id_media: int
    duration_ms: int | None = Field(default=None, ge=1000, le=600_000)
    weight: int = Field(default=1, ge=1, le=100)
    sort_order: int = 0


class CreativeUpdate(BaseModel):
    duration_ms: int | None = Field(default=None, ge=1000, le=600_000)
    weight: int | None = Field(default=None, ge=1, le=100)
    sort_order: int | None = None
    status: Literal["active", "archived"] | None = None


class CreativeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_creative: int
    id_media: int
    duration_ms: int | None = None
    weight: int = 1
    sort_order: int = 0
    status: str = "active"
    # Approbation S5 (enforced si l'org a campaign_creative_approval)
    approval_status: str = "approved"
    reviewed_at: datetime | None = None
    # Métadonnées média (peuplées par l'endpoint)
    media_name: str | None = None
    media_type: str | None = None
    media_duration_ms: int | None = None
    thumbnail_url: str | None = None


class CreativeReview(BaseModel):
    """Motif optionnel de la revue (tracé dans l'audit log)."""
    note: str | None = Field(default=None, max_length=500)


# ── Campagnes ────────────────────────────────────────────────────────────────
class DailyHours(BaseModel):
    start: str  # "09:00"
    end: str    # "20:00"

    @model_validator(mode="after")
    def _check(self) -> DailyHours:
        import re
        for v in (self.start, self.end):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                raise ValueError("expected time format HH:MM")
        if self.end <= self.start:
            raise ValueError("end must be > start")
        return self


class CampaignCreate(BaseModel):
    id_advertiser: int
    campaign_name: str
    starts_on: date_cls
    ends_on: date_cls
    daily_hours: DailyHours | None = None
    sov_percent: float = Field(default=10.0, gt=0, le=100)
    max_plays_per_hour: int | None = Field(default=None, ge=1, le=120)
    # S2 audio (migr 0094) : 'audio' = campagne de SPOTS AUDIO (paniers §XIX) vendue en
    # spots/heure — sov_percent est ignoré. Immuable après création.
    campaign_kind: Literal["visual", "audio"] = "visual"
    spots_per_hour: int | None = Field(default=None, ge=1, le=60)
    pricing_model: Literal["sov", "flat"] = "sov"
    price_amount: float | None = Field(default=None, ge=0)
    price_currency: str = Field(default="EUR", min_length=3, max_length=3)
    cpm_rate: float | None = Field(default=None, ge=0)
    priority: int = Field(default=50, ge=1, le=100)
    targets: list[CampaignTargetSpec] = []

    @model_validator(mode="after")
    def _check(self) -> CampaignCreate:
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must be >= starts_on")
        if self.campaign_kind == "audio" and not self.spots_per_hour:
            raise ValueError("spots_per_hour is required for an audio campaign")
        return self


class CampaignUpdate(BaseModel):
    campaign_name: str | None = None
    id_advertiser: int | None = None
    starts_on: date_cls | None = None
    ends_on: date_cls | None = None
    daily_hours: DailyHours | None = None
    sov_percent: float | None = Field(default=None, gt=0, le=100)
    max_plays_per_hour: int | None = Field(default=None, ge=1, le=120)
    pricing_model: Literal["sov", "flat"] | None = None
    price_amount: float | None = Field(default=None, ge=0)
    price_currency: str | None = Field(default=None, min_length=3, max_length=3)
    cpm_rate: float | None = Field(default=None, ge=0)
    priority: int | None = Field(default=None, ge=1, le=100)
    overbook_override: bool | None = None
    # Audio : cadence vendue éditable ; le TYPE de campagne, lui, est immuable.
    spots_per_hour: int | None = Field(default=None, ge=1, le=60)
    targets: list[CampaignTargetSpec] | None = None


class CampaignApprovalInfo(BaseModel):
    """Demande d'approbation §XXI ACTIVE sur la campagne (C1) — badge UI + gate publish."""
    id_request: int
    status: str  # pending_review | changes_requested | approved


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_campaign: int
    id_advertiser: int
    campaign_name: str
    status: str
    starts_on: date_cls
    ends_on: date_cls
    daily_hours: dict | None = None
    sov_percent: float
    max_plays_per_hour: int | None = None
    campaign_kind: str = "visual"
    spots_per_hour: int | None = None
    pricing_model: str
    price_amount: float | None = None
    price_currency: str = "EUR"
    cpm_rate: float | None = None
    priority: int = 50
    overbook_override: bool = False
    created_at: datetime
    updated_at: datetime
    # Peuplés par l'endpoint
    advertiser_name: str | None = None
    creative_count: int = 0
    target_player_count: int = 0
    targets: list[CampaignTargetRead] = []
    approval_request: CampaignApprovalInfo | None = None


class CampaignStatusUpdate(BaseModel):
    """Transitions manuelles : publish (→ approved/live), pause, resume, cancel, complete."""
    action: Literal["publish", "pause", "resume", "cancel", "complete"]


# ── Réglages retail media de l'org (S5 + tarif v2 + quotas upload 0084) ──────
class CampaignSettingsRead(BaseModel):
    creative_approval: bool = False
    # Tarif SOV par défaut de l'org (€ / 1 % SOV / jour / écran) — None = pas de devis
    sov_daily_rate: float | None = None
    # Quotas d'upload du portail annonceur (§3.4, configurables — retour Ed #6)
    max_creatives: int = 20
    creative_max_mb: int = 1024
    # Sélection PAR ÉCRAN dans le self-service portail (migr 0089, défaut OFF —
    # expose la liste nominative des écrans d'un site à un externe)
    portal_player_selection: bool = False
    # Durée MAX d'un créatif en secondes (garde-fou boucle courte) — None = illimité
    creative_max_duration_s: int | None = 30
    # S2 audio (migr 0094) : plafond de spots/h par écran (défaut org, héritage
    # player > site > org) + tarif audio par défaut (€/spot/h/jour — None = pas de devis)
    audio_spots_cap: int = 12
    audio_spot_daily_rate: float | None = None


class CampaignSettingsUpdate(BaseModel):
    creative_approval: bool | None = None
    sov_daily_rate: float | None = Field(default=None, ge=0)
    max_creatives: int | None = Field(default=None, ge=1, le=200)
    creative_max_mb: int | None = Field(default=None, ge=1, le=10_240)
    portal_player_selection: bool | None = None
    # None absent = intouché ; null EXPLICITE = retirer la limite (model_fields_set)
    creative_max_duration_s: int | None = Field(default=None, ge=1, le=3600)
    audio_spots_cap: int | None = Field(default=None, ge=1, le=60)
    # None absent = intouché ; null EXPLICITE = retirer le tarif (model_fields_set)
    audio_spot_daily_rate: float | None = Field(default=None, ge=0)


# ── Inventaire / occupation SOV (S2) ─────────────────────────────────────────
class OccupancyCheckRequest(BaseModel):
    """Projection d'occupation AVANT sauvegarde/publication (warning 80 %, blocage 100 %)."""
    starts_on: date_cls
    ends_on: date_cls
    sov_percent: float = Field(default=10.0, gt=0, le=100)
    # S2 audio : kind='audio' → projection en SPOTS/HEURE (spots_per_hour requis),
    # sov_percent ignoré ; les pct de la réponse sont relatifs au plafond spots/h.
    campaign_kind: Literal["visual", "audio"] = "visual"
    spots_per_hour: int | None = Field(default=None, ge=1, le=60)
    targets: list[CampaignTargetSpec] = []
    exclude_id_campaign: int | None = None  # édition : ne pas se compter soi-même
    # Exclusion concurrentielle (migr 0087) : la projection/devis retire les sites
    # qui excluent la catégorie de CET annonceur.
    id_advertiser: int | None = None


class OccupancyPlayerRow(BaseModel):
    id_player: int
    player_name: str | None = None
    current_pct: float
    projected_pct: float
    # Plafond effectif du player (héritage player > site > 100) — v2 migr 0082
    cap_pct: float = 100.0
    # S2 audio : détail en spots/h (les pct ci-dessus sont alors relatifs au plafond)
    current_spots: int | None = None
    projected_spots: int | None = None
    cap_spots: int | None = None


class PriceEstimate(BaseModel):
    """Prix INDICATIF (devis) — Σ tarifs effectifs des players ciblés × SOV × jours.
    Le prix contractuel reste le champ manuel de la campagne."""

    estimated_price: float
    priced_players: int
    unpriced_players: int = 0
    days: int


class FeasibilityPlayerRow(BaseModel):
    """Plafond de SOV réalisable d'un player : boucle maison de B items / H ms →
    max ≈ B×d/(H+B×d) (anti-martèlement : 1 pub max par frontière de boucle)."""
    id_player: int
    player_name: str | None = None
    max_sov_pct: float
    home_items: int
    home_duration_ms: int


class CampaignFeasibility(BaseModel):
    """SOV max réalisable de la campagne = celui du PIRE player ciblé (boucle
    courante — approximation assumée, cf. services/campaign_feasibility)."""
    max_sov_pct: float
    creative_duration_ms: int
    players: list[FeasibilityPlayerRow] = []  # triés du pire au meilleur, cap 20
    unresolved_players: int = 0  # ciblés sans boucle courante (non comptés)


class OccupancyCheckResult(BaseModel):
    blocked: bool
    warning: bool
    max_projected_pct: float
    players: list[OccupancyPlayerRow] = []
    # Devis (None si aucun tarif configuré org/site/player)
    price: PriceEstimate | None = None
    # SOV réalisable (None si aucune boucle courante résolue sur les cibles)
    achievable: CampaignFeasibility | None = None
    # S2 audio : spots/h encore vendables (= min du plafond restant des players ciblés)
    achievable_spots_per_hour: int | None = None


class InventoryRow(BaseModel):
    """Occupation vendue d'un player sur la fenêtre demandée (campagnes approuvées)."""
    id_player: int
    player_name: str
    occupied_pct: float


# ── Portail annonceur (S7 B2) — accès externes + branding ────────────────────

class AdvertiserUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_user: int
    email: str
    full_name: str | None = None
    is_active: bool
    is_demo: bool = False
    last_login_at: datetime | None = None
    created_at: datetime


class AdvertiserUserInvite(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    full_name: str | None = None
    # Compte DÉMO du portail (prospect) : lecture seule totale — la garde démo bloque
    # toute écriture et la bannière/bulles démo s'affichent dans le portail.
    is_demo: bool = False


class AdvertiserUserInviteResult(BaseModel):
    user: AdvertiserUserRead
    # Mot de passe initial généré — affiché UNE seule fois (à transmettre à l'annonceur).
    # None = compte EXISTANT rattaché sans toucher au mot de passe (get-or-link) ; un
    # compte désactivé ré-invité reçoit un nouveau mot de passe (l'ancien est perdu).
    initial_password: str | None = None
    # True = l'email existait déjà (rattaché/réactivé au lieu de créé) — message UI adapté
    linked_existing: bool = False


class PortalBranding(BaseModel):
    org_name: str
    advertiser_name: str | None = None
    primary_color: str = "#4875B4"
    report_title: str | None = None
    logo_url: str | None = None


# ── Import de campagne par fichier JSON (S6 — ordre d'insertion annonceur) ───
# Format documenté : docs/FORMAT_IMPORT_CAMPAGNE.md. Les cibles sont données par
# NOM (résolues à l'import) ; les créatifs par URL publique (téléchargés en tâche
# Celery puis pipeline média normal — transcode, renditions).

class ImportAdvertiserSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_name: str | None = None
    contact_email: str | None = None


class ImportCampaignSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    starts_on: date_cls
    ends_on: date_cls
    daily_hours: dict | None = None            # {"start":"09:00","end":"20:00"}
    sov_percent: float = Field(gt=0, le=100)
    pricing_model: Literal["sov", "flat", "cpm"] = "sov"
    price_amount: float | None = None
    price_currency: str = "EUR"
    cpm_rate: float | None = None
    priority: int = 50

    @model_validator(mode="after")
    def _dates(self) -> ImportCampaignSpec:
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on < starts_on")
        if self.daily_hours is not None:
            s, e = self.daily_hours.get("start"), self.daily_hours.get("end")
            ok = (isinstance(s, str) and isinstance(e, str)
                  and len(s) == 5 and len(e) == 5 and s[2] == ":" and e[2] == ":")
            if not ok:
                raise ValueError('daily_hours = {"start":"HH:MM","end":"HH:MM"}')
        return self


class ImportTargetSpec(BaseModel):
    """Cible par NOM : org (name ignoré) | site | site_group | group | player."""
    type: Literal["org", "site", "site_group", "group", "player"]
    name: str | None = None

    @model_validator(mode="after")
    def _name(self) -> ImportTargetSpec:
        if self.type != "org" and not (self.name or "").strip():
            raise ValueError(f"name required for a {self.type} target")
        return self


class ImportCreativeSpec(BaseModel):
    url: str = Field(min_length=8)
    name: str | None = None                     # nom d'affichage (défaut : fichier de l'URL)
    duration_ms: int | None = Field(default=None, gt=0)   # requis pour une image
    weight: int = Field(default=1, ge=1)


class CampaignImportOrder(BaseModel):
    version: int = 1
    advertiser: ImportAdvertiserSpec
    campaign: ImportCampaignSpec
    targets: list[ImportTargetSpec] = Field(min_length=1)
    creatives: list[ImportCreativeSpec] = Field(min_length=1, max_length=20)


class ImportResolvedTarget(BaseModel):
    type: str
    name: str | None = None
    target_id: int | None = None
    error: str | None = None


class ImportResolvedCreative(BaseModel):
    url: str
    media_name: str
    media_type: str | None = None
    error: str | None = None


class ImportDryRunResult(BaseModel):
    ok: bool
    errors: list[str] = []
    advertiser_name: str
    advertiser_matched: bool                    # False = sera créé
    targets: list[ImportResolvedTarget] = []
    target_player_count: int = 0
    creatives: list[ImportResolvedCreative] = []
    occupancy: OccupancyCheckResult | None = None
