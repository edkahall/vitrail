from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class PlayerCreate(BaseModel):
    player_name: str
    player_os: str
    id_site: Optional[int] = None


class PlayerUpdate(BaseModel):
    player_name: Optional[str] = None
    player_os: Optional[str] = None
    id_site: Optional[int] = None
    player_role: Optional[Literal["content", "audio", "mixed"]] = None
    # Retail Media : exclu des campagnes (retiré de toute cible campagne, migr 0075)
    campaigns_optout: bool | None = None
    # Inventaire v2 (migr 0082) : plafond d'occupation (%) + tarif SOV (€/%/jour) —
    # explicitement null = revenir à l'héritage site/org (cf. model_fields_set endpoint)
    campaign_max_occupancy_pct: Optional[int] = None
    campaign_sov_daily_rate: Optional[float] = None
    display_config: Optional[dict] = None
    # Confirme le passage forcé en Chromium pour intégrer un player mpv/exo à un groupe LAN Sync
    # HÉTÉROGÈNE (cf. règle player_compat : un groupe avec décodage HW natif doit être homogène).
    # Sinon, le rejoindre en mpv/exo dans un groupe hétérogène est rejeté (409 LAN_SYNC_HETEROGENEOUS).
    force_chromium: bool = False


class PlayerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_player: int
    id_org: int
    id_site: Optional[int] = None
    player_name: str
    player_os: Optional[str] = None
    player_version: Optional[str] = None
    player_role: str = "content"
    campaigns_optout: bool = False
    campaign_max_occupancy_pct: Optional[int] = None
    campaign_sov_daily_rate: Optional[float] = None
    status: str
    ip_address: Optional[str] = None
    wan_ip: Optional[str] = None
    mac_address: Optional[str] = None

    @field_validator("ip_address", "wan_ip", mode="before")
    @classmethod
    def coerce_ip(cls, v: object) -> Optional[str]:
        return str(v) if v is not None else None
    last_seen_at: Optional[datetime] = None
    cpu_usage: Optional[float] = None
    ram_usage: Optional[float] = None
    disk_usage: Optional[float] = None
    current_playlist_id: Optional[int] = None
    token_expires_at: Optional[datetime] = None
    # Dernière capture d'écran : horodatage + URL signée MinIO (None si aucune capture).
    last_screenshot_at: Optional[datetime] = None
    screenshot_url: Optional[str] = None
    # Filtres de réception par tags (id_tag) — affichés en pastilles sur la carte player.
    tag_filters: list[int] = []
    # Configuration écran RS232/IP — clés : type, serial_port, baud_rate, host, tcp_port, model,
    # address, lan_sync_enabled, lan_sync_group
    display_config: Optional[dict] = None
    # Capacités matérielles auto-détectées (décodage vidéo HW) — remontées par le player.
    capabilities: Optional[dict] = None
    # Alertes actives — codes : offline_critical | cpu_high | ram_high | disk_high | token_7d | token_3d | token_expired | version_outdated
    alert_reasons: list[str] = []
    # Dernière rotation auto de la clé PoP (P0.1 self-heal). NULL = jamais tournée (TOFU initial).
    pop_key_rotated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # Aptitude du player à être dans SON groupe LAN Sync ({level, reason}) — calculée par
    # l'endpoint AVEC le contexte du groupe (homogénéité des membres), pas dérivable du seul
    # player. Source de vérité = services.player_compat.lan_sync_eligibility. None tant que
    # non peuplée (ex. sérialisation hors endpoint).
    lan_sync_eligibility: Optional[dict] = None


class PlayerDecodeHealthRead(BaseModel):
    """Synthèse décodage HW d'un player (matrice de conformité). Cf.
    services.player_capabilities.decode_health."""

    id_player: int
    player_name: str
    player_os: Optional[str] = None
    status: str
    effective_decode: str  # hardware | software | unknown
    method: Optional[str] = None  # vaapi | v4l2 | d3d11 | mediacodec
    severity: str  # ok | warning | unknown
    sw_ceiling: str
    gpu_vendor: Optional[str] = None
    gpu_model: Optional[str] = None
    warnings: list[str] = []


class PlayerTokenResponse(BaseModel):
    player_token: str
    expires_at: datetime


class HeartbeatPayload(BaseModel):
    type: Literal["player:status"]
    status: str
    current_content: Optional[int] = None
    cpu: Optional[float] = None
    ram: Optional[float] = None
    disk: Optional[float] = None
    player_version: Optional[str] = None
    # Capacités matérielles auto-détectées (décodage vidéo HW), remontées par le player.
    capabilities: Optional[dict] = None


class StorageCheckTarget(BaseModel):
    """Cible canonique, même vocabulaire que les programmes et les campagnes."""
    target_type: str
    target_id: int | None = None
    tag_expr: dict[str, list[str]] = {}


class StorageCheckRequest(BaseModel):
    """Contrôle de capacité AVANT affectation (point 4 du chantier stockage).

    Deux façons de désigner la même chose, selon l'appelant :
      · `id_players` / `id_medias` — listes déjà résolues ;
      · `targets` / `id_playlist` — ce que manipule RÉELLEMENT un tiroir d'affectation.
    La seconde est résolue **par le serveur** : lui faire faire le travail évite que
    l'écran réimplémente le ciblage et le dépliage des sous-playlists, et donc qu'il
    contrôle autre chose que ce qui sera diffusé.
    """
    id_players: list[int] = []
    id_medias: list[int] = []
    targets: list[StorageCheckTarget] = []
    id_playlist: int | None = None


class StorageCheckPlayer(BaseModel):
    id_player: int
    player_name: str
    verdict: str                    # ok | tight | insufficient | unknown
    reason: str
    required_bytes: int
    missing_count: int
    free_bytes: int | None = None
    disk_total_bytes: int | None = None


class StorageCheckResult(BaseModel):
    players: list[StorageCheckPlayer] = []
    worst: str = "ok"
    required_bytes: int = 0


class PlayerCommandRequest(BaseModel):
    command: Literal[
        "restart", "reboot_os", "reset_config", "screenshot", "maintenance",
        "quit_app", "display_command", "detect_capabilities", "toggle_overlay",
        "play_now", "hdmi_input", "set_timezone",
    ]
    params: Optional[dict] = None


class BulkTargetSpec(BaseModel):
    """Cible canonique (même contrat que campagnes/schedules → services/targeting.py)."""

    target_type: Literal["org", "site", "site_group", "group", "player", "tags"]
    target_id: Optional[int] = None
    tag_expr: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _check(self) -> "BulkTargetSpec":
        if self.target_type in ("site", "site_group", "group", "player") and self.target_id is None:
            raise ValueError(f"target_id required for target_type={self.target_type}")
        if self.target_type == "tags" and not self.tag_expr:
            raise ValueError("tag_expr required for target_type=tags")
        return self


class PlayerBulkCommandRequest(BaseModel):
    """Commande envoyée à PLUSIEURS players via des cibles canoniques.

    Les commandes destructives exigent `pin` (cf. services/pin_guard.py) : à l'échelle
    d'un parc, un clic distrait coûte cher.
    """

    command: Literal[
        "restart", "reboot_os", "reset_config", "screenshot", "maintenance",
        "quit_app", "display_command", "detect_capabilities", "toggle_overlay",
        "play_now", "hdmi_input", "set_timezone",
    ]
    params: Optional[dict] = None
    targets: list[BulkTargetSpec]
    # PIN 6 chiffres — requis seulement pour les commandes destructives.
    pin: Optional[str] = None

    @model_validator(mode="after")
    def _check_targets(self) -> "PlayerBulkCommandRequest":
        if not self.targets:
            raise ValueError("At least one target is required.")
        return self


class PlayerBulkCommandResult(BaseModel):
    command: str
    dispatched: list[int]
    count: int


class PlayerMaintenanceRequest(BaseModel):
    """Mode maintenance manuel (org-admin) : pose/retire le statut « collant » `maintenance`.
    enabled=True → status=maintenance (alertes suspendues, distinct dans les dashboards) ;
    enabled=False → sortie (le statut réel est ré-évalué au prochain heartbeat / beat santé)."""

    enabled: bool


class DisplayCommandRequest(BaseModel):
    """Commande envoyée à l'écran physique connecté au player (RS232, IP ou HDMI-CEC).
    Sans contrôleur configuré, power_on/power_off basculent sur le repli « coupure de
    la sortie vidéo » du player (display_config.output_power_fallback)."""
    command: Literal["power_on", "power_off", "set_input", "set_brightness", "get_status",
                     "set_contrast", "set_volume", "mute_on", "mute_off", "run_startup_sequence"]
    params: Optional[dict] = None  # source (set_input), level 0-100 (set_brightness/contrast/volume)
