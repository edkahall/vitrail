"""Schémas Sites + heures d'ouverture + auto-power écrans (§ heures d'ouverture)."""

from __future__ import annotations

from datetime import date as date_cls, datetime, time as time_cls
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.timezones import validate_tz_or_raise as _validate_timezone

# Depuis P2.2 le fuseau d'un site décide de l'heure à laquelle il DIFFUSE, plus seulement
# de l'allumage de ses écrans. Une faute de frappe (« Paris ») ne casse rien de visible :
# elle replie silencieusement le site sur l'heure du serveur. Un 422 à la saisie coûte
# deux secondes ; le silence coûte une journée d'antenne à la mauvaise heure.


# ── Horaires hebdomadaires ───────────────────────────────────────────────────
class SiteHoursItem(BaseModel):
    """Horaire d'un jour de la semaine (0=Lun … 6=Dim)."""
    model_config = ConfigDict(from_attributes=True)

    weekday: int
    is_closed: bool = False
    open_time: Optional[time_cls] = None
    close_time: Optional[time_cls] = None

    @model_validator(mode="after")
    def _check(self) -> "SiteHoursItem":
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be 0..6")
        if not self.is_closed and (self.open_time is None or self.close_time is None):
            raise ValueError("open_time and close_time required if the day is not closed")
        return self


class SiteHoursReplace(BaseModel):
    """Remplace l'ensemble des horaires hebdo d'un site (PUT)."""
    hours: list[SiteHoursItem]


# ── Exceptions datées ────────────────────────────────────────────────────────
class SiteExceptionCreate(BaseModel):
    date_from: date_cls
    date_to: date_cls
    kind: Literal["closed", "hours", "open_24h"]
    open_time: Optional[time_cls] = None
    close_time: Optional[time_cls] = None
    label: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "SiteExceptionCreate":
        if self.date_to < self.date_from:
            raise ValueError("date_to must be >= date_from")
        if self.kind == "hours" and (self.open_time is None or self.close_time is None):
            raise ValueError("open_time and close_time required for kind=hours")
        return self


class SiteExceptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_exception: int
    id_site: int
    date_from: date_cls
    date_to: date_cls
    kind: str
    open_time: Optional[time_cls] = None
    close_time: Optional[time_cls] = None
    label: Optional[str] = None


# ── Sites ────────────────────────────────────────────────────────────────────
class SiteCreate(BaseModel):
    site_name: str
    address: Optional[str] = None
    country: Optional[str] = None
    site_number: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    ntp_server: Optional[str] = None
    auto_power_enabled: bool = False
    power_lead_min: int = 15
    power_lag_min: int = 15
    respect_holidays: bool = False
    holiday_region: str = "FR"
    # Inventaire v2 (migr 0082) — plafond campagnes (%) + tarif SOV (€/%/jour) du site
    campaign_max_occupancy_pct: Optional[int] = None
    campaign_sov_daily_rate: Optional[float] = None

    _check_tz = field_validator("timezone")(_validate_timezone)


class SiteUpdate(BaseModel):
    site_name: Optional[str] = None
    address: Optional[str] = None
    country: Optional[str] = None
    site_number: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    ntp_server: Optional[str] = None
    auto_power_enabled: Optional[bool] = None
    power_lead_min: Optional[int] = None
    power_lag_min: Optional[int] = None
    respect_holidays: Optional[bool] = None
    holiday_region: Optional[str] = None
    campaign_max_occupancy_pct: Optional[int] = None
    campaign_sov_daily_rate: Optional[float] = None
    # None = ne pas toucher ; liste = REMPLACE l'ensemble des exclusions du site
    campaign_excluded_categories: Optional[list[int]] = None

    _check_tz = field_validator("timezone")(_validate_timezone)


class SitePlayerRead(BaseModel):
    """Player rattaché à un site (vue allégée pour la page Sites)."""
    model_config = ConfigDict(from_attributes=True)

    id_player: int
    player_name: str
    player_os: Optional[str] = None
    status: str
    last_power_state: Optional[bool] = None
    # Mode d'alimentation (display_config.power_schedule.mode), calculé côté endpoint.
    power_mode: str = "follow_site"


class SiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_site: int
    id_org: int
    site_name: str
    address: Optional[str] = None
    country: Optional[str] = None
    site_number: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    ntp_server: Optional[str] = None
    auto_power_enabled: bool = False
    power_lead_min: int = 15
    power_lag_min: int = 15
    respect_holidays: bool = False
    holiday_region: str = "FR"
    campaign_max_occupancy_pct: Optional[int] = None
    campaign_sov_daily_rate: Optional[float] = None
    created_at: datetime
    # Comptes / agrégats (peuplés par l'endpoint)
    player_count: int = 0
    hours: list[SiteHoursItem] = []
    campaign_excluded_categories: list[int] = []
