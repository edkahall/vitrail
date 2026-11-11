"""Actions agentiques de l'AI Assistant (§XXVII v2) — écriture avec CONFIRMATION.

Modèle de sécurité : **le LLM ne mute JAMAIS directement**. Il appelle un outil `propose_*`
qui se contente de RÉSOUDRE la demande en langage naturel en une action concrète et validée
(noms→ids, RBAC, RLS, désambiguïsation) et renvoie une **proposition** + un **token signé**.
L'écriture réelle n'a lieu que lorsque l'utilisateur clique « Valider » → `execute_action`
(re-vérifie le token, le RBAC, puis réutilise les handlers existants : staging + publish +
push sync + conformité restent identiques au back-office) → audit `cms_audit_log`.

Whitelist STRICTE (cf. `ACTION_PERMISSIONS`) : items de playlist (ajout/retrait/tag),
création de playlist/feed, pistes audio, création d'événement manuel, et retrait en masse
d'un feed des playlists qui l'utilisent. Toute autre action est rejetée. « Validité
toujours » = item sans règle (= perpétuel par défaut).

⚠️ Zones : l'UI numérote les zones À PARTIR DE 1 (`zone_index + 1`). L'utilisateur dit
« zone 1 » → `zone_index = 0`. Le resolver fait le mapping et la proposition réaffiche
« Zone 1 » pour lever toute ambiguïté.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import date as _date
from datetime import time as _time

import redis.asyncio as aioredis
from jose import JWTError, jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit_log import CmsAuditLog
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.user import CmsUser
from app.services import rbac

logger = logging.getLogger(__name__)

ACTION_TTL_S = 600  # un token de proposition vaut 10 min
_TOKEN_TYPE = "assistant_action"
_BATCH_TOKEN_TYPE = "assistant_batch"

# Whitelist : action_type → permission RBAC requise (module, operation).
ACTION_PERMISSIONS: dict[str, tuple[str, str]] = {
    "add_playlist_item": ("playlists", "edit"),
    "remove_playlist_item": ("playlists", "edit"),
    "create_playlist": ("playlists", "create"),
    "add_tagged_content_to_playlist": ("playlists", "edit"),
    "create_feed": ("media", "create"),
    "add_audio_track": ("audio_baskets", "configure"),
    "remove_audio_track": ("audio_baskets", "configure"),
    "create_event": ("events", "create"),
    "remove_feed_from_active_playlists": ("playlists", "edit"),
    "play_vod": ("vod", "trigger"),
    "rename_player": ("players", "edit"),
    "create_schedule": ("schedules", "create"),
    "delete_schedule": ("schedules", "delete"),
    "update_schedule": ("schedules", "edit"),
    "add_schedule_target": ("schedules", "edit"),
    "remove_schedule_target": ("schedules", "edit"),
    "set_playlist_item_rules": ("playlists", "edit"),
    "tag_media": ("media", "edit"),
    "move_media_to_folder": ("media", "edit"),
    "assign_basket_to_players": ("audio_baskets", "configure"),
    "unassign_basket_from_players": ("audio_baskets", "configure"),
    "configure_audio_basket": ("audio_baskets", "configure"),
    # Commandes players (Phase P.2) — gate RÉEL = org-admin (cf. ACTION_REQUIRES_ORG_ADMIN) ;
    # le tuple n'est qu'un repli de whitelist, jamais utilisé pour ces actions.
    "restart_player_app": ("players", "read"),
    "reboot_player": ("players", "read"),
    "force_resync": ("players", "read"),
    "player_screenshot": ("players", "read"),
    "toggle_overlay": ("players", "read"),
    "set_maintenance": ("players", "read"),
    # Écran physique RS232/IP (gate org-admin) — placeholder, gate réel via le set ci-dessous.
    "screen_power": ("players", "read"),
    "screen_set_input": ("players", "read"),
    # Switch entrée HDMI + fuseau device (gate réel org-admin, cf. set ci-dessous).
    "hdmi_switch": ("players", "read"),
    "set_player_timezone": ("players", "read"),
    # Mise à jour OTA §XXX (gate réel org-admin + PIN par messagerie, cf. sets ci-dessous).
    "update_players": ("players", "read"),
}

# Commandes players : action_type → (commande Redis, libellé, avertissement éventuel).
_COMMAND_META: dict[str, dict] = {
    "restart_player_app": {"cmd": "restart", "verb": "Restart the application",
                           "warn": "The application reloads (brief black screen)."},
    "reboot_player": {"cmd": "reboot_os", "verb": "Restart the device (OS reboot)",
                      "warn": "Hardware reboot: screens will be unavailable for ~1–2 min."},
    "force_resync": {"cmd": "force_sync", "verb": "Force a content resync",
                     "warn": None},
    "player_screenshot": {"cmd": "screenshot", "verb": "Take a screenshot",
                          "warn": None},
    "toggle_overlay": {"cmd": "toggle_overlay",
                       "verb": "Show/hide the on-screen diagnostic info", "warn": None},
    "set_maintenance": {"cmd": "maintenance", "verb": "Show the maintenance screen",
                        "warn": "Content will resume at the next resync."},
}
PLAYER_COMMANDS = {a: m["cmd"] for a, m in _COMMAND_META.items()}

# Actions sensibles réservées à l'org-admin (au lieu d'une permission module:op).
ACTION_REQUIRES_ORG_ADMIN: set[str] = set(_COMMAND_META) | {
    "screen_power", "screen_set_input", "hdmi_switch", "set_player_timezone",
    "update_players",
}

# Actions exigeant le PIN par MESSAGERIE (Jeanine, migr 0090) — 2e facteur du
# « OUI » : un compte Telegram n'est pas une auth forte. Sans effet sur le widget
# CMS (session authentifiée). Périmètre validé par Ed 2026-07-13 (ce qui peut
# COUPER/perdre un écran, détruire de la donnée ou décaler les horaires) ; les
# actions OTA vont d'office ici (décision Ed : OTA agent gaté PIN).
PIN_REQUIRED_ACTIONS: set[str] = {
    "reboot_player",
    "restart_player_app",
    "screen_power",
    "screen_set_input",
    "set_maintenance",
    "set_player_timezone",
    "delete_schedule",
    "remove_schedule_target",
    "update_players",
}


async def is_org_admin(db: AsyncSession, id_user: int) -> bool:
    keys = await rbac.get_user_role_keys(db, id_user)
    return bool({"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(keys))


async def check_action_permission(db: AsyncSession, id_user: int, action_type: str) -> bool:
    """Autorise une action : whitelist + (org-admin pour les actions sensibles, sinon
    permission module:op RBAC)."""
    if action_type not in ACTION_PERMISSIONS:
        return False
    if action_type in ACTION_REQUIRES_ORG_ADMIN:
        return await is_org_admin(db, id_user)
    module, op = ACTION_PERMISSIONS[action_type]
    return await rbac.check_permission(db, id_user, module, op)


# ── Token signé (jose, secret JWT) ────────────────────────────────────────────

def sign_action(id_user: int, id_org: int, action_type: str, params: dict) -> str:
    payload = {
        "type": _TOKEN_TYPE,
        "uid": id_user,
        "org": id_org,
        "act": action_type,
        "params": params,
        "jti": secrets.token_urlsafe(16),  # anti-rejeu (single-use, cf. consume_action_token)
        "exp": int(time.time()) + ACTION_TTL_S,
    }
    return jwt.encode(payload, settings.CMS_JWT_SECRET, algorithm=settings.CMS_JWT_ALGORITHM)


def verify_action(token: str, user: CmsUser) -> dict | None:
    """Décode + valide un token d'action (signature, expiration, liaison user/org). None si KO."""
    try:
        payload = jwt.decode(
            token, settings.CMS_JWT_SECRET, algorithms=[settings.CMS_JWT_ALGORITHM]
        )
    except JWTError:
        return None
    if payload.get("type") != _TOKEN_TYPE:
        return None
    if payload.get("uid") != user.id_user or payload.get("org") != user.id_org:
        return None
    if payload.get("act") not in ACTION_PERMISSIONS:
        return None
    return payload


def sign_batch(id_user: int, id_org: int, steps: list[dict]) -> str:
    """Signe un LOT d'actions confirmé en un seul tour de conversation.

    `steps` = [{"act": action_type, "params": {...}}, …]. Un seul token couvre toutes les
    étapes → une seule confirmation côté utilisateur → exécution séquentielle avec publish
    différé (cf. `execute_batch`)."""
    payload = {
        "type": _BATCH_TOKEN_TYPE,
        "uid": id_user,
        "org": id_org,
        "steps": steps,
        "jti": secrets.token_urlsafe(16),  # anti-rejeu (single-use, cf. consume_action_token)
        "exp": int(time.time()) + ACTION_TTL_S,
    }
    return jwt.encode(payload, settings.CMS_JWT_SECRET, algorithm=settings.CMS_JWT_ALGORITHM)


def verify_batch(token: str, user: CmsUser) -> list[dict] | None:
    """Décode + valide un token de LOT. Renvoie la liste d'étapes, ou None si KO."""
    try:
        payload = jwt.decode(
            token, settings.CMS_JWT_SECRET, algorithms=[settings.CMS_JWT_ALGORITHM]
        )
    except JWTError:
        return None
    if payload.get("type") != _BATCH_TOKEN_TYPE:
        return None
    if payload.get("uid") != user.id_user or payload.get("org") != user.id_org:
        return None
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    for s in steps:
        if not isinstance(s, dict) or s.get("act") not in ACTION_PERMISSIONS:
            return None
    return steps


async def consume_action_token(token: str) -> bool:
    """Anti-rejeu (audit 2026-06-24) : marque le `jti` du token comme consommé (SET NX Redis,
    TTL = durée de vie restante). Renvoie True à la 1ʳᵉ utilisation, False si le token a déjà
    été exécuté → empêche de rejouer la même mutation dans la fenêtre du TTL. Les tokens sans
    `jti` (anciens) ne sont pas bloqués (compat)."""
    try:
        payload = jwt.decode(
            token, settings.CMS_JWT_SECRET, algorithms=[settings.CMS_JWT_ALGORITHM]
        )
    except JWTError:
        return False
    jti = payload.get("jti")
    if not jti:
        return True
    exp = payload.get("exp")
    ttl = max(1, int(exp) - int(time.time())) if exp else ACTION_TTL_S
    redis = aioredis.from_url(settings.REDIS_URL)
    try:
        created = await redis.set(f"signflow:assistant:jti:{jti}", "1", nx=True, ex=ttl)
        return bool(created)
    finally:
        await redis.aclose()


# ── Résolution de noms → entités (org-scopée via la session RLS) ──────────────

async def _resolve_playlist(db: AsyncSession, id_org: int, name: str):
    """Trouve UNE playlist par nom (exact insensible à la casse, puis partiel). Renvoie
    (playlist, error_dict)."""
    base = select(CmsPlaylist).where(
        CmsPlaylist.id_org == id_org,
        CmsPlaylist.is_deleted == False,  # noqa: E712
    )
    exact = (await db.execute(
        base.where(func.lower(CmsPlaylist.playlist_name) == name.lower())
    )).scalars().all()
    matches = exact or (
        await db.execute(base.where(CmsPlaylist.playlist_name.ilike(f"%{name}%")))
    ).scalars().all()
    if not matches:
        return None, {"error": f"No playlist named \"{name}\"."}
    if len(matches) > 1:
        return None, {
            "error": f"Several playlists match \"{name}\"",
            "candidates": [m.playlist_name for m in matches[:8]],
        }
    return matches[0], None


async def _resolve_media(db: AsyncSession, id_org: int, name: str):
    """Trouve UN média par nom (exact insensible à la casse, puis partiel). Renvoie
    (media, error_dict)."""
    base = select(CmsMedia).where(
        CmsMedia.id_org == id_org,
        CmsMedia.is_deleted == False,  # noqa: E712
    )
    exact = (await db.execute(
        base.where(func.lower(CmsMedia.media_name) == name.lower())
    )).scalars().all()
    matches = exact or (
        await db.execute(base.where(CmsMedia.media_name.ilike(f"%{name}%")))
    ).scalars().all()
    if not matches:
        return None, {"error": f"No media named \"{name}\"."}
    if len(matches) > 1:
        return None, {
            "error": f"Several media items match \"{name}\"",
            "candidates": [m.media_name for m in matches[:8]],
        }
    return matches[0], None


# ── Résolveurs d'action (langage naturel → proposition concrète) ──────────────

async def resolve_add_playlist_item(db: AsyncSession, id_org: int, params: dict) -> dict:
    pl, err = await _resolve_playlist(db, id_org, str(params.get("playlist", "")))
    if err:
        return err
    media, err = await _resolve_media(db, id_org, str(params.get("media", "")))
    if err:
        return err
    if media.media_type == "audio":
        return {"error": f"\"{media.media_name}\" is an audio file — for audio baskets only, "
                         "not standard playlists."}

    zone_human = int(params.get("zone") or 1)          # UI = 1-based
    zone_index = max(0, zone_human - 1)
    duration_ms = None
    if params.get("duration_seconds"):
        duration_ms = int(float(params["duration_seconds"]) * 1000)

    warnings = []
    if pl.draft_items is not None:
        warnings.append("An unpublished draft of this playlist will be published at the same time.")

    return {
        "action_type": "add_playlist_item",
        "params": {
            "id_playlist": pl.id_playlist,
            "id_media": media.id_media,
            "zone_index": zone_index,
            "duration_ms": duration_ms,
        },
        "description": (
            f"Add \"{media.media_name}\" to playlist \"{pl.playlist_name}\" "
            f"in zone {zone_human}, always valid."
        ),
        "warnings": warnings,
    }


async def resolve_remove_playlist_item(db: AsyncSession, id_org: int, params: dict) -> dict:
    pl, err = await _resolve_playlist(db, id_org, str(params.get("playlist", "")))
    if err:
        return err
    media, err = await _resolve_media(db, id_org, str(params.get("media", "")))
    if err:
        return err

    count = (await db.execute(
        select(func.count()).select_from(CmsPlaylistItem).where(
            CmsPlaylistItem.id_playlist == pl.id_playlist,
            CmsPlaylistItem.id_media == media.id_media,
        )
    )).scalar_one()
    if count == 0:
        return {"error": f"\"{media.media_name}\" is not in playlist "
                         f"\"{pl.playlist_name}\"."}

    return {
        "action_type": "remove_playlist_item",
        "params": {"id_playlist": pl.id_playlist, "id_media": media.id_media},
        "description": (
            f"Remove \"{media.media_name}\" from playlist \"{pl.playlist_name}\" "
            f"({count} occurrence{'s' if count > 1 else ''})."
        ),
        "warnings": [],
    }


async def _resolve_basket(db: AsyncSession, id_org: int, name: str):
    from app.models.audio_basket import CmsAudioBasket
    base = select(CmsAudioBasket).where(CmsAudioBasket.id_org == id_org)
    exact = (await db.execute(
        base.where(func.lower(CmsAudioBasket.basket_name) == name.lower())
    )).scalars().all()
    matches = exact or (
        await db.execute(base.where(CmsAudioBasket.basket_name.ilike(f"%{name}%")))
    ).scalars().all()
    if not matches:
        return None, {"error": f"No audio basket named \"{name}\"."}
    if len(matches) > 1:
        return None, {"error": f"Several baskets match \"{name}\"",
                      "candidates": [m.basket_name for m in matches[:8]]}
    return matches[0], None


async def resolve_create_playlist(db: AsyncSession, id_org: int, params: dict) -> dict:
    name = str(params.get("name", "")).strip()
    if not name:
        return {"error": "Give a playlist name."}
    exists = (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_org == id_org,
            func.lower(CmsPlaylist.playlist_name) == name.lower(),
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalars().first()
    warnings = ["A playlist with this name already exists (a second one will be created)."] if exists else []
    return {
        "action_type": "create_playlist",
        "params": {"name": name},
        "description": f"Create playlist \"{name}\".",
        "warnings": warnings,
    }


async def resolve_add_tagged_content_to_playlist(
    db: AsyncSession, id_org: int, params: dict
) -> dict:
    from app.models.tag import CmsMediaTagValue, CmsTagValue
    pl, err = await _resolve_playlist(db, id_org, str(params.get("playlist", "")))
    if err:
        return err
    tag = str(params.get("tag", "")).strip()
    if not tag:
        return {"error": "Specify the tag of the content to add."}

    # Médias (visuels, non audio) portant un tag dont la valeur correspond.
    rows = (await db.execute(
        select(CmsMedia.id_media, CmsMedia.media_name)
        .join(CmsMediaTagValue, CmsMediaTagValue.id_media == CmsMedia.id_media)
        .join(CmsTagValue, CmsTagValue.id_tag == CmsMediaTagValue.id_tag)
        .where(
            CmsMedia.id_org == id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
            CmsMedia.media_type != "audio",
            func.lower(CmsTagValue.tag_value).ilike(f"%{tag.lower()}%"),
        ).distinct()
    )).all()
    if not rows:
        return {"error": f"No visual content tagged \"{tag}\"."}
    ids = [r.id_media for r in rows]
    names = [r.media_name for r in rows]
    return {
        "action_type": "add_tagged_content_to_playlist",
        "params": {"id_playlist": pl.id_playlist, "id_medias": ids},
        "description": (
            f"Add {len(ids)} item(s) tagged \"{tag}\" to playlist "
            f"\"{pl.playlist_name}\": {', '.join(names[:5])}"
            f"{'…' if len(names) > 5 else ''}."
        ),
        "warnings": [],
    }


async def resolve_create_feed(db: AsyncSession, id_org: int, params: dict) -> dict:
    from app.services.assistant_feed_catalog import find_source
    source = str(params.get("source", "")).strip()
    url = str(params.get("url", "")).strip()
    name = str(params.get("name", "")).strip()

    entry = find_source(source) if source else None
    if entry:
        url = url or entry["url"]
        name = name or entry["name"]
    if not url:
        return {"error": "Source not in the catalog. Give the exact RSS URL of the feed."}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "The feed URL must start with http:// or https://."}
    name = name or (source or "RSS feed")
    mode = str(params.get("display_mode") or "ticker")

    return {
        "action_type": "create_feed",
        "params": {"feed_name": name, "url": url, "display_mode": mode},
        "description": f"Create feed \"{name}\" (RSS: {url}), display \"{mode}\".",
        "warnings": [],
    }


async def resolve_add_audio_track(db: AsyncSession, id_org: int, params: dict) -> dict:
    basket, err = await _resolve_basket(db, id_org, str(params.get("basket", "")))
    if err:
        return err
    media, err = await _resolve_media(db, id_org, str(params.get("media", "")))
    if err:
        return err
    if media.media_type != "audio":
        return {"error": f"\"{media.media_name}\" is not an audio file."}
    return {
        "action_type": "add_audio_track",
        "params": {"id_basket": basket.id_basket, "id_media": media.id_media},
        "description": f"Add track \"{media.media_name}\" to audio basket "
                       f"\"{basket.basket_name}\".",
        "warnings": [],
    }


async def resolve_remove_audio_track(db: AsyncSession, id_org: int, params: dict) -> dict:
    from app.models.audio_basket import CmsAudioBasketItem
    basket, err = await _resolve_basket(db, id_org, str(params.get("basket", "")))
    if err:
        return err
    media, err = await _resolve_media(db, id_org, str(params.get("media", "")))
    if err:
        return err
    count = (await db.execute(
        select(func.count()).select_from(CmsAudioBasketItem).where(
            CmsAudioBasketItem.id_basket == basket.id_basket,
            CmsAudioBasketItem.id_media == media.id_media,
        )
    )).scalar_one()
    if count == 0:
        return {"error": f"\"{media.media_name}\" is not in basket "
                         f"\"{basket.basket_name}\"."}
    return {
        "action_type": "remove_audio_track",
        "params": {"id_basket": basket.id_basket, "id_media": media.id_media},
        "description": f"Remove track \"{media.media_name}\" from audio basket "
                       f"\"{basket.basket_name}\".",
        "warnings": [],
    }


async def _resolve_feed_media(db: AsyncSession, id_org: int, name: str):
    """Trouve UN média de type `feed_display` (= un feed/page d'actualité) par nom."""
    base = select(CmsMedia).where(
        CmsMedia.id_org == id_org,
        CmsMedia.is_deleted == False,  # noqa: E712
        CmsMedia.media_type == "feed_display",
    )
    exact = (await db.execute(
        base.where(func.lower(CmsMedia.media_name) == name.lower())
    )).scalars().all()
    matches = exact or (
        await db.execute(base.where(CmsMedia.media_name.ilike(f"%{name}%")))
    ).scalars().all()
    if not matches:
        return None, {"error": f"No feed named \"{name}\"."}
    if len(matches) > 1:
        return None, {"error": f"Several feeds match \"{name}\"",
                      "candidates": [m.media_name for m in matches[:8]]}
    return matches[0], None


async def _resolve_target_config(
    db: AsyncSession, id_org: int, target: str
) -> tuple[dict | None, dict | None]:
    """Résout une cible en langage naturel → target_config (§XX.7). Vide / « tous » → org
    entière ; sinon tente un groupe de players, puis un player. (target_config, error)."""
    target = (target or "").strip()
    if not target or target.lower() in ("all", "tous", "tout", "org", "everyone", "toute l'org"):
        return {"target_type": "org", "target_ids": []}, None

    from app.models.player import CmsPlayer
    from app.models.player_group import CmsPlayerGroup

    grp = (await db.execute(
        select(CmsPlayerGroup).where(
            CmsPlayerGroup.id_org == id_org,
            CmsPlayerGroup.group_name.ilike(f"%{target}%"),
        )
    )).scalars().all()
    if len(grp) == 1:
        return {"target_type": "group", "target_ids": [grp[0].id_group]}, None
    if len(grp) > 1:
        return None, {"error": f"Several groups match \"{target}\"",
                      "candidates": [g.group_name for g in grp[:8]]}

    players = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_org == id_org,
            CmsPlayer.player_name.ilike(f"%{target}%"),
        )
    )).scalars().all()
    if len(players) == 1:
        return {"target_type": "player", "target_ids": [players[0].id_player]}, None
    if len(players) > 1:
        return None, {"error": f"Several players match \"{target}\"",
                      "candidates": [p.player_name for p in players[:8]]}
    return None, {"error": f"No group or player named \"{target}\"."}


async def resolve_create_event(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare la création d'un événement MANUEL (override déclenché à la demande) diffusant
    une playlist OU un média visuel, ciblant l'org entière par défaut (ou un groupe/player)."""
    name = str(params.get("name", "")).strip()
    if not name:
        return {"error": "Give an event name."}
    content = str(params.get("content", "")).strip()
    if not content:
        return {"error": "Specify the event content (a playlist or a media item)."}

    # Le contenu est une playlist en priorité, sinon un média visuel.
    pl, pl_err = await _resolve_playlist(db, id_org, content)
    id_playlist = id_media = None
    content_label = ""
    if pl is not None:
        id_playlist = pl.id_playlist
        content_label = f"playlist \"{pl.playlist_name}\""
    elif pl_err and pl_err.get("candidates"):
        return pl_err  # plusieurs playlists → désambiguïse
    else:
        media, m_err = await _resolve_media(db, id_org, content)
        if media is None:
            if m_err and m_err.get("candidates"):
                return m_err
            return {"error": f"No playlist or media named \"{content}\"."}
        if media.media_type == "audio":
            return {"error": f"\"{media.media_name}\" is an audio file — a content event "
                             "needs a playlist or a visual media item."}
        id_media = media.id_media
        content_label = f"media \"{media.media_name}\""

    target_config, terr = await _resolve_target_config(db, id_org, str(params.get("target", "")))
    if terr:
        return terr

    duration_ms = None
    dur_label = "no end time (manual revert)"
    if params.get("duration_seconds"):
        secs = float(params["duration_seconds"])
        duration_ms = int(secs * 1000)
        dur_label = f"duration {int(secs)} s"

    # « Garder les écrans allumés » (auto-power heures d'ouverture) : l'event maintient
    # l'affichage des players ciblés hors horaires (ex. « on ferme à 23h ce soir »).
    keep_screens_on = params.get("keep_screens_on") is True

    tgt = target_config["target_type"]
    target_label = {"org": "the whole organization"}.get(tgt, f"target {tgt}")
    return {
        "action_type": "create_event",
        "params": {
            "event_name": name,
            "id_playlist": id_playlist,
            "id_media": id_media,
            "target_config": target_config,
            "duration_ms": duration_ms,
            "keep_screens_on": keep_screens_on,
        },
        "description": (
            f"Create manual event \"{name}\" playing {content_label} on "
            f"{target_label}, {dur_label}"
            + (", screens KEPT ON during the event" if keep_screens_on else "")
            + ". It will be created INACTIVE — trigger it from Events."
        ),
        "warnings": [],
    }


async def resolve_remove_feed_from_active_playlists(
    db: AsyncSession, id_org: int, params: dict
) -> dict:
    """Prépare le retrait d'un feed de TOUTES les playlists qui l'utilisent, avec un récap
    d'impact (état de diffusion par playlist) à confirmer (action en masse, Tier 3)."""
    name = str(params.get("feed", "") or params.get("media", "")).strip()
    if not name:
        return {"error": "Specify the feed to remove."}
    media, err = await _resolve_feed_media(db, id_org, name)
    if err:
        return err

    from app.services import media_deletion
    impact = await media_deletion.analyze(db, media)
    playlists = impact.get("playlists", [])
    if not playlists:
        return {"error": f"Feed \"{media.media_name}\" is not in any playlist."}

    id_playlists = [p["id_playlist"] for p in playlists]
    live = [p for p in playlists if p.get("broadcast") == "active"]
    emptied = [p for p in playlists if p.get("will_be_deleted")]
    warnings = []
    if live:
        warnings.append(
            f"{len(live)} playlist(s) are ON AIR right now: "
            f"{', '.join(p['playlist_name'] for p in live)} — screens will change."
        )
    if emptied:
        warnings.append(
            f"{len(emptied)} playlist(s) will be left with no content: "
            f"{', '.join(p['playlist_name'] for p in emptied)}."
        )
    listing = ", ".join(f"{p['playlist_name']} ({p.get('broadcast', 'inactive')})"
                        for p in playlists[:6])
    return {
        "action_type": "remove_feed_from_active_playlists",
        "params": {
            "id_media": media.id_media,
            "id_playlists": id_playlists,
            "feed_name": media.media_name,
        },
        "description": (
            f"Remove feed \"{media.media_name}\" from {len(id_playlists)} playlist(s): "
            f"{listing}{'…' if len(playlists) > 6 else ''}."
        ),
        "warnings": warnings,
    }


async def _resolve_player_command_target(db: AsyncSession, id_org: int, params: dict):
    """Résout la cible d'une commande (player / groupe / « tous ») → (ids, noms, label, error)."""
    target = str(params.get("target", "")).strip()
    if not target or target.lower() in ("all", "tous", "tout", "everyone", "toute l'org", "org"):
        rows = (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name).where(CmsPlayer.id_org == id_org)
        )).all()
        if not rows:
            return None, None, None, {"error": "No player in the organization."}
        return [r.id_player for r in rows], [r.player_name for r in rows], "all players", None

    from app.models.player_group import CmsPlayerGroup, CmsPlayerGroupMember
    grp = (await db.execute(
        select(CmsPlayerGroup).where(
            CmsPlayerGroup.id_org == id_org,
            CmsPlayerGroup.group_name.ilike(f"%{target}%"),
        )
    )).scalars().all()
    if len(grp) > 1:
        return None, None, None, {"error": f"Several groups match \"{target}\"",
                                  "candidates": [g.group_name for g in grp[:8]]}
    if len(grp) == 1:
        members = (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name)
            .join(CmsPlayerGroupMember, CmsPlayerGroupMember.id_player == CmsPlayer.id_player)
            .where(CmsPlayerGroupMember.id_group == grp[0].id_group, CmsPlayer.id_org == id_org)
        )).all()
        if not members:
            return None, None, None, {
                "error": f"Group \"{grp[0].group_name}\" has no player."
            }
        return ([m.id_player for m in members], [m.player_name for m in members],
                f"group \"{grp[0].group_name}\"", None)

    from app.models.organization import CmsSite
    sites = (await db.execute(select(CmsSite).where(
        CmsSite.id_org == id_org, CmsSite.site_name.ilike(f"%{target}%")
    ))).scalars().all()
    if len(sites) > 1:
        return None, None, None, {"error": f"Several sites match \"{target}\"",
                                  "candidates": [s.site_name for s in sites[:8]]}
    if len(sites) == 1:
        members = (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name).where(
                CmsPlayer.id_org == id_org, CmsPlayer.id_site == sites[0].id_site)
        )).all()
        if not members:
            return None, None, None, {
                "error": f"Site \"{sites[0].site_name}\" has no player."
            }
        return ([m.id_player for m in members], [m.player_name for m in members],
                f"site \"{sites[0].site_name}\"", None)

    players = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_org == id_org,
            CmsPlayer.player_name.ilike(f"%{target}%"),
        )
    )).scalars().all()
    if len(players) > 1:
        return None, None, None, {"error": f"Several players match \"{target}\"",
                                  "candidates": [pl.player_name for pl in players[:8]]}
    if len(players) == 1:
        return ([players[0].id_player], [players[0].player_name],
                f"player \"{players[0].player_name}\"", None)
    return None, None, None, {"error": f"No group or player named \"{target}\"."}


def _make_command_resolver(action_type: str):
    meta = _COMMAND_META[action_type]

    async def _resolve(db: AsyncSession, id_org: int, params: dict) -> dict:
        ids, names, label, err = await _resolve_player_command_target(db, id_org, params)
        if err:
            return err
        return {
            "action_type": action_type,
            "params": {"player_ids": ids, "label": label, "names": names[:10]},
            "description": f"{meta['verb']} on {label} ({len(ids)} player(s)).",
            "warnings": [meta["warn"]] if meta["warn"] else [],
        }

    return _resolve


async def resolve_play_vod(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare le lancement d'une vidéo VOD (une fois / en boucle / stop) sur les players de
    la/les config(s) VOD qui contiennent ce média (cible opt. = player/groupe parmi eux)."""
    from app.models.vod import CmsVodConfig, CmsVodConfigMedia
    from app.services.vod_targeting import vod_player_ids
    name = str(params.get("media") or params.get("video") or "").strip()
    if not name:
        return {"error": "Which video do you want to play?"}
    media, err = await _resolve_media(db, id_org, name)
    if err:
        return err
    if media.media_type == "audio":
        return {"error": f"\"{media.media_name}\" is an audio file — VOD is for "
                         "videos/images."}

    configs = (await db.execute(
        select(CmsVodConfig)
        .join(CmsVodConfigMedia, CmsVodConfigMedia.id_vod == CmsVodConfig.id_vod)
        .where(CmsVodConfig.id_org == id_org, CmsVodConfigMedia.id_media == media.id_media)
    )).scalars().all()
    if not configs:
        return {"error": f"\"{media.media_name}\" is not in any VOD list. "
                         "Add it to a VOD configuration first."}
    # Cible résolue par config (source unique) : une config en ciblage SITE vise tous les
    # players du site, pas une liste explicite.
    allowed: set[int] = set()
    for cfg in configs:
        allowed |= await vod_player_ids(db, cfg)
    if not allowed:
        return {"error": "No player is attached to this media item's VOD."}

    mode = str(params.get("mode") or "once")
    if mode not in ("once", "loop", "stop"):
        mode = "once"

    target = str(params.get("target", "")).strip()
    if target and target.lower() not in ("all", "tous", "tout", "everyone", "org"):
        ids, _names, label, terr = await _resolve_player_command_target(db, id_org, params)
        if terr:
            return terr
        target_ids = [i for i in ids if i in allowed]
        if not target_ids:
            return {"error": f"{label} is not in the VOD of \"{media.media_name}\"."}
    else:
        target_ids = sorted(allowed)
        label = "all VOD players"

    names = [r[0] for r in (await db.execute(
        select(CmsPlayer.player_name).where(CmsPlayer.id_player.in_(target_ids))
    )).all()]
    verb = {"once": "Play (once)", "loop": "Play in a loop",
            "stop": "Stop playing"}[mode]
    return {
        "action_type": "play_vod",
        "params": {"id_media": media.id_media, "mode": mode,
                   "player_ids": target_ids, "label": label, "names": names[:10]},
        "description": f"{verb} \"{media.media_name}\" on {label} ({len(target_ids)} player(s)).",
        "warnings": [],
    }


async def resolve_rename_player(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare le renommage d'un player."""
    current = str(params.get("player") or params.get("current") or "").strip()
    new_name = str(params.get("new_name") or params.get("name") or "").strip()
    if not current or not new_name:
        return {"error": "Give the player to rename and its new name."}
    players = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_org == id_org, CmsPlayer.player_name.ilike(f"%{current}%")
        )
    )).scalars().all()
    if not players:
        return {"error": f"No player named \"{current}\"."}
    if len(players) > 1:
        return {"error": f"Several players match \"{current}\"",
                "candidates": [p.player_name for p in players[:8]]}
    return {
        "action_type": "rename_player",
        "params": {"id_player": players[0].id_player, "new_name": new_name},
        "description": f"Rename player \"{players[0].player_name}\" to \"{new_name}\".",
        "warnings": [],
    }


# ── Socle NL → règle horaire (partagé programmes + règles d'item, Session C/D) ──
_DAY_KEYS = {
    "lun": 0, "mon": 0, "mar": 1, "tue": 1, "die": 1, "mer": 2, "wed": 2, "mié": 2,
    "jeu": 3, "thu": 3, "ven": 4, "fri": 4, "vie": 4, "sam": 5, "sat": 5,
    "dim": 6, "sun": 6, "dom": 6,
}
_DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_days(v) -> list[int] | None:
    if not v:
        return None
    out: list[int] = []
    for d in v:
        if isinstance(d, bool):
            continue
        if isinstance(d, int):
            out.append(d % 7)
        else:
            key = str(d).strip().lower()[:3]
            if key in _DAY_KEYS:
                out.append(_DAY_KEYS[key])
    return sorted(set(out)) or None


def _parse_hm(v) -> str | None:
    """Normalise une heure « HH:MM » (renvoie une chaîne JSON-safe pour le token)."""
    if not v:
        return None
    try:
        parts = str(v).replace("h", ":").split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (ValueError, IndexError):
        pass
    return None


def _parse_iso_date(v) -> str | None:
    if not v:
        return None
    try:
        return _date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        return None


def _parse_date_obj(v):
    """Chaîne ISO normalisée → objet date (pour l'ORM). None si vide."""
    return _date.fromisoformat(v) if v else None


def _parse_time_obj(v):
    """Chaîne « HH:MM » normalisée → objet time (pour l'ORM). None si vide."""
    if not v:
        return None
    h, m = v.split(":")
    return _time(int(h), int(m))


def _build_time_rule(params: dict) -> dict:
    """Construit une règle horaire normalisée (JSON-safe) depuis des paramètres structurés.
    rule_type déduit : date_range si dates, sinon weekly si jours/heures, sinon always."""
    days = _parse_days(params.get("days") or params.get("days_of_week"))
    ts, te = _parse_hm(params.get("time_start")), _parse_hm(params.get("time_end"))
    df, dt = _parse_iso_date(params.get("date_from")), _parse_iso_date(params.get("date_to"))
    if df or dt:
        rule_type = "date_range"
    elif days or ts or te:
        rule_type = "weekly"
    else:
        rule_type = "always"
    return {"rule_type": rule_type, "days_of_week": days,
            "time_start": ts, "time_end": te, "date_from": df, "date_to": dt}


def _describe_rule(rule: dict) -> str:
    if rule["rule_type"] == "always":
        return "always on air"
    bits = []
    if rule.get("days_of_week"):
        bits.append("/".join(_DAY_LABELS[d] for d in rule["days_of_week"]))
    if rule.get("time_start") or rule.get("time_end"):
        bits.append(f"{rule.get('time_start') or '00:00'}–{rule.get('time_end') or '23:59'}")
    if rule.get("date_from") or rule.get("date_to"):
        bits.append(f"from {rule.get('date_from') or '…'} to {rule.get('date_to') or '…'}")
    return ", ".join(bits) or "always on air"


async def _resolve_schedule_target(db: AsyncSession, id_org: int, target: str):
    """Résout la cible d'un programme (groupe OU player ; pas d'org). (type, id, label, err)."""
    target = (target or "").strip()
    if not target:
        return None, None, None, {
            "error": "Specify the schedule target (a player or a group)."
        }
    from app.models.player_group import CmsPlayerGroup
    grp = (await db.execute(select(CmsPlayerGroup).where(
        CmsPlayerGroup.id_org == id_org, CmsPlayerGroup.group_name.ilike(f"%{target}%")
    ))).scalars().all()
    if len(grp) > 1:
        return None, None, None, {"error": f"Several groups match \"{target}\"",
                                  "candidates": [g.group_name for g in grp[:8]]}
    if len(grp) == 1:
        return "player_group", grp[0].id_group, f"group \"{grp[0].group_name}\"", None
    players = (await db.execute(select(CmsPlayer).where(
        CmsPlayer.id_org == id_org, CmsPlayer.player_name.ilike(f"%{target}%")
    ))).scalars().all()
    if len(players) > 1:
        return None, None, None, {"error": f"Several players match \"{target}\"",
                                  "candidates": [pl.player_name for pl in players[:8]]}
    if len(players) == 1:
        return "player", players[0].id_player, f"player \"{players[0].player_name}\"", None
    return None, None, None, {"error": f"No group or player named \"{target}\"."}


async def _resolve_schedule(db: AsyncSession, id_org: int, name: str):
    from app.models.schedule import CmsSchedule
    base = select(CmsSchedule).where(
        CmsSchedule.id_org == id_org, CmsSchedule.is_deleted == False,  # noqa: E712
    )
    exact = (await db.execute(
        base.where(func.lower(CmsSchedule.schedule_name) == name.lower())
    )).scalars().all()
    matches = exact or (
        await db.execute(base.where(CmsSchedule.schedule_name.ilike(f"%{name}%")))
    ).scalars().all()
    if not matches:
        return None, {"error": f"No schedule named \"{name}\"."}
    if len(matches) > 1:
        return None, {"error": f"Several schedules match \"{name}\"",
                      "candidates": [s.schedule_name for s in matches[:8]]}
    return matches[0], None


async def _push_targets_sync(
    db: AsyncSession, id_org: int, target_type: str, target_id: int
) -> None:
    from app.services.targeting import Target, players_for_targets
    pids = await players_for_targets(db, id_org, [Target(target_type, target_id)])
    await _dispatch_player_command(list(pids), "force_sync")


async def resolve_create_schedule(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare la création d'un programme (schedule content) diffusant une playlist sur une
    cible (player/groupe) selon une règle horaire."""
    name = str(params.get("name") or params.get("schedule_name") or "").strip()
    if not name:
        return {"error": "Give a schedule name."}
    content = str(params.get("playlist") or params.get("content") or "").strip()
    if not content:
        return {"error": "Which playlist should be scheduled?"}
    pl, err = await _resolve_playlist(db, id_org, content)
    if err:
        return err
    tt, tid, tlabel, terr = await _resolve_schedule_target(
        db, id_org, str(params.get("target", "")))
    if terr:
        return terr
    rule = _build_time_rule(params)
    priority = int(params.get("priority") or 0)
    valid_from = _parse_iso_date(params.get("valid_from"))
    valid_until = _parse_iso_date(params.get("valid_until"))
    validity = ""
    if valid_from or valid_until:
        validity = f" (valid {valid_from or '…'} → {valid_until or '…'})"
    return {
        "action_type": "create_schedule",
        "params": {"schedule_name": name, "id_playlist": pl.id_playlist,
                   "target_type": tt, "target_id": tid, "rule": rule,
                   "priority": priority, "valid_from": valid_from, "valid_until": valid_until},
        "description": (f"Schedule \"{pl.playlist_name}\" on {tlabel} — {_describe_rule(rule)}"
                        f"{validity}, priority {priority}."),
        "warnings": [],
    }


async def resolve_delete_schedule(db: AsyncSession, id_org: int, params: dict) -> dict:
    sched, err = await _resolve_schedule(
        db, id_org, str(params.get("schedule") or params.get("name") or ""))
    if err:
        return err
    from app.models.schedule import CmsScheduleTarget
    ntargets = (await db.execute(
        select(func.count()).select_from(CmsScheduleTarget).where(
            CmsScheduleTarget.id_schedule == sched.id_schedule)
    )).scalar_one()
    return {
        "action_type": "delete_schedule",
        "params": {"id_schedule": sched.id_schedule},
        "description": (f"Delete schedule \"{sched.schedule_name}\" "
                        f"({ntargets} target(s))."),
        "warnings": ["The affected players will stop playing according to this schedule."],
    }


async def _resolve_screen_targets(db: AsyncSession, id_org: int, params: dict,
                                  include_fallback: bool = True):
    """Résout la cible (player/groupe/site/tous) puis NE GARDE que les players configurés pour
    piloter un écran physique (`display_config.type` ∈ rs232/ip/cec) OU — si
    `include_fallback` (power on/off seulement) — en repli « couper la sortie vidéo »
    (`output_power_fallback`, écran non pilotable). Renvoie
    (equipped_ids, equipped_names, excluded_names, label, error)."""
    ids, _names, label, err = await _resolve_player_command_target(db, id_org, params)
    if err:
        return None, None, None, None, err
    players = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player.in_(ids)))).scalars().all()
    equipped, excluded = [], []
    for pl in players:
        dc = pl.display_config or {}
        if dc.get("type") in ("rs232", "ip", "cec") or (
                include_fallback and dc.get("output_power_fallback")):
            equipped.append((pl.id_player, pl.player_name))
        else:
            excluded.append(pl.player_name)
    if not equipped:
        return None, None, None, None, {
            "error": f"No player in {label} is configured to control a screen "
                     "(RS232/IP/CEC controller or video output cut-off). "
                     "Configure it first (done by a human)."}
    return ([e[0] for e in equipped], [e[1] for e in equipped], excluded, label, None)


def _screen_excl_warning(excluded: list[str]) -> list[str]:
    if not excluded:
        return []
    head = ", ".join(excluded[:6]) + ("…" if len(excluded) > 6 else "")
    return [f"{len(excluded)} player(s) without a screen controller (skipped): {head}."]


async def resolve_screen_power(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Allume/éteint l'écran physique des players (équipés RS232/IP) d'une cible."""
    state = str(params.get("state") or params.get("power") or "").strip().lower()
    on = state in ("on", "allume", "allumer", "true", "1", "marche")
    if state not in ("on", "off", "allume", "allumer", "eteins", "éteins", "eteindre",
                     "éteindre", "true", "false", "0", "1", "marche", "arret", "arrêt"):
        return {"error": "Specify \"on\" or \"off\"."}
    eq_ids, eq_names, excluded, label, err = await _resolve_screen_targets(db, id_org, params)
    if err:
        return err
    cmd = "power_on" if on else "power_off"
    verb = "Turn on" if on else "Turn off"
    return {
        "action_type": "screen_power",
        "params": {"player_ids": eq_ids, "display_cmd": cmd, "names": eq_names[:10]},
        "description": f"{verb} the screen of {label} ({len(eq_ids)} equipped screen(s)).",
        "warnings": _screen_excl_warning(excluded),
    }


async def resolve_screen_set_input(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Change la source d'entrée de l'écran physique (hdmi1/hdmi2/dp/pc)."""
    source = str(params.get("source") or params.get("input") or "").strip().lower().replace(" ", "")
    if source not in ("hdmi1", "hdmi2", "dp", "pc"):
        return {"error": "Expected source: hdmi1, hdmi2, dp or pc."}
    # set_input exige un vrai contrôleur (RS232/IP/CEC) — le repli « coupure de
    # sortie vidéo » ne sait pas changer d'entrée.
    eq_ids, eq_names, excluded, label, err = await _resolve_screen_targets(
        db, id_org, params, include_fallback=False)
    if err:
        return err
    return {
        "action_type": "screen_set_input",
        "params": {"player_ids": eq_ids, "display_cmd": "set_input", "source": source,
                   "names": eq_names[:10]},
        "description": f"Switch the screen of {label} to source \"{source}\" "
                       f"({len(eq_ids)} equipped screen(s)).",
        "warnings": _screen_excl_warning(excluded),
    }


async def resolve_hdmi_switch(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Bascule l'écran sur l'entrée HDMI physique du player (start) ou reprend la playlist
    (stop). Ne garde que les players à entrée HDMI (BrightSign XT/4K/XD1230 — miroir du gate
    de sync). Réutilise la commande `hdmi_input` : session tracée (audit `hdmi_session` +
    reprise wall-clock au stop), auto-OFF à la coupure écran."""
    from app.api.v1.endpoints.ws import _player_has_hdmi_input  # import tardif (évite cycle)

    mode = str(params.get("mode", "")).strip().lower()
    if mode in ("start", "on", "hdmi", "switch", "bascule", "basculer"):
        mode = "start"
    elif mode in ("stop", "off", "playlist", "retour", "revenir"):
        mode = "stop"
    else:
        return {"error": "Specify the mode: \"start\" (switch to the HDMI input) or "
                         "\"stop\" (back to the playlist)."}
    ids, _names, label, err = await _resolve_player_command_target(db, id_org, params)
    if err:
        return err
    players = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player.in_(ids)))).scalars().all()
    capable = [(p.id_player, p.player_name) for p in players if _player_has_hdmi_input(p)]
    excluded = [p.player_name for p in players if not _player_has_hdmi_input(p)]
    if not capable:
        return {"error": f"No player in {label} has a physical HDMI input "
                         "(BrightSign XT series / 4K1142 / XD1230)."}
    verb = ("Switch the screen to the HDMI input" if mode == "start"
            else "Back to the playlist (resumes at the current time)")
    warns = []
    if excluded:
        head = ", ".join(excluded[:6]) + ("…" if len(excluded) > 6 else "")
        warns.append(f"{len(excluded)} player(s) without an HDMI input (skipped): {head}.")
    return {
        "action_type": "hdmi_switch",
        "params": {"player_ids": [c[0] for c in capable], "mode": mode,
                   "names": [c[1] for c in capable][:10]},
        "description": f"{verb} — {label} ({len(capable)} player(s) with an HDMI input).",
        "warnings": warns,
    }


async def resolve_set_player_timezone(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Règle le fuseau horaire de l'OS d'UN player (commande `set_timezone` existante).
    Cible = un player unique (le fuseau est par appareil). Le format attendu dépend de
    l'OS : IANA (Europe/Paris) pour linux/rpi/android ; fuseau canonique pour Windows ;
    libellé natif (CET…) pour BrightSign — qui REBOOTE pour appliquer."""
    tz = str(params.get("timezone", "")).strip()
    if not tz or len(tz) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/ ]+", tz):
        return {"error": "Invalid time zone — expected: an IANA identifier (e.g. Europe/Paris), "
                         "a canonical Windows time zone, or a BrightSign label (e.g. CET)."}
    ids, names, label, err = await _resolve_player_command_target(db, id_org, params)
    if err:
        return err
    if len(ids) != 1:
        return {"error": "The time zone is set one player AT A TIME — specify ONE player "
                         f"(current target: {label}, {len(ids)} players)."}
    player = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == ids[0]))).scalar_one_or_none()
    warns = []
    if player is not None and player.player_os == "brightsign":
        warns.append("BrightSign: a NATIVE time zone label is expected (e.g. CET ≡ Europe/Paris) "
                     "and the player REBOOTS to apply it.")
    elif player is not None and (player.player_os or "").startswith("windows"):
        warns.append("Windows: canonical time zone of the group (e.g. Monaco → Central European "
                     "time).")
    return {
        "action_type": "set_player_timezone",
        "params": {"player_ids": ids, "timezone": tz, "names": names[:10]},
        "description": f"Set the time zone of {label} to \"{tz}\".",
        "warnings": warns,
    }


async def resolve_update_players(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare une mise à jour OTA (§XXX) des players d'une cible vers la DERNIÈRE version
    STABLE de LEUR plateforme (`player_os` — même convention que le catalogue : une version
    linux ne part jamais sur un BrightSign). Les players déjà à jour sont ignorés. Pas de
    version explicite ni de canal beta par l'assistant : ces cas restent dans l'UI
    Réglages → Mises à jour (canary/beta/scheduled)."""
    from app.services import ota

    ids, _names, label, err = await _resolve_player_command_target(db, id_org, params)
    if err:
        return err
    players = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player.in_(ids)))).scalars().all()

    latest_by_platform: dict[str, object] = {}
    groups: dict[int, dict] = {}   # id_version → lot homogène (plateforme, players)
    up_to_date: list[str] = []
    no_version: list[str] = []
    for p in players:
        platform = p.player_os or ""
        if platform not in latest_by_platform:
            latest_by_platform[platform] = await ota.latest_player_version(
                db, platform or None)
        latest = latest_by_platform[platform]
        if latest is None:
            no_version.append(p.player_name)
            continue
        if ota.semver_key(p.player_version) >= ota.semver_key(latest.version):
            up_to_date.append(p.player_name)
            continue
        g = groups.setdefault(latest.id_version, {
            "id_version": latest.id_version, "version": latest.version,
            "platform": platform or "generic", "player_ids": [], "names": [],
        })
        g["player_ids"].append(p.id_player)
        g["names"].append(p.player_name)

    if not groups:
        if up_to_date and not no_version:
            return {"error": f"Nothing to do: the players of {label} are already up to date "
                             f"({', '.join(up_to_date[:8])})."}
        return {"error": f"No version in the catalog for the players of {label} — "
                         "publish a version first in Settings → Updates."}

    total = sum(len(g["player_ids"]) for g in groups.values())
    parts = [
        f"{g['platform']} → {g['version']} ({', '.join(g['names'][:6])}"
        f"{'…' if len(g['names']) > 6 else ''})"
        for g in groups.values()
    ]
    warnings = ["Each player downloads then INSTALLS the update and restarts its "
                "application (brief black screen during the install)."]
    if up_to_date:
        head = ", ".join(up_to_date[:6]) + ("…" if len(up_to_date) > 6 else "")
        warnings.append(f"{len(up_to_date)} player(s) already up to date (skipped): {head}.")
    if no_version:
        head = ", ".join(no_version[:6]) + ("…" if len(no_version) > 6 else "")
        warnings.append(f"{len(no_version)} player(s) with no version in the catalog for their "
                        f"platform (skipped): {head}.")
    return {
        "action_type": "update_players",
        "params": {"groups": list(groups.values()), "label": label},
        "description": (f"Update {total} player(s) of {label} to the latest "
                        f"stable version for their platform: {'; '.join(parts)}."),
        "warnings": warnings,
    }


async def resolve_move_media_to_folder(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Range des médias dans un dossier de la médiathèque. Sélection : un média par nom
    (motif), OU tous les médias NON CLASSÉS (`unfiled: true`)."""
    from app.models.media_folder import CmsMediaFolder

    folder_name = str(params.get("folder", "")).strip()
    if not folder_name:
        return {"error": "Specify the destination folder."}
    folders = (await db.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_org == id_org,
        CmsMediaFolder.folder_name.ilike(f"%{folder_name}%"),
    ))).scalars().all()
    if not folders:
        return {"error": f"No folder named \"{folder_name}\" (create it first in "
                         "the media library)."}
    if len(folders) > 1:
        return {"error": f"Several folders match \"{folder_name}\"",
                "candidates": [f.folder_name for f in folders[:8]]}
    folder = folders[0]

    if params.get("unfiled") is True:
        medias = (await db.execute(select(CmsMedia).where(
            CmsMedia.id_org == id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
            CmsMedia.id_folder.is_(None),
        ))).scalars().all()
        sel_label = "all unfiled media"
    else:
        name = str(params.get("media", "")).strip()
        if not name:
            return {"error": "Specify the media to file (a name/pattern, or "
                             "\"unfiled\")."}
        medias = (await db.execute(select(CmsMedia).where(
            CmsMedia.id_org == id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
            CmsMedia.media_name.ilike(f"%{name}%"),
        ))).scalars().all()
        sel_label = f"media matching \"{name}\""
    medias = [m for m in medias if m.id_folder != folder.id_folder]
    if not medias:
        return {"error": "No media to move (already in this folder, or no "
                         "match)."}
    names = [m.media_name for m in medias[:8]]
    return {
        "action_type": "move_media_to_folder",
        "params": {"id_media": [m.id_media for m in medias], "id_folder": folder.id_folder,
                   "folder_name": folder.folder_name},
        "description": (f"Move {sel_label} ({len(medias)} media item(s): "
                        + ", ".join(names) + ("…" if len(medias) > 8 else "")
                        + f") to folder \"{folder.folder_name}\"."),
        "warnings": [],
    }


async def resolve_update_schedule(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare la modification d'un programme (patch partiel : playlist, priorité, activation,
    validité, règle horaire). Ne change que ce qui est fourni."""
    sched, err = await _resolve_schedule(
        db, id_org, str(params.get("schedule") or params.get("name") or ""))
    if err:
        return err
    changes: dict = {"id_schedule": sched.id_schedule}
    bits: list[str] = []
    if params.get("playlist"):
        pl, e = await _resolve_playlist(db, id_org, str(params["playlist"]))
        if e:
            return e
        changes["id_playlist"] = pl.id_playlist
        bits.append(f"playlist → \"{pl.playlist_name}\"")
    if params.get("priority") is not None:
        changes["priority"] = int(params["priority"])
        bits.append(f"priority {changes['priority']}")
    if params.get("active") is not None or params.get("is_active") is not None:
        val = bool(params.get("active") if params.get("active") is not None
                   else params.get("is_active"))
        changes["is_active"] = val
        bits.append("enabled" if val else "disabled")
    vf = _parse_iso_date(params.get("valid_from"))
    vu = _parse_iso_date(params.get("valid_until"))
    if vf is not None:
        changes["valid_from"] = vf
        bits.append(f"start {vf}")
    if vu is not None:
        changes["valid_until"] = vu
        bits.append(f"end {vu}")
    if any(params.get(k) for k in ("days", "time_start", "time_end", "date_from", "date_to")):
        rule = _build_time_rule(params)
        changes["rule"] = rule
        bits.append(_describe_rule(rule))
    if len(changes) == 1:
        return {"error": "Specify what to change (priority, activation, validity, "
                         "playlist or times)."}
    return {
        "action_type": "update_schedule", "params": changes,
        "description": f"Edit schedule \"{sched.schedule_name}\": {', '.join(bits)}.",
        "warnings": [],
    }


async def resolve_add_schedule_target(db: AsyncSession, id_org: int, params: dict) -> dict:
    sched, err = await _resolve_schedule(
        db, id_org, str(params.get("schedule") or params.get("name") or ""))
    if err:
        return err
    tt, tid, label, terr = await _resolve_schedule_target(db, id_org, str(params.get("target", "")))
    if terr:
        return terr
    return {
        "action_type": "add_schedule_target",
        "params": {"id_schedule": sched.id_schedule, "target_type": tt, "target_id": tid},
        "description": f"Add {label} to schedule \"{sched.schedule_name}\".",
        "warnings": [],
    }


async def resolve_remove_schedule_target(db: AsyncSession, id_org: int, params: dict) -> dict:
    sched, err = await _resolve_schedule(
        db, id_org, str(params.get("schedule") or params.get("name") or ""))
    if err:
        return err
    tt, tid, label, terr = await _resolve_schedule_target(db, id_org, str(params.get("target", "")))
    if terr:
        return terr
    return {
        "action_type": "remove_schedule_target",
        "params": {"id_schedule": sched.id_schedule, "target_type": tt, "target_id": tid},
        "description": f"Remove {label} from schedule \"{sched.schedule_name}\".",
        "warnings": [],
    }


async def resolve_set_playlist_item_rules(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare le réglage de la règle de diffusion d'un média dans une playlist (s'applique à
    toutes ses occurrences, ou à une zone précise). Optionnellement RESTREINTE à un player ou
    groupe (override §XVII) ; sinon `default` (tous). « toujours » = efface la règle de ce scope."""
    pl, err = await _resolve_playlist(db, id_org, str(params.get("playlist", "")))
    if err:
        return err
    media, err = await _resolve_media(db, id_org, str(params.get("media", "")))
    if err:
        return err
    zone_human = params.get("zone")
    zone_index = (int(zone_human) - 1) if zone_human else None

    # Scope optionnel : un player/groupe → override pour cette seule cible (sinon tous).
    scope, target_id, scope_label = "default", None, "all players"
    target = str(params.get("target", "")).strip()
    if target and target.lower() not in ("all", "tous", "tout", "default"):
        scope, target_id, scope_label, terr = await _resolve_schedule_target(db, id_org, target)
        if terr:
            return terr

    cnt_q = select(func.count()).select_from(CmsPlaylistItem).where(
        CmsPlaylistItem.id_playlist == pl.id_playlist, CmsPlaylistItem.id_media == media.id_media)
    if zone_index is not None:
        cnt_q = cnt_q.where(CmsPlaylistItem.zone_index == zone_index)
    count = (await db.execute(cnt_q)).scalar_one()
    if count == 0:
        return {"error": f"\"{media.media_name}\" is not in playlist "
                         f"\"{pl.playlist_name}\"{f' in zone {zone_human}' if zone_human else ''}."}

    rule = _build_time_rule(params)
    rules_list = ([] if rule["rule_type"] == "always"
                  else [{**rule, "scope": scope, "target_id": target_id}])
    scope_txt = "" if scope == "default" else f" for {scope_label}"
    return {
        "action_type": "set_playlist_item_rules",
        "params": {"id_playlist": pl.id_playlist, "id_media": media.id_media,
                   "zone_index": zone_index, "scope": scope, "target_id": target_id,
                   "rules": rules_list},
        "description": (f"Set when \"{media.media_name}\" plays in "
                        f"\"{pl.playlist_name}\"{scope_txt}: {_describe_rule(rule)} "
                        f"({count} occurrence(s))."),
        "warnings": [],
    }


def _slug(s: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in s.strip().lower())
    return "_".join(p for p in out.split("_") if p)[:50] or "general"


async def resolve_tag_media(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare le tag d'un média (le namespace/la valeur sont créés s'ils n'existent pas)."""
    media, err = await _resolve_media(db, id_org, str(params.get("media", "")))
    if err:
        return err
    value = str(params.get("value") or params.get("tag") or "").strip()
    if not value:
        return {"error": "Which tag value should be applied (e.g. \"promo\")?"}
    ns_label = str(params.get("namespace") or "").strip() or "Général"
    return {
        "action_type": "tag_media",
        "params": {"id_media": media.id_media, "media_name": media.media_name,
                   "namespace_key": _slug(ns_label), "namespace_label": ns_label, "value": value},
        "description": (f"Tag \"{media.media_name}\" with \"{ns_label}: {value}\" "
                        "(the tag will be created if it does not exist)."),
        "warnings": [],
    }


async def resolve_assign_basket_to_players(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare l'activation d'un panier audio sur des players (player/groupe/tous), avec ratio."""
    basket, err = await _resolve_basket(db, id_org, str(params.get("basket", "")))
    if err:
        return err
    ids, _names, label, terr = await _resolve_player_command_target(db, id_org, params)
    if terr:
        return terr
    ratio = max(0, min(100, int(params.get("mix_ratio") or 50)))
    return {
        "action_type": "assign_basket_to_players",
        "params": {"id_basket": basket.id_basket, "basket_name": basket.basket_name,
                   "player_ids": ids, "label": label, "mix_ratio": ratio},
        "description": (f"Enable audio basket \"{basket.basket_name}\" on {label} "
                        f"({len(ids)} player(s)), mix {ratio}%."),
        "warnings": [],
    }


async def resolve_configure_audio_basket(db: AsyncSession, id_org: int, params: dict) -> dict:
    """Prépare la config d'un panier audio : aléatoire, fréquence du jingle (mode + valeur),
    ducking (% pendant les annonces), bornes de mix min/max, activation. Patch partiel."""
    basket, err = await _resolve_basket(db, id_org, str(params.get("basket", "")))
    if err:
        return err
    changes: dict = {"id_basket": basket.id_basket, "basket_name": basket.basket_name}
    bits: list[str] = []
    if params.get("shuffle") is not None:
        changes["shuffle"] = bool(params["shuffle"])
        bits.append("shuffle " + ("on" if changes["shuffle"] else "off"))
    if params.get("trigger_mode") in ("tracks", "minutes"):
        changes["trigger_mode"] = params["trigger_mode"]
    if params.get("trigger_value") is not None:
        changes["trigger_value"] = max(1, int(params["trigger_value"]))
    if "trigger_mode" in changes or "trigger_value" in changes:
        bits.append(f"triggers every {changes.get('trigger_value', basket.trigger_value)} "
                    f"{changes.get('trigger_mode', basket.trigger_mode)}")
    if params.get("active") is not None or params.get("is_active") is not None:
        v = bool(params.get("active") if params.get("active") is not None
                 else params.get("is_active"))
        changes["is_active"] = v
        bits.append("enabled" if v else "disabled")
    if params.get("min_ratio") is not None:
        changes["min_ratio"] = max(0, min(100, int(params["min_ratio"])))
        bits.append(f"min {changes['min_ratio']}%")
    if params.get("max_ratio") is not None:
        changes["max_ratio"] = max(0, min(100, int(params["max_ratio"])))
        bits.append(f"max {changes['max_ratio']}%")
    if params.get("ducking_pct") is not None:
        pct = max(0, min(100, int(params["ducking_pct"])))
        duck = dict(basket.ducking_config or {})
        duck["target_pct"] = pct
        changes["ducking_config"] = duck
        bits.append(f"ducking → music at {pct}% during announcements")
    if len(changes) <= 2:
        return {"error": "Specify what to set (shuffle, jingle frequency, ducking, "
                         "min/max bounds, activation)."}
    return {
        "action_type": "configure_audio_basket", "params": changes,
        "description": f"Configure audio basket \"{basket.basket_name}\": {', '.join(bits)}.",
        "warnings": [],
    }


async def resolve_unassign_basket_from_players(db: AsyncSession, id_org: int, params: dict) -> dict:
    basket, err = await _resolve_basket(db, id_org, str(params.get("basket", "")))
    if err:
        return err
    ids, _names, label, terr = await _resolve_player_command_target(db, id_org, params)
    if terr:
        return terr
    return {
        "action_type": "unassign_basket_from_players",
        "params": {"id_basket": basket.id_basket, "basket_name": basket.basket_name,
                   "player_ids": ids, "label": label},
        "description": (f"Remove audio basket \"{basket.basket_name}\" from {label} "
                        f"({len(ids)} player(s))."),
        "warnings": [],
    }


_RESOLVERS = {
    "add_playlist_item": resolve_add_playlist_item,
    "remove_playlist_item": resolve_remove_playlist_item,
    "create_playlist": resolve_create_playlist,
    "add_tagged_content_to_playlist": resolve_add_tagged_content_to_playlist,
    "create_feed": resolve_create_feed,
    "add_audio_track": resolve_add_audio_track,
    "remove_audio_track": resolve_remove_audio_track,
    "create_event": resolve_create_event,
    "remove_feed_from_active_playlists": resolve_remove_feed_from_active_playlists,
    "play_vod": resolve_play_vod,
    "rename_player": resolve_rename_player,
    "create_schedule": resolve_create_schedule,
    "delete_schedule": resolve_delete_schedule,
    "update_schedule": resolve_update_schedule,
    "add_schedule_target": resolve_add_schedule_target,
    "remove_schedule_target": resolve_remove_schedule_target,
    "set_playlist_item_rules": resolve_set_playlist_item_rules,
    "tag_media": resolve_tag_media,
    "assign_basket_to_players": resolve_assign_basket_to_players,
    "unassign_basket_from_players": resolve_unassign_basket_from_players,
    "configure_audio_basket": resolve_configure_audio_basket,
    "screen_power": resolve_screen_power,
    "screen_set_input": resolve_screen_set_input,
    "hdmi_switch": resolve_hdmi_switch,
    "set_player_timezone": resolve_set_player_timezone,
    "update_players": resolve_update_players,
    "move_media_to_folder": resolve_move_media_to_folder,
    **{a: _make_command_resolver(a) for a in _COMMAND_META},
}


async def resolve_action(
    db: AsyncSession, id_org: int, action_type: str, params: dict
) -> dict:
    """Résout une action en proposition (sans rien écrire). `{error:...}` si impossible."""
    fn = _RESOLVERS.get(action_type)
    if fn is None:
        return {"error": f"Unknown action: {action_type}"}
    return await fn(db, id_org, params)


# ── Exécution (déclenchée par le clic « Valider », RBAC re-vérifié) ───────────

def _audit(db, user, action_type, resource_type, resource_id, params) -> None:
    db.add(CmsAuditLog(
        id_org=user.id_org,
        id_user=user.id_user,
        action=f"assistant_{action_type}",
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        detail={k: v for k, v in params.items()},
        source="assistant",
    ))


async def _get_playlist(db: AsyncSession, id_playlist: int):
    return (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_playlist,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()


# Ops de playlist-item : mutent un brouillon → publish DIFFÉRÉ (groupé par playlist).
_PLAYLIST_OPS = {"add_playlist_item", "remove_playlist_item", "add_tagged_content_to_playlist",
                 "set_playlist_item_rules"}


async def _dispatch_player_command(player_ids: list[int], command: str) -> None:
    """Publie une commande sur le canal Redis de chaque player (fan-out WS), comme
    `POST /players/{id}/command`. `force_sync` = message `player:force_sync` dédié."""
    if not player_ids:
        return
    redis = aioredis.from_url(settings.REDIS_URL)
    try:
        for pid in player_ids:
            if command == "force_sync":
                msg = {"type": "player:force_sync"}
            else:
                msg = {"type": "player:command", "command": command, "params": {}}
            await redis.publish(f"signflow:player:{int(pid)}:cmds", json.dumps(msg))
    finally:
        await redis.aclose()


async def _dispatch_display_command(player_ids: list[int], display_cmd: str, extra: dict) -> None:
    """Pilote l'écran physique (RS232/IP) des players via `player:command display_command`."""
    if not player_ids:
        return
    redis = aioredis.from_url(settings.REDIS_URL)
    try:
        params = {"display_cmd": display_cmd, **(extra or {})}
        msg = json.dumps({"type": "player:command", "command": "display_command", "params": params})
        for pid in player_ids:
            await redis.publish(f"signflow:player:{int(pid)}:cmds", msg)
    finally:
        await redis.aclose()


async def _execute_self_contained(
    db: AsyncSession, user: CmsUser, action_type: str, params: dict
) -> dict:
    """Exécute une action INDÉPENDANTE (création playlist/feed, piste audio). Chacune se
    suffit à elle-même et commit immédiatement — pas de publish différé (≠ playlist-item).
    Le RBAC est supposé déjà vérifié par l'appelant (`execute_batch`)."""
    # ── Création de playlist ───────────────────────────────────────────────────
    if action_type == "create_playlist":
        pl = CmsPlaylist(id_org=user.id_org, playlist_name=params["name"],
                         created_by=user.id_user)
        db.add(pl)
        await db.flush()
        _audit(db, user, action_type, "playlist", pl.id_playlist, params)
        await db.commit()
        return {"ok": True, "message": f"Playlist \"{params['name']}\" created."}

    # ── Création de feed (datafeed + média pointeur, comme l'endpoint feed) ────
    if action_type == "create_feed":
        from app.models.feed import CmsDatafeed
        feed = CmsDatafeed(
            id_org=user.id_org, feed_name=params["feed_name"], feed_type="rss",
            config={"url": params["url"]}, display_mode=params["display_mode"],
            status="active", created_by=user.id_user,
        )
        db.add(feed)
        await db.flush()
        db.add(CmsMedia(
            id_org=user.id_org, media_name=params["feed_name"], media_type="feed_display",
            mime_type="application/rss+xml", storage_key="", checksum_sha256="",
            source="feed", media_metadata={"id_feed": feed.id_feed},
            transcode_status="done", is_encrypted=False, conformance_status="ready",
            created_by=user.id_user,
        ))
        _audit(db, user, action_type, "feed", feed.id_feed, params)
        await db.commit()
        try:
            from app.workers.feed_worker import fetch_feed
            fetch_feed.delay(feed.id_feed)
        except Exception:  # noqa: BLE001 — best-effort, le poll régulier rattrapera
            pass
        return {"ok": True, "message": f"Feed \"{params['feed_name']}\" created."}

    # ── Paniers audio (ajout/retrait piste) ────────────────────────────────────
    if action_type in ("add_audio_track", "remove_audio_track"):
        from app.models.audio_basket import CmsAudioBasketItem
        id_basket = int(params["id_basket"])
        id_media = int(params["id_media"])
        if action_type == "add_audio_track":
            db.add(CmsAudioBasketItem(id_basket=id_basket, id_media=id_media))
            msg = "Track added to the audio basket."
        else:
            from sqlalchemy import delete
            await db.execute(delete(CmsAudioBasketItem).where(
                CmsAudioBasketItem.id_basket == id_basket,
                CmsAudioBasketItem.id_media == id_media,
            ))
            msg = "Track removed from the audio basket."
        _audit(db, user, action_type, "audio_basket", id_basket, params)
        await db.commit()
        return {"ok": True, "message": msg}

    # ── Affectation d'un panier audio à des players (upsert + ratio) ───────────
    if action_type in ("assign_basket_to_players", "unassign_basket_from_players"):
        from app.models.audio_basket import CmsPlayerBasketConfig
        id_basket = int(params["id_basket"])
        pids = [int(x) for x in params.get("player_ids", [])]
        if action_type == "assign_basket_to_players":
            ratio = int(params.get("mix_ratio", 50))
            for pid in pids:
                existing = (await db.execute(select(CmsPlayerBasketConfig).where(
                    CmsPlayerBasketConfig.id_player == pid,
                    CmsPlayerBasketConfig.id_basket == id_basket,
                ))).scalar_one_or_none()
                if existing:
                    existing.mix_ratio = ratio
                    existing.is_active = True
                    existing.activated_by = user.id_user
                else:
                    db.add(CmsPlayerBasketConfig(
                        id_player=pid, id_basket=id_basket, mix_ratio=ratio,
                        is_active=True, activated_by=user.id_user))
            msg = (f"Basket \"{params['basket_name']}\" enabled on {len(pids)} player(s) "
                   f"(mix {ratio}%).")
        else:
            from sqlalchemy import delete
            await db.execute(delete(CmsPlayerBasketConfig).where(
                CmsPlayerBasketConfig.id_basket == id_basket,
                CmsPlayerBasketConfig.id_player.in_(pids),
            ))
            msg = f"Basket \"{params['basket_name']}\" removed from {len(pids)} player(s)."
        _audit(db, user, action_type, "audio_basket", id_basket,
               {"player_ids": pids, "mix_ratio": params.get("mix_ratio")})
        await db.commit()
        await _dispatch_player_command(pids, "force_sync")
        return {"ok": True, "message": msg}

    # ── Configuration d'un panier audio (patch partiel + resync des players) ───
    if action_type == "configure_audio_basket":
        from app.models.audio_basket import CmsAudioBasket, CmsPlayerBasketConfig
        basket = (await db.execute(select(CmsAudioBasket).where(
            CmsAudioBasket.id_basket == int(params["id_basket"]),
            CmsAudioBasket.id_org == user.id_org,
        ))).scalar_one_or_none()
        if basket is None:
            return {"ok": False, "error": "Audio basket not found."}
        for f in ("shuffle", "trigger_mode", "trigger_value", "is_active",
                  "min_ratio", "max_ratio", "ducking_config"):
            if f in params:
                setattr(basket, f, params[f])
        _audit(db, user, action_type, "audio_basket", basket.id_basket, params)
        await db.commit()
        pids = [r[0] for r in (await db.execute(select(CmsPlayerBasketConfig.id_player).where(
            CmsPlayerBasketConfig.id_basket == basket.id_basket,
            CmsPlayerBasketConfig.is_active == True,  # noqa: E712
        ))).all()]
        await _dispatch_player_command(pids, "force_sync")
        return {"ok": True, "message": f"Basket \"{basket.basket_name}\" configured."}

    # ── Tag d'un média (namespace + valeur créés au besoin, puis assignation) ──
    if action_type == "tag_media":
        from app.models.tag import CmsMediaTagValue, CmsTagNamespace, CmsTagValue
        ns = (await db.execute(select(CmsTagNamespace).where(
            CmsTagNamespace.id_org == user.id_org,
            CmsTagNamespace.namespace_key == params["namespace_key"],
        ))).scalar_one_or_none()
        if ns is None:
            ns = CmsTagNamespace(id_org=user.id_org, namespace_key=params["namespace_key"],
                                 label=params.get("namespace_label"), applies_to=["media"])
            db.add(ns)
            await db.flush()
        val = (await db.execute(select(CmsTagValue).where(
            CmsTagValue.id_namespace == ns.id_namespace,
            func.lower(CmsTagValue.tag_value) == params["value"].lower(),
        ))).scalar_one_or_none()
        if val is None:
            val = CmsTagValue(id_namespace=ns.id_namespace, tag_value=params["value"])
            db.add(val)
            await db.flush()
        exists = (await db.execute(select(CmsMediaTagValue).where(
            CmsMediaTagValue.id_media == int(params["id_media"]),
            CmsMediaTagValue.id_tag == val.id_tag,
        ))).scalar_one_or_none()
        if exists is None:
            db.add(CmsMediaTagValue(id_media=int(params["id_media"]), id_tag=val.id_tag))
        _audit(db, user, action_type, "media", params["id_media"],
               {"namespace": params["namespace_key"], "value": params["value"]})
        await db.commit()
        # Un tag peut changer le filtrage par tag des players → resync de l'org (best-effort).
        org_pids = [r[0] for r in (await db.execute(
            select(CmsPlayer.id_player).where(CmsPlayer.id_org == user.id_org)
        )).all()]
        await _dispatch_player_command(org_pids, "force_sync")
        return {"ok": True, "message": f"\"{params['media_name']}\" tagged \"{params['value']}\"."}

    # ── Renommage d'un player ──────────────────────────────────────────────────
    if action_type == "rename_player":
        pl = (await db.execute(select(CmsPlayer).where(
            CmsPlayer.id_player == int(params["id_player"]), CmsPlayer.id_org == user.id_org
        ))).scalar_one_or_none()
        if pl is None:
            return {"ok": False, "error": "Player not found."}
        old = pl.player_name
        pl.player_name = params["new_name"]
        _audit(db, user, action_type, "player", pl.id_player,
               {"old": old, "new": params["new_name"]})
        await db.commit()
        return {"ok": True, "message": f"Player \"{old}\" renamed to \"{params['new_name']}\"."}

    # ── Création d'un programme (schedule content) ─────────────────────────────
    if action_type == "create_schedule":
        from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget
        sched = CmsSchedule(
            id_org=user.id_org, schedule_name=params["schedule_name"], schedule_kind="content",
            id_playlist=params["id_playlist"], priority=int(params.get("priority", 0)),
            valid_from=_parse_date_obj(params.get("valid_from")),
            valid_until=_parse_date_obj(params.get("valid_until")),
            created_by=user.id_user,
        )
        db.add(sched)
        await db.flush()
        r = params["rule"]
        db.add(CmsScheduleRule(
            id_schedule=sched.id_schedule, rule_type=r["rule_type"],
            days_of_week=r.get("days_of_week"),
            time_start=_parse_time_obj(r.get("time_start")),
            time_end=_parse_time_obj(r.get("time_end")),
            date_from=_parse_date_obj(r.get("date_from")),
            date_to=_parse_date_obj(r.get("date_to")),
        ))
        db.add(CmsScheduleTarget(id_schedule=sched.id_schedule,
                                 target_type=params["target_type"], target_id=params["target_id"]))
        _audit(db, user, action_type, "schedule", sched.id_schedule, params)
        await db.commit()
        await _push_targets_sync(db, user.id_org, params["target_type"], params["target_id"])
        return {"ok": True, "message": f"Schedule \"{params['schedule_name']}\" created."}

    # ── Modification d'un programme (patch partiel + resync des cibles) ────────
    if action_type == "update_schedule":
        from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget
        sched = (await db.execute(select(CmsSchedule).where(
            CmsSchedule.id_schedule == int(params["id_schedule"]),
            CmsSchedule.id_org == user.id_org, CmsSchedule.is_deleted == False,  # noqa: E712
        ))).scalar_one_or_none()
        if sched is None:
            return {"ok": False, "error": "Schedule not found."}
        if "id_playlist" in params:
            sched.id_playlist = params["id_playlist"]
        if "priority" in params:
            sched.priority = params["priority"]
        if "is_active" in params:
            sched.is_active = params["is_active"]
        if "valid_from" in params:
            sched.valid_from = _parse_date_obj(params["valid_from"])
        if "valid_until" in params:
            sched.valid_until = _parse_date_obj(params["valid_until"])
        if "rule" in params:
            from sqlalchemy import delete
            await db.execute(delete(CmsScheduleRule).where(
                CmsScheduleRule.id_schedule == sched.id_schedule))
            r = params["rule"]
            db.add(CmsScheduleRule(
                id_schedule=sched.id_schedule, rule_type=r["rule_type"],
                days_of_week=r.get("days_of_week"),
                time_start=_parse_time_obj(r.get("time_start")),
                time_end=_parse_time_obj(r.get("time_end")),
                date_from=_parse_date_obj(r.get("date_from")),
                date_to=_parse_date_obj(r.get("date_to")),
            ))
        _audit(db, user, action_type, "schedule", sched.id_schedule, params)
        await db.commit()
        targets = (await db.execute(select(CmsScheduleTarget).where(
            CmsScheduleTarget.id_schedule == sched.id_schedule))).scalars().all()
        for t in targets:
            await _push_targets_sync(db, user.id_org, t.target_type, t.target_id)
        return {"ok": True, "message": f"Schedule \"{sched.schedule_name}\" updated."}

    # ── Ajout / retrait d'une cible d'un programme ─────────────────────────────
    if action_type in ("add_schedule_target", "remove_schedule_target"):
        from app.models.schedule import CmsSchedule, CmsScheduleTarget
        sched = (await db.execute(select(CmsSchedule).where(
            CmsSchedule.id_schedule == int(params["id_schedule"]),
            CmsSchedule.id_org == user.id_org, CmsSchedule.is_deleted == False,  # noqa: E712
        ))).scalar_one_or_none()
        if sched is None:
            return {"ok": False, "error": "Schedule not found."}
        tt, tid = params["target_type"], int(params["target_id"])
        if action_type == "add_schedule_target":
            exists = (await db.execute(select(CmsScheduleTarget).where(
                CmsScheduleTarget.id_schedule == sched.id_schedule,
                CmsScheduleTarget.target_type == tt, CmsScheduleTarget.target_id == tid,
            ))).scalar_one_or_none()
            if exists is None:
                db.add(CmsScheduleTarget(id_schedule=sched.id_schedule,
                                         target_type=tt, target_id=tid))
            msg = "Target added to the schedule."
        else:
            from sqlalchemy import delete
            await db.execute(delete(CmsScheduleTarget).where(
                CmsScheduleTarget.id_schedule == sched.id_schedule,
                CmsScheduleTarget.target_type == tt, CmsScheduleTarget.target_id == tid,
            ))
            msg = "Target removed from the schedule."
        _audit(db, user, action_type, "schedule", sched.id_schedule,
               {"target_type": tt, "target_id": tid})
        await db.commit()
        await _push_targets_sync(db, user.id_org, tt, tid)
        return {"ok": True, "message": msg}

    # ── Suppression d'un programme (soft-delete + resync des cibles) ───────────
    if action_type == "delete_schedule":
        from app.models.schedule import CmsSchedule, CmsScheduleTarget
        sched = (await db.execute(select(CmsSchedule).where(
            CmsSchedule.id_schedule == int(params["id_schedule"]),
            CmsSchedule.id_org == user.id_org, CmsSchedule.is_deleted == False,  # noqa: E712
        ))).scalar_one_or_none()
        if sched is None:
            return {"ok": False, "error": "Schedule not found."}
        targets = (await db.execute(select(CmsScheduleTarget).where(
            CmsScheduleTarget.id_schedule == sched.id_schedule))).scalars().all()
        name = sched.schedule_name
        sched.is_deleted = True
        _audit(db, user, action_type, "schedule", sched.id_schedule, {"name": name})
        await db.commit()
        for t in targets:
            await _push_targets_sync(db, user.id_org, t.target_type, t.target_id)
        return {"ok": True, "message": f"Schedule \"{name}\" deleted."}

    # ── Création d'un événement manuel (inactif → à déclencher) ────────────────
    if action_type == "create_event":
        from app.models.event import CmsEvent
        ev = CmsEvent(
            id_org=user.id_org,
            event_name=params["event_name"],
            event_type="manual",
            event_kind="content",
            id_playlist=params.get("id_playlist"),
            id_media=params.get("id_media"),
            duration_ms=params.get("duration_ms"),
            target_config=params["target_config"],
            keep_screens_on=params.get("keep_screens_on") is True,
            created_by=user.id_user,
        )
        db.add(ev)
        await db.flush()
        _audit(db, user, action_type, "event", ev.id_event, params)
        await db.commit()
        return {"ok": True, "message": f"Event \"{params['event_name']}\" created (inactive)."}

    return {"ok": False, "error": "Action not allowed."}


async def execute_batch(db: AsyncSession, user: CmsUser, steps: list[dict]) -> dict:
    """Exécute une SÉRIE d'actions confirmée en un seul tour, avec **publish DIFFÉRÉ**.

    Les mutations de brouillon de toutes les étapes touchant une MÊME playlist sont d'abord
    appliquées, PUIS un seul `publish_playlist` par playlist concernée à la fin → les players
    passent directement de l'ancien état au nouvel état (jamais d'état VIDE transitoire entre
    deux étapes), avec un seul resync graceful par playlist. Les actions indépendantes
    (création playlist/feed, piste audio) s'exécutent en place.

    `steps` = [{"act": action_type, "params": {...}}, …]. Le RBAC est re-vérifié pour CHAQUE
    étape avant toute écriture (refus de tout le lot si une étape n'est pas permise).
    """
    if not steps:
        return {"ok": False, "error": "No action to run."}

    # Le contexte RLS (app.current_org_id) est garanti à chaque transaction par le listener
    # `after_begin` (db/session.py) → pas de re-pose défensive nécessaire ici.
    for step in steps:
        act = step.get("act")
        if act not in ACTION_PERMISSIONS:
            return {"ok": False, "error": "Action not allowed."}
        if not await check_action_permission(db, user.id_user, act):
            return {"ok": False, "error": f"Permission denied for \"{act}\"."}

    # Imports locaux (évite l'import circulaire endpoint ↔ service)
    from fastapi import Response

    from app.api.v1.endpoints.playlists import _ensure_draft, _next_uid, publish_playlist

    drafts: dict[int, list[dict]] = {}       # id_playlist → brouillon muté (en mémoire)
    playlists: dict[int, CmsPlaylist] = {}   # id_playlist → entité
    results: list[dict] = []

    for step in steps:
        act = step["act"]
        params = step["params"]

        # Lecture VOD ad-hoc (takeover play_now) : payload résolu PAR player (rendition) + dispatch.
        if act == "play_vod":
            media = (await db.execute(select(CmsMedia).where(
                CmsMedia.id_media == int(params["id_media"]), CmsMedia.id_org == user.id_org,
                CmsMedia.is_deleted == False, CmsMedia.is_blocked == False,  # noqa: E712
            ))).scalar_one_or_none()
            pids = [int(x) for x in params.get("player_ids", [])]
            if media is None or not pids:
                results.append({"ok": False, "message": "Media or VOD players not found."})
                continue
            players_v = (await db.execute(
                select(CmsPlayer).where(CmsPlayer.id_player.in_(pids))
            )).scalars().all()
            from app.api.v1.endpoints.vod import _resolve_play_payload
            redis = aioredis.from_url(settings.REDIS_URL)
            try:
                for pl_v in players_v:
                    payload = await _resolve_play_payload(db, pl_v, media, params["mode"])
                    await redis.publish(
                        f"signflow:player:{pl_v.id_player}:cmds",
                        json.dumps({"type": "player:command", "command": "play_now",
                                    "params": payload}),
                    )
            finally:
                await redis.aclose()
            _audit(db, user, act, "vod", media.id_media,
                   {"player_ids": pids, "mode": params["mode"]})
            results.append({
                "ok": True,
                "message": (f"VOD \"{media.media_name}\" ({params['mode']}) → "
                            f"{len(players_v)} player(s)."),
            })
            continue

        # Écran physique (RS232/IP) : display_command sur les players équipés résolus.
        if act in ("screen_power", "screen_set_input"):
            pids = [int(x) for x in params.get("player_ids", [])]
            extra = {"source": params["source"]} if act == "screen_set_input" else {}
            await _dispatch_display_command(pids, params["display_cmd"], extra)
            _audit(db, user, act, "player", None,
                   {"player_ids": pids, "display_cmd": params["display_cmd"], **extra})
            results.append({"ok": True,
                            "message": f"Screen command sent to {len(pids)} screen(s)."})
            continue

        # Switch entrée HDMI (BrightSign à entrée) : même commande que le drawer/VOD — la
        # session est tracée par le player (audit hdmi_session + reprise wall-clock au stop).
        if act == "hdmi_switch":
            pids = [int(x) for x in params.get("player_ids", [])]
            redis = aioredis.from_url(settings.REDIS_URL)
            try:
                msg = json.dumps({"type": "player:command", "command": "hdmi_input",
                                  "params": {"mode": params["mode"], "source": "assistant"}})
                for pid in pids:
                    await redis.publish(f"signflow:player:{pid}:cmds", msg)
            finally:
                await redis.aclose()
            _audit(db, user, act, "player", None,
                   {"player_ids": pids, "mode": params["mode"]})
            verb = "HDMI switch" if params["mode"] == "start" else "Back to playlist"
            results.append({"ok": True, "message": f"{verb} sent to {len(pids)} player(s)."})
            continue

        # Fuseau device : commande set_timezone (le player applique et remonte au heartbeat).
        if act == "set_player_timezone":
            pids = [int(x) for x in params.get("player_ids", [])]
            redis = aioredis.from_url(settings.REDIS_URL)
            try:
                msg = json.dumps({"type": "player:command", "command": "set_timezone",
                                  "params": {"timezone": params["timezone"]}})
                for pid in pids:
                    await redis.publish(f"signflow:player:{pid}:cmds", msg)
            finally:
                await redis.aclose()
            _audit(db, user, act, "player", pids[0] if pids else None,
                   {"player_ids": pids, "timezone": params["timezone"]})
            results.append({"ok": True,
                            "message": f"Time zone \"{params['timezone']}\" sent "
                                       f"(check it is reported at the next heartbeat)."})
            continue

        # Rangement de médias dans un dossier (mutation DB directe, scopée org).
        if act == "move_media_to_folder":
            mids = [int(x) for x in params.get("id_media", [])]
            fid = int(params["id_folder"])
            medias_mv = (await db.execute(select(CmsMedia).where(
                CmsMedia.id_media.in_(mids), CmsMedia.id_org == user.id_org,
            ))).scalars().all()
            for m_mv in medias_mv:
                m_mv.id_folder = fid
            _audit(db, user, act, "media_folder", fid,
                   {"id_media": mids, "count": len(medias_mv)})
            await db.commit()
            results.append({"ok": True,
                            "message": f"{len(medias_mv)} media item(s) moved to "
                                       f"\"{params.get('folder_name', '')}\"."})
            continue

        # Mise à jour OTA §XXX : UN déploiement approuvé PAR version/plateforme (réutilise
        # ota.create_deployment = mêmes gardes plateforme/rollback que l'UI), puis force_sync
        # → les players voient la directive `update` immédiatement (même flux que l'endpoint).
        if act == "update_players":
            from app.services import ota
            from app.services.firmware import FirmwareRequired
            from app.services.targeting import Target
            done: list[str] = []
            for g in params.get("groups", []):
                pids = [int(x) for x in g.get("player_ids", [])]
                if not pids:
                    continue
                try:
                    dep = await ota.create_deployment(
                        db, user.id_org, int(g["id_version"]),
                        [Target("player", pid) for pid in pids],
                        "immediate", {}, user.id_user,
                    )
                # Décision 2 : firmware BS manquant → message actionnable, pas un échec générique.
                except FirmwareRequired as e:
                    results.append({"ok": False, "message": str(e)})
                    continue
                await _dispatch_player_command(pids, "force_sync")
                _audit(db, user, act, "update_deployment", dep.id_deployment,
                       {"id_version": int(g["id_version"]), "version": g.get("version"),
                        "platform": g.get("platform"), "player_ids": pids})
                done.append(f"\"{g.get('version')}\" ({g.get('platform')}) → "
                            f"{dep.total_targets} player(s)")
            await db.commit()
            if not done:
                results.append({"ok": False, "message": "No player to update."})
                continue
            results.append({"ok": True,
                            "message": "OTA deployment created: " + "; ".join(done)
                                       + ". Track it in Settings → Updates."})
            continue

        # Commande player (dispatch Redis, pas de mutation DB) : fan-out sur les players résolus.
        if act in PLAYER_COMMANDS:
            cmd = PLAYER_COMMANDS[act]
            pids = [int(x) for x in params.get("player_ids", [])]
            await _dispatch_player_command(pids, cmd)
            _audit(db, user, act, "player", None, {"player_ids": pids, "command": cmd})
            results.append(
                {"ok": True, "message": f"Command \"{cmd}\" sent to {len(pids)} player(s)."}
            )
            continue

        # Retrait en masse d'un feed de plusieurs playlists : mute chaque brouillon via la
        # même machinerie (drafts) → un seul publish différé par playlist touchée.
        if act == "remove_feed_from_active_playlists":
            id_media = int(params["id_media"])
            removed = 0
            for raw in params.get("id_playlists", []):
                id_pl = int(raw)
                if id_pl not in playlists:
                    pl = await _get_playlist(db, id_pl)
                    if pl is None:
                        continue
                    playlists[id_pl] = pl
                    drafts[id_pl] = await _ensure_draft(db, pl)
                before = drafts[id_pl]
                after = [e for e in before if e.get("id_media") != id_media]
                if len(after) != len(before):
                    drafts[id_pl] = after
                    removed += 1
                _audit(db, user, act, "playlist", id_pl, {"id_media": id_media})
            results.append({"ok": True, "message": f"Feed removed from {removed} playlist(s)."})
            continue

        if act not in _PLAYLIST_OPS:
            results.append(await _execute_self_contained(db, user, act, params))
            continue

        id_pl = int(params["id_playlist"])
        if id_pl not in playlists:
            pl = await _get_playlist(db, id_pl)
            if pl is None:
                results.append({"ok": False, "message": "Playlist not found."})
                continue
            playlists[id_pl] = pl
            drafts[id_pl] = await _ensure_draft(db, pl)
        draft = drafts[id_pl]

        if act == "add_playlist_item":
            draft.append({
                "uid": _next_uid(draft), "id_media": int(params["id_media"]),
                "sort_order": len(draft), "duration_ms": params.get("duration_ms"),
                "zone_index": int(params.get("zone_index", 0)),
                "transition_type": "cut", "rules": [],
            })
            results.append({"ok": True, "message": "Media added to the playlist."})
        elif act == "add_tagged_content_to_playlist":
            uid = _next_uid(draft)
            order = len(draft)
            for mid in params["id_medias"]:
                draft.append({
                    "uid": uid, "id_media": int(mid), "sort_order": order,
                    "duration_ms": None, "zone_index": 0, "transition_type": "cut", "rules": [],
                })
                uid -= 1
                order += 1
            results.append({"ok": True,
                            "message": f"{len(params['id_medias'])} item(s) added."})
        elif act == "set_playlist_item_rules":
            id_media = int(params["id_media"])
            zone = params.get("zone_index")
            rules_list = params.get("rules") or []
            # Clé de scope : on ne remplace QUE les règles du même scope/cible (préserve le
            # `default` quand on pose un override player/groupe, et inversement).
            key = (params.get("scope", "default"), params.get("target_id"))
            matched = 0
            for e in draft:
                if e.get("id_media") == id_media and (zone is None or e.get("zone_index") == zone):
                    kept = [r for r in (e.get("rules") or [])
                            if (r.get("scope", "default"), r.get("target_id")) != key]
                    e["rules"] = kept + [dict(r) for r in rules_list]
                    matched += 1
            if matched == 0:
                results.append({"ok": False, "message": "Media not in the playlist."})
                continue
            results.append({"ok": True, "message": f"Rules applied to {matched} occurrence(s)."})
        else:  # remove_playlist_item
            id_media = int(params["id_media"])
            new = [e for e in draft if e.get("id_media") != id_media]
            if len(new) == len(draft):
                results.append({"ok": False, "message": "Media already not in the playlist."})
                continue
            drafts[id_pl] = new
            results.append({"ok": True, "message": "Media removed from the playlist."})

        _audit(db, user, act, "playlist", id_pl, params)

    # Persiste tous les brouillons, PUIS un seul publish (graceful) par playlist → aucun
    # état vide transitoire et un unique resync par playlist concernée.
    for id_pl, draft in drafts.items():
        playlists[id_pl].draft_items = draft
    await db.commit()
    for id_pl in drafts:
        # current_user = l'utilisateur qui exécute l'action (garde de gouvernance appliquée :
        # il doit pouvoir éditer le nœud d'une playlist gouvernée).
        await publish_playlist(id_pl, user, db, Response())
    await db.commit()

    ok = bool(results) and all(r.get("ok") for r in results)
    msgs = [r.get("message") or r.get("error") or "" for r in results]
    return {"ok": ok, "results": results, "message": " ".join(m for m in msgs if m)}


async def execute_action(db: AsyncSession, user: CmsUser, action_type: str, params: dict) -> dict:
    """Exécute UNE action validée (= lot d'une seule étape). Re-vérifie le RBAC, réutilise
    les chemins existants (staging/publish/push, création feed+pointeur…), journalise dans
    cms_audit_log. Conservé pour la compatibilité (tests, token mono-action legacy)."""
    return await execute_batch(db, user, [{"act": action_type, "params": params}])
