"""WebSocket player — connexion persistante, heartbeat, fan-out Redis (§III.4).

GET /ws/player?token=XXX

Flux :
  1. Auth token player (type="player", sub=id_player)
  2. Marquer player online + mettre à jour ip_address
  3. Envoyer player:sync initial (schedule actif + items + URLs signées MinIO)
  4. Boucle recv : heartbeats player → mise à jour métriques DB
  5. Boucle pubsub : commandes Redis → forward au player
  6. Déconnexion → marquer offline

Fan-out Redis : PUBLISH signflow:player:{id}:cmds <json>
"""

import asyncio
import calendar
import ipaddress
import json
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _local_now(tz_name: str | None = None) -> datetime:
    """Instant courant en heure locale de l'ÉCRAN (délègue au noyau Scheduling).

    `tz_name` = fuseau du SITE du player (`player_timezone.player_tz_name`). Sans lui on
    retombe sur SIGNFLOW_TZ, ce qui n'est juste que pour un parc mono-fuseau : un écran à
    New York évaluerait ses règles horaires à l'heure du serveur (P2.2). Tout appelant qui
    connaît le player doit donc le passer.
    """
    return local_now(tz_name)

import anyio
import redis.asyncio as aioredis
from fastapi import APIRouter, Query, WebSocket
from jose import JWTError
from sqlalchemy import case, func, or_, select, update

from app.core.config import settings
from app.core.security import create_player_token, decode_token
from app.db.session import WorkerSessionLocal
from app.models.event import CmsEvent
from app.models.feed import CmsDatafeed, CmsFeedItem
from app.models.media import CmsMedia, CmsMediaRendition
from app.models.organization import CmsSite, CmsSiteException, CmsSiteHours
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroupMember
from app.models.playlist import CmsPlaylist, CmsPlaylistItem, CmsPlaylistItemRule
from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget, CmsScheduleBasket
from app.models.template import CmsTemplate
from app.models.audio_basket import (
    CmsAudioBasket,
    CmsAudioBasketItem,
    CmsPlayerBasketConfig,
)
from app.models.audit_log import CmsAuditLog
from app.models.local_control import CmsLocalControlProfile
from app.models.tag import (
    CmsMediaTagValue,
    CmsPlayerTagFilter,
    CmsPlaylistItemNsOverride,
    CmsPlaylistItemTag,
    CmsTagNamespace,
    CmsTagValue,
)
from app.services import campaign_audio
from app.services import campaign_delivery
from app.services import document_media
from app.services import ota
from app.services import player_timezone
from app.services import renditions as rendition_svc
from app.services.playlist_rotation import rotation_seed, unroll_slots
from app.services import site_hours as site_hours_svc
from app.services import video_wall
from app.services.targeting import (
    event_target_matches,
    player_site_group_ids,
    player_site_id,
)
from app.services.player_token import (
    IDENTITY_PREV,
    IDENTITY_UNKNOWN,
    grace_until,
    needs_rotation,
    token_identity,
)
from app.services.media import get_signed_url
from app.services.prefetch import upcoming_media
from app.services.scheduling import (
    TimeRule,
    local_now,
    rule_active_at,
    rules_active_at,
    schedule_active_at,
    time_rule_from_schedule,
)
from app.services.tags import compute_effective_tags, item_matches_player

router = APIRouter(prefix="/ws", tags=["websocket"])


# ── Wall-clock sync helpers ────────────────────────────────────────────────────

def _epoch_ms(dt: datetime) -> int:
    """datetime naïf UTC → epoch millisecondes."""
    return calendar.timegm(dt.timetuple()) * 1000


def _midnight_ms(now: datetime) -> int:
    """Minuit UTC du jour courant → epoch ms."""
    return _epoch_ms(datetime(now.year, now.month, now.day))


def _wall_clock_position(items: list[dict], ref_ms: int, now_ms: int) -> tuple[int, int] | None:
    """
    Calcule (start_index, start_offset_ms) pour la position wall-clock.

    Retourne None si le fallback loop doit s'appliquer (item sans durée, total=0).
    """
    if not items:
        return None
    if any(not item.get("duration_ms") for item in items):
        return None
    total_ms = sum(item["duration_ms"] for item in items)
    if total_ms == 0:
        return None
    elapsed_ms = (now_ms - ref_ms) % total_ms
    cumulative = 0
    for i, item in enumerate(items):
        dur = item["duration_ms"]
        if elapsed_ms < cumulative + dur:
            return i, elapsed_ms - cumulative
        cumulative += dur
    return 0, 0


def _wall_clock_ref(now: datetime, active_event, matched_rule) -> tuple[int, int]:
    """Référence (ref_ms, now_ms) pour la position wall-clock — partagée par la liste
    plate et par chaque zone d'un template multi-zones (§25.4)."""
    if active_event and active_event.activated_at:
        # Events : activated_at est UTC-aware → comparer en UTC pour les deux termes
        now_utc_aware = datetime.now(UTC)
        now_ms = int(now_utc_aware.timestamp() * 1000)
        act_at = active_event.activated_at
        if act_at.tzinfo is None:
            act_at = act_at.replace(tzinfo=UTC)
        ref_ms = int(act_at.timestamp() * 1000)
    elif matched_rule and matched_rule.time_start:
        now_ms = _epoch_ms(now)
        ref_dt = datetime(now.year, now.month, now.day,
                          matched_rule.time_start.hour, matched_rule.time_start.minute, 0)
        ref_ms = _epoch_ms(ref_dt)
    else:
        now_ms = _epoch_ms(now)
        ref_ms = _midnight_ms(now)
    return ref_ms, now_ms


def _build_template_block(
    template_row: "CmsTemplate", items: list[dict], sync_mode: str,
    ref_ms: int, now_ms: int,
) -> dict | None:
    """Bloc `template` du payload de sync (§25.4 zones de template).

    Un template à PLUSIEURS zones → le player rend chaque zone comme une sous-playlist
    indépendante (items filtrés par `zone_index`, wall-clock propre à la zone, rendu
    Chromium). Un template à une seule zone (ou aucune) = équivalent plein écran mono-zone
    → on n'émet PAS de bloc (le player garde son chemin mono-zone : LAN/mpv/VOD intacts).
    Les items sont déjà filtrés (tags + règles de diffusion) en amont.
    """
    layout = template_row.layout or {}
    zones_def = layout.get("zones") or []
    if len(zones_def) <= 1:
        return None
    zones_out = []
    for z in zones_def:
        zi = int(z.get("index", 0))
        z_items = [it for it in items if int(it.get("zone_index", 0)) == zi]
        s_idx, s_off = 0, 0
        if sync_mode == "wall_clock" and z_items:
            pos = _wall_clock_position(z_items, ref_ms, now_ms)
            if pos is not None:
                s_idx, s_off = pos
        zones_out.append({
            "index": zi,
            "x": int(z.get("x", 0)), "y": int(z.get("y", 0)),
            "w": int(z.get("w", 0)), "h": int(z.get("h", 0)),
            "label": z.get("label", ""),
            "items": z_items,
            "start_index": s_idx,
            "start_offset_ms": s_off,
        })
    return {
        "id_template": template_row.id_template,
        "canvas_width": int(layout.get("canvas_width") or 1920),
        "canvas_height": int(layout.get("canvas_height") or 1080),
        "layout_type": layout.get("layout_type") or "full",
        "zones": zones_out,
    }


# ── Filtrage par tags (§XVII) ───────────────────────────────────────────────────

async def _filter_items_by_tags(db, id_player: int, items: list[dict]) -> list[dict]:
    """Retire les items que le profil de réception (tags) du player n'accepte pas.

    Contenu sans tag dans un namespace filtré = universel (passe). Player sans
    filtre = accepte tout. Batch-load des tags média/item/override pour éviter le N+1.
    """
    if not items:
        return items

    # Profil du player. Aucun filtre → tout passe (cas par défaut, sortie rapide).
    pf_rows = (await db.execute(
        select(CmsTagNamespace.namespace_key, CmsTagValue.tag_value)
        .join(CmsTagValue, CmsTagValue.id_namespace == CmsTagNamespace.id_namespace)
        .join(CmsPlayerTagFilter, CmsPlayerTagFilter.id_tag == CmsTagValue.id_tag)
        .where(CmsPlayerTagFilter.id_player == id_player)
    )).all()
    if not pf_rows:
        return items
    player_filters: dict[str, list[str]] = {}
    for ns_key, val in pf_rows:
        player_filters.setdefault(ns_key, []).append(val)

    media_ids = [it["media"]["id_media"] for it in items if it.get("media")]
    item_ids = [it["id_item"] for it in items if it.get("id_item") is not None]

    media_tags_map: dict[int, dict[str, list[str]]] = {}
    if media_ids:
        for mid, ns_key, val in (await db.execute(
            select(CmsMediaTagValue.id_media, CmsTagNamespace.namespace_key, CmsTagValue.tag_value)
            .join(CmsTagValue, CmsTagValue.id_tag == CmsMediaTagValue.id_tag)
            .join(CmsTagNamespace, CmsTagNamespace.id_namespace == CmsTagValue.id_namespace)
            .where(CmsMediaTagValue.id_media.in_(media_ids))
        )).all():
            media_tags_map.setdefault(mid, {}).setdefault(ns_key, []).append(val)

    item_tags_map: dict[int, dict[str, list[str]]] = {}
    item_ovr_map: dict[int, set[str]] = {}
    if item_ids:
        for iid, ns_key, val in (await db.execute(
            select(CmsPlaylistItemTag.id_item, CmsTagNamespace.namespace_key, CmsTagValue.tag_value)
            .join(CmsTagValue, CmsTagValue.id_tag == CmsPlaylistItemTag.id_tag)
            .join(CmsTagNamespace, CmsTagNamespace.id_namespace == CmsTagValue.id_namespace)
            .where(CmsPlaylistItemTag.id_item.in_(item_ids))
        )).all():
            item_tags_map.setdefault(iid, {}).setdefault(ns_key, []).append(val)
        for iid, ns_key in (await db.execute(
            select(CmsPlaylistItemNsOverride.id_item, CmsTagNamespace.namespace_key)
            .join(CmsTagNamespace, CmsTagNamespace.id_namespace == CmsPlaylistItemNsOverride.id_namespace)
            .where(CmsPlaylistItemNsOverride.id_item.in_(item_ids))
        )).all():
            item_ovr_map.setdefault(iid, set()).add(ns_key)

    out = []
    for it in items:
        mid = (it.get("media") or {}).get("id_media")
        iid = it.get("id_item")
        eff = compute_effective_tags(
            media_tags_map.get(mid, {}),
            item_tags_map.get(iid, {}),
            item_ovr_map.get(iid, set()),
        )
        if item_matches_player(eff, player_filters):
            out.append(it)
    return out


def _item_rule_to_timerule(r: CmsPlaylistItemRule) -> TimeRule:
    return TimeRule(
        rule_type=r.rule_type,
        days_of_week=r.days_of_week,
        time_start=r.time_start,
        time_end=r.time_end,
        date_from=r.date_from,
        date_to=r.date_to,
    )


async def _filter_items_by_rules(
    items: list[dict], now: datetime, db, id_player: int, group_ids: set[int]
) -> list[dict]:
    """Retire les items hors de leur fenêtre de diffusion (§ retours clients item 4).

    Un item sans règle = perpétuel (gardé). Sinon, on retient le jeu de règles **le plus
    spécifique** pour ce player (player > player_group > default, override pas union) et
    l'item est gardé ssi l'heure LOCALE `now` tombe dans l'une de ces règles.
    Batch-load des règles pour éviter le N+1.
    """
    item_ids = [it["id_item"] for it in items if it.get("id_item") is not None]
    if not item_ids:
        return items

    # Par item : règles classées par scope (player ne retient que celles ciblant CE player ;
    # group ne retient que les groupes du player).
    player_rules: dict[int, list[TimeRule]] = {}
    group_rules: dict[int, list[TimeRule]] = {}
    default_rules: dict[int, list[TimeRule]] = {}
    for r in (await db.execute(
        select(CmsPlaylistItemRule).where(CmsPlaylistItemRule.id_item.in_(item_ids))
    )).scalars().all():
        if r.scope == "player":
            if r.target_id == id_player:
                player_rules.setdefault(r.id_item, []).append(_item_rule_to_timerule(r))
        elif r.scope == "player_group":
            if r.target_id in group_ids:
                group_rules.setdefault(r.id_item, []).append(_item_rule_to_timerule(r))
        else:  # default
            default_rules.setdefault(r.id_item, []).append(_item_rule_to_timerule(r))

    if not (player_rules or group_rules or default_rules):
        return items

    out = []
    for it in items:
        iid = it.get("id_item")
        # le plus spécifique l'emporte (remplace, pas union)
        effective = player_rules.get(iid) or group_rules.get(iid) or default_rules.get(iid)
        if not effective or rules_active_at(effective, now):
            out.append(it)
    return out


async def _build_basket_items(db, id_basket: int, now: datetime) -> list[dict]:
    """Items actifs d'un panier (dans leur fenêtre de validité) + URLs signées."""
    item_rows = (await db.execute(
        select(CmsAudioBasketItem, CmsMedia)
        .join(CmsMedia, CmsMedia.id_media == CmsAudioBasketItem.id_media)
        .where(
            CmsAudioBasketItem.id_basket == id_basket,
            CmsAudioBasketItem.is_active == True,  # noqa: E712
            CmsMedia.is_deleted == False,  # noqa: E712
            CmsMedia.is_blocked == False,  # noqa: E712 — quarantaine (§ Q5), audio inclus
        )
        .order_by(CmsAudioBasketItem.sort_order)
    )).all()
    items = []
    for it, media in item_rows:
        if it.valid_from and it.valid_from > now.astimezone(it.valid_from.tzinfo):
            continue
        if it.valid_until and it.valid_until < now.astimezone(it.valid_until.tzinfo):
            continue
        # Règle de diffusion récurrente (heure locale) : jours + créneau. Absente = perpétuel.
        if it.rule_days or it.rule_time_start or it.rule_time_end:
            tr = TimeRule(
                rule_type="weekly",
                days_of_week=it.rule_days,
                time_start=it.rule_time_start,
                time_end=it.rule_time_end,
            )
            if not rule_active_at(tr, now):
                continue
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, media.storage_key, 86400))
        except Exception:
            url = None
        items.append({
            "id_media": media.id_media,
            "media_name": media.media_name,
            "url": url,
            "duration_ms": media.duration_ms,
            "weight": it.weight,
            "checksum_sha256": media.checksum_sha256,
            # Offline-first : l'audio est mis en cache local comme la vidéo/image. Pas de
            # rendition pour l'audio → cache_key = storage_key servi. Le player télécharge
            # et sert en signflow:// → musique disponible offline + plus d'échec stream.
            "cache_key": media.storage_key,
            "cache_checksum": media.checksum_sha256,
            "mime_type": media.mime_type,
            "media_type": "audio",
        })
    return items


async def _resolve_audio_baskets(db, id_player: int, tz_name: str | None = None) -> list[dict]:
    """
    Résout les paniers audio d'un player (§XIX) — modèle *set-based* : plusieurs
    paniers peuvent jouer simultanément (mixés selon leurs ratios, ducking côté player).

    Sources, par ordre d'application :
      1. Schedules audio ciblant le player (direct + groupes) dont une règle matche
         l'instant courant → panier actif au ratio du schedule.
      2. Events audio actifs ciblant le player → override par panier (priment).
      3. Overrides opérateur (Store Control Panel, cms_player_basket_config) :
         désactivent ou ré-ajustent le ratio d'un panier déjà programmé — SAUF sur un
         panier `is_mandatory`, qui ne se laisse ni couper ni ré-ratier (A2).
    Clamp final : si la somme des ratios > 100, on droppe les paniers de plus basse
    priorité. Retourne [] si aucun panier actif.

    ⚠️ Tout ce qui entre ici est filtré par l'ORGANISATION du player (A1, audit du
    2026-07-29) : la session est BYPASSRLS, donc rien ne le fait à notre place.

    `tz_name` (P2.2) : fuseau du site du player. Ce chemin calcule son PROPRE instant —
    ne pas le lui passer ferait diverger le son et l'image sur le même écran.
    """
    now = _local_now(tz_name or await player_timezone.player_tz_name(db, id_player))

    # 🔑 A1 — l'org du player est la clé de TOUS les filtres ci-dessous. Le chemin audio
    # était le seul à ne pas la poser (les deux chemins visuels le font), et
    # `event_target_matches` accepte `target_type='org'` INCONDITIONNELLEMENT : un
    # événement audio d'une organisation activait donc son panier sur les écrans de
    # TOUTES les organisations. Latent en production (0 événement audio) — mais c'est une
    # diffusion inter-tenant, la classe la plus grave du produit.
    id_org = (await db.execute(
        select(CmsPlayer.id_org).where(CmsPlayer.id_player == id_player)
    )).scalar_one_or_none()
    if id_org is None:
        return []

    group_ids = set((await db.execute(
        select(CmsPlayerGroupMember.id_group).where(CmsPlayerGroupMember.id_player == id_player)
    )).scalars().all())
    site_group_ids = await player_site_group_ids(db, id_player)
    id_site = await player_site_id(db, id_player)

    # ── 1. Schedules audio ciblant le player ───────────────────────────────────
    sched_ids: set[int] = set((await db.execute(
        select(CmsScheduleTarget.id_schedule).where(
            CmsScheduleTarget.target_type == "player",
            CmsScheduleTarget.target_id == id_player,
        )
    )).scalars().all())
    if group_ids:
        sched_ids |= set((await db.execute(
            select(CmsScheduleTarget.id_schedule).where(
                CmsScheduleTarget.target_type == "player_group",
                CmsScheduleTarget.target_id.in_(group_ids),
            )
        )).scalars().all())
    if id_site is not None:
        sched_ids |= set((await db.execute(
            select(CmsScheduleTarget.id_schedule).where(
                CmsScheduleTarget.target_type == "site",
                CmsScheduleTarget.target_id == id_site,
            )
        )).scalars().all())
    if site_group_ids:
        sched_ids |= set((await db.execute(
            select(CmsScheduleTarget.id_schedule).where(
                CmsScheduleTarget.target_type == "site_group",
                CmsScheduleTarget.target_id.in_(site_group_ids),
            )
        )).scalars().all())

    # id_basket -> {"ratio", "priority", "source"}
    collected: dict[int, dict] = {}

    if sched_ids:
        scheds = (await db.execute(
            select(CmsSchedule).where(
                CmsSchedule.id_schedule.in_(sched_ids),
                CmsSchedule.schedule_kind == "audio",
                CmsSchedule.is_active == True,  # noqa: E712
                CmsSchedule.is_deleted == False,  # noqa: E712
            ).order_by(CmsSchedule.priority.desc())
        )).scalars().all()
        for sched in scheds:
            rules = (await db.execute(
                select(CmsScheduleRule).where(CmsScheduleRule.id_schedule == sched.id_schedule)
            )).scalars().all()
            if not rules or not any(
                schedule_active_at(time_rule_from_schedule(r), sched.valid_from, sched.valid_until, now)
                for r in rules
            ):
                continue
            # Programme multi-paniers : chaque panier du schedule rejoint le mix
            sbs = (await db.execute(
                select(CmsScheduleBasket).where(CmsScheduleBasket.id_schedule == sched.id_schedule)
            )).scalars().all()
            for sb in sbs:
                prev = collected.get(sb.id_basket)
                if prev is None or sched.priority > prev["priority"]:
                    collected[sb.id_basket] = {
                        "ratio": sb.mix_ratio, "priority": sched.priority, "source": "schedule"
                    }

    # ── 2. Events audio actifs (override) ──────────────────────────────────────
    audio_events = (await db.execute(
        select(CmsEvent).where(
            CmsEvent.id_org == id_org,   # A1 — cf. en-tête : sans ça, `target_type='org'`
            CmsEvent.status == "active", #      d'une autre organisation matchait ici.
            CmsEvent.event_kind == "audio",
        ).order_by(CmsEvent.priority.desc())
    )).scalars().all()
    for ev in audio_events:
        if ev.id_basket is None:
            continue
        if not event_target_matches(
            ev.target_config, id_player, group_ids, site_group_ids, id_site=id_site
        ):
            continue
        ratio = ev.mix_ratio if ev.mix_ratio is not None else 50
        # Les events priment sur les schedules pour un même panier (priorité décalée)
        collected[ev.id_basket] = {"ratio": ratio, "priority": 1000 + ev.priority, "source": "event"}

    # ── 3. Overrides opérateur (Store Control Panel §19.3) ─────────────────────
    # A2 — un panier OBLIGATOIRE ne se laisse ni couper ni ré-ratier par l'opérateur local.
    # C'est ce que l'écran promet depuis toujours (★ « ratio verrouillé pour les niveaux
    # inférieurs ») et que rien ne vérifiait : 3 des 6 paniers de la production portent ce
    # drapeau. Le point d'application est ICI, à la résolution, parce que c'est le seul
    # endroit que TOUS les chemins traversent — et parce que des lignes d'override
    # antérieures à ce correctif peuvent déjà exister en base.
    mandatory_ids: set[int] = set()
    if collected:
        mandatory_ids = set((await db.execute(
            select(CmsAudioBasket.id_basket).where(
                CmsAudioBasket.id_basket.in_(collected.keys()),
                CmsAudioBasket.is_mandatory == True,  # noqa: E712
            )
        )).scalars().all())

    overrides = (await db.execute(
        select(CmsPlayerBasketConfig).where(CmsPlayerBasketConfig.id_player == id_player)
    )).scalars().all()
    for ov in overrides:
        if ov.id_basket in collected and ov.id_basket not in mandatory_ids:
            if not ov.is_active:
                del collected[ov.id_basket]
            else:
                collected[ov.id_basket]["ratio"] = ov.mix_ratio

    if not collected:
        return []

    # ── 4+5. Payloads + budget. Le budget ≤100 ne concerne QUE le mix continu
    # (music/ambiance), et il est SÉPARÉ PAR FAMILLE : music et ambiance sont 2 couches
    # jouées SIMULTANÉMENT (pistes parallèles côté player), chacune son budget propre
    # (somme des % music ≤100 ET somme des % ambiance ≤100, indépendamment). Les paniers
    # déclencheurs (message/jingle/emergency) ne consomment aucun budget et ne sont jamais
    # droppés (ils sont insérés/déclenchés par le runtime — cf. audio_player.js).
    ordered = sorted(collected.items(), key=lambda kv: kv[1]["priority"], reverse=True)
    baskets = []
    mix_total = {"music": 0, "ambiance": 0}  # budget consommé PAR famille
    for id_basket, meta in ordered:
        basket = (await db.execute(
            select(CmsAudioBasket).where(
                CmsAudioBasket.id_basket == id_basket,
                CmsAudioBasket.id_org == id_org,   # A1 — dernier verrou : même si une ligne
                CmsAudioBasket.is_active == True,  # de programme/override désignait un panier
            )                                      # étranger, il ne sortirait pas d'ici.
        )).scalar_one_or_none()
        if basket is None:
            continue
        if basket.basket_type in mix_total:
            if mix_total[basket.basket_type] + meta["ratio"] > 100:
                continue  # dépasse le budget de SA famille → drop (priorité plus basse)
            mix_total[basket.basket_type] += meta["ratio"]
        items = await _build_basket_items(db, id_basket, now)
        if not items:
            continue
        baskets.append({
            "id_basket": basket.id_basket,
            "basket_name": basket.basket_name,
            "basket_type": basket.basket_type,
            "mix_ratio": meta["ratio"],
            "min_ratio": basket.min_ratio,
            "max_ratio": basket.max_ratio,
            "ducking_config": basket.ducking_config or {},
            "trigger_mode": basket.trigger_mode,
            "trigger_value": basket.trigger_value,
            "shuffle": basket.shuffle,
            "items": items,
        })

    return baskets


async def _resolve_local_control(db, id_player: int, display_config: dict | None) -> dict | None:
    """
    Résout le profil d'accès au Store Control Panel LAN d'un player (§19.3).

    Retourne le profil actif (token + permissions) SI le serveur de contrôle local
    est activé sur ce player (display_config.local_control_enabled). Le player utilise
    ce profil pour valider le token QR de l'opérateur et ouvrir une session locale.

    Retourne None si désactivé, ou si aucun profil actif/non-expiré n'existe.
    """
    if not display_config or not display_config.get("local_control_enabled"):
        return None

    profile = (await db.execute(
        select(CmsLocalControlProfile).where(
            CmsLocalControlProfile.id_player == id_player,
            CmsLocalControlProfile.revoked == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if profile is None:
        return None
    # Expiration : profil expiré = inutile à transmettre
    if profile.expires_at and profile.expires_at < datetime.now(UTC):
        return None

    return {
        "token": profile.token,
        "role": profile.role,
        "allowed_basket_ids": profile.allowed_basket_ids,
        "can_edit_ratio": profile.can_edit_ratio,
        "can_set_volume": profile.can_set_volume,
        "can_trigger_event": profile.can_trigger_event,
        "expires_at": profile.expires_at.isoformat() if profile.expires_at else None,
    }


async def _audit_local_control(db, id_org, action: str, resource_type: str,
                               resource_id, detail: dict) -> None:
    """Trace une action issue du Store Control Panel (§19.3, source=local_control)."""
    db.add(CmsAuditLog(
        id_org=id_org,
        id_user=None,                      # l'opérateur n'est pas un utilisateur CMS
        action=f"local_control:{action}",
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        detail=detail,
        source="local_control",
    ))


async def _apply_local_control_action(db, id_player: int, msg: dict) -> None:
    """
    Applique une action de l'opérateur magasin reçue du serveur de contrôle local
    du player (§19.3). Le player a déjà appliqué l'action localement (réactif) ;
    ici on persiste l'état côté serveur (source de vérité) + audit.

    Re-valide le profil (actif, non-expiré, serveur activé) et les permissions
    avant toute écriture. Une action non autorisée est auditée puis ignorée.
    """
    action = msg.get("action")
    if not action:
        return

    player = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player)
    )).scalar_one_or_none()
    if player is None:
        return

    display_config = player.display_config or {}
    if not display_config.get("local_control_enabled"):
        return  # serveur désactivé — on ignore (sécurité)

    profile = (await db.execute(
        select(CmsLocalControlProfile).where(
            CmsLocalControlProfile.id_player == id_player,
            CmsLocalControlProfile.revoked == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if profile is None:
        return
    if profile.expires_at and profile.expires_at < datetime.now(UTC):
        return

    id_org = player.id_org
    allowed = profile.allowed_basket_ids  # None = tous

    def _basket_allowed(id_basket: int) -> bool:
        return allowed is None or id_basket in allowed

    # ── reset_overrides (rappel des réglages serveur depuis le téléphone) ───────
    # Efface tous les overrides opérateur du player → les ratios/états du programme
    # serveur (schedules audio) reprennent la main au prochain resync.
    if action == "reset_overrides":
        if not profile.can_edit_ratio:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "ratio_not_permitted"})
            await db.commit()
            return
        existing = (await db.execute(
            select(CmsPlayerBasketConfig).where(CmsPlayerBasketConfig.id_player == id_player)
        )).scalars().all()
        for e in existing:
            await db.delete(e)
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "cleared": len(existing), "role": profile.role})
        await db.commit()
        return

    # ── set_ratios (batch — auto-équilibrage option C depuis le téléphone) ──────
    if action == "set_ratios":
        if not profile.can_edit_ratio:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "ratio_not_permitted"})
            await db.commit()
            return
        applied = []
        for r in (msg.get("ratios") or []):
            idb = r.get("id_basket")
            if idb is None or not _basket_allowed(idb):
                continue
            basket = (await db.execute(
                select(CmsAudioBasket).where(
                    CmsAudioBasket.id_basket == idb, CmsAudioBasket.id_org == id_org,
                )
            )).scalar_one_or_none()
            if basket is None:
                continue
            # A2 — un panier obligatoire est écarté de l'auto-équilibrage : son ratio est
            # verrouillé. (Sauter l'entrée, et non refuser le lot : les autres paniers de
            # la même requête sont légitimes.)
            if basket.is_mandatory:
                continue
            val = max(0, min(100, int(r.get("mix_ratio", 0))))
            cfg = (await db.execute(
                select(CmsPlayerBasketConfig).where(
                    CmsPlayerBasketConfig.id_player == id_player,
                    CmsPlayerBasketConfig.id_basket == idb,
                )
            )).scalar_one_or_none()
            if cfg is None:
                db.add(CmsPlayerBasketConfig(id_player=id_player, id_basket=idb, mix_ratio=val, is_active=True))
            else:
                cfg.mix_ratio = val
                cfg.is_active = True
            applied.append({"id_basket": idb, "mix_ratio": val})
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "ratios": applied, "role": profile.role})
        await db.commit()
        return

    # ── toggle_basket / set_ratio ──────────────────────────────────────────────
    if action in ("toggle_basket", "set_ratio"):
        id_basket = msg.get("id_basket")
        if id_basket is None or not _basket_allowed(id_basket):
            await _audit_local_control(db, id_org, action, "audio_basket", id_basket,
                                       {"result": "forbidden", "reason": "basket_not_allowed"})
            await db.commit()
            return
        if action == "set_ratio" and not profile.can_edit_ratio:
            await _audit_local_control(db, id_org, action, "audio_basket", id_basket,
                                       {"result": "forbidden", "reason": "ratio_not_permitted"})
            await db.commit()
            return

        # Vérifie que le panier appartient bien à l'org du player
        basket = (await db.execute(
            select(CmsAudioBasket).where(
                CmsAudioBasket.id_basket == id_basket,
                CmsAudioBasket.id_org == id_org,
            )
        )).scalar_one_or_none()
        if basket is None:
            return

        cfg = (await db.execute(
            select(CmsPlayerBasketConfig).where(
                CmsPlayerBasketConfig.id_player == id_player,
                CmsPlayerBasketConfig.id_basket == id_basket,
            )
        )).scalar_one_or_none()

        # A2 — panier OBLIGATOIRE : l'opérateur local ne peut ni le couper ni changer son
        # ratio. C'est le sens de `is_mandatory` (§XIX.2, et le libellé de l'écran) ; il
        # n'était appliqué nulle part. Refus TRACÉ, comme les autres refus du panneau —
        # sans quoi le magasin verrait un bouton sans effet, ce qui est pire qu'un refus.
        if basket.is_mandatory:
            await _audit_local_control(db, id_org, action, "audio_basket", id_basket,
                                       {"result": "rejected", "reason": "basket_mandatory",
                                        "role": profile.role})
            await db.commit()
            return

        new_active = cfg.is_active if cfg else True
        new_ratio = cfg.mix_ratio if cfg else 50
        if action == "toggle_basket":
            new_active = bool(msg.get("is_active", not new_active))
        else:  # set_ratio
            new_ratio = max(0, min(100, int(msg.get("mix_ratio", new_ratio))))

        # Budget ≤100 : uniquement pour le mix continu (music/ambiance), et SÉPARÉ PAR
        # FAMILLE (music et ambiance = 2 couches indépendantes jouées en parallèle). On
        # ne somme donc que les autres paniers actifs de LA MÊME famille. Les paniers
        # déclencheurs (message/jingle/emergency) ne consomment pas le budget.
        if basket.basket_type in ("music", "ambiance") and new_active:
            others = (await db.execute(
                select(func.coalesce(func.sum(CmsPlayerBasketConfig.mix_ratio), 0))
                .select_from(CmsPlayerBasketConfig)
                .join(CmsAudioBasket, CmsAudioBasket.id_basket == CmsPlayerBasketConfig.id_basket)
                .where(
                    CmsPlayerBasketConfig.id_player == id_player,
                    CmsPlayerBasketConfig.is_active == True,  # noqa: E712
                    CmsPlayerBasketConfig.id_basket != id_basket,
                    CmsAudioBasket.basket_type == basket.basket_type,
                )
            )).scalar_one()
            if others + new_ratio > 100:
                await _audit_local_control(db, id_org, action, "audio_basket", id_basket,
                                           {"result": "rejected", "reason": "ratio_sum_exceeds_100",
                                            "projected": int(others + new_ratio)})
                await db.commit()
                return

        if cfg is None:
            db.add(CmsPlayerBasketConfig(
                id_player=id_player, id_basket=id_basket,
                mix_ratio=new_ratio, is_active=new_active,
            ))
        else:
            cfg.is_active = new_active
            cfg.mix_ratio = new_ratio
        await _audit_local_control(db, id_org, action, "audio_basket", id_basket,
                                   {"result": "ok", "is_active": new_active, "mix_ratio": new_ratio,
                                    "role": profile.role})
        await db.commit()
        return

    # ── set_volume ─────────────────────────────────────────────────────────────
    if action == "set_volume":
        if not profile.can_set_volume:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "volume_not_permitted"})
            await db.commit()
            return
        volume = max(0, min(100, int(msg.get("volume", 100))))
        new_cfg = dict(display_config)
        new_cfg["audio_volume"] = volume
        player.display_config = new_cfg  # réassignation = SQLAlchemy détecte le changement JSONB
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "volume": volume, "role": profile.role})
        await db.commit()
        return

    # ── set_family_volume (trim fin Music / Ambiance, persisté comme le volume général) ──
    if action == "set_family_volume":
        if not profile.can_set_volume:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "volume_not_permitted"})
            await db.commit()
            return
        family = msg.get("family")
        if family not in ("music", "ambiance"):
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "rejected", "reason": "bad_family"})
            await db.commit()
            return
        volume = max(0, min(100, int(msg.get("volume", 100))))
        key = "audio_music_volume" if family == "music" else "audio_ambiance_volume"
        new_cfg = dict(display_config)
        new_cfg[key] = volume
        player.display_config = new_cfg
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "family": family, "volume": volume, "role": profile.role})
        await db.commit()
        return

    # ── set_output (sortie audio paniers + son contenus) ───────────────────────
    if action == "set_output":
        if not profile.can_set_volume:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "volume_not_permitted"})
            await db.commit()
            return
        new_cfg = dict(display_config)
        out = msg.get("basket_output")
        if out in ("auto", "hdmi", "jack", "usb"):
            new_cfg["audio_basket_output"] = out
        snd = msg.get("content_sound")
        if snd in ("on", "muted"):
            new_cfg["content_sound"] = snd
        player.display_config = new_cfg
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "basket_output": new_cfg.get("audio_basket_output"),
                                    "content_sound": new_cfg.get("content_sound"), "role": profile.role})
        await db.commit()
        return

    # ── trigger_event ──────────────────────────────────────────────────────────
    if action == "trigger_event":
        if not profile.can_trigger_event:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "event_not_permitted"})
            await db.commit()
            return
        # Déclenchement d'annonce : audit-only en v1 (déclenchement runtime côté player).
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "id_basket": msg.get("id_basket"),
                                    "role": profile.role})
        await db.commit()
        return

    # ── play_vod (VOD à la demande LAN-local, Feature B Phase 3) ─────────────────
    # La lecture (takeover) est appliquée LOCALEMENT par les pairs Electron/Android
    # (broadcast UDP :7779 signé, sans serveur). Réutilise la permission
    # can_trigger_event (déclencher une lecture ad-hoc).
    if action == "play_vod":
        if not profile.can_trigger_event:
            await _audit_local_control(db, id_org, action, "player", id_player,
                                       {"result": "forbidden", "reason": "vod_not_permitted"})
            await db.commit()
            return
        # Relais CLOUD vers les cibles SOURDES au broadcast UDP : l'autorun BrightSign
        # < v50 n'écoute pas le port 7779 (seulement 7778/LAN sync) alors qu'il est
        # listé comme pair — sans ce relais, la VOD du panneau magasin ne démarrait
        # jamais sur ces BS. Depuis le renderer 2.0.19 (autorun v50), le BS reçoit le
        # broadcast UDP directement (parité offline) → le relais est GATÉ aux versions
        # antérieures (sinon double lecture : UDP + relais = la vidéo redémarre).
        # On ré-emprunte le chemin cloud play_now (même payload que /vod/{id}/play),
        # re-vérifié serveur : cible = player BS de l'org appartenant à une config VOD
        # qui contient ce média.
        relayed: list[int] = []
        vod_media_id = msg.get("id_media")
        vod_mode = msg.get("mode") or "once"
        raw_targets = msg.get("id_players") or []
        target_ids = [int(t) for t in raw_targets if isinstance(t, (int, float))]
        if vod_media_id and target_ids:
            from app.api.v1.endpoints.vod import _resolve_play_payload  # import tardif (cycle)
            from app.services.ota import semver_key
            from app.services.vod_targeting import players_with_vod_media

            # Cibles réellement visées par une config VOD portant ce média — ciblage
            # explicite OU par site (migr 0096) ; un JOIN direct raterait le second.
            eligible = await players_with_vod_media(db, id_org, target_ids, vod_media_id)
            bs_targets = (await db.execute(
                select(CmsPlayer).where(
                    CmsPlayer.id_player.in_(eligible),
                    CmsPlayer.id_org == id_org,
                    CmsPlayer.player_os == "brightsign",
                )
            )).scalars().unique().all() if eligible else []
            bs_targets = [p for p in bs_targets if semver_key(p.player_version) < (2, 0, 19)]
            media = None
            if bs_targets:
                media = (await db.execute(select(CmsMedia).where(
                    CmsMedia.id_media == vod_media_id,
                    CmsMedia.id_org == id_org,
                    CmsMedia.is_deleted == False,  # noqa: E712
                    CmsMedia.is_blocked == False,  # noqa: E712
                    CmsMedia.media_type != "hdmi_input",  # switch HDMI ≠ lecture ad-hoc
                ))).scalar_one_or_none()
            if media is not None:
                r = aioredis.from_url(settings.REDIS_URL)
                try:
                    for p in bs_targets:
                        params = await _resolve_play_payload(db, p, media, vod_mode)
                        await r.publish(
                            f"signflow:player:{p.id_player}:cmds",
                            json.dumps({"type": "player:command", "command": "play_now",
                                        "params": params}),
                        )
                        relayed.append(p.id_player)
                finally:
                    await r.aclose()
        await _audit_local_control(db, id_org, action, "player", id_player,
                                   {"result": "ok", "id_media": msg.get("id_media"),
                                    "mode": msg.get("mode"), "role": profile.role,
                                    "relayed_brightsign": sorted(relayed)})
        await db.commit()
        return


async def _subtitle_urls(media: CmsMedia) -> list[dict]:
    """URLs WebVTT signées des sous-titres d'un média (§7) → [{lang, label, url}].

    Les pistes sont stockées dans media_metadata.subtitles (extraites à l'upload par
    le worker). Le player injecte un <track> par entrée et active celle de son pays.
    """
    subs = (media.media_metadata or {}).get("subtitles") or []
    out = []
    for s in subs:
        key = s.get("vtt_key")
        if not key:
            continue
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, key, 86400))
        except Exception:
            continue
        out.append({"lang": s.get("lang"), "label": s.get("label"), "url": url})
    return out


def _url_display_media_dict(media: "CmsMedia") -> dict:
    """Bloc `media` d'un item url_display (§XXV.2 — « Site Web »).

    Deux modes (cf. docs/SPIKE_url_display*.md) :
    - **webview** : pas de fichier → on émet l'URL live du site + la config ; le player rend une
      iframe (offline-first via la config persistée). cache_key=None.
    - **screenshot_server** : si une capture Playwright est prête, on émet l'URL de l'IMAGE
      (signée MinIO) + cache_key → mise en cache offline-first comme une image (rewriteSyncForStore),
      le player rend un `<img>` (marche partout, hors framing). Tant que la capture n'est pas prête,
      on retombe sur webview (iframe du site live) pour ne pas afficher de vide.
    """
    meta = media.media_metadata or {}
    render_mode = meta.get("render_mode") or "webview"
    shot_key = meta.get("screenshot_key")

    if render_mode == "screenshot_server" and shot_key:
        try:
            url_out = get_signed_url(shot_key, 86400)
        except Exception:  # noqa: BLE001
            url_out = None
        cache_key = shot_key
        eff_mode = "screenshot_server"
    else:
        url_out = meta.get("url", "")
        cache_key = None
        eff_mode = "webview"   # capture pas (encore) prête → iframe live en attendant

    url_config = {k: v for k, v in meta.items() if k != "url"}
    url_config["render_mode"] = eff_mode   # mode EFFECTIF pour ce sync (peut différer du configuré)
    return {
        "id_media": media.id_media,
        "media_name": media.media_name,
        "media_type": "url_display",
        "mime_type": "image/png" if eff_mode == "screenshot_server" else media.mime_type,
        "url": url_out,
        "url_config": url_config,
        "checksum_sha256": "",
        "cache_key": cache_key,
        "cache_checksum": None,
    }


def _hdmi_input_media_dict(media: "CmsMedia") -> dict:
    """Bloc `media` d'un item hdmi_input : média pointeur SANS fichier — l'item demande au
    player d'afficher son **entrée HDMI physique** pendant duration_ms (BrightSign XT/4K1142/
    XD1230 ; le renderer monte `<video src="tv:brightsign.biz/hdmi">`). Servi UNIQUEMENT aux
    players capables (`_player_has_hdmi_input`) — les autres ne reçoivent pas l'item."""
    return {
        "id_media": media.id_media,
        "media_name": media.media_name,
        "media_type": "hdmi_input",
        "mime_type": media.mime_type or "application/x-hdmi-input",
        "url": None,
        "checksum_sha256": "",
        "cache_key": None,
        "cache_checksum": None,
    }


def _feed_source_label(link_url: str | None) -> str | None:
    """Domaine d'un item (ex. 'lemonde.fr') pour l'affichage « source »."""
    if not link_url:
        return None
    try:
        from urllib.parse import urlparse
        host = urlparse(link_url).hostname or ""
        return host[4:] if host.startswith("www.") else host or None
    except Exception:  # noqa: BLE001
        return None


async def _feed_display_media_dict(db, media: "CmsMedia") -> dict:
    """Bloc `media` d'un item feed_display (§XXV.3) : embarque le `display_mode` + la config + les
    items courants (top N approuvés) → le player rend un ticker / carrousel offline-first (tout est
    dans le sync persisté). Pas de fichier ; cache_key=None (les vignettes sont chargées en ligne
    en tranche 1 — le re-hébergement MinIO offline viendra ensuite)."""
    meta = media.media_metadata or {}
    id_feed = meta.get("id_feed")
    display_mode = "ticker"
    display_config: dict = {}
    items_out: list[dict] = []
    if id_feed:
        feed = (await db.execute(
            select(CmsDatafeed).where(CmsDatafeed.id_feed == id_feed)
        )).scalar_one_or_none()
        if feed and not feed.is_deleted:
            display_mode = feed.display_mode or "ticker"
            display_config = feed.display_config or {}
            rows = (await db.execute(
                select(CmsFeedItem).where(
                    CmsFeedItem.id_feed == id_feed,
                    CmsFeedItem.moderation_status == "approved",
                ).order_by(
                    CmsFeedItem.published_at.desc().nullslast(),
                    CmsFeedItem.fetched_at.desc(),
                ).limit(feed.max_items or 10)
            )).scalars().all()
            items_out = []
            for it in rows:
                # Vignette re-hébergée dans MinIO (thumbnail_s3) → URL signée de notre infra
                # (offline/anti-SSRF) ; repli sur l'URL distante d'origine tant que pas re-hébergée.
                image_url = it.image_url
                # `image_cache_key` = clé stable du content store (offline-first Phase 2). Posée
                # seulement quand la vignette est re-hébergée (thumbnail_s3) : le player télécharge
                # depuis l'URL signée puis bascule `image_url` sur signflow:// (cache local).
                image_cache_key = it.thumbnail_s3 or None
                if it.thumbnail_s3:
                    try:
                        image_url = get_signed_url(it.thumbnail_s3, 86400)
                    except Exception:  # noqa: BLE001
                        image_url = it.image_url
                        image_cache_key = None
                items_out.append({
                    "title": it.title,
                    "description": it.description,
                    "summary": it.summary_ai or None,
                    "image_url": image_url,
                    "image_cache_key": image_cache_key,
                    "link_url": it.link_url,
                    "author": it.author,
                    "source": _feed_source_label(it.link_url),
                    "published_at": it.published_at.isoformat() if it.published_at else None,
                    # data_chart : valeurs numériques (None pour les feeds RSS/Atom).
                    "value_num": it.value_num,
                    "value_unit": it.value_unit,
                    "change_pct": it.change_pct,
                    "series": it.series,
                })
    return {
        "id_media": media.id_media,
        "media_name": media.media_name,
        "media_type": "feed_display",
        "mime_type": media.mime_type,
        "url": None,
        "feed_config": {
            "display_mode": display_mode,
            "display_config": display_config,
            "items": items_out,
        },
        "checksum_sha256": "",
        "cache_key": None,
        "cache_checksum": None,
    }


async def _audit_player_event(db, id_player: int, msg: dict) -> None:
    """Journalise un événement remonté par le player. v1 : url_display inaccessible (§XXV.2) →
    audit log (les vraies notifications §XXII = Sprint 2.11). Le player met l'alerte en file si
    le serveur est down et la rejoue à la reconnexion.
    v2 (2026-07-08) : `hdmi_session` = traçabilité du SWITCH d'entrée HDMI (override VOD /
    drawer / page locale /control — l'item de playlist émet déjà son PoP générique). Émis par
    le renderer au switch OFF avec la durée RÉELLE et la source du déclenchement — seule voie
    possible pour la page locale, que le serveur ne voit jamais passer."""
    event = msg.get("event")
    if event not in ("url_display_unreachable", "hdmi_session"):
        return
    org_id = (await db.execute(
        select(CmsPlayer.id_org).where(CmsPlayer.id_player == id_player)
    )).scalar_one_or_none()
    if event == "url_display_unreachable":
        db.add(CmsAuditLog(
            id_org=org_id,
            action="url_display_unreachable",
            resource_type="url_display",
            resource_id=(str(msg["id_media"]) if msg.get("id_media") is not None else None),
            detail={"url": msg.get("url"), "error": msg.get("error"), "id_player": id_player},
            source="player",
        ))
    else:  # hdmi_session
        db.add(CmsAuditLog(
            id_org=org_id,
            action="hdmi_session",
            resource_type="hdmi_input",
            resource_id=(str(msg["id_media"]) if msg.get("id_media") is not None else None),
            detail={
                "id_player": id_player,
                "source": str(msg.get("source") or "remote")[:32],  # vod | drawer | local | remote
                "duration_ms": msg.get("duration_ms"),
                "started_at": msg.get("started_at"),
                "ended_at": msg.get("ended_at"),
            },
            source="player",
        ))
    await db.commit()


async def _resolve_fallback(db, player: "CmsPlayer | None") -> dict:
    """Config de repli (§17.5) injectée dans chaque player:sync.

    - video_message / video_config : affichés par le player vidéo lorsqu'il n'a
      aucun item à diffuser (playlist vide après filtrage par tags, ou pas de
      schedule actif) — au lieu du message générique embarqué.
    - audio : piste de secours jouée en boucle par le player audio quand aucun
      panier n'a de contenu. Priorité player → (org : non modélisé) → bundle CC0
      (défaut local côté player si audio == None).
    """
    if player is None:
        return {"video_message": None, "video_config": {}, "audio": None}
    audio = None
    if player.fallback_audio_id:
        media = (await db.execute(
            select(CmsMedia).where(
                CmsMedia.id_media == player.fallback_audio_id,
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.is_blocked == False,  # noqa: E712 — quarantaine (§ Q5)
            )
        )).scalar_one_or_none()
        if media is not None:
            try:
                url = await anyio.to_thread.run_sync(partial(get_signed_url, media.storage_key, 86400))
            except Exception:
                url = None
            if url:
                audio = {"id_media": media.id_media, "media_name": media.media_name, "url": url}
    return {
        "video_message": player.fallback_video_message or None,
        "video_config": player.fallback_video_config or {},
        "audio": audio,
    }


async def _player_target_height(db, player: "CmsPlayer") -> int | None:
    """Hauteur vidéo max à servir à ce player pour la sélection de rendition.

    Hors groupe LAN : sa propre capacité (écran ∩ décodage). En groupe LAN sync : le
    **min** des cibles connues des membres du groupe → tout le mur joue le même encode
    (cohérence frame `switch_at`/offset). Cf. EVOLUTIONS.md.
    """
    own = rendition_svc.player_target_height(player)
    dc = player.display_config or {}
    group = (dc.get("lan_sync_group") or "").strip()
    if not (dc.get("lan_sync_enabled") and group):
        return own

    # Membres du même groupe LAN (label dans display_config) sur la même org.
    members = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == player.id_org)
    )).scalars().all()
    targets: list[int] = []
    for m in members:
        mdc = m.display_config or {}
        if mdc.get("lan_sync_enabled") and (mdc.get("lan_sync_group") or "").strip() == group:
            t = rendition_svc.player_target_height(m)
            if t is not None:
                targets.append(t)
    return min(targets) if targets else own


async def _bake_group_height(db, player: "CmsPlayer") -> int | None:
    """Hauteur de sortie effective pour la rendition ROTÉE de ce player (None = inconnue).

    Hors groupe LAN : sa propre sortie (`bake_output_height` — bs_video_mode du drawer, puis
    la sortie réellement en service). En groupe LAN actif : le **min** des sorties des membres
    BrightSign à gravure active → tout le mur roté joue le MÊME fichier (cohérence frame,
    même règle que `_player_target_height` pour les renditions classiques).
    """
    own = rendition_svc.bake_output_height(player)
    dc = player.display_config or {}
    group = (dc.get("lan_sync_group") or "").strip()
    if not (dc.get("lan_sync_enabled") and group):
        return own
    members = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == player.id_org)
    )).scalars().all()
    heights: list[int] = []
    for m in members:
        mdc = m.display_config or {}
        if not (mdc.get("lan_sync_enabled") and (mdc.get("lan_sync_group") or "").strip() == group):
            continue
        if rendition_svc.bake_rotation_profile(m) is None:
            continue  # membre non roté (ou non-BS) : il ne joue pas les fichiers gravés
        h = rendition_svc.bake_output_height(m)
        if h is not None:
            heights.append(h)
    return min(heights) if heights else own


def _player_prefers_hevc(player: "CmsPlayer | None") -> str | None:
    """'hevc' si ce player préfère une variante HEVC, sinon None.

    Un player en rendu natif ExoPlayer (`display_config.video_renderer='exo'`, box Android
    TV) décode le HEVC 4K@60 en HW alors que son décodeur h264 cale au-delà de 4K@30 — il
    déclare 'hevc' dans `capabilities.codecs`. La WebView (autres players Android) ne lit pas
    le HEVC → on ne préfère le HEVC qu'en mode exo. Cf. project_exoplayer_android.
    """
    if player is None:
        return None
    dc = getattr(player, "display_config", None) or {}
    if dc.get("video_renderer") != "exo":
        return None
    caps = getattr(player, "capabilities", None) or {}
    codecs = caps.get("codecs") or []
    return "hevc" if "hevc" in codecs else None


def _player_browser_only(player: "CmsPlayer | None") -> bool:
    """True si le player rend la vidéo via un <video> Chromium (ChromeOS PWA, extension,
    Electron/WebView) — donc ne lit QUE les conteneurs MP4/WebM. mpv et ExoPlayer (rendu natif)
    lisent les autres conteneurs (MKV...) → False. Sert à exclure une originale .mkv pour ces
    players (ceinture/bretelles ; les nouveaux uploads sortent déjà en .mp4 côté worker)."""
    if player is None:
        return False
    dc = getattr(player, "display_config", None) or {}
    return (dc.get("video_renderer") or "auto") not in ("mpv", "exo")


def _player_video_can_hevc(player: "CmsPlayer | None") -> bool:
    """True si un player NAVIGATEUR (<video>) DÉCLARE décoder le HEVC. ChromeOS récent à
    décodeur HEVC matériel remonte `capabilities.video_can_hevc=true` (via `canPlayType('hvc1')`).
    La WebView Chromium 83 (Minix) ne le remonte pas → False → on lui sert du H.264. Distinct de
    `capabilities.codecs` (= MediaCodec du device, qui inclut le HEVC même quand la WebView,
    elle, ne sait pas l'exploiter)."""
    if player is None:
        return False
    caps = getattr(player, "capabilities", None) or {}
    return bool(caps.get("video_can_hevc"))


def _player_has_hdmi_input(player: "CmsPlayer | None") -> bool:
    """True si le player possède une entrée HDMI physique exploitable en item de playlist.

    Miroir serveur de `bsHasHdmiInput` (frontend players/page.tsx) : gamme BrightSign XT
    (XT1143/44/45, XT2145…), 4K1142/4K1242, XD1230 — HD/LS exclus (pas d'entrée). Dérivé de
    `capabilities.bs_model` remonté par l'autorun. Les items `hdmi_input` ne sont servis
    qu'aux players capables (gate au sync) ; les autres ne voient jamais l'item."""
    if player is None:
        return False
    caps = getattr(player, "capabilities", None) or {}
    model = str(caps.get("bs_model") or "").upper()
    if not model:
        return False
    return (
        (model.startswith("XT") and len(model) > 2 and model[2].isdigit())
        or (model.startswith(("4K11", "4K12")) and len(model) >= 6)
        or model == "XD1230"
    )


async def _resolve_variant_key(
    db, media: CmsMedia, target_height: int | None, prefer_codec: str | None = None,
    browser_only: bool = False, allow_hevc: bool = False,
    bake_profile: str | None = None, bake_height: int | None = None,
) -> str:
    """storage_key de la variante (rendition) à servir pour ce média et cette cible.

    Repli gracieux : pas de rendition prête, cible inconnue, ou média non-vidéo →
    l'originale (`media.storage_key`). Le parent reste toujours la source de vérité.
    `prefer_codec` (ex. 'hevc' pour un player ExoPlayer) : à définition retenue, préfère
    une variante de ce codec si une rendition prête existe.
    `browser_only` : player rendant via <video> Chromium → exclure les conteneurs non-navigateur
    (MKV...) de la sélection, y compris l'originale.
    `allow_hevc` : ce player navigateur DÉCLARE décoder le HEVC (ChromeOS récent) → ne pas
    exclure le HEVC (lui servir le 4K HEVC). False (WebView Chromium 83) → rendition H.264.
    `bake_profile` ('rot_cw'/'rot_ccw') : player BrightSign monté en portrait → servir la
    rendition ROTÉE de ce profil, et RIEN d'autre (cf. renditions.BAKE_PROFILE_BY_ROTATION).
    `bake_height` : hauteur de la SORTIE effective (bs_video_mode/min du groupe LAN) — ≤ 1080
    sur une source plus grande que FHD → variante `<profil>_fhd` (cadre 1920×1080 H.264,
    ~4× plus légère, décodage FHD ; demande d'Ed 2026-08-14). None → profil de base (4K).
    """
    if media.media_type != "video":
        return media.storage_key
    if bake_profile:
        # La DÉFINITION du profil roté dépend du MÉDIA (une source déjà FHD n'a pas de
        # variante _fhd distincte) → décidée ici, au seul endroit qui voit les deux.
        bake_profile = rendition_svc.bake_variant_profile(
            bake_profile, bake_height, media.width, media.height)
        # Rotation à graver : le pool est la SEULE rendition rotée du bon sens — jamais
        # l'originale ni une rendition classique, qui apparaîtraient COUCHÉES sur la dalle
        # (le widget BS reste en paysage). Absente → produite À LA DEMANDE (c'est ici que
        # « à l'affectation » se réalise : ce chemin n'est atteint que quand le contenu est
        # réellement servi/pré-caché pour un écran roté — playlists, events, gouvernance,
        # campagnes, VOD, look-ahead passent tous par lui), et l'originale sert de repli
        # dégradé (couchée) jusqu'à ce que la rendition soit prête (force_sync du worker).
        # Le pool contient les DEUX définitions du bon sens (base 4K + _fhd) : si la
        # définition demandée n'est pas prête, une rendition rotée de l'AUTRE définition
        # est un repli infiniment meilleur que le parent (elle est DROITE — le parent est
        # couché). Cas réel : un HD qui passe de la 4K à la _fhd garde l'écran droit
        # pendant l'encodage de la _fhd.
        base = bake_profile[:-4] if bake_profile.endswith("_fhd") else bake_profile
        wanted = (bake_profile, base, f"{base}_fhd")
        rot_rows = (await db.execute(
            select(CmsMediaRendition).where(
                CmsMediaRendition.id_parent == media.id_media,
                CmsMediaRendition.profile.in_(set(wanted)),
            )
        )).scalars().all()
        by_profile = {r.profile: r for r in rot_rows}
        exact = by_profile.get(bake_profile)
        if exact is not None and exact.status == "ready" and exact.storage_key:
            return exact.storage_key
        if exact is None:
            # Aucune ligne (même pas failed) → enqueue une fois ; la tâche crée sa ligne
            # 'processing' en quelques secondes, donc le sync suivant (30 s) ne ré-enqueue
            # pas. Un 'failed' ne se rejoue PAS d'ici (échec déterministe : régénération
            # manuelle par l'endpoint renditions). Best-effort : un broker muet ne casse
            # jamais un sync — on re-tentera au prochain passage.
            try:
                from app.workers.media_worker import generate_rendition
                generate_rendition.delay(media.id_media, bake_profile)
            except Exception:  # noqa: BLE001
                pass
        # Repli : n'importe quelle rendition rotée PRÊTE du même sens (droite), sinon le
        # parent (couché) — jamais une rendition de l'autre sens.
        for prof in wanted:
            r = by_profile.get(prof)
            if r is not None and r.status == "ready" and r.storage_key:
                return r.storage_key
        return media.storage_key
    if not getattr(media, "has_renditions", False):
        return media.storage_key
    # cible inconnue → on sert l'originale, SAUF si elle est non-lisible navigateur pour un player
    # navigateur : il faut alors quand même choisir une rendition safe (select_variant filtrera).
    key = media.storage_key or ""
    orig_codec = ((media.media_metadata or {}).get("video") or {}).get("codec")
    # Parent non lisible par un <video> : conteneur exotique OU codec HEVC quand le player ne sait
    # PAS le décoder (même en .mp4, la WebView Chromium 83 ne décode pas le HEVC) → forcer une
    # rendition navigateur-safe même à cible inconnue. Un ChromeOS HEVC-capable (allow_hevc) garde
    # son parent HEVC.
    unsafe_for_browser = browser_only and (
        ("." in key and key.rsplit(".", 1)[-1].lower() not in ("mp4", "m4v", "webm"))
        or (not allow_hevc and rendition_svc.is_browser_unsafe_codec(orig_codec))
    )
    if target_height is None and not unsafe_for_browser:
        return media.storage_key
    rends = (await db.execute(
        select(CmsMediaRendition).where(
            CmsMediaRendition.id_parent == media.id_media,
            CmsMediaRendition.status == "ready",
        )
    )).scalars().all()
    if not rends:
        return media.storage_key
    rlist = [
        {"height": r.height, "storage_key": r.storage_key, "profile": r.profile,
         "codec": r.codec}
        for r in rends
    ]
    variant = rendition_svc.select_variant(
        media.height, media.storage_key, rlist, target_height,
        prefer_codec=prefer_codec, original_codec=orig_codec, browser_only=browser_only,
        allow_hevc=allow_hevc,
    )
    return variant["storage_key"] or media.storage_key


async def _document_page_items(
    media: CmsMedia,
    doc_pages_spec: str | None,
    per_page_ms: int,
    *,
    id_item: int | None = None,
    sort_order: int = 0,
    zone_index: int = 0,
    transition_type: str = "cut",
    touch_navigation: bool = False,
    touch_resume_after_s: int | None = None,
) -> list[dict] | None:
    """Document multipage converti → N items IMAGE (déroulé par le serveur, §3.2).

    Même principe que la rotation : les players reçoivent une boucle plate d'items
    qu'ils savent déjà rendre (zéro modif player, wall-clock/LAN sync/PoP intacts).
    Chaque page porte sa propre cache_key/checksum (offline-first par page).
    None si le document n'a pas (encore) de pages converties.
    """
    pages = document_media.document_pages(media.media_metadata)
    if not pages:
        return None
    total = len(pages)
    out: list[dict] = []
    for page_no, p in document_media.selected_pages(pages, doc_pages_spec):
        key = p.get("key")
        if not key:
            continue
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, key, 86400))
        except Exception:  # noqa: BLE001
            url = None
        out.append({
            "id_item": id_item,
            "sort_order": sort_order,
            "zone_index": zone_index,
            "duration_ms": per_page_ms,
            "transition_type": transition_type,
            # Pages 2..N (ordre d'émission) : jamais de pub insérée AVANT — un spot ne
            # doit pas couper un document en pleine lecture (splice_ad_items saute ces
            # frontières ; décision Ed 2026-07-16).
            **({"no_ad_before": True} if out else {}),
            # Feuilletage au doigt (migr 0129) : porté par CHAQUE page du document, car c'est
            # l'item servi que le renderer regarde. ⛔ Émis SEULEMENT s'il est activé : payload
            # inchangé à l'octet près pour tout le parc tant qu'aucun opérateur ne coche la
            # case. Même vocabulaire qu'en base et à l'écran — trois noms pour une même chose
            # est exactement ce qui a coûté la séance d'installeur du 09/08.
            **({"touch_navigation": True} if touch_navigation else {}),
            **({"touch_resume_after_s": touch_resume_after_s}
               if touch_navigation and touch_resume_after_s else {}),
            "media": {
                "id_media": media.id_media,
                "media_name": f"{media.media_name} — p.{page_no}/{total}",
                # Servi comme une IMAGE : tous les renderers (7 OS) la jouent déjà.
                "media_type": "image",
                "mime_type": "image/jpeg",
                "url": url,
                "checksum_sha256": p.get("checksum"),
                "cache_key": key,
                "cache_checksum": p.get("checksum"),
                "cache_key_fallback": None,
                "cache_checksum_fallback": None,
                "subtitles": [],
                # Diagnostic (overlay player / PoP verbose) — ignoré par les renderers.
                "doc_page": page_no,
                "doc_page_count": total,
            },
        })
    return out or None


async def _build_compiled_items(
    db, compiled_items: list, now_utc: datetime, target_height: int | None = None,
    prefer_codec: str | None = None, browser_only: bool = False, allow_hevc: bool = False,
    has_hdmi_input: bool = False, bake_profile: str | None = None,
    bake_height: int | None = None,
) -> list[dict]:
    """CompiledItem (gouvernance) → items player:sync (signés, embargo/blocage appliqués).

    Réutilise la même construction que la résolution schedule. `id_item` est None (les items
    proviennent de plusieurs playlists fusionnées → pas d'identité d'item playlist unique).
    """
    items: list[dict] = []

    # « Pas de pub entre les contenus » : un CompiledItem protégé peut produire PLUSIEURS
    # items de payload (un document se déroule en une page par item) — on marque donc tout
    # ce qu'il a produit. Marquage DIFFÉRÉ (au tour suivant, puis après la boucle) parce
    # que les branches ci-dessous font `continue` : impossible de marquer en fin de tour.
    def _mark_protected(start: int, protect: bool) -> None:
        if not protect:
            return
        for built in items[start:]:
            built.setdefault("no_ad_before", True)

    pending: tuple[int, bool] | None = None
    for idx, ci in enumerate(compiled_items):
        if pending is not None:
            _mark_protected(*pending)
        pending = (len(items), bool(getattr(ci, "no_ad_before", False)))
        media = (await db.execute(
            select(CmsMedia).where(
                CmsMedia.id_media == ci.id_media,
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.is_blocked == False,  # noqa: E712 — quarantaine (§ Q5)
            )
        )).scalar_one_or_none()
        if media is None:
            continue
        effective_embargo = media.not_before_utc
        if media.embargo_event_id:
            ev = (await db.execute(
                select(CmsEvent).where(CmsEvent.id_event == media.embargo_event_id)
            )).scalar_one_or_none()
            if ev and ev.not_before_utc:
                effective_embargo = ev.not_before_utc
        if effective_embargo and effective_embargo > now_utc:
            continue
        if media.media_type == "url_display":
            items.append({
                "id_item": None,
                "sort_order": idx,
                "zone_index": 0,
                "duration_ms": ci.duration_ms,
                "transition_type": "cut",
                "media": _url_display_media_dict(media),
            })
            continue
        if media.media_type == "feed_display":
            items.append({
                "id_item": None,
                "sort_order": idx,
                "zone_index": 0,
                "duration_ms": ci.duration_ms,
                "transition_type": "cut",
                "media": await _feed_display_media_dict(db, media),
            })
            continue
        if media.media_type == "hdmi_input":
            if not has_hdmi_input:
                continue  # gate capacité : player sans entrée HDMI → item non servi
            items.append({
                "id_item": None,
                "sort_order": idx,
                "zone_index": 0,
                "duration_ms": ci.duration_ms,
                "transition_type": "cut",
                "media": _hdmi_input_media_dict(media),
            })
            continue
        if media.media_type == "document":
            # Chemin compilé (gouvernance/campagnes) : pas d'item de playlist porteur
            # d'une sélection → toutes les pages, durée compilée = durée PAR PAGE.
            page_items = await _document_page_items(
                media, None, ci.duration_ms or 10000, sort_order=idx,
            )
            if page_items:
                items.extend(page_items)
                continue
            if (media.mime_type or "").lower() != "application/pdf":
                continue  # PPT/PPTX non converti = injouable → non servi
        variant_key = await _resolve_variant_key(
            db, media, target_height, prefer_codec, browser_only, allow_hevc,
            bake_profile=bake_profile, bake_height=bake_height,
        )
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, variant_key, 86400))
        except Exception:
            url = None
        items.append({
            "id_item": None,
            "sort_order": idx,
            "zone_index": 0,
            "duration_ms": ci.duration_ms,
            "transition_type": "cut",
            "media": {
                "id_media": media.id_media,
                "media_name": media.media_name,
                "media_type": media.media_type,
                "mime_type": media.mime_type,
                "url": url,
                "checksum_sha256": media.checksum_sha256,
                # Store local (offline-first) : clé du cache = objet RÉELLEMENT servi
                # (parent ou rendition) ; checksum d'intégrité seulement quand on sert
                # le parent (la rendition n'a pas encore son propre sha au payload).
                "cache_key": variant_key,
                "cache_checksum": media.checksum_sha256 if variant_key == media.storage_key else None,
                # Repli offline : si la variante servie n'est pas (encore) cachée mais que
                # le parent l'est (ex. config 4K antérieure) + serveur down → jouer le parent
                # plutôt qu'un écran noir. None quand on sert déjà le parent (variant==parent).
                "cache_key_fallback": media.storage_key if variant_key != media.storage_key else None,
                "cache_checksum_fallback": media.checksum_sha256 if variant_key != media.storage_key else None,
                "subtitles": await _subtitle_urls(media),
            },
        })
    if pending is not None:
        _mark_protected(*pending)
    return items


async def _inject_campaign_items(
    db, player: "CmsPlayer", items: list[dict], now: datetime,
    target_height: int | None = None, prefer_codec: str | None = None,
    browser_only: bool = False, allow_hevc: bool = False,
    bake_profile: str | None = None, bake_height: int | None = None,
) -> list[dict]:
    """Retail Media §2.8 S3 : intercale les créatifs des campagnes live dans la boucle.

    Appelé APRÈS les filtres (tags/règles d'item) et AVANT le wall-clock — la liste
    finale (maison + créatifs) définit index/durées, identique pour tous les players
    d'un groupe LAN (compilation déterministe, cf. services/campaign_delivery).
    Un créatif = un item média normal (PoP signé automatique), marqué `campaign`
    pour le diagnostic. Défensif : toute erreur → boucle maison inchangée (le sync
    ne casse jamais pour une campagne mal formée).
    """
    if not items or player is None:
        return items
    try:
        home_ms = sum(int(i.get("duration_ms") or 0) for i in items)
        slots = await campaign_delivery.campaign_ad_slots(
            db, player.id_org, player.id_player, now, home_ms,
        )
        if not slots:
            return items
        now_utc = datetime.now(UTC)

        # Un _build_compiled_items par créatif UNIQUE (embargo/quarantaine/renditions
        # gérés au même endroit que la gouvernance), puis assemblage par slot.
        built_by_key: dict[tuple[int, int], dict | None] = {}
        for slot in slots:
            key = (slot.id_media, slot.duration_ms)
            if key in built_by_key:
                continue
            pseudo = SimpleNamespace(id_media=slot.id_media, duration_ms=slot.duration_ms)
            built = await _build_compiled_items(
                db, [pseudo], now_utc,
                target_height, prefer_codec, browser_only, allow_hevc,
                bake_profile=bake_profile, bake_height=bake_height,
            )
            built_by_key[key] = built[0] if built else None

        ad_items: list[tuple[campaign_delivery.AdSlot, dict]] = []
        for slot in slots:
            base = built_by_key.get((slot.id_media, slot.duration_ms))
            if base is None:
                continue  # média du créatif supprimé/bloqué/sous embargo → slot ignoré
            ad_items.append((slot, {
                **base,
                "campaign": {"id_campaign": slot.id_campaign, "id_creative": slot.id_creative},
            }))
        if not ad_items:
            return items
        return campaign_delivery.splice_ad_items(items, ad_items)
    except Exception as exc:  # noqa: BLE001 — défensif : jamais casser un sync
        print(f"[WS] injection campagnes échec player {player.id_player}: {exc!r}")
        return items


async def _inject_campaign_items_multizone(
    db, player: "CmsPlayer", items: list[dict], now: datetime,
    template_row, target_height: int | None = None, prefer_codec: str | None = None,
    browser_only: bool = False, allow_hevc: bool = False,
    bake_profile: str | None = None, bake_height: int | None = None,
) -> list[dict]:
    """Retail Media × templates MULTI-ZONES (P3.6) — injection dans la ZONE PRINCIPALE.

    Pourquoi la zone 0 et pas la liste à plat : en multi-zones, `items` mélange les items
    de TOUTES les zones (chacune est re-séparée ensuite par `_build_template_block` sur
    `zone_index`). Y intercaler un spot le placerait dans une zone arbitraire — un bandeau
    d'actualités, par exemple. La zone principale est aussi la seule horloge dont on
    dispose : chaque zone a son propre wall-clock, il n'existe pas de timeline globale.

    Le MODE d'affichage est porté par le template (`campaign_placement`, migr 0124) et
    voyage dans le marquage `campaign.fullscreen` :

    · `fullscreen` (défaut) — le player RECOUVRE tout l'écran le temps du spot. Seul mode
      cohérent avec la tarification au SOV, qui suppose une exposition plein écran. Les
      zones continuent de tourner DESSOUS (leur wall-clock reste vrai, donc aucune dérive
      au retour) : elles sont couvertes et muettes, pas suspendues.
    · `zone` — le spot est un item normal de la zone principale, les autres zones
      continuent d'être visibles. Le cas du menu board, où tout effacer serait inacceptable.

    Défensif comme le chemin mono-zone : toute erreur → boucle inchangée.
    """
    if not items or player is None or template_row is None:
        return items
    zone0 = [it for it in items if int(it.get("zone_index", 0) or 0) == 0]
    if not zone0:
        return items          # pas de zone principale servie → rien où injecter
    others = [it for it in items if int(it.get("zone_index", 0) or 0) != 0]

    injected = await _inject_campaign_items(
        db, player, zone0, now, target_height, prefer_codec, browser_only, allow_hevc,
        bake_profile=bake_profile, bake_height=bake_height,
    )
    if injected is zone0:
        return items          # aucun créatif à servir → on ne touche à rien

    fullscreen = (getattr(template_row, "campaign_placement", "fullscreen") or "fullscreen") != "zone"
    for it in injected:
        if it.get("campaign"):
            # Le spot appartient à la zone principale même en plein écran : c'est SA
            # timeline qui le déclenche, le recouvrement n'est qu'un mode de rendu.
            it["zone_index"] = 0
            it["campaign"] = {**it["campaign"], "fullscreen": fullscreen}
    return injected + others


#: Clé de synchro LAN d'un programme gouverné (retour Ed 2026-07-25 ①).
#
# La clé de synchro du renderer est `id_playlist ?? -id_event ?? null` et le leader
# **n'émet aucun `sync_play`** quand elle est nulle (`sync_lan.js` : `if (!state.id_playlist)
# return`). Or un payload gouverné n'a NI l'un NI l'autre ⇒ zéro correction UDP, les écrans
# ne tenaient que par leur wall-clock et dérivaient. Le serveur fournit donc une clé.
#
# ⛔ Ne PAS réutiliser `id_playlist` : il alimente le Proof of Play, qui est **facturable**
# en Retail Media — une valeur inventée là fausserait des preuves signées.
#
# Contraintes de la valeur :
#  · **identique** pour tous les écrans qui jouent le MÊME programme compilé (sinon pas de
#    groupe LAN) et **différente** dès que le programme diffère → dérivée de l'ensemble des
#    couches actives (nœud + programme), pas du seul nœud : deux players d'un même nœud mais
#    d'ancrages différents ne compilent pas forcément la même chose ;
#  · jamais en collision avec un `id_playlist` réel (positif) ni avec un `-id_event`
#    (magnitude = un id de table, donc petite) → plage réservée [-2e9, -1e9] ;
#  · **tient dans un entier 32 bits signé** — la chaîne LAN traverse l'autorun BrightScript,
#    dont les entiers sont 32 bits : au-delà, débordement silencieux ;
#  · **déterministe entre processus** → CRC32, jamais `hash()` (randomisé par `PYTHONHASHSEED`,
#    donc deux workers donneraient deux clés au même groupe).
_GOV_SYNC_KEY_BASE = 1_000_000_000


def _governance_sync_key(program) -> int:
    """Clé de synchro LAN stable d'un programme gouverné compilé."""
    import zlib

    canonical = ";".join(
        f"{l.id_node}:{l.id_schedule}"
        for l in sorted(
            (l for l in program.layers if l.status == "active" and l.item_count > 0),
            key=lambda l: (l.id_node, l.id_schedule),
        )
    )
    digest = zlib.crc32(canonical.encode()) % 1_000_000_000
    return -(_GOV_SYNC_KEY_BASE + digest)


async def _resolve_governance_payload(
    db, player: "CmsPlayer", now: datetime,
    display_config, audio_baskets, local_control, fallback, blocked_media,
    target_height: int | None = None,
) -> dict | None:
    """Programme **compilé** de gouvernance (§18.4 lot 2b), opt-in par la donnée.

    Renvoie un payload player:sync si le player est gouverné ET qu'au moins une couche est
    active à `now` (items non vides après filtres) ; sinon None → l'appelant retombe sur la
    résolution schedule legacy (zéro régression pour les players non gouvernés).
    """
    from app.api.v1.endpoints.governance import gather_layers
    from app.services.governance_engine import compile_program

    layers = await gather_layers(db, player.id_org, player.id_player, now)
    if not layers:
        return None
    program = compile_program(layers, now)
    if not program.items:
        return None

    now_utc = datetime.now(UTC)
    bake_profile = rendition_svc.bake_rotation_profile(player)
    bake_height = await _bake_group_height(db, player) if bake_profile else None
    items = await _build_compiled_items(
        db, program.items, now_utc, target_height, _player_prefers_hevc(player),
        _player_browser_only(player), _player_video_can_hevc(player),
        has_hdmi_input=_player_has_hdmi_input(player), bake_profile=bake_profile,
        bake_height=bake_height,
    )
    # Filtrage par tags du player (axe réception §XVII) — avant le wall-clock.
    items = await _filter_items_by_tags(db, player.id_player, items)
    if not items:
        return None

    # Retail Media §2.8 : créatifs de campagnes live intercalés (avant le wall-clock).
    items = await _inject_campaign_items(
        db, player, items, now, target_height, _player_prefers_hevc(player),
        _player_browser_only(player), _player_video_can_hevc(player),
        bake_profile=bake_profile, bake_height=bake_height,
    )

    # Wall-clock : cycle compilé perpétuel ancré à minuit (déterministe).
    start_index, start_offset_ms = None, None
    pos = _wall_clock_position(items, _midnight_ms(now), _epoch_ms(now))
    if pos is not None:
        start_index, start_offset_ms = pos

    return {
        "type": "player:sync",
        "id_playlist": None,
        "id_schedule": None,
        "id_governance": True,
        # Clé de synchro LAN (①) : champ NOUVEAU, ignoré par les renderers antérieurs à
        # 2.0.27 / 1.6.x — un parc non mis à jour se comporte exactement comme avant.
        "sync_key": _governance_sync_key(program),
        "player_name": player.player_name,
        "items": items,
        "start_index": start_index,
        "start_offset_ms": start_offset_ms,
        "display_config": display_config,
        "audio_baskets": audio_baskets,
        "local_control": local_control,
        "fallback": fallback,
        "blocked_media": blocked_media,
        "resolved_at": now.isoformat(),
    }


async def _multizone_media_ids(db, player: "CmsPlayer") -> set[int]:
    """Médias servis à ce player via un template MULTI-zones (≥2 zones).

    Le multi-zones (§25.4) est TOUJOURS rendu en Chromium <video> → la variante servie est
    forcée navigateur-safe (browser_only=True, prefer_codec=None), distincte de la variante
    normale d'un player mpv/exo. Le look-ahead doit donc pré-télécharger CETTE variante,
    sinon le contenu programmé futur tombera en streaming hors-ligne (= écran noir serveur
    coupé) une fois la playlist active. Retourne l'ensemble des id_media concernés.
    """
    from app.services.player_inventory import _group_ids
    from app.services.prefetch import _content_schedule_ids

    id_player = player.id_player
    group_ids = await _group_ids(db, id_player)
    site_group_ids = await player_site_group_ids(db, id_player)

    # Playlists ciblant le player : schedules content + events content (même résolution que
    # l'inventaire / le prefetch — directs + groupes + gouvernance).
    playlist_ids: set[int] = set()
    sched_ids = await _content_schedule_ids(db, player.id_org, id_player, group_ids)
    if sched_ids:
        for (pid,) in (await db.execute(select(CmsSchedule.id_playlist).where(
            CmsSchedule.id_schedule.in_(sched_ids),
            CmsSchedule.schedule_kind == "content",
            CmsSchedule.is_active == True,    # noqa: E712
            CmsSchedule.is_deleted == False,  # noqa: E712
        ))).all():
            if pid:
                playlist_ids.add(pid)
    events = (await db.execute(select(CmsEvent).where(
        CmsEvent.id_org == player.id_org,
        CmsEvent.event_kind == "content",
        CmsEvent.status.notin_(("ended", "cancelled")),
    ))).scalars().all()
    for ev in events:
        if event_target_matches(
            ev.target_config, id_player, group_ids, site_group_ids, id_site=player.id_site
        ) and ev.id_playlist:
            playlist_ids.add(ev.id_playlist)
    if not playlist_ids:
        return set()

    # Parmi elles, celles pointant un template à ≥2 zones.
    mz_playlist_ids: set[int] = set()
    for pid, layout in (await db.execute(
        select(CmsPlaylist.id_playlist, CmsTemplate.layout)
        .join(CmsTemplate, CmsTemplate.id_template == CmsPlaylist.id_template)
        .where(CmsPlaylist.id_playlist.in_(playlist_ids),
               CmsTemplate.is_deleted == False)  # noqa: E712
    )).all():
        if len(((layout or {}).get("zones")) or []) > 1:
            mz_playlist_ids.add(pid)
    if not mz_playlist_ids:
        return set()

    return {r[0] for r in (await db.execute(select(CmsPlaylistItem.id_media).where(
        CmsPlaylistItem.id_playlist.in_(mz_playlist_ids)))).all()}


async def _build_prefetch(db, id_player: int, tz_name: str | None = None) -> list[dict]:
    """Look-ahead (offline-first) : contenu programmé futur (<= horizon) à pré-télécharger,
    ordonné par proximité de diffusion. `cache_key`/`url` = variante RÉELLEMENT servie au
    player (rendition adaptative) → il télécharge à l'avance le bon objet. Le player gère la
    priorité et l'éviction disque-aware (le plus proche d'abord, le passé/lointain évincé).

    `tz_name` (P2.2) : le look-ahead DOIT raisonner sur la même horloge que la diffusion.
    Sur un autre fuseau il pré-téléchargerait — et attendrait — du contenu qui ne passe pas
    à cette heure-là : c'est la classe de défaut du 18/07 (faux « média manquant »).
    """
    now = _local_now(tz_name or await player_timezone.player_tz_name(db, id_player))
    player = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player)
    )).scalar_one_or_none()
    if not player:
        return []
    upcoming = await upcoming_media(db, id_player, now)
    if not upcoming:
        return []
    target_height = await _player_target_height(db, player)
    prefer_codec = _player_prefers_hevc(player)
    browser_only = _player_browser_only(player)
    allow_hevc = _player_video_can_hevc(player)
    # Rotation à graver (BrightSign portrait) : le look-ahead DOIT pré-télécharger la
    # rendition ROTÉE — c'est elle que le sync servira (classe de défaut du 18/07 sinon).
    bake_profile = rendition_svc.bake_rotation_profile(player)
    bake_height = await _bake_group_height(db, player) if bake_profile else None
    # §25.4 : les médias servis via un template multi-zones reçoivent la variante navigateur-safe
    # (le player les rend en <video>). On pré-télécharge donc CETTE variante pour eux, sinon le
    # contenu programmé futur tomberait en streaming hors-ligne une fois actif.
    mz_media_ids = await _multizone_media_ids(db, player)
    medias = (await db.execute(select(CmsMedia).where(
        CmsMedia.id_media.in_(upcoming.keys()),
        CmsMedia.is_deleted == False,  # noqa: E712
        CmsMedia.is_blocked == False,  # noqa: E712
    ))).scalars().all()
    out: list[dict] = []
    for media in medias:
        if media.media_type == "url_display":
            continue  # pas de fichier à pré-télécharger (offline-first via la config du sync)
        play_at = upcoming[media.id_media]
        priority = int((play_at - datetime(1970, 1, 1)).total_seconds())
        # Document multipage converti : pré-télécharger les PAGES (ce sont elles qui
        # sont servies dans le payload), pas le PDF/PPTX source.
        doc_pages = (
            document_media.document_pages(media.media_metadata)
            if media.media_type == "document" else None
        )
        if doc_pages:
            for p in doc_pages:
                key = p.get("key")
                if not key:
                    continue
                try:
                    url = await anyio.to_thread.run_sync(partial(get_signed_url, key, 86400))
                except Exception:  # noqa: BLE001
                    url = None
                out.append({
                    "id_media": media.id_media,
                    "media_type": "image",
                    "cache_key": key,
                    "cache_checksum": p.get("checksum"),
                    "mime_type": "image/jpeg",
                    "url": url,
                    "play_at": play_at.isoformat(),
                    "priority": priority,
                })
            continue
        if media.id_media in mz_media_ids:
            cache_key = await _resolve_variant_key(
                db, media, target_height, None, True, allow_hevc,
                bake_profile=bake_profile, bake_height=bake_height)
        else:
            cache_key = await _resolve_variant_key(
                db, media, target_height, prefer_codec, browser_only, allow_hevc,
                bake_profile=bake_profile, bake_height=bake_height)
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, cache_key, 86400))
        except Exception:
            url = None
        out.append({
            "id_media": media.id_media,
            "media_type": media.media_type,
            "cache_key": cache_key,
            "cache_checksum": media.checksum_sha256 if cache_key == media.storage_key else None,
            "mime_type": media.mime_type,
            "url": url,
            "play_at": play_at.isoformat(),
            # priorité = secondes depuis l'epoch (soustraction naïve, TZ-cohérente) : plus
            # petit = diffusion plus proche = plus prioritaire au DL et à la conservation.
            "priority": priority,
        })
    out.sort(key=lambda x: x["priority"])
    return out


def _vod_media_has_audio(media) -> bool:
    """Le média porte-t-il une piste audio ? Délègue à la règle UNIQUE de l'endpoint VOD
    (import tardif : évite un cycle ws <-> vod au chargement du module)."""
    from app.api.v1.endpoints.vod import _media_has_audio
    return _media_has_audio(media)


async def _build_vod_media(db, id_player: int) -> list[dict]:
    """VOD à la demande (Feature B, Phase 3) : médias des configs VOD ciblant ce player,
    résolus par variante (rendition) + URL signée comme le prefetch. Le player expose cette
    liste au panneau de contrôle LAN (§19.3) pour la lecture ad-hoc LOCALE (takeover), et la
    résout offline-first (cache local prioritaire). Pré-caché en Phase 1 → lecture instantanée.
    """
    from app.models.vod import CmsVodConfigMedia  # import tardif (évite cycle)
    from app.services.vod_targeting import vod_config_ids_for_player

    player = (await db.execute(select(CmsPlayer).where(CmsPlayer.id_player == id_player))).scalar_one_or_none()
    if not player:
        return []
    # Configs ciblant ce player : liste explicite OU ciblage par site (migr 0096).
    vod_ids = await vod_config_ids_for_player(db, player.id_org, id_player)
    if not vod_ids:
        return []
    rows = (await db.execute(
        select(CmsVodConfigMedia.id_media, CmsVodConfigMedia.sort_order)
        .where(CmsVodConfigMedia.id_vod.in_(vod_ids))
        .order_by(CmsVodConfigMedia.sort_order, CmsVodConfigMedia.id_vod_media)
    )).all()
    # dédoublonne (un média peut être dans plusieurs configs), garde le 1er ordre vu
    seen: set[int] = set()
    ordered_ids: list[int] = []
    for mid, _so in rows:
        if mid not in seen:
            seen.add(mid)
            ordered_ids.append(mid)
    if not ordered_ids:
        return []

    target_height = await _player_target_height(db, player)
    # Le takeover VOD est TOUJOURS rendu par un <video> Chromium (overlay), même sur un player
    # mpv (NUC) ou exo (MINIX) → on force browser_only=True (jamais de HEVC/MKV injouable par un
    # <video>) ; allow_hevc reste la capacité navigateur déclarée (ChromeOS récent). prefer_codec=None.
    allow_hevc = _player_video_can_hevc(player)
    # Rotation à graver : une VOD plein écran sur une dalle BS portrait doit être pré-rotée
    # comme le reste (le widget reste en paysage).
    bake_profile = rendition_svc.bake_rotation_profile(player)
    bake_height = await _bake_group_height(db, player) if bake_profile else None

    medias = {m.id_media: m for m in (await db.execute(select(CmsMedia).where(
        CmsMedia.id_media.in_(ordered_ids),
        CmsMedia.is_deleted == False,  # noqa: E712
        CmsMedia.is_blocked == False,  # noqa: E712
    ))).scalars().all()}

    out: list[dict] = []
    for mid in ordered_ids:
        media = medias.get(mid)
        if media is None:
            continue
        if media.media_type == "hdmi_input":
            # Pointeur « entrée HDMI » (pas de fichier) : le switch passe par le trigger CLOUD
            # (play_vod_now → cmd hdmi_input), pas par le panneau LAN local — l'exposer ici
            # ferait résoudre une rendition/URL sur un storage_key vide. Backlog : switch HDMI
            # dans le Store Control Panel LAN.
            continue
        cache_key = await _resolve_variant_key(
            db, media, target_height, None, True, allow_hevc,
            bake_profile=bake_profile, bake_height=bake_height)
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, cache_key, 86400))
        except Exception:  # noqa: BLE001
            url = None
        out.append({
            "id_media": media.id_media,
            "media_name": media.media_name,
            "media_type": media.media_type,
            "cache_key": cache_key,
            # checksum SEULEMENT si on sert l'original (les renditions n'ont pas le checksum du parent)
            "cache_checksum": media.checksum_sha256 if cache_key == media.storage_key else None,
            "mime_type": media.mime_type,
            "url": url,
            "duration_ms": media.duration_ms,
            # Ducking du panneau LAN local : sans cette info, un déclenchement local ferait
            # baisser la musique même pour une vidéo MUETTE (cf. _media_has_audio, vod.py).
            "has_audio": _vod_media_has_audio(media),
        })
    return out


async def _build_vod_configs(db, player) -> list[dict]:
    """Configs VOD PILOTABLES depuis le panneau de contrôle local de ce player (§19.3).

    ⚠️ Distinct de `_build_vod_media`, et c'est tout l'objet du correctif du 2026-07-19 :
      - `vod_media` (plat)   = ce que CE player peut JOUER lui-même → configs qui le CIBLENT
                               (médias pré-cachés, URLs résolues) ;
      - `vod_configs` (ici)  = ce qu'il peut PILOTER → toutes les configs de son SITE, chacune
                               avec SA cible résolue.
    Avant, le panneau fusionnait tout : médias de toutes les configs mélangés et liste
    complète des players du site, sans dire quel player appartenait à quelle config
    (constaté par Ed sur le NUC : VOD_Domicile + VOD_Site + VOD_Twingo indistinguables).

    Pas d'URL ni de cache_key ici : un player de contrôle n'a pas forcément ces médias en
    cache, il ne fait que déclencher chez les autres (le paquet LAN ne porte qu'un id_media).
    Les players NAVIGATEUR sont exclus des cibles (pas de socket UDP), comme pour `peers`.
    """
    from app.models.vod import CmsVodConfigMedia  # import tardif (évite cycle)
    from app.services.vod_targeting import vod_configs_for_site, vod_player_ids

    configs = await vod_configs_for_site(db, player.id_org, player.id_site)
    if not configs:
        return []

    out: list[dict] = []
    for cfg in configs:
        pids = await vod_player_ids(db, cfg)
        targets = (await db.execute(select(CmsPlayer).where(
            CmsPlayer.id_player.in_(pids),
            func.coalesce(CmsPlayer.player_os, "").notin_(("chromeos", "chrome_extension")),
        ))).scalars().all() if pids else []
        media_rows = (await db.execute(
            select(CmsMedia, CmsVodConfigMedia.sort_order)
            .join(CmsVodConfigMedia, CmsVodConfigMedia.id_media == CmsMedia.id_media)
            .where(
                CmsVodConfigMedia.id_vod == cfg.id_vod,
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.is_blocked == False,  # noqa: E712
            )
            .order_by(CmsVodConfigMedia.sort_order, CmsVodConfigMedia.id_vod_media)
        )).all()
        out.append({
            "id_vod": cfg.id_vod,
            "vod_name": cfg.vod_name,
            "medias": [
                {
                    "id_media": m.id_media,
                    "media_name": m.media_name,
                    "media_type": m.media_type,
                    # Pilote le ducking côté player déclenché (cf. _media_has_audio).
                    "has_audio": _vod_media_has_audio(m),
                }
                for m, _so in media_rows
            ],
            "players": [
                {"id_player": p.id_player, "name": p.player_name}
                for p in sorted(targets, key=lambda x: x.id_player)
            ],
        })
    return out


async def _build_vod_lan(db, id_player: int) -> dict | None:
    """VOD à la demande (Feature B, Phase 3) — déclenchement multi-cible AUTONOME LAN.

    Permet au panneau de contrôle local (§19.3) d'un player de lancer une vidéo sur N'IMPORTE
    QUEL player du groupe VOD, sans serveur (broadcast UDP signé entre players sur le LAN).
    Renvoie le secret partagé (même valeur pour tous les players du site → signe/vérifie les
    commandes, livré ici donc caché offline) + la liste des pairs ciblables (players partageant
    une config VOD avec celui-ci). None si le player n'est dans aucune config VOD.
    """
    import hashlib
    import hmac as _hmac

    # import tardif (évite cycle)
    from app.services.vod_targeting import vod_config_ids_for_player, vod_players_for_config_ids

    player = (await db.execute(select(CmsPlayer).where(CmsPlayer.id_player == id_player))).scalar_one_or_none()
    if not player:
        return None

    vod_ids = await vod_config_ids_for_player(db, player.id_org, id_player)
    if not vod_ids:
        # Player de CONTRÔLE non ciblé : il ne joue rien lui-même mais doit pouvoir déclencher
        # chez les autres → il lui faut le secret. On ne le donne que si son site porte
        # effectivement des configs VOD (sinon rien à piloter). Décision Ed 2026-07-19.
        from app.services.vod_targeting import vod_configs_for_site
        if not await vod_configs_for_site(db, player.id_org, player.id_site):
            return None

    # Pairs = tous les players ciblés par les mêmes configs VOD (self inclus) → cibles du picker.
    # On EXCLUT les players navigateur (ChromeOS PWA + extension Chrome) : le sandbox navigateur ne
    # peut pas écouter de socket UDP → ils ne peuvent pas être cibles d'un trigger LAN (ils restent
    # déclenchables via l'UI cloud). Inutile de les afficher dans le picker (cf. retour Ed).
    peer_ids = await vod_players_for_config_ids(db, player.id_org, vod_ids)
    peers = (await db.execute(select(CmsPlayer).where(
        CmsPlayer.id_player.in_(peer_ids),
        func.coalesce(CmsPlayer.player_os, "").notin_(("chromeos", "chrome_extension"))))).scalars().all()

    # Secret partagé par SITE (les configs VOD imposent un id_site commun) → tous les players
    # ciblables ont le même → ils signent/valident mutuellement. Repli org si site nul.
    scope = f"site:{player.id_site}" if player.id_site is not None else f"org:{player.id_org}"
    secret = _hmac.new(
        settings.CMS_JWT_SECRET.encode(), f"vodlan:{scope}".encode(), hashlib.sha256
    ).hexdigest()

    return {
        "secret": secret,
        "self_id": id_player,
        "peers": [
            {"id_player": p.id_player, "name": p.player_name}
            for p in sorted(peers, key=lambda x: x.id_player)
        ],
    }


async def _resolve_offline_power(db, player, display_config: dict | None,
                                 site: "CmsSite | None" = None) -> dict | None:
    """Bloc `offline_power` du display_config : autonomie auto-power hors connexion.

    Pré-calcule 14 jours de fenêtres d'allumage absolues (heure LOCALE du site) pour
    que le player, persistance display_config.json aidant, allume/éteigne son écran
    même sans serveur (coupure ligne internet / serveur down). Miroir strict de la
    logique du worker `apply_site_power` (event_worker) :
      - uniquement si le site du player a `auto_power_enabled` ;
      - uniquement si l'écran est piloté (rs232/ip/cec) ou en repli sortie vidéo ;
      - mode player : follow_site (fenêtres du site, marges lead/lag comprises) /
        custom (fenêtre vitrine propre) / always_on / manual (None = ne rien piloter).
    Limites assumées (filet de sécurité, pas la source de vérité) : les events
    `keep_screens_on` et les changements postérieurs à la dernière sync ne sont pas
    connus offline. Le serveur reprend la main dès la reconnexion (re-assertion via
    reset de last_power_state au connect WS).
    Format : {"mode": "...", "intervals": [["YYYY-MM-DDTHH:MM:SS", ...], ...]}
    (intervals absent pour always_on). None = pas de pilotage offline.
    """
    dc = display_config or {}
    if dc.get("type") not in ("rs232", "ip", "cec") and not dc.get("output_power_fallback"):
        return None
    power_cfg = dc.get("power_schedule") or {}
    mode = power_cfg.get("mode", "follow_site")
    if mode == "manual":
        return None

    # Parité stricte avec le worker : l'auto-power est un opt-in PAR SITE — un player
    # (même always_on/custom) d'un site sans auto_power_enabled n'est jamais piloté.
    if not player.id_site:
        return None
    if site is None:
        site = (await db.execute(
            select(CmsSite).where(CmsSite.id_site == player.id_site)
        )).scalar_one_or_none()
    if site is None or not site.auto_power_enabled:
        return None
    if mode == "always_on":
        return {"mode": "always_on"}

    try:
        tz = ZoneInfo(site.timezone or settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    now_local = datetime.now(UTC).astimezone(tz).replace(tzinfo=None)
    start = now_local.date()

    if mode == "custom":
        intervals = site_hours_svc.custom_power_intervals(power_cfg, start=start)
    else:  # follow_site
        hours = (await db.execute(
            select(CmsSiteHours).where(CmsSiteHours.id_site == site.id_site)
        )).scalars().all()
        weekly = {
            h.weekday: site_hours_svc.DayHours(
                is_closed=h.is_closed, open_time=h.open_time, close_time=h.close_time
            )
            for h in hours
        }
        exc_rows = (await db.execute(
            select(CmsSiteException).where(
                CmsSiteException.id_site == site.id_site,
                CmsSiteException.date_to >= start - timedelta(days=1),
                CmsSiteException.date_from <= start + timedelta(days=14),
            )
        )).scalars().all()
        exceptions = [
            site_hours_svc.Exception_(
                date_from=e.date_from, date_to=e.date_to, kind=e.kind,
                open_time=e.open_time, close_time=e.close_time,
            )
            for e in exc_rows
        ]
        intervals = site_hours_svc.site_power_intervals(
            weekly, exceptions,
            lead_min=site.power_lead_min, lag_min=site.power_lag_min,
            respect_holidays=site.respect_holidays, region=site.holiday_region,
            start=start,
        )

    return {
        "mode": mode,
        "intervals": [
            [on_dt.isoformat(timespec="seconds"), off_dt.isoformat(timespec="seconds")]
            for on_dt, off_dt in intervals
        ],
    }


async def _resolve_sync_payload(db, id_player: int) -> dict:
    """Message player:sync + liste `prefetch` (look-ahead) attachée à TOUT payload.

    Fin wrapper autour de `_build_sync_payload` pour que le prefetch soit présent quelle que
    soit la forme du payload (empty / event / schedule / gouvernance) sans toucher chaque
    point de retour.
    """
    # Fuseau de l'écran résolu UNE fois pour tout le payload (P2.2) : diffusion et
    # look-ahead doivent raisonner sur la MÊME horloge, et le résoudre par fonction
    # coûterait une requête de plus par sync, pour tout le parc, toutes les 30 s.
    tz_name = await player_timezone.player_tz_name(db, id_player)
    payload = await _build_sync_payload(db, id_player, tz_name=tz_name)
    payload["prefetch"] = await _build_prefetch(db, id_player, tz_name=tz_name)
    payload["vod_media"] = await _build_vod_media(db, id_player)
    payload["vod_lan"] = await _build_vod_lan(db, id_player)
    # Panneau de contrôle local (§19.3) : configs PILOTABLES, chacune avec sa cible résolue.
    # Distinct de `vod_media` (ce que CE player peut jouer) — cf. _build_vod_configs.
    # Défensif comme le bloc OTA : une erreur ici ne doit pas casser le sync de tout le parc.
    try:
        _p = (await db.execute(
            select(CmsPlayer).where(CmsPlayer.id_player == id_player)
        )).scalar_one_or_none()
        payload["vod_configs"] = await _build_vod_configs(db, _p) if _p else []
    except Exception:  # noqa: BLE001
        payload["vod_configs"] = []
    # OTA §XXX slice 2 : directive de mise à jour si le player a une cible actionnable (None sinon).
    # Backward-compatible : un renderer sans handler ignore ce bloc. Défensif : une erreur ici ne
    # doit JAMAIS casser le sync payload d'un player (tourne pour tout le parc à chaque sync).
    try:
        payload["update"] = await ota.directive_for_player(db, id_player)
    except Exception as exc:  # noqa: BLE001
        payload["update"] = None
        print(f"[WS] OTA directive échec player {id_player}: {exc!r}")
    return payload


async def _build_sync_payload(db, id_player: int, tz_name: str | None = None) -> dict:
    """
    Construit le message player:sync.

    Priorité : event actif (§XX) > schedule actif (§XIII) > empty.
    Un event peut cibler une playlist OU un media unique (id_media, Sprint 2.4).
    Les schedules peuvent cibler des players individuels OU des groupes de players.

    `tz_name` (P2.2) : fuseau du SITE de l'écran — c'est lui qui décide de l'heure à
    laquelle les règles horaires sont comparées. Résolu ici s'il n'est pas fourni.
    """
    tz_name = tz_name or await player_timezone.player_tz_name(db, id_player)
    now = _local_now(tz_name)
    empty = {
        "type": "player:sync",
        "id_playlist": None,
        "id_schedule": None,
        "items": [],
        "resolved_at": now.isoformat(),
    }

    # ── 0. Player + groupes d'appartenance ────────────────────────────────────
    player = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player)
    )).scalar_one_or_none()
    # Rôle audio/contenu (§XIX) → display_config effectif. Un player 'mixed' partage
    # une seule sortie audio (jack/USB) avec ses paniers : le son des contenus standards
    # est donc forcé muté côté serveur (source de vérité, robuste à un cache obsolète).
    # On copie avant override pour ne jamais muter l'objet ORM.
    player_role = player.player_role if player else "content"
    display_config = player.display_config if player else None
    if player_role == "mixed":
        display_config = {**(display_config or {}), "content_sound": "muted"}
    # Video Wall : transform de compensation de bezel (mode B overscan / mode A crop) calculé
    # serveur-side depuis display_config.wall, poussé au renderer. None si pas de mur configuré.
    wall_tf = video_wall.player_wall_transform((display_config or {}).get("wall"))
    if wall_tf is not None:
        display_config = {**(display_config or {}), "wall_transform": wall_tf}
    # Site du player (chargé une fois) : NTP hérité + fenêtres offline auto-power.
    player_site = None
    if player and player.id_site:
        player_site = (await db.execute(
            select(CmsSite).where(CmsSite.id_site == player.id_site)
        )).scalar_one_or_none()
    # NTP du site (migr 0071) : hérité par les players SANS config NTP propre (une config
    # posée par player — relais LAN sync ntp_role server/client — PRIME sur le site).
    # Appliqué par applyNtpConfig côté player (chrony linux/rpi, w32time windows).
    if (player_site is not None and player_site.ntp_server
            and not (display_config or {}).get("ntp_role")
            and not (display_config or {}).get("ntp_server")):
        display_config = {**(display_config or {}),
                          "ntp_role": "client", "ntp_server": player_site.ntp_server}
    # Autonomie offline (auto-power) : fenêtres d'allumage pré-calculées 14 j, persistées
    # côté player (display_config.json) → il pilote son écran seul si le serveur est
    # injoignable. Rafraîchi à chaque sync (~30 s online) → exceptions/changements suivis.
    if player:
        offline_power = await _resolve_offline_power(db, player, display_config, site=player_site)
        if offline_power is not None:
            display_config = {**(display_config or {}), "offline_power": offline_power}
    empty["display_config"] = display_config

    # Nom CMS du player — inclus dans tous les payloads (overlay de maintenance, §diag).
    player_name = player.player_name if player else None
    empty["player_name"] = player_name

    # Paniers audio in-store (§XIX) — UNIQUEMENT pour les rôles 'audio' et 'mixed'.
    # Un player 'content' (visuel only, §XIX) ne diffuse AUCUN fond audio : on lui envoie
    # une liste vide → côté player setBaskets([]) stoppe le runtime audio. Source de vérité
    # serveur (robuste au cache obsolète) ; un changement de rôle déclenche un force_sync
    # (endpoints/players.py) → l'ambiance s'arrête à chaud. Aligné sur le gate diagnostic
    # _resolve_sync_payload (endpoints/players.py:688).
    audio_baskets = (
        await _resolve_audio_baskets(db, id_player, tz_name=tz_name)
        if player and player_role in ("audio", "mixed")
        else []
    )
    # Retail Media AUDIO S2 (§2.8 × §XIX, migr 0094) : panier SYNTHÉTIQUE des spots
    # annonceurs, greffé UNIQUEMENT si le player a un panier MUSICAL actif avec du
    # contenu (« pas de musique = pas d'emplacement », décision Ed 16/07). Défensif :
    # une erreur d'injection ne casse jamais un sync (pattern _inject_campaign_items).
    if audio_baskets and any(
        b.get("basket_type") == "music" and b.get("items") for b in audio_baskets
    ):
        try:
            ad_basket = await campaign_audio.audio_spot_basket(
                db, player.id_org, id_player, now
            )
            if ad_basket:
                audio_baskets.append(ad_basket)
        except Exception as exc:  # noqa: BLE001 — jamais casser un sync
            print(f"[WS] injection spots audio échec player {id_player}: {exc!r}")
    empty["audio_baskets"] = audio_baskets

    # Profil Store Control Panel LAN (§19.3) — inclus dans tous les payloads (None si désactivé)
    local_control = await _resolve_local_control(db, id_player, display_config) if player else None
    empty["local_control"] = local_control

    # Config de repli (§17.5) — message vidéo custom + piste audio de secours.
    # Incluse dans tous les payloads : le player s'en sert dès qu'il n'a pas de contenu.
    # La piste audio de secours suit la même règle que les paniers : un player 'content'
    # ne joue pas de fond audio → on annule l'audio de repli (le message vidéo reste).
    fallback = await _resolve_fallback(db, player)
    if player_role == "content" and fallback.get("audio"):
        fallback = {**fallback, "audio": None}
    empty["fallback"] = fallback

    # Quarantaine / kill-switch (§ Q5) — ids des médias bloqués de l'org. Inclus dans
    # tous les payloads : garde côté player (défense en profondeur, même offline / cache).
    # Les médias bloqués sont AUSSI exclus des items ci-dessous (enforcement server-side).
    blocked_media: list[int] = []
    if player:
        blocked_media = [r for (r,) in (await db.execute(
            select(CmsMedia.id_media).where(
                CmsMedia.id_org == player.id_org, CmsMedia.is_blocked == True  # noqa: E712
            )
        )).all()]
    empty["blocked_media"] = blocked_media

    # ── Rôle 'Audio seul' (§XIX) : player audio dédié ──────────────────────────
    # Il ne diffuse AUCUNE playlist de contenu visuel — uniquement ses paniers audio
    # (déjà dans empty["audio_baskets"]) + le visuel de repli minimaliste. On court-circuite
    # donc toute résolution event/schedule/gouvernance et on renvoie le payload idle.
    # Source de vérité serveur (robuste au cache) ; le changement de rôle déclenche un
    # force_sync (endpoints/players.py) → le contenu s'arrête à chaud. Retour terrain Ed 2026-06-20.
    if player_role == "audio":
        return empty

    # Renditions adaptatives : hauteur vidéo cible pour ce player (min du groupe LAN).
    # Calculée une fois ; utilisée pour choisir la variante de chaque média vidéo servi.
    target_height = await _player_target_height(db, player) if player else None
    # Préférence codec (HEVC pour un player ExoPlayer dont le décodeur h264 cale en 4K@60).
    prefer_codec = _player_prefers_hevc(player)
    # Conteneur navigateur-safe : un player <video> Chromium (ChromeOS/extension/WebView) ne lit
    # pas le MKV/MOV → on exclut l'originale non-mp4 de la sélection de variante (ceinture/bretelles).
    browser_only = _player_browser_only(player)
    # Ce player navigateur déclare-t-il décoder le HEVC (ChromeOS récent) ? Sinon (WebView 83) → H.264.
    allow_hevc = _player_video_can_hevc(player)
    # Rotation à graver (BrightSign monté en portrait, 2026-08-14) : toutes les vidéos servies
    # à ce player passent par leur rendition PRÉ-ROTÉE — le widget reste en paysage.
    bake_profile = rendition_svc.bake_rotation_profile(player)
    bake_height = (await _bake_group_height(db, player)) if (player and bake_profile) else None

    player_group_ids: set[int] = set()
    player_site_group_ids_: set[int] = set()
    if player:
        player_group_ids = set(
            (await db.execute(
                select(CmsPlayerGroupMember.id_group).where(
                    CmsPlayerGroupMember.id_player == id_player
                )
            )).scalars().all()
        )
        player_site_group_ids_ = await player_site_group_ids(db, id_player)

    # ── 1. Event override (§XX) ────────────────────────────────────────────────
    active_event: CmsEvent | None = None
    if player:
        # ⚠️ `status == 'active'` NE SUFFIT PAS : c'est `expire_events` (beat 30 s) qui
        # bascule un event echu, donc pendant 30 s au plus la base le dit encore actif.
        # Sans cette garde, une synchro demandee JUSTE APRES la fin d'un event « joue une
        # fois » se voit reservir l'event — et le player relance la video. C'est ce qui
        # rendait le correctif cote player inoperant a lui seul (constat d'Ed, 11/08).
        active_events = (await db.execute(
            select(CmsEvent)
            .where(
                CmsEvent.id_org == player.id_org,
                CmsEvent.status == "active",
                CmsEvent.event_kind == "content",
                # ⚠️ PAS `now` : `_local_now` rend l'heure MURALE du site, NAIVE. La comparer
                # a un `timestamptz` la ferait interpreter dans le fuseau de la SESSION
                # PostgreSQL — meme classe de defaut que l'embargo (P2.6). `expires_at`
                # est un instant ABSOLU : il se compare a un instant absolu.
                or_(CmsEvent.expires_at.is_(None),
                    CmsEvent.expires_at > datetime.now(UTC)),
            )
            .order_by(CmsEvent.priority.desc())
        )).scalars().all()

        for ev in active_events:
            if event_target_matches(
                ev.target_config, id_player, player_group_ids, player_site_group_ids_,
                id_site=player.id_site,
            ):
                active_event = ev
                break

    # ── 2. Schedule fallback ──────────────────────────────────────────────────
    active_sched: CmsSchedule | None = None
    matched_rule: CmsScheduleRule | None = None
    if active_event is None and player is not None:
        # ── Gouvernance §XVIII Phase 2 (lot 2b) : programme compilé multi-couches.
        # Opt-in par la donnée : ne s'active que si le player est gouverné ET qu'une
        # couche est active maintenant. Sinon None → résolution schedule legacy ci-dessous.
        gov_payload = await _resolve_governance_payload(
            db, player, now, display_config, audio_baskets, local_control, fallback,
            blocked_media, target_height,
        )
        if gov_payload is not None:
            return gov_payload

    if active_event is None:
        # Cibles directes (player)
        targets = list(
            (await db.execute(
                select(CmsScheduleTarget).where(
                    CmsScheduleTarget.target_type == "player",
                    CmsScheduleTarget.target_id == id_player,
                )
            )).scalars().all()
        )
        # Cibles via groupes de players
        if player_group_ids:
            group_targets = list(
                (await db.execute(
                    select(CmsScheduleTarget).where(
                        CmsScheduleTarget.target_type == "player_group",
                        CmsScheduleTarget.target_id.in_(player_group_ids),
                    )
                )).scalars().all()
            )
            targets.extend(group_targets)
        # Cible SITE (le player est rattaché au site visé)
        if player.id_site is not None:
            targets.extend(list(
                (await db.execute(
                    select(CmsScheduleTarget).where(
                        CmsScheduleTarget.target_type == "site",
                        CmsScheduleTarget.target_id == player.id_site,
                    )
                )).scalars().all()
            ))
        # Cibles via groupes de sites (S6 — le site du player appartient au groupe)
        if player_site_group_ids_:
            targets.extend(list(
                (await db.execute(
                    select(CmsScheduleTarget).where(
                        CmsScheduleTarget.target_type == "site_group",
                        CmsScheduleTarget.target_id.in_(player_site_group_ids_),
                    )
                )).scalars().all()
            ))

        if not targets:
            return empty

        schedule_ids = [t.id_schedule for t in targets]
        schedules = list(
            (await db.execute(
                select(CmsSchedule)
                .where(
                    CmsSchedule.id_schedule.in_(schedule_ids),
                    CmsSchedule.schedule_kind == "content",
                    CmsSchedule.is_active == True,  # noqa: E712
                    CmsSchedule.is_deleted == False,  # noqa: E712
                    # D2 (2026-07-24) : un programme rattaché à un nœud de gouvernance est
                    # diffusé UNIQUEMENT par ce nœud. Ses cibles classiques sont ignorées ici,
                    # sinon il rejouerait par le chemin classique SANS règle de délégation
                    # dès que la couche gouvernée est vide (fenêtre fermée, budget, etc.).
                    CmsSchedule.id_governance_node.is_(None),
                )
                .order_by(CmsSchedule.priority.desc())
            )).scalars().all()
        )

        for sched in schedules:
            rules = list(
                (await db.execute(
                    select(CmsScheduleRule).where(
                        CmsScheduleRule.id_schedule == sched.id_schedule
                    )
                )).scalars().all()
            )
            if not rules:
                continue
            for r in rules:
                if schedule_active_at(time_rule_from_schedule(r), sched.valid_from, sched.valid_until, now):
                    active_sched = sched
                    matched_rule = r
                    break
            if active_sched:
                break

        if active_sched is None:
            return empty

    # ── 3a. Event avec media unique (pas de playlist) ─────────────────────────
    if active_event and not active_event.id_playlist and active_event.id_media:
        media = (await db.execute(
            select(CmsMedia).where(
                CmsMedia.id_media == active_event.id_media,
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.is_blocked == False,  # noqa: E712 — quarantaine (§ Q5)
            )
        )).scalar_one_or_none()
        if media is None:
            return empty
        if media.media_type == "url_display":
            items = [{
                "id_item": None,
                "sort_order": 0,
                "zone_index": 0,
                "duration_ms": active_event.duration_ms or 10000,
                "transition_type": "fade",
                "media": _url_display_media_dict(media),
            }]
        elif media.media_type == "feed_display":
            items = [{
                "id_item": None,
                "sort_order": 0,
                "zone_index": 0,
                "duration_ms": active_event.duration_ms or 10000,
                "transition_type": "fade",
                "media": await _feed_display_media_dict(db, media),
            }]
        elif media.media_type == "hdmi_input":
            if not _player_has_hdmi_input(player):
                return empty  # gate capacité : player sans entrée HDMI → rien à diffuser
            items = [{
                "id_item": None,
                "sort_order": 0,
                "zone_index": 0,
                "duration_ms": active_event.duration_ms or 10000,
                "transition_type": "fade",
                "media": _hdmi_input_media_dict(media),
            }]
        elif media.media_type == "document" and (
            page_items := await _document_page_items(
                media, None, active_event.duration_ms or 10000, transition_type="fade",
            )
        ):
            # Event → document multipage : cycle des pages (durée event = PAR PAGE).
            items = page_items
        else:
            if media.media_type == "document" and (media.mime_type or "").lower() != "application/pdf":
                return empty  # PPT/PPTX non converti = injouable
            storage_key = await _resolve_variant_key(
                db, media, target_height, prefer_codec, browser_only, allow_hevc,
                bake_profile=bake_profile, bake_height=bake_height,
            )
            try:
                url = await anyio.to_thread.run_sync(partial(get_signed_url, storage_key, 86400))
            except Exception:
                url = None
            items = [{
                "id_item": None,
                "sort_order": 0,
                "zone_index": 0,
                "duration_ms": active_event.duration_ms or media.duration_ms or 10000,
                "transition_type": "fade",
                "media": {
                    "id_media": media.id_media,
                    "media_name": media.media_name,
                    "media_type": media.media_type,
                    "mime_type": media.mime_type,
                    "url": url,
                    "checksum_sha256": media.checksum_sha256,
                    "cache_key": storage_key,
                    "cache_checksum": media.checksum_sha256 if storage_key == media.storage_key else None,
                    # Repli offline : si la variante servie n'est pas (encore) cachée mais que
                    # le parent l'est (ex. config 4K antérieure) + serveur down → jouer le parent
                    # plutôt qu'un écran noir. None quand on sert déjà le parent (variant==parent).
                    "cache_key_fallback": media.storage_key if storage_key != media.storage_key else None,
                    "cache_checksum_fallback": media.checksum_sha256 if storage_key != media.storage_key else None,
                    "subtitles": await _subtitle_urls(media),
                },
            }]
        # Sync wall-clock pour les groupes LAN : un event mono-média (1 item, bouclé) doit démarrer
        # au MÊME point de boucle sur tous les membres du groupe — sinon chacun démarre à l'instant
        # où SON sync arrive → départ décalé (ce path renvoyait offset 0 fixe, contrairement aux
        # playlists wall_clock). Réf = activated_at de l'event ; période = durée NATURELLE de la
        # vidéo (media.duration_ms) pour que le seek reste dans les bornes même si l'event a une
        # durée d'affichage plus longue. La LAN sync (UDP) maintient ensuite l'alignement serré
        # sur les boucles suivantes.
        #
        # 🔑 DEUXIÈME RAISON, INDÉPENDANTE DE LA SYNCHRO ENTRE ÉCRANS (2026-08-11).
        # Un event « joue une fois » se termine à un instant FIXE que le SERVEUR décide
        # (`expires_at`, posé à l'activation). Un écran qui rejoint en cours de diffusion —
        # redémarrage, coupure réseau, écran rallumé — et qui repartirait du DÉBUT se ferait
        # couper à l'échéance sans avoir atteint la fin : il ne verrait qu'un fragment, et
        # d'autant moins qu'il arrive tard. Le décalage n'est alors pas un alignement entre
        # écrans, c'est un placement sur une horloge que le serveur va lui-même arrêter.
        # Il vaut donc pour TOUT écran, groupe LAN ou non (question d'Ed : « techniquement,
        # est-il possible de le faire commencer à l'endroit où il devrait être ? »).
        #
        # ⛔ Volontairement PAS étendu aux events en BOUCLE hors groupe LAN : il n'y existe
        # aucune « position où l'on devrait être » — ça tourne jusqu'à la fermeture de la
        # fenêtre, et démarrer à zéro laisse voir le contenu ENTIER. L'y appliquer changerait
        # le comportement de tout le parc sans rien faire gagner à personne.
        #
        # ⚠️ Le décalage est calculé ICI, sur l'horloge du SERVEUR : il ne dépend d'aucune
        # horloge de player. C'est ce qui le rend sûr sur un écran hors groupe LAN, dont on
        # ne sait rien de la synchronisation NTP.
        single_offset_ms = 0
        # Période du modulo = période de boucle RÉELLE du renderer (item.duration_ms ci-dessus
        # = active_event.duration_ms or media.duration_ms), PAS media.duration_ms seul : si l'event
        # impose une durée d'affichage ≠ durée naturelle, le renderer reboucle à active_event.
        # duration_ms → un modulo sur media.duration_ms placerait l'offset hors de la fenêtre de
        # boucle (seek tronqué/écran figé, désync même sans restart). On miroite la même valeur.
        loop_period_ms = active_event.duration_ms or media.duration_ms
        wants_offset = bool(
            (display_config or {}).get("lan_sync_enabled")  # aligner les écrans entre eux
            or active_event.duration_ms                     # « joue une fois » : horloge serveur
        )
        if (
            wants_offset
            and active_event.activated_at
            and media.media_type == "video"
            and loop_period_ms
        ):
            act_at = active_event.activated_at
            if act_at.tzinfo is None:
                act_at = act_at.replace(tzinfo=UTC)
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            ref_ms = int(act_at.timestamp() * 1000)
            single_offset_ms = (now_ms - ref_ms) % loop_period_ms
        return {
            "type": "player:sync",
            "id_playlist": None,
            "id_media": active_event.id_media,
            "id_schedule": None,
            "id_event": active_event.id_event,
            "event_name": active_event.event_name,
            # Le player ne savait pas distinguer « joue une fois » de « boucle » : il
            # rebouclait TOUJOURS a la fin de sa liste, si bien qu'un event play-once
            # relancait sa video et se faisait couper quelques secondes plus tard, quand
            # le beat finissait par le faire expirer.
            "play_once": bool(active_event.duration_ms),
            "player_name": player_name,
            "items": items,
            "start_index": 0,
            "start_offset_ms": single_offset_ms,
            "display_config": display_config,
            "audio_baskets": audio_baskets,
            "local_control": local_control,
            "fallback": fallback,
            "blocked_media": blocked_media,
            "resolved_at": now.isoformat(),
        }

    # ── 3b. Playlist ID (event avec playlist ou schedule) ─────────────────────
    active_playlist_id = (
        active_event.id_playlist if active_event else active_sched.id_playlist  # type: ignore[union-attr]
    )
    if active_playlist_id is None:
        return empty

    # Charge la playlist + son template (réutilisés en §5/§5b). Un template MULTI-zones est
    # TOUJOURS rendu en Chromium <video> côté player (jamais mpv/exo dans une sous-zone, §25.4)
    # → on force la résolution de variante navigateur-safe (comme le takeover VOD) : sinon un
    # player mpv/exo (NUC/MINIX) recevrait une variante HEVC/MKV INJOUABLE par le <video> de la
    # zone. target_height est conservé → un 4K reçoit bien sa rendition FHD selon la cible ;
    # allow_hevc reste la capacité navigateur déclarée (un Chromium HEVC-capable garde le HEVC).
    playlist_row = (await db.execute(
        select(CmsPlaylist).where(CmsPlaylist.id_playlist == active_playlist_id)
    )).scalar_one_or_none()
    template_row = None
    is_multizone = False
    if playlist_row and playlist_row.id_template:
        template_row = (await db.execute(
            select(CmsTemplate).where(
                CmsTemplate.id_template == playlist_row.id_template,
                CmsTemplate.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if template_row and len((template_row.layout or {}).get("zones") or []) > 1:
            is_multizone = True
            browser_only = True
            prefer_codec = None

    # ── 4. Construction des items ──────────────────────────────────────────────
    items_rows = list(
        (await db.execute(
            select(CmsPlaylistItem)
            .where(CmsPlaylistItem.id_playlist == active_playlist_id)
            .order_by(CmsPlaylistItem.sort_order)
        )).scalars().all()
    )

    now_utc = datetime.now(UTC)

    async def _compile_item(item: CmsPlaylistItem, zone_override: int | None = None) -> dict | None:
        """Compile un item MÉDIA → dict payload (None = non servi : média supprimé/bloqué,
        embargo, gate HDMI). Factorisé pour servir aussi les items des sous-playlists
        (rotation) — `zone_override` = zone du slot parent."""
        zone_index = item.zone_index if zone_override is None else zone_override
        media = (await db.execute(
            select(CmsMedia).where(
                CmsMedia.id_media == item.id_media,
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.is_blocked == False,  # noqa: E712 — quarantaine (§ Q5)
            )
        )).scalar_one_or_none()
        if media is None:
            return None
        # Embargo : calcul du plancher effectif (direct OU hérité de l'event lié)
        effective_embargo = media.not_before_utc
        if media.embargo_event_id:
            ev = (await db.execute(
                select(CmsEvent).where(CmsEvent.id_event == media.embargo_event_id)
            )).scalar_one_or_none()
            if ev and ev.not_before_utc:
                effective_embargo = ev.not_before_utc
        if effective_embargo and effective_embargo > now_utc:
            return None  # item sous embargo — ignoré jusqu'au lever
        if media.media_type == "url_display":
            return {
                "id_item": item.id_item,
                "sort_order": item.sort_order,
                "zone_index": zone_index,
                "duration_ms": item.duration_ms or 10000,
                "transition_type": item.transition_type,
                "media": _url_display_media_dict(media),
            }
        if media.media_type == "feed_display":
            return {
                "id_item": item.id_item,
                "sort_order": item.sort_order,
                "zone_index": zone_index,
                "duration_ms": item.duration_ms or 10000,
                "transition_type": item.transition_type,
                "media": await _feed_display_media_dict(db, media),
            }
        if media.media_type == "hdmi_input":
            if not _player_has_hdmi_input(player):
                return None  # gate capacité : player sans entrée HDMI → item non servi
            return {
                "id_item": item.id_item,
                "sort_order": item.sort_order,
                "zone_index": zone_index,
                "duration_ms": item.duration_ms or 10000,
                "transition_type": item.transition_type,
                "media": _hdmi_input_media_dict(media),
            }
        if media.media_type == "document":
            page_items = await _document_page_items(
                media, item.doc_pages, item.duration_ms or 10000,
                id_item=item.id_item, sort_order=item.sort_order,
                zone_index=zone_index, transition_type=item.transition_type,
                # Seul le chemin PLAYLIST porte le réglage : les chemins compilés
                # (gouvernance, campagnes, event mono-média) n'ont pas d'item de playlist,
                # donc pas de feuilletage — et c'est cohérent, personne ne l'y a demandé.
                touch_navigation=item.touch_navigation,
                touch_resume_after_s=item.touch_resume_after_s,
            )
            if page_items:
                return page_items
            # Pas (encore) de pages converties : PDF → comportement historique
            # (viewer iframe, 1re page) ; PPT/PPTX brut = injouable → non servi.
            if (media.mime_type or "").lower() != "application/pdf":
                return None
        storage_key = await _resolve_variant_key(
            db, media, target_height, prefer_codec, browser_only, allow_hevc,
            bake_profile=bake_profile, bake_height=bake_height,
        )
        try:
            url = await anyio.to_thread.run_sync(partial(get_signed_url, storage_key, 86400))
        except Exception:
            url = None
        return {
            "id_item": item.id_item,
            "sort_order": item.sort_order,
            "zone_index": zone_index,
            "duration_ms": item.duration_ms or media.duration_ms or 10000,
            "transition_type": item.transition_type,
            "media": {
                "id_media": media.id_media,
                "media_name": media.media_name,
                "media_type": media.media_type,
                "mime_type": media.mime_type,
                "url": url,
                "checksum_sha256": media.checksum_sha256,
                "cache_key": storage_key,
                "cache_checksum": media.checksum_sha256 if storage_key == media.storage_key else None,
                # Repli offline : si la variante servie n'est pas (encore) cachée mais que
                # le parent l'est (ex. config 4K antérieure) + serveur down → jouer le parent
                # plutôt qu'un écran noir. None quand on sert déjà le parent (variant==parent).
                "cache_key_fallback": media.storage_key if storage_key != media.storage_key else None,
                "cache_checksum_fallback": media.checksum_sha256 if storage_key != media.storage_key else None,
                "subtitles": await _subtitle_urls(media),
            },
        }

    # Compilation en SLOTS : média direct / sous-playlist inline (rotation_mode=all) /
    # groupe rotation (« N items par tour », retour beta 2026-07).
    base_slots: list[dict] = []
    for item in items_rows:
        if item.item_type == "sub_playlist":
            if not item.id_child_playlist:
                continue
            child = (await db.execute(
                select(CmsPlaylist).where(
                    CmsPlaylist.id_playlist == item.id_child_playlist,
                    CmsPlaylist.is_deleted == False,  # noqa: E712
                )
            )).scalar_one_or_none()
            if child is None:
                continue  # enfant supprimé → slot ignoré (pas d'écran noir)
            crows = list((await db.execute(
                select(CmsPlaylistItem)
                .where(
                    CmsPlaylistItem.id_playlist == child.id_playlist,
                    CmsPlaylistItem.item_type == "media",  # garde : 1 seul niveau
                )
                .order_by(CmsPlaylistItem.sort_order)
            )).scalars().all())
            # Une entrée par item ENFANT — un document multipage reste UNE unité de
            # rotation (liste imbriquée de ses pages, aplatie par unroll_slots).
            compiled_children: list[dict | list] = []
            kept_ids: list[int] = []
            for cr in crows:
                c = await _compile_item(cr, zone_override=item.zone_index)
                if c is not None:
                    compiled_children.append(c)
                    kept_ids.append(cr.id_item)
            if not compiled_children:
                continue
            # « Pas de pub entre les contenus » (no_ads_inside) : le bloc émis à
            # chaque tour est protégé — unroll_slots marque no_ad_before sur tout
            # sauf le 1er item du bloc (le spot glisse avant/après, jamais dedans).
            protect = bool(child.no_ads_inside)
            if child.rotation_mode in ("one_sequential", "one_random") and len(compiled_children) > 1:
                base_slots.append({
                    "kind": "rotation",
                    "items": compiled_children,
                    "mode": child.rotation_mode,
                    "k": max(1, min(child.items_per_turn or 1, len(compiled_children))),
                    "seed": rotation_seed(child.id_playlist, kept_ids),
                    "protect": protect,
                })
            else:
                base_slots.append(
                    {"kind": "inline", "items": compiled_children, "protect": protect})
        else:
            c = await _compile_item(item)
            if c is not None:
                # Un document multipage compile en LISTE d'items (1 par page).
                base_slots.append(
                    {"kind": "inline", "items": c if isinstance(c, list) else [c]}
                )

    # Déroulé du cycle de rotation PAR LE SERVEUR : la boucle plate envoyée contient
    # les T tours successifs (T = lcm des N/gcd(N,K)) → zéro logique player,
    # wall-clock/LAN sync/PoP/injection campagnes inchangés. Déterministe : seed =
    # contenu publié de l'enfant → tous les players d'un groupe LAN construisent le
    # même déroulé.
    items = unroll_slots(base_slots)

    # ── 4b. Filtrage par tags (§XVII) ──────────────────────────────────────────
    # Retire les items que le profil de réception du player n'accepte pas. Doit
    # précéder le calcul wall-clock (la liste filtrée définit index/durées).
    items = await _filter_items_by_tags(db, id_player, items)

    # ── 4c. Filtrage par règles de diffusion d'item (§ retours clients item 4) ──
    # Retire les items time-boxés hors de leur fenêtre (heure LOCALE). Item sans
    # règle = perpétuel ; override par cible player>groupe>default. Avant le wall-clock.
    items = await _filter_items_by_rules(items, now, db, id_player, player_group_ids)

    # ── 4d. Retail Media §2.8 : injection des créatifs de campagnes live ────────
    # Schedules uniquement — un event actif = takeover exceptionnel (pas de pub pendant
    # un override).
    #
    # P3.6 (2026-08-06) : le MULTI-ZONES n'est plus exclu. Il l'était « le temps du
    # backlog », mais **aucun service ne connaissait cette notion** (mesuré) :
    # `campaign_eligible_players`, l'occupancy-check, le SOV et le devis comptaient ces
    # écrans comme vendables ⇒ un annonceur pouvait être facturé pour des écrans qui ne
    # diffuseraient jamais son spot. C'était un angle mort de FACTURATION, pas une simple
    # fonctionnalité manquante.
    if active_event is None:
        if not is_multizone:
            items = await _inject_campaign_items(
                db, player, items, now, target_height, prefer_codec, browser_only, allow_hevc,
                bake_profile=bake_profile, bake_height=bake_height,
            )
        else:
            items = await _inject_campaign_items_multizone(
                db, player, items, now, template_row,
                target_height, prefer_codec, browser_only, allow_hevc,
                bake_profile=bake_profile, bake_height=bake_height,
            )

    # ── 5. Wall-clock position ─────────────────────────────────────────────────
    start_index: int | None = None
    start_offset_ms: int | None = None

    # playlist_row + template_row déjà chargés au §4 (pour l'override navigateur-safe multi-zones).
    sync_mode = playlist_row.sync_mode if playlist_row else "wall_clock"
    # Référence wall-clock (partagée par la liste plate et les zones du template, §25.4).
    ref_ms, now_ms = _wall_clock_ref(now, active_event, matched_rule)
    if playlist_row and sync_mode == "wall_clock" and items:
        pos = _wall_clock_position(items, ref_ms, now_ms)
        if pos is not None:
            start_index, start_offset_ms = pos

    # ── 5b. Zones de template (§25.4) ──────────────────────────────────────────
    # Si la playlist pointe un template MULTI-zones, on émet un bloc `template` :
    # chaque zone = sous-playlist (items filtrés par zone_index) + son wall-clock.
    # Le player rend alors plusieurs zones simultanément (Chromium). Un template
    # mono-zone n'émet rien (chemin mono-zone inchangé).
    template_block = None
    if template_row is not None:
        template_block = _build_template_block(template_row, items, sync_mode, ref_ms, now_ms)

    # display_config est inclus dans tous les payloads pour que le player initialise
    # son controleur d'écran (RS232/IP) depuis la config server-side.
    if active_event:
        return {
            "type": "player:sync",
            "id_playlist": active_event.id_playlist,
            "id_schedule": None,
            "id_event": active_event.id_event,
            "event_name": active_event.event_name,
            "play_once": bool(active_event.duration_ms),
            "player_name": player_name,
            "playlist_name": playlist_row.playlist_name if playlist_row else None,
            "items": items,
            "template": template_block,
            "start_index": start_index,
            "start_offset_ms": start_offset_ms,
            "display_config": display_config,
            "audio_baskets": audio_baskets,
            "local_control": local_control,
            "fallback": fallback,
            "blocked_media": blocked_media,
            "resolved_at": now.isoformat(),
        }
    return {
        "type": "player:sync",
        "id_playlist": active_sched.id_playlist,  # type: ignore[union-attr]
        "id_schedule": active_sched.id_schedule,  # type: ignore[union-attr]
        "schedule_name": active_sched.schedule_name,  # type: ignore[union-attr]
        "player_name": player_name,
        "playlist_name": playlist_row.playlist_name if playlist_row else None,
        "items": items,
        "template": template_block,
        "start_index": start_index,
        "start_offset_ms": start_offset_ms,
        "display_config": display_config,
        "audio_baskets": audio_baskets,
        "local_control": local_control,
        "fallback": fallback,
        "blocked_media": blocked_media,
        "resolved_at": now.isoformat(),
    }


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@router.websocket("/player")
async def ws_player(
    websocket: WebSocket,
    token: str = Query(...),
) -> None:
    try:
        # Token machine de player = identifiant de device permanent → on ne vérifie pas
        # l'expiration (cf. create_player_token / decode_token). La révocation reste possible
        # via rotation du token stocké au re-provision.
        payload = decode_token(token, verify_exp=False)
    except JWTError:
        await websocket.close(code=4001, reason="Invalid token")
        return

    if payload.get("type") != "player":
        await websocket.close(code=4001, reason="Invalid token type")
        return

    id_player = int(payload["sub"])

    async with WorkerSessionLocal() as db:
        player = (await db.execute(
            select(CmsPlayer).where(CmsPlayer.id_player == id_player)
        )).scalar_one_or_none()

        if player is None:
            await websocket.close(code=4001, reason="Player not found")
            return

        # Révocation / grâce (« Option B », 2026-06-26) : le WS ne vérifiait historiquement PAS
        # la correspondance avec le token stocké (trou relevé à l'audit). On l'aligne sur le
        # chemin HTTP : token courant OU ancien token encore dans la fenêtre de grâce ; tout
        # autre token (révoqué / fuité) → fermeture.
        now = datetime.now(UTC)
        identity = token_identity(
            token, player.player_token, player.player_token_prev,
            player.player_token_prev_until, now,
        )
        if identity == IDENTITY_UNKNOWN:
            await websocket.close(code=4001, reason="Token revoked")
            return

        await websocket.accept()

        # `websocket.client.host` = l'upstream immédiat (passerelle Docker / proxy), inutile
        # comme IP du player. Derrière Caddy/nginx, la vraie IP du client est en tête du
        # X-Forwarded-For = la WAN IP. La LAN IP réelle, elle, est INCONNUE du serveur (un
        # player navigateur ne peut pas la remonter — le navigateur masque l'IP locale) → on
        # NE met PAS la WAN IP dans `ip_address` (ça afficherait la même valeur dans les deux
        # champs). Un player Electron écrasera `ip_address` par sa vraie IP LAN via le heartbeat.
        peer = websocket.client.host if websocket.client else None
        fwd = websocket.headers.get("x-forwarded-for")
        xff = fwd.split(",")[0].strip() if fwd else None
        if xff:
            try:
                ipaddress.ip_address(xff)
            except ValueError:
                xff = None
        # Maintenance = statut « collant » posé manuellement (org-admin) : un player en
        # maintenance qui se (re)connecte ne doit PAS repasser online tout seul — sinon le
        # mode manuel ne tiendrait jamais. On rafraîchit last_seen/IP mais on préserve le statut.
        conn_status = "maintenance" if player.status == "maintenance" else "online"
        if xff:
            values = dict(status=conn_status, last_seen_at=datetime.now(UTC),
                          wan_ip=xff, ip_address=None)
        else:
            values = dict(status=conn_status, last_seen_at=datetime.now(UTC), ip_address=peer)

        # ── Rotation du token (« Option B ») ─────────────────────────────────────
        # Grâce-à-la-reconnexion : un player qui revient avec un token expiré/legacy est ré-accepté
        # (identité OK ci-dessus) puis son token est renouvelé en silence — JAMAIS de saisie
        # manuelle pour cause d'expiration. Si le player présente l'ancien token (prev) alors qu'un
        # token courant existe (push raté), on lui repousse simplement le token courant.
        refreshed_token: str | None = None
        if identity == IDENTITY_PREV:
            refreshed_token = player.player_token
        elif needs_rotation(player.token_expires_at, now):
            new_token, new_exp = create_player_token(id_player)
            values.update(
                player_token=new_token,
                token_expires_at=new_exp,
                player_token_prev=player.player_token,
                player_token_prev_until=grace_until(now),
            )
            refreshed_token = new_token

        await db.execute(
            update(CmsPlayer).where(CmsPlayer.id_player == id_player).values(**values)
        )
        await db.commit()

        if refreshed_token:
            try:
                await websocket.send_json({"type": "token:refresh", "token": refreshed_token})
            except Exception as exc:
                print(f"[WS] push token:refresh échoué pour player {id_player}: {exc}")

        try:
            sync_payload = await _resolve_sync_payload(db, id_player)
            await websocket.send_json(sync_payload)
        except Exception as exc:
            print(f"[WS] sync initial échoué pour player {id_player}: {exc}")

    redis_client = aioredis.from_url(settings.REDIS_URL)
    pubsub = redis_client.pubsub()
    channel = f"signflow:player:{id_player}:cmds"
    await pubsub.subscribe(channel)

    # Ré-assertion auto-power à la (re)connexion : pendant que le player était
    # injoignable (coupure électrique/réseau), l'écran a pu changer d'état tout seul
    # (auto-standby « no signal » d'une dalle rs232/ip/cec, pilote offline du player).
    # Reset last_power_state → le prochain tick apply_site_power (≤60 s) ré-émet l'état
    # voulu quelle que soit la dernière valeur mémorisée (commande redondante inoffensive).
    # ⚠️ Fait APRÈS pubsub.subscribe : reset avant = fenêtre où le worker voit le player
    # online + état NULL et publie la baseline dans un canal PAS ENCORE écouté → perdue,
    # et l'état étant marqué à jour elle ne serait jamais ré-émise.
    async with WorkerSessionLocal() as db:
        await db.execute(
            update(CmsPlayer).where(CmsPlayer.id_player == id_player)
            .values(last_power_state=None)
        )
        await db.commit()

    async def _recv_loop() -> None:
        async with WorkerSessionLocal() as db:
            async for msg in websocket.iter_json():
                if msg.get("type") == "display:status":
                    # Statut retourné par le player après exécution d'une commande écran
                    # Phase 2 : log uniquement. Phase 3 : stocker dans cms_display_log.
                    print(
                        f"[WS] display:status player={id_player} "
                        f"cmd={msg.get('command')} result={msg.get('result')}"
                    )
                    continue
                if msg.get("type") == "player:status":
                    # Statut : préserve un « maintenance » posé manuellement (org-admin) — le
                    # heartbeat rafraîchit last_seen/metrics mais ne réveille pas un player
                    # volontairement mis en maintenance (garde-fou côté DB, sticky).
                    update_vals: dict = dict(
                        last_seen_at=datetime.now(UTC),
                        status=case(
                            (CmsPlayer.status == "maintenance", "maintenance"),
                            else_=msg.get("status", "online"),
                        ),
                        cpu_usage=msg.get("cpu"),
                        ram_usage=msg.get("ram"),
                        disk_usage=msg.get("disk"),
                        current_playlist_id=msg.get("current_content"),
                        player_version=msg.get("player_version"),
                    )
                    # Prefer the player-reported LAN IP over the Docker gateway IP
                    if msg.get("ip_address"):
                        update_vals["ip_address"] = msg["ip_address"]
                    if msg.get("mac_address"):
                        update_vals["mac_address"] = msg["mac_address"]
                    if msg.get("wan_ip"):
                        update_vals["wan_ip"] = msg["wan_ip"]
                    # Capacités matérielles (décodage HW) — le player les remonte
                    # depuis capabilities.json (détection install). On ne met à jour
                    # que si présent et que c'est bien un dict (le player peut ne pas
                    # l'envoyer à chaque heartbeat).
                    caps = msg.get("capabilities")
                    if isinstance(caps, dict) and caps:
                        update_vals["capabilities"] = caps
                    # UPDATE protégé : une erreur ici (type/contrainte) ne doit PAS tuer la boucle
                    # de réception → sinon la connexion WS est démontée (close 1006) à CHAQUE
                    # heartbeat (cas BrightSign QtWebEngine constaté 2026-06-27). On loggue + on
                    # poursuit (le heartbeat suivant retentera) ; le lien reste vivant.
                    try:
                        await db.execute(
                            update(CmsPlayer)
                            .where(CmsPlayer.id_player == id_player)
                            .values(**update_vals)
                        )
                        await db.commit()
                    except Exception as exc:
                        await db.rollback()
                        import traceback
                        print(f"[WS] player:status UPDATE échec player {id_player}: {exc!r}")
                        traceback.print_exc()
                    # OTA §XXX slice 2 : succès observable — si la version remontée atteint la cible
                    # d'un déploiement en cours, la cible passe `success` + rollup. Best-effort,
                    # n'affecte jamais le heartbeat (le lien reste vivant en cas d'erreur).
                    try:
                        changed = await ota.mark_success_if_updated(
                            db, id_player, msg.get("player_version"))
                        if changed:
                            await db.commit()
                            from app.workers.ota_worker import ota_rollup
                            for dep_id in changed:
                                ota_rollup.delay(dep_id)
                    except Exception as exc:
                        await db.rollback()
                        print(f"[WS] OTA success-detect échec player {id_player}: {exc!r}")
                    continue
                if msg.get("type") == "player:event":
                    await _audit_player_event(db, id_player, msg)
                    continue
                if msg.get("type") == "update:status":
                    # OTA §XXX slice 2 : le player reporte l'avancement de son apply. On met à jour
                    # SA cible (report_target filtre WHERE id_player = self) puis rollup async.
                    try:
                        id_deployment = int(msg.get("id_deployment"))
                        st = str(msg.get("status") or "")
                    except (TypeError, ValueError):
                        continue
                    if st not in ("downloading", "staged", "installing", "success", "failed"):
                        continue
                    try:
                        changed = await ota.report_target(
                            db, id_player, id_deployment, st, msg.get("error"))
                        if changed:
                            await db.commit()
                            from app.workers.ota_worker import ota_rollup
                            ota_rollup.delay(id_deployment)
                    except Exception as exc:
                        await db.rollback()
                        print(f"[WS] update:status échec player {id_player}: {exc!r}")
                    continue
                if msg.get("type") == "local_control:action":
                    # Action de l'opérateur magasin (Store Control Panel §19.3) :
                    # persiste l'état côté serveur + audit, puis renvoie l'état autoritatif.
                    try:
                        await _apply_local_control_action(db, id_player, msg)
                    except Exception as exc:
                        await db.rollback()
                        print(f"[WS] local_control:action échec player {id_player}: {exc}")
                    try:
                        sync_payload = await _resolve_sync_payload(db, id_player)
                        await websocket.send_json(sync_payload)
                    except Exception as exc:
                        print(f"[WS] local_control resync échec player {id_player}: {exc}")
                    continue

    async def _pubsub_loop() -> None:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                raw = msg["data"]
                # redis.asyncio (sans decode_responses) renvoie des bytes. send_text()
                # avec des bytes pousse une TRAME BINAIRE côté uvicorn → certains clients
                # (Android OkHttp n'override que onMessage(text)) l'ignorent en silence.
                # On décode toujours en str pour garantir une trame TEXTE.
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    await websocket.send_text(raw)
                    continue
                if isinstance(data, dict) and data.get("type") == "player:force_sync":
                    async with WorkerSessionLocal() as db2:
                        try:
                            sync_payload = await _resolve_sync_payload(db2, id_player)
                            sync_payload["preemptive"] = data.get("preemptive", False)
                            await websocket.send_json(sync_payload)
                        except Exception as exc:
                            print(f"[WS] force_sync échoué player {id_player}: {exc}")
                else:
                    # send_json garantit une trame texte (data est déjà le dict parsé).
                    await websocket.send_json(data)

    recv_task = asyncio.create_task(_recv_loop())
    pubsub_task = asyncio.create_task(_pubsub_loop())

    try:
        await asyncio.wait([recv_task, pubsub_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        recv_task.cancel()
        pubsub_task.cancel()
        await asyncio.gather(recv_task, pubsub_task, return_exceptions=True)
        await pubsub.unsubscribe(channel)
        await redis_client.aclose()
        async with WorkerSessionLocal() as db:
            await db.execute(
                update(CmsPlayer)
                .where(CmsPlayer.id_player == id_player)
                .values(status="offline", last_seen_at=datetime.now(UTC))
            )
            await db.commit()
