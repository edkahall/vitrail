"""Schémas Feed Display (§XXV.3) — RSS/Atom.

Un feed = source pollable (`cms_datafeeds`, porte le display_mode/config) + un média pointeur
(`cms_media` `feed_display`, metadata={id_feed}) schedulable comme un contenu normal.

Modes compatibles RSS/Atom (§25.3) : tranche 1 = ticker + card_carousel ; tranche 2 = card_grid,
full_item, split_layout, compact_list. Les modes social_wall / video_feed / data_chart ne
s'appliquent pas aux flux RSS/Atom (ils visent les feed_types socials/financiers — feed-proxy).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

FeedType = Literal[
    "rss", "atom", "google_news", "youtube_channel",
    # ── servis par signflow-feed-proxy (§25.3) ──
    "instagram", "facebook_page", "twitter_x", "linkedin", "tiktok",
    "crypto", "financial", "weather_widget",
]
DisplayMode = Literal[
    "ticker", "card_carousel", "card_grid", "full_item", "split_layout", "compact_list",
    "video_feed", "social_wall", "data_chart",
]

# video_feed = grille de vignettes vidéo (poster + badge ▶ + titre). N'a de sens que pour une
# chaîne YouTube (le flux RSS de chaîne porte media:thumbnail = la vignette de chaque vidéo, et
# `link` = la page watch). On ne LIT pas la vidéo (YouTube non framable) → on affiche les
# dernières vidéos. Réservé au feed_type youtube_channel.
VIDEO_MODES: tuple[str, ...] = ("video_feed",)

# Modes d'affichage SANS image. Google News (RSS) ne fournit jamais d'image par article
# (aucun media_thumbnail/media_content/enclosure dans le flux) → on n'autorise que ces modes
# pour ce feed_type (sinon cartes à trou gris). Les RSS/Atom génériques restent libres
# (présence d'images = dépend de la source, connue seulement après le 1er fetch).
IMAGELESS_MODES: tuple[str, ...] = ("ticker", "compact_list")

# ── Feeds servis par signflow-feed-proxy (§25.3) ─────────────────────────────
# Le worker route ces feed_types vers le microservice (au lieu de fetch+feedparser direct).
PROXY_TYPES: tuple[str, ...] = (
    "instagram", "facebook_page", "twitter_x", "linkedin", "tiktok",
    "crypto", "financial", "weather_widget",
)
# Socials propriétaires : requièrent un token OAuth (Vault) côté proxy. Sans credentials, le proxy
# répond `needs_credentials` → 0 item, dégradation gracieuse (les items existants restent affichés).
SOCIAL_OAUTH_TYPES: tuple[str, ...] = ("instagram", "facebook_page", "twitter_x", "linkedin", "tiktok")
# Connecteurs PUBLICS (aucun compte/clé) : testables immédiatement.
PUBLIC_PROXY_TYPES: tuple[str, ...] = ("crypto", "financial", "weather_widget")

# Modes réservés (gardes dédiés, comme VIDEO_MODES) :
SOCIAL_WALL_TYPES: tuple[str, ...] = ("instagram", "facebook_page", "twitter_x", "linkedin", "tiktok")
DATA_CHART_TYPES: tuple[str, ...] = ("crypto", "financial", "weather_widget")

# Matrice de compatibilité display_mode × feed_type (§25.3) — définie pour les types PROXY
# seulement ; rss/atom/google_news/youtube gardent leurs validateurs existants.
PROXY_MODE_COMPAT: dict[str, tuple[str, ...]] = {
    "instagram":     ("card_grid", "card_carousel", "full_item", "split_layout", "social_wall"),
    "facebook_page": ("card_grid", "card_carousel", "full_item", "split_layout", "social_wall"),
    "twitter_x":     ("ticker", "card_grid", "card_carousel", "full_item", "compact_list", "social_wall"),
    "linkedin":      ("ticker", "card_grid", "card_carousel", "full_item", "compact_list", "social_wall"),
    "tiktok":        ("card_grid", "card_carousel", "compact_list", "social_wall"),
    "crypto":        ("ticker", "card_grid", "compact_list", "data_chart"),
    "financial":     ("ticker", "card_grid", "compact_list", "data_chart"),
    "weather_widget": ("ticker", "card_grid", "compact_list", "data_chart"),
}


def mode_allowed(feed_type: str, display_mode: str) -> bool:
    """Compatibilité mode × type pour les feeds proxy + modes réservés social_wall/data_chart.
    (google_news/video_feed gardent leurs validateurs dédiés.) True pour les cas non régis ici."""
    if display_mode == "social_wall":
        return feed_type in SOCIAL_WALL_TYPES
    if display_mode == "data_chart":
        return feed_type in DATA_CHART_TYPES
    if feed_type in PROXY_MODE_COMPAT:
        return display_mode in PROXY_MODE_COMPAT[feed_type]
    return True


def mode_error(feed_type: str, display_mode: str) -> str:
    if display_mode == "social_wall":
        return ("Social wall mode (social_wall) is only available for a social feed "
                "(Instagram, Facebook, X, LinkedIn, TikTok).")
    if display_mode == "data_chart":
        return "Chart mode (data_chart) is only available for a crypto, financial or weather feed."
    return f"Display mode \"{display_mode}\" is not compatible with this feed type."


class FeedDisplayConfig(BaseModel):
    """Réglages visuels (union de tous les modes — chaque mode lit ce qui le concerne)."""

    # carrousel / grille / full / split (rotation)
    item_duration_s: int = Field(default=8, ge=2, le=120)
    transition: Literal["slide", "fade"] = "slide"
    show_image: bool = True
    show_date: bool = True
    show_description: bool = True       # full_item, split_layout
    # grille
    grid_columns: int = Field(default=3, ge=1, le=6)
    grid_rows: int = Field(default=2, ge=1, le=4)
    # split
    split_side: Literal["left", "right"] = "left"   # côté de l'image
    # ticker
    scroll_speed_px_s: int = Field(default=80, ge=10, le=600)
    separator: str = " ◆ "
    # video_feed : badge ▶ sur les vignettes (repère « vidéo »). Optionnel car il peut laisser
    # croire à tort que c'est cliquable (le signage est passif).
    show_play_icon: bool = True
    # social_wall : mosaïque de posts (réutilise grid_columns/grid_rows) + légende tronquée.
    caption_max_chars: int = Field(default=140, ge=0, le=400)
    # data_chart : type de graphique + affichage de la variation.
    chart_type: Literal["bars", "line", "gauge"] = "bars"
    show_change: bool = True
    # commun
    show_source: bool = True


class FeedConfig(BaseModel):
    """Config de la source. RSS/Atom/YouTube → `url`. Feeds proxy (socials/financiers/météo) →
    paramètres dédiés (account, query, coins, location…), résolus par signflow-feed-proxy."""

    url: Optional[str] = None
    timeout_s: int = Field(default=10, ge=2, le=60)
    # ── paramètres des connecteurs proxy (§25.3) ──
    account: Optional[str] = None          # socials : @handle, page
    query: Optional[str] = None            # financial : symbole(s)
    coins: Optional[list[str]] = None      # crypto : ['bitcoin','ethereum']
    currency: Optional[str] = None         # crypto/financial : EUR, USD…
    location: Optional[str] = None         # weather : nom de ville
    lat: Optional[float] = None            # weather : latitude
    lon: Optional[float] = None            # weather : longitude
    provider: Optional[str] = None         # ex. coingecko, open-meteo
    country: Optional[str] = None
    lang: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v and not (v.lower().startswith("http://") or v.lower().startswith("https://")):
            raise ValueError("The feed URL must start with http:// or https://")
        return v


class FeedCreate(BaseModel):
    feed_name: str = Field(min_length=1, max_length=255)
    feed_type: FeedType = "rss"
    config: FeedConfig
    polling_interval_s: int = Field(default=900, ge=30, le=86400)
    display_mode: DisplayMode = "ticker"
    display_config: FeedDisplayConfig = Field(default_factory=FeedDisplayConfig)
    max_items: int = Field(default=10, ge=1, le=50)
    moderation_enabled: bool = False
    ai_summary_enabled: bool = False

    @model_validator(mode="after")
    def _google_news_imageless(self) -> "FeedCreate":
        if self.feed_type == "google_news" and self.display_mode not in IMAGELESS_MODES:
            raise ValueError(
                "The Google News type provides no images: choose a mode without "
                "images (scrolling ticker or compact list)."
            )
        return self

    @model_validator(mode="after")
    def _video_feed_youtube_only(self) -> "FeedCreate":
        if self.display_mode in VIDEO_MODES and self.feed_type != "youtube_channel":
            raise ValueError(
                "Video mode (video_feed) is only available for a YouTube channel."
            )
        return self

    @model_validator(mode="after")
    def _proxy_modes_compatible(self) -> "FeedCreate":
        if not mode_allowed(self.feed_type, self.display_mode):
            raise ValueError(mode_error(self.feed_type, self.display_mode))
        return self


class FeedUpdate(BaseModel):
    feed_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    feed_type: Optional[FeedType] = None
    config: Optional[FeedConfig] = None
    polling_interval_s: Optional[int] = Field(default=None, ge=30, le=86400)
    display_mode: Optional[DisplayMode] = None
    display_config: Optional[FeedDisplayConfig] = None
    max_items: Optional[int] = Field(default=None, ge=1, le=50)
    moderation_enabled: Optional[bool] = None
    ai_summary_enabled: Optional[bool] = None
    status: Optional[Literal["active", "paused"]] = None


class FeedModerateRequest(BaseModel):
    """Modération §XXI : approuver / rejeter un lot d'items d'un feed."""

    item_ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["approve", "reject"]


class FeedItemRead(BaseModel):
    id_item: int
    title: Optional[str] = None
    description: Optional[str] = None
    summary_ai: Optional[str] = None
    image_url: Optional[str] = None
    link_url: Optional[str] = None
    author: Optional[str] = None
    value_num: Optional[float] = None
    value_unit: Optional[str] = None
    change_pct: Optional[float] = None
    published_at: Optional[datetime] = None
    moderation_status: str


class FeedRead(BaseModel):
    id_feed: int
    id_media: Optional[int] = None          # média feed_display schedulable associé
    feed_name: str
    feed_type: str
    url: Optional[str] = None
    config: dict = {}                       # source brute (account/coins/location… pour les proxy)
    polling_interval_s: int
    display_mode: Optional[str] = None
    display_config: FeedDisplayConfig
    max_items: int
    moderation_enabled: bool
    ai_summary_enabled: bool = False
    status: str
    last_fetched_at: Optional[datetime] = None
    last_error: Optional[str] = None
    item_count: int = 0
    created_at: datetime
    updated_at: datetime
