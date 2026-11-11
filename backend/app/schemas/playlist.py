from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ItemRuleBase(BaseModel):
    """Règle de diffusion d'un item (heure locale). Miroir de schedule_rules + scope.

    scope `default` (tous les players) | `player` | `player_group` (override par cible, P2).
    Pour player/player_group, `target_id` est obligatoire ; pour default, il doit être nul.
    """

    rule_type: str = "weekly"  # always | weekly | date_range
    days_of_week: Optional[list[int]] = None  # 0=Lun … 6=Dim
    time_start: Optional[time] = None
    time_end: Optional[time] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    scope: str = "default"  # default | player | player_group
    target_id: Optional[int] = None

    @field_validator("rule_type")
    @classmethod
    def _valid_rule_type(cls, v: str) -> str:
        if v not in ("always", "weekly", "date_range"):
            raise ValueError("invalid rule_type (always|weekly|date_range)")
        return v

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        if v not in ("default", "player", "player_group"):
            raise ValueError("invalid scope (default|player|player_group)")
        return v

    @model_validator(mode="after")
    def _check_target(self):
        if self.scope == "default":
            if self.target_id is not None:
                raise ValueError("target_id not allowed for scope=default")
        elif self.target_id is None:
            raise ValueError("target_id required for scope player/player_group")
        return self


class ItemRuleCreate(ItemRuleBase):
    pass


class ItemRuleRead(ItemRuleBase):
    model_config = {"from_attributes": True}

    id_item_rule: int
    id_item: int


class PlaylistItemCreate(BaseModel):
    """Item média (id_media) OU sous-playlist imbriquée (id_child_playlist) —
    exactement l'un des deux."""

    id_media: Optional[int] = None
    id_child_playlist: Optional[int] = None
    sort_order: int = 0
    # Pour un DOCUMENT multipage : durée PAR PAGE (défaut 10 s).
    duration_ms: Optional[int] = None
    zone_index: int = 0
    transition_type: str = "cut"
    # Documents multipages : sélection de pages "2-5,8" (None = toutes).
    doc_pages: Optional[str] = Field(default=None, max_length=200)
    # Documents multipages : feuilletage au doigt (migr 0129). Inerte hors document et sur un
    # écran non tactile. `touch_resume_after_s` : délai sans interaction avant de reprendre la
    # diffusion normale (None = défaut du renderer).
    touch_navigation: bool = False
    touch_resume_after_s: Optional[int] = Field(default=None, ge=5, le=3600)

    @model_validator(mode="after")
    def _one_of(self):
        if (self.id_media is None) == (self.id_child_playlist is None):
            raise ValueError("id_media OR id_child_playlist required (exactly one)")
        return self


class PlaylistItemsBulkCreate(BaseModel):
    """Ajout de plusieurs contenus d'un coup au brouillon (multi-sélection UI)."""
    id_medias: list[int]


class PlaylistItemRead(BaseModel):
    model_config = {"from_attributes": True}

    id_item: int
    id_playlist: int
    id_media: Optional[int] = None
    # media | sub_playlist (playlist imbriquée)
    item_type: str = "media"
    id_child_playlist: Optional[int] = None
    sort_order: int
    duration_ms: Optional[int]
    zone_index: int
    transition_type: str
    # Documents multipages : sélection "2-5,8" (None = toutes) + nb de pages du média
    # (enrichi par l'endpoint — pour afficher « N pages × durée » dans l'éditeur).
    doc_pages: Optional[str] = None
    media_page_count: Optional[int] = None
    # Feuilletage au doigt (migr 0129) — voir PlaylistItemCreate.
    touch_navigation: bool = False
    touch_resume_after_s: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    media_name: Optional[str] = None
    media_type: Optional[str] = None
    # Durée NATIVE du média (extraite à l'upload, vidéo/audio) — enrichie par l'endpoint.
    # L'éditeur affiche duration_ms (override manuel) sinon celle-ci ; le player applique
    # le même repli (ws.py : item.duration_ms or media.duration_ms or 10000).
    media_duration_ms: Optional[int] = None
    # Conformité du média (§XVI.2) : null|ready|playable|nonconform — enrichi par l'endpoint
    conformance_status: Optional[str] = None
    # True si l'item porte ≥1 règle de diffusion (item time-boxé) ; False = perpétuel.
    has_rules: bool = False
    # Enrichissement sub_playlist : nom + nb d'items + réglages rotation de l'enfant.
    child_name: Optional[str] = None
    child_item_count: Optional[int] = None
    child_rotation_mode: Optional[str] = None
    child_items_per_turn: Optional[int] = None


class ReorderItem(BaseModel):
    id_item: int
    sort_order: int


class PlaylistItemsReorder(BaseModel):
    items: list[ReorderItem]


class PlaylistItemUpdate(BaseModel):
    """Mise à jour d'un item du brouillon : durée d'exposition (utile pour image/PDF sans durée
    native) + transition. Champs optionnels ; `model_fields_set` distingue « non fourni » de
    « null ». `duration_ms` null = repli sur la durée par défaut côté player (10 s)."""

    duration_ms: Optional[int] = Field(default=None, ge=1000)
    transition_type: Optional[str] = None
    # §25.4 zones de template : affecte l'item à une zone du template de la playlist.
    zone_index: Optional[int] = Field(default=None, ge=0)
    # Documents multipages : sélection de pages "2-5,8" (null/vide = toutes les pages).
    # Pour ces items, duration_ms = durée PAR PAGE.
    doc_pages: Optional[str] = Field(default=None, max_length=200)
    # Feuilletage au doigt (migr 0129) — voir PlaylistItemCreate.
    touch_navigation: Optional[bool] = None
    touch_resume_after_s: Optional[int] = Field(default=None, ge=5, le=3600)


class PlaylistCreate(BaseModel):
    playlist_name: str
    description: Optional[str] = None
    id_template: Optional[int] = None
    sync_mode: str = "wall_clock"
    # Gouvernance §XVIII contenu : nœud propriétaire (NULL = playlist d'organisation).
    id_governance_node: Optional[int] = None


class PlaylistUpdate(BaseModel):
    playlist_name: Optional[str] = None
    description: Optional[str] = None
    id_template: Optional[int] = None
    sync_mode: Optional[str] = None
    # Gouvernance §XVIII : re-rattacher / détacher la playlist d'un nœud.
    id_governance_node: Optional[int] = None
    # Rotation (sous-playlist « N items par tour ») : all | one_sequential | one_random
    rotation_mode: Optional[str] = None
    items_per_turn: Optional[int] = Field(default=None, ge=1, le=50)
    # « Pas de publicité entre les contenus » (effet en sous-playlist uniquement).
    no_ads_inside: Optional[bool] = None

    @field_validator("rotation_mode")
    @classmethod
    def _valid_rotation(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("all", "one_sequential", "one_random"):
            raise ValueError("invalid rotation_mode (all|one_sequential|one_random)")
        return v


class PlaylistRead(BaseModel):
    model_config = {"from_attributes": True}

    id_playlist: int
    id_org: int
    playlist_name: str
    description: Optional[str] = None
    id_template: Optional[int] = None
    sync_mode: str = "wall_clock"
    total_duration_ms: Optional[int] = None
    # Nombre d'items (sous-playlists comprises, comptées 1) — enrichi par l'endpoint liste.
    item_count: Optional[int] = None
    # Synchronisable LAN/wall-clock : sync_mode == wall_clock ET tous les items
    # ont une durée résolue (item.duration_ms OU media.duration_ms). Sinon le
    # wall-clock retombe sur un fallback 10s par item → désync silencieuse.
    sync_ready: bool = False
    # True si ≥1 item porte une règle de diffusion (pour le badge « Programmé »).
    has_item_rules: bool = False
    # « Synchronisable à fenêtres » : sync_ready EST vrai ET il y a des règles de
    # diffusion (toutes scope=default → jeu d'items player-uniforme à chaque instant,
    # juste une re-sync graceful aux frontières). Un override scope=player/group casse
    # sync_ready (jeux d'items divergents entre players).
    sync_windowed: bool = False
    # Staging (#1) : True si la playlist a un brouillon d'édition non publié.
    has_draft_changes: bool = False
    # Rotation : all (playlist normale) | one_sequential | one_random + N items/tour.
    rotation_mode: str = "all"
    items_per_turn: int = 1
    # « Pas de publicité entre les contenus » (effet en sous-playlist uniquement).
    no_ads_inside: bool = False
    # True si cette playlist est référencée comme sous-playlist par ≥1 parent.
    used_as_child: bool = False
    # Gouvernance §XVIII : nœud propriétaire (NULL = playlist d'organisation).
    id_governance_node: Optional[int] = None
    # True si la playlist est gouvernée par un nœud que l'utilisateur courant ne peut PAS
    # éditer (visible en lecture seule, D6) → le front grise les actions d'édition.
    governed_readonly: bool = False
    # True si le nœud de la playlist exige la modération (publication via approbation).
    requires_approval: bool = False
    created_at: datetime
    updated_at: datetime


class PlaylistDetail(PlaylistRead):
    items: list[PlaylistItemRead] = []
    # Modération §XVIII/§XXI : demande d'approbation ACTIVE sur cette playlist, s'il y en a
    # une (pending_review | changes_requested | approved). L'éditeur affiche « en cours de
    # validation » au lieu de proposer une soumission qui repartirait en 409.
    approval_status: Optional[str] = None
    approval_request_id: Optional[int] = None
    # Durée maximale d'UN contenu imposée par la chaîne de gouvernance (secondes), ou None.
    # Sert un AVERTISSEMENT au chargement de l'éditeur : un contenu au-dessus sera écarté à
    # la compilation, et le découvrir seulement à l'aperçu arrivait trop tard (Ed, 26/07).
    governance_max_item_sec: Optional[int] = None
    # Contenus de cette playlist qui dépassent ce plafond, NOMMÉS (« certains contenus »
    # enverrait chercher au mauvais endroit). Vide si aucun plafond ou aucun dépassement.
    governance_too_long: list[str] = []


# ── Simulation timeline déterministe (§ retours clients — Simulation v2) ──────────
class SimulateTimelineRequest(BaseModel):
    date: str                       # "YYYY-MM-DD" (jour simulé, en heure locale SIGNFLOW_TZ)
    start_time: str = "00:00"       # "HH:MM" — début de la simulation dans la journée
    player_id: Optional[int] = None  # applique les overrides de règle scope=player/player_group


class TimelineSegment(BaseModel):
    id_item: int
    id_media: Optional[int] = None
    media_name: Optional[str] = None
    media_type: Optional[str] = None
    start: str                      # "HH:MM:SS" heure locale
    end: str
    duration_ms: int
    # kind="item" : passage normal de la playlist ; kind="event" : fenêtre où un event
    # programmé (scheduled) prend la main sur le player ciblé.
    kind: str = "item"
    event_name: Optional[str] = None   # nom de l'event (kind="event")
    content_name: Optional[str] = None  # contenu diffusé par l'event (playlist/média)
    priority: Optional[int] = None      # priorité de l'event (kind="event")


class SimulateTimelineResult(BaseModel):
    sync_mode: str
    segments: list[TimelineSegment] = []
    truncated: bool = False         # True si la journée dépasse la limite de segments
