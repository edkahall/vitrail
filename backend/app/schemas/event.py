from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, field_validator, model_validator

from app.core.timezones import safe_tz_name, validate_tz_or_raise

# Cibles d'event réellement honorées par `targeting.event_target_matches`.
# Doit rester synchronisé avec ce matcher : un type listé ici mais absent du
# matcher = event silencieusement non diffusé.
EVENT_TARGET_TYPES = {"player", "group", "site", "site_group", "org"}

# Types admis DANS la forme hétérogène {"targets": [...]} (2026-07-30). `org` en est
# exclu : « tout le parc » absorbe toute autre entrée — l'union deviendrait un org
# déguisé, illisible à l'écran et refusé par la gouvernance pour d'autres raisons.
# Un event org s'exprime par la forme héritée {"target_type": "org"}.
EVENT_MULTI_TARGET_TYPES = {"player", "group", "site", "site_group"}


def _check_target_config(tc: dict) -> None:
    """Garde-fou commun création/mise à jour — les DEUX formes de target_config.

    Sans lui, un target_type non géré par `targeting.event_target_matches` était accepté
    puis ignoré à la diffusion, sans erreur (même bug que les cibles de schedule, audit
    2026-07-18). Tout nouveau type s'ajoute au catalogue ci-dessus **et** dans le matcher.
    """
    if "targets" in tc:
        if "target_type" in tc:
            # Les deux formes dans le même document : impossible de savoir laquelle fait
            # foi — refuser l'ambiguïté plutôt que d'en préférer une en silence.
            raise ValueError(
                "ambiguous target_config: 'targets' and 'target_type' are mutually exclusive"
            )
        entries = tc.get("targets")
        if not isinstance(entries, list) or not entries:
            raise ValueError("target_config.targets must be a non-empty list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("target_config.targets: each entry must be an object")
            e_type = entry.get("target_type", "player")
            if e_type not in EVENT_MULTI_TARGET_TYPES:
                raise ValueError(
                    f"invalid target_type in targets: {e_type!r} "
                    f"(expected: {', '.join(sorted(EVENT_MULTI_TARGET_TYPES))}; "
                    "'org' is expressed with the target_type-only form)"
                )
            ids = entry.get("target_ids")
            if not isinstance(ids, list) or not ids:
                # Une entrée sans identifiant ne vise personne : dans un mélange, c'est
                # forcément un geste raté — le nommer plutôt que de l'avaler.
                raise ValueError(
                    f"target_config.targets: entry {e_type!r} has no target_ids"
                )
        return
    t_type = tc.get("target_type", "player")
    if t_type not in EVENT_TARGET_TYPES:
        raise ValueError(
            f"invalid target_type: {t_type!r} "
            f"(expected: {', '.join(sorted(EVENT_TARGET_TYPES))})"
        )


class EventCreate(BaseModel):
    event_name: str
    # manual | scheduled | conditional | ai_predicted
    event_type: str
    # content (playlist/media visuel) | audio (panier audio §XIX)
    event_kind: str = "content"
    # Soit id_playlist, soit id_media est obligatoire (content) ; id_basket (audio)
    id_playlist: Optional[int] = None
    id_media: Optional[int] = None
    id_basket: Optional[int] = None
    mix_ratio: Optional[int] = None
    priority: int = 70
    duration_ms: Optional[int] = None
    revert_on_end: bool = True
    # Plage de validité (optionnelle)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    # Si True : l'event repasse en inactive après chaque expiration
    is_recurring: bool = False
    # Si True : coupe le contenu à l'heure exacte. Si False : attend la fin de l'item en cours.
    is_preemptive: bool = False
    # Si True : garde les écrans des players ciblés allumés tant que l'event est actif (auto-power lot 2)
    keep_screens_on: bool = False
    # Embargo UTC : plancher absolu indépendant de SIGNFLOW_TZ (lancement mondial)
    not_before_utc: Optional[datetime] = None
    # Fuseau de RÉFÉRENCE des heures du trigger_config (P3.13). None = SIGNFLOW_TZ.
    # ⚠️ Ne concerne PAS `not_before_utc` : l'embargo est un instant absolu, par conception.
    timezone: Optional[str] = None
    # Expression logique AND/OR (§XX.4) — None pour event_type=manual
    trigger_config: Optional[dict[str, Any]] = None
    # {"target_type": "player"|"group"|"site"|"site_group"|"org", "target_ids": [...]}
    target_config: dict[str, Any]
    # Gouvernance §XVIII (migr 0112) : nœud propriétaire. None = événement d'organisation.
    # Non NULL → les cibles doivent tenir dans le périmètre du nœud.
    id_governance_node: Optional[int] = None

    _check_tz = field_validator("timezone")(validate_tz_or_raise)

    @model_validator(mode="after")
    def check_target(self) -> "EventCreate":
        if self.event_kind == "audio":
            if self.id_basket is None:
                raise ValueError("id_basket is required for an audio event")
        elif self.id_playlist is None and self.id_media is None:
            raise ValueError("id_playlist or id_media is required")
        _check_target_config(self.target_config or {})
        return self


class EventUpdate(BaseModel):
    event_name: Optional[str] = None
    id_playlist: Optional[int] = None
    id_media: Optional[int] = None
    id_basket: Optional[int] = None
    mix_ratio: Optional[int] = None
    priority: Optional[int] = None
    duration_ms: Optional[int] = None
    revert_on_end: Optional[bool] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_recurring: Optional[bool] = None
    is_preemptive: Optional[bool] = None
    keep_screens_on: Optional[bool] = None
    not_before_utc: Optional[datetime] = None
    timezone: Optional[str] = None
    trigger_config: Optional[dict[str, Any]] = None
    target_config: Optional[dict[str, Any]] = None
    # `model_fields_set` distingue « non fourni » de « détaché (null) » : le second est
    # reservé à un admin d'un nœud PARENT (N2).
    id_governance_node: Optional[int] = None

    _check_tz = field_validator("timezone")(validate_tz_or_raise)

    @model_validator(mode="after")
    def check_target(self) -> "EventUpdate":
        # Même garde-fou qu'à la création : une mise à jour ne doit pas pouvoir
        # réintroduire un target_type que le matcher ignore.
        if self.target_config is not None:
            _check_target_config(self.target_config)
        return self


class EventRead(BaseModel):
    model_config = {"from_attributes": True}

    id_event: int
    id_org: int
    event_name: str
    event_type: str
    event_kind: str = "content"
    id_playlist: Optional[int] = None
    # Enrichi par les endpoints liste/détail pour l'affichage
    playlist_name: Optional[str] = None
    id_media: Optional[int] = None
    media_name: Optional[str] = None
    id_governance_node: Optional[int] = None
    id_basket: Optional[int] = None
    basket_name: Optional[str] = None
    mix_ratio: Optional[int] = None
    priority: int
    duration_ms: Optional[int] = None
    revert_on_end: bool
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_recurring: bool = False
    is_preemptive: bool = False
    keep_screens_on: bool = False
    not_before_utc: Optional[datetime] = None
    # Toujours servi RÉSOLU (jamais None) : l'écran doit pouvoir dire dans quel fuseau se
    # lisent les heures, sans avoir à connaître le SIGNFLOW_TZ du serveur.
    timezone: str
    status: str
    trigger_config: Optional[dict[str, Any]] = None
    target_config: dict[str, Any]
    activated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_by: Optional[int] = None
    # `mode="before"` : la résolution doit avoir lieu AVANT la validation du type, sinon un
    # `timezone` NULL en base (le cas de tous les événements antérieurs à la migr 0123) ferait
    # échouer la sérialisation de chaque endpoint qui rend un événement. Posé sur le schéma et
    # pas dans `_enrich_event` : il existe une dizaine de chemins qui construisent un EventRead.
    _resolve_tz = field_validator("timezone", mode="before")(
        lambda v: safe_tz_name(v)  # noqa: E731
    )
    created_at: datetime
    updated_at: datetime


class EventStackItem(BaseModel):
    """Élément de la stack d'events actifs d'un player (§XX.5)."""

    model_config = {"from_attributes": True}

    id_event: int
    event_name: str
    event_type: str
    id_playlist: Optional[int] = None
    id_media: Optional[int] = None
    priority: int
    status: str
    activated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class EventAiSuggestionRead(BaseModel):
    model_config = {"from_attributes": True}

    id_suggestion: int
    id_org: int
    trigger_summary: str
    suggested_config: dict[str, Any]
    id_playlist: Optional[int] = None
    confidence: Decimal
    based_on: Optional[str] = None
    status: str
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    id_event_created: Optional[int] = None
    created_at: datetime
