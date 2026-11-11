from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class MediaListItem(BaseModel):
    model_config = {"from_attributes": True}

    id_media: int
    media_name: str
    media_type: str
    mime_type: Optional[str] = None
    file_size_bytes: Optional[int] = None  # taille de l'upload original
    served_size_bytes: Optional[int] = None  # taille du fichier réellement SERVI (re-encode), enrichi par l'endpoint
    duration_ms: Optional[int] = None
    thumbnail_url: Optional[str] = None
    source: str
    attribution_text: Optional[str] = None
    transcode_status: str
    # Passthrough / conformité (§XVI.2)
    encode_mode: str = "auto"
    conformance_status: Optional[str] = None  # ready | playable | nonconform
    # Embargo
    not_before_utc: Optional[datetime] = None
    embargo_event_id: Optional[int] = None
    embargo_event_name: Optional[str] = None  # enrichi par l'endpoint
    tags: list[int] = []  # id_tags (§XVII) — enrichi par l'endpoint liste
    id_folder: Optional[int] = None  # dossier de classement (NULL = racine)
    # Gouvernance §XVIII contenu (D9).
    id_governance_node: Optional[int] = None
    governed_readonly: bool = False
    is_blocked: bool = False  # quarantaine / kill-switch (§ Q5)
    block_reason: Optional[str] = None
    has_renditions: bool = False  # artefacts adaptatifs présents (badge)
    id_feed: Optional[int] = None  # pour un média pointeur feed_display → édition du feed
    page_count: Optional[int] = None  # document multipage converti (badge « N pages »)
    # Avancement de l'encodage en cours (barre de progression). Enrichi par l'endpoint
    # depuis Redis, JAMAIS stocké en base (cf. services/encode_progress.py). Absent =
    # aucun encodage vivant : soit c'est fini, soit le worker est mort — dans les deux
    # cas l'écran retombe sur le simple statut, sans chiffre trompeur.
    encode_progress: Optional[dict] = None
    # Verdict de l'étalonnage (statut `awaiting_decision`) : durée estimée + replis
    # proposés. Extrait de `metadata` par l'endpoint — la liste n'expose pas tout le
    # metadata (il porte des détails internes qui n'ont rien à faire au navigateur).
    encode_decision: Optional[dict] = None
    created_at: datetime


class HdmiInputCreate(BaseModel):
    """Création d'un média pointeur « Entrée HDMI » (sans fichier) : programmé en playlist,
    il bascule le player (BrightSign XT/4K1142/XD1230) sur son entrée HDMI physique."""

    name: str = Field(..., min_length=1, max_length=255)


class MediaStats(BaseModel):
    """Comptes par type sur toute la bibliothèque (indépendant de la pagination)."""

    total: int
    by_type: dict[str, int]


class MediaRead(BaseModel):
    model_config = {"from_attributes": True}

    id_media: int
    id_org: int
    media_name: str
    media_type: str
    mime_type: Optional[str]
    file_size_bytes: Optional[int]
    duration_ms: Optional[int]
    width: Optional[int]
    height: Optional[int]
    thumbnail_url: Optional[str]
    source: str
    attribution_text: Optional[str]
    isrc: Optional[str]
    rights_metadata: dict
    media_metadata: dict = {}
    transcode_status: str
    # Passthrough / conformité (§XVI.2) — le rapport détaillé est dans media_metadata["conformance"]
    encode_mode: str = "auto"
    conformance_status: Optional[str] = None  # ready | playable | nonconform
    id_folder: Optional[int] = None  # dossier de classement (NULL = racine)
    is_blocked: bool = False  # quarantaine / kill-switch (§ Q5)
    block_reason: Optional[str] = None
    has_renditions: bool = False  # artefacts adaptatifs présents (badge)
    page_count: Optional[int] = None  # document multipage converti (§3.2)
    cdn_url_signed: Optional[str] = None
    # Avancement de l'encodage en cours — cf. MediaListItem.encode_progress.
    encode_progress: Optional[dict] = None
    # Verdict de l'étalonnage — servi par le DÉTAIL autant que par la liste : un tiroir
    # ouvert par lien direct doit être aussi informé que la liste (classe de défaut vue
    # sur `governed_readonly` à l'audit du menu Média).
    encode_decision: Optional[dict] = None
    # Gouvernance §XVIII contenu (D9) : nœud propriétaire (NULL = média d'org).
    id_governance_node: Optional[int] = None
    governed_readonly: bool = False  # gouverné par un nœud non éditable → grisé au front
    created_at: datetime
    updated_at: datetime


class RenditionRead(BaseModel):
    """Artefact (rendition adaptative) d'un média parent — drawer médiathèque."""
    model_config = {"from_attributes": True}

    id_rendition: int
    id_parent: int
    profile: str  # uhd | fhd | hd720
    width: Optional[int] = None
    height: Optional[int] = None
    codec: Optional[str] = None
    file_size_bytes: Optional[int] = None
    conformance_status: Optional[str] = None
    status: str  # pending | processing | ready | failed
    error_detail: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RenditionGenerateRequest(BaseModel):
    """Body pour POST /media/{id}/renditions — génère/régénère une rendition.

    profile : profil cible (fhd par défaut pour démarrer minimal). Idempotent par
    (parent, profile) : régénère l'artefact existant.
    """
    profile: str = "fhd"


class MediaEncodeModeUpdate(BaseModel):
    """Body pour PATCH /media/{id}/encode-mode — bascule auto/passthrough + re-traitement."""
    encode_mode: str  # auto | passthrough


class MediaRenameUpdate(BaseModel):
    """Body pour PATCH /media/{id}/rename — renomme le libellé du média (pas le fichier)."""
    media_name: str = Field(..., min_length=1, max_length=255)


class MediaGovernanceNodeUpdate(BaseModel):
    """Body pour PATCH /media/{id}/governance-node — rattache/détache le média d'un nœud
    de gouvernance §XVIII (NULL = média d'organisation)."""
    id_governance_node: Optional[int] = None


class MediaPassthroughAck(BaseModel):
    """Body pour POST /media/{id}/passthrough-ack — acquittement d'un verdict 🟡/🔴.

    L'utilisateur reconnaît avoir vu les raisons (tracées dans cms_audit_log).
    """
    note: Optional[str] = None


class MediaPassthroughAckResult(BaseModel):
    id_media: int
    conformance_status: Optional[str]
    acknowledged_at: datetime


class MediaEncodeDecision(BaseModel):
    """Body pour POST /media/{id}/encode-decision — l'utilisateur tranche un encodage long.

    Pas d'action « annuler » ici volontairement : supprimer un média passe par le `DELETE`
    existant, qui porte déjà l'analyse d'impact, la garde de gouvernance et la traçabilité.
    Un second chemin de suppression finirait par diverger du premier.
    """
    action: Literal["proceed", "downscale"]
    # Requis (et vérifié) pour `downscale` : profil parmi ceux réellement proposés.
    profile: Optional[str] = None


class MediaEncodeDecisionResult(BaseModel):
    id_media: int
    transcode_status: str
    downscale_box: Optional[int] = None
    estimate_s: Optional[int] = None


class MediaRightsUpdate(BaseModel):
    """Body pour PATCH /media/{id}/rights — droits d'auteur SACEM (§XIX.6).

    Sémantique replace : isrc + rights_metadata sont remplacés tels quels.
    """
    isrc: Optional[str] = None
    rights_metadata: dict = {}


class IsrcLookupResult(BaseModel):
    """Résultat best-effort d'un lookup MusicBrainz par ISRC."""
    found: bool
    isrc: str
    title: Optional[str] = None
    composer: Optional[str] = None
    publisher: Optional[str] = None
    album: Optional[str] = None
    year: Optional[int] = None


class MediaEmbargoUpdate(BaseModel):
    """Body pour PATCH /media/{id}/embargo."""
    not_before_utc: Optional[datetime] = None
    embargo_event_id: Optional[int] = None


class AffectedPlaylist(BaseModel):
    id_playlist: int
    playlist_name: str


class MediaEmbargoResult(BaseModel):
    """Réponse de PATCH /media/{id}/embargo — inclut les playlists à risque."""
    media: MediaListItem
    affected_playlists: list[AffectedPlaylist]


class StockSearchResult(BaseModel):
    provider: str
    provider_id: str
    title: Optional[str]
    description: Optional[str]
    preview_url: str
    full_url: str
    attribution_text: str
    photographer: str
    photographer_url: Optional[str]
    width: Optional[int]
    height: Optional[int]


class StockImportRequest(BaseModel):
    provider: str
    provider_id: str
    full_url: str
    preview_url: str
    attribution_text: str
    photographer: str
    title: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


# ── Dossiers médias (§ retours clients) ──────────────────────────────────────────

class FolderCreate(BaseModel):
    folder_name: str
    parent_id: Optional[int] = None


class FolderUpdate(BaseModel):
    """Renommer (folder_name) et/ou déplacer (parent_id, None = racine)."""
    folder_name: Optional[str] = None
    parent_id: Optional[int] = None


class FolderRead(BaseModel):
    model_config = {"from_attributes": True}

    id_folder: int
    id_org: int
    parent_id: Optional[int] = None
    folder_name: str
    depth: int
    path: str
    # Dossier système (`advertisers`) : nom affiché traduit par l'interface, ni renommable
    # ni déplaçable.
    system_key: Optional[str] = None
    child_count: int = 0   # sous-dossiers directs — enrichi par l'endpoint
    media_count: int = 0   # médias directement dans ce dossier — enrichi par l'endpoint
    # Chemin LISIBLE, ancêtres compris (« Video / Divers »). Sans lui, deux dossiers
    # homonymes places sous deux parents differents sont INDISCERNABLES partout ou l'on
    # affiche une liste a plat — c'est ce qui faisait dire « un des deux n'apparaît pas »
    # dans le tiroir de classement (plusieurs dossiers de même nom en pratique).
    full_path: str = ""
    created_at: datetime


class MediaMoveRequest(BaseModel):
    """Déplacer des médias vers un dossier (id_folder None = racine)."""
    id_media: list[int]
    id_folder: Optional[int] = None


# ── Quarantaine / kill-switch (§ Q5) ─────────────────────────────────────────────

class MediaQuarantineRequest(BaseModel):
    """Body pour POST /media/{id}/quarantine — raison tracée dans l'audit."""
    reason: Optional[str] = None


class MediaQuarantineResult(BaseModel):
    id_media: int
    is_blocked: bool
    block_reason: Optional[str] = None
    blocked_at: Optional[datetime] = None


# ── Documents multipages (§3.2) ─────────────────────────────────────────────────

class DocumentPageRead(BaseModel):
    """Page convertie d'un document (aperçu médiathèque / éditeur de playlist)."""
    page: int                 # 1-based
    url: str                  # URL signée (TTL court — aperçu, pas diffusion)
    width: Optional[int] = None
    height: Optional[int] = None


class DocumentPagesResult(BaseModel):
    id_media: int
    page_count: int
    truncated: bool = False   # rendu plafonné (document > DOC_MAX_PAGES)
    pages: list[DocumentPageRead] = []
