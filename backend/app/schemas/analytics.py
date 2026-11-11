import ipaddress
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class FleetCounts(BaseModel):
    total: int
    online: int
    warning: int
    offline: int
    maintenance: int


class TopContentRow(BaseModel):
    id_media: int | None = None
    media_name: str | None = None
    media_type: str | None = None
    plays: int
    seconds: int


class MetricsPoint(BaseModel):
    """Un jour de rollup (§XXIV) — sert les séries temporelles du dashboard."""
    date: date
    players_total: int
    players_online: int
    online_pct: float | None = None
    conformity_pct: float | None = None
    alerts_active: int
    plays_count: int
    health_score: int | None = None


class ExecutiveDashboard(BaseModel):
    from_date: date
    until_date: date
    # KPIs (snapshot live à l'instant de la requête)
    health_score: int | None = None
    fleet: FleetCounts
    conformity_pct: float | None = None
    alerts_active: int
    uptime_pct: float | None = None           # moyenne du % online sur la période (rollup)
    plays_count: int                          # diffusions sur la période
    play_seconds: int
    # Graphiques
    top_content: list[TopContentRow]
    series: list[MetricsPoint]                # rollup quotidien [from, until]


# ── Branding white-label + ROI (§24.3 / §24.5) ───────────────────────────────

class ReportBranding(BaseModel):
    primary_color: str = "#4875B4"
    report_title: str | None = None
    report_subtitle: str | None = None
    roi_hours_per_day: float = 12
    roi_contacts_hour: int = 50
    roi_cpm_eur: float = 5
    roi_paper_eur: float = 50
    logo_url: str | None = None       # URL signée (lecture seule)
    has_logo: bool = False


class ReportBrandingUpdate(BaseModel):
    primary_color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    report_title: str | None = Field(None, max_length=120)
    report_subtitle: str | None = Field(None, max_length=200)
    roi_hours_per_day: float | None = Field(None, ge=0, le=24)
    roi_contacts_hour: int | None = Field(None, ge=0, le=100000)
    roi_cpm_eur: float | None = Field(None, ge=0, le=100000)
    roi_paper_eur: float | None = Field(None, ge=0, le=100000)


# ── Share links (§24.4) ──────────────────────────────────────────────────────

class ShareLinkCreate(BaseModel):
    label: str | None = Field(None, max_length=120)
    period_days: int = Field(30, ge=1, le=365)
    expires_in_days: int | None = Field(None, ge=1, le=3650)  # None = permanent
    # Options avancées (§24.4) — None = ouvert.
    password: str | None = Field(None, min_length=4, max_length=128)
    ip_allowlist: list[str] | None = Field(None, max_length=32)

    @field_validator("ip_allowlist", mode="before")
    @classmethod
    def _validate_cidrs(cls, v):
        if v is None:
            return None
        cleaned = []
        for item in v:
            item = str(item).strip()
            if not item:
                continue
            try:
                # Accepte IP nue ("81.2.3.4") ou CIDR ("192.168.1.0/24").
                net = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid CIDR: {item}") from exc
            cleaned.append(str(net))
        return cleaned or None


class ShareLinkRead(BaseModel):
    id_share: int
    token: str
    url: str
    label: str | None = None
    period_days: int
    expires_at: datetime | None = None
    revoked: bool
    access_count: int
    last_accessed_at: datetime | None = None
    has_password: bool = False
    ip_allowlist: list[str] | None = None
    created_at: datetime


# ── Clés BI (§24.7) ──────────────────────────────────────────────────────────

class BiKeyCreate(BaseModel):
    label: str | None = Field(None, max_length=255)
    expires_in_days: int | None = Field(None, ge=1, le=3650)  # None = permanent


class BiKeyRead(BaseModel):
    id_key: int
    key_prefix: str
    label: str | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    key: str | None = None  # valeur en clair — renvoyée UNIQUEMENT à la création


# ── ROI + vue publique ───────────────────────────────────────────────────────

class RoiEstimate(BaseModel):
    screens: int
    days: int
    hours_per_day: float
    contacts_per_hour: float
    cpm_eur: float
    paper_eur_per_month: float
    uptime_pct: float
    impressions: int
    network_value_eur: float
    paper_savings_eur: float
    total_roi_eur: float


class NocRow(BaseModel):
    """Une ligne de la flotte pour le dashboard NOC / Opérations (§24.2)."""
    id_player: int
    player_name: str
    id_site: int | None = None
    site_name: str | None = None
    player_os: str | None = None
    player_version: str | None = None
    status: str
    last_seen_at: datetime | None = None
    disk_usage: float | None = None
    alert_reasons: list[str] = []


class NocResponse(BaseModel):
    # Compteurs sur TOUT le périmètre visible, jamais sur les seules lignes renvoyées.
    total: int
    online: int
    warning: int
    offline: int
    maintenance: int
    rows: list[NocRow]
    # True quand le parc dépasse le plafond de lignes détaillées : l'écran DOIT le dire,
    # sinon une table tronquée se lit comme un parc complet (audit Dashboard 2026-07-28).
    rows_truncated: bool = False


class ContentPerfRow(BaseModel):
    """Performance d'un contenu sur la période (Proof of Play, §24.2)."""
    id_media: int | None = None
    media_name: str | None = None
    media_type: str | None = None
    plays: int
    seconds: int
    players: int
    sites: int


class ContentPerfResponse(BaseModel):
    from_date: date
    until_date: date
    total_plays: int
    total_seconds: int
    rows: list[ContentPerfRow]


class ReportScheduleCreate(BaseModel):
    label: str | None = Field(None, max_length=120)
    frequency: str = Field("monthly", pattern="^(weekly|monthly)$")
    send_hour: int = Field(7, ge=0, le=23)
    send_dow: int = Field(1, ge=0, le=6)       # weekly : 0=lundi
    send_dom: int = Field(1, ge=1, le=28)      # monthly : jour du mois
    period_days: int = Field(30, ge=1, le=365)
    language: str = Field("fr", max_length=5)
    recipients: list[str] = Field(default_factory=list)
    include_roi: bool = True
    enabled: bool = True


class ReportScheduleUpdate(BaseModel):
    label: str | None = None
    frequency: str | None = Field(None, pattern="^(weekly|monthly)$")
    send_hour: int | None = Field(None, ge=0, le=23)
    send_dow: int | None = Field(None, ge=0, le=6)
    send_dom: int | None = Field(None, ge=1, le=28)
    period_days: int | None = Field(None, ge=1, le=365)
    language: str | None = Field(None, max_length=5)
    recipients: list[str] | None = None
    include_roi: bool | None = None
    enabled: bool | None = None


class ReportScheduleRead(BaseModel):
    id_schedule: int
    label: str | None = None
    frequency: str
    send_hour: int
    send_dow: int
    send_dom: int
    period_days: int
    language: str
    recipients: list[str]
    include_roi: bool
    enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime


class ReportHistoryRow(BaseModel):
    id_history: int
    id_schedule: int | None = None
    report_type: str
    period_from: date | None = None
    period_until: date | None = None
    recipients: list[str]
    status: str
    detail: str | None = None
    created_at: datetime


class SmtpStatus(BaseModel):
    configured: bool


class GeoSite(BaseModel):
    """Un site géolocalisé pour la carte du parc (§24.2)."""
    id_site: int
    site_name: str
    country: str | None = None
    site_number: str | None = None
    latitude: float
    longitude: float
    players_total: int
    players_online: int
    players_warning: int
    players_offline: int
    players_maintenance: int = 0


class GeoResponse(BaseModel):
    sites: list[GeoSite]
    unplaced: int                  # sites sans coordonnées (à placer)


class AudienceSiteRow(BaseModel):
    id_site: int | None = None
    site_name: str | None = None
    plays: int
    seconds: int
    est_impressions: int


class AudienceHourRow(BaseModel):
    hour: int                      # 0..23 (heure locale)
    plays: int
    est_impressions: int


class AudienceResponse(BaseModel):
    from_date: date
    until_date: date
    contacts_per_hour: int         # hypothèse (branding ROI)
    total_est_impressions: int
    by_site: list[AudienceSiteRow]
    by_hour: list[AudienceHourRow]


class PublicShareView(BaseModel):
    org_name: str
    primary_color: str
    report_title: str | None = None
    report_subtitle: str | None = None
    logo_url: str | None = None
    # Type du lien (migr 0076) : executive → dashboard+roi ; campaign → campaign_report.
    report_type: str = "executive"
    dashboard: ExecutiveDashboard | None = None
    roi: RoiEstimate | None = None
    campaign_report: dict | None = None
