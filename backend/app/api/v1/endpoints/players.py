"""
Endpoints Players (§III — Player Registration & Management).

POST   /players               — créer player (org_admin)
GET    /players               — liste players de l'org
GET    /players/{id}/current-state — état résolu (event/schedule/playlist actifs)
GET    /players/{id}          — détail
PUT    /players/{id}          — mise à jour
DELETE /players/{id}          — suppression
POST   /players/{id}/token    — générer / régénérer token 30j
POST   /players/{id}/command  — envoyer commande via Redis pub/sub (restart, screenshot)
POST   /players/{id}/screenshot — upload capture d'écran (auth player)
GET    /players/{id}/screenshot — URL signée de la dernière capture (org_admin)
"""

import json
from datetime import datetime, timezone
from functools import partial
from typing import Annotated

import anyio
import io
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import (
    get_current_player,
    get_db,
    get_db_worker,
    require_admin_or_permission,
    require_org_admin,
    require_permission,
)
from app.core.security import create_player_token
from app.models.audit_log import CmsAuditLog
from app.models.event import CmsEvent
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroupMember
from app.models.player_version import CmsUpdateDeploymentTarget
from app.models.pop import CmsProofOfPlay
from app.models.vod import CmsVodConfigPlayer
from app.models.tag import CmsPlayerTagFilter
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget
from app.models.user import CmsUser
from app.schemas.player import (
    StorageCheckRequest,
    StorageCheckTarget,
    StorageCheckResult,
    DisplayCommandRequest,
    PlayerBulkCommandRequest,
    PlayerBulkCommandResult,
    PlayerCommandRequest,
    PlayerCreate,
    PlayerMaintenanceRequest,
    PlayerDecodeHealthRead,
    PlayerRead,
    PlayerTokenResponse,
    PlayerUpdate,
)
from app.schemas.player_group import PlayerStatsRead
from app.services.media import download_file, get_signed_url, upload_file
from app.services.player_compat import lan_sync_eligibility
from app.services.player_capabilities import decode_health
from app.services.pin_guard import bulk_command_requires_pin, verify_pin_or_raise
from app.services.targeting import (
    Target,
    event_target_config_without,
    event_target_matches,
    player_site_group_ids,
    players_for_targets,
)

router = APIRouter(prefix="/players", tags=["players"])



def _collect_cacheable(node, out: dict) -> None:
    """Walk récursif d'un payload de sync → collecte {cache_key: url} de tout média cacheable.

    Ramasse les paires (cache_key, url) ET (image_cache_key, image_url) (vignettes de feed),
    où qu'elles soient dans la structure (items, zones, prefetch, vod). Dédupe par cache_key.
    """
    if isinstance(node, dict):
        ck, url = node.get("cache_key"), node.get("url")
        if ck and url:
            out[ck] = url
        ick, iurl = node.get("image_cache_key"), node.get("image_url")
        if ick and iurl:
            out[ick] = iurl
        for v in node.values():
            _collect_cacheable(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect_cacheable(v, out)


@router.get("/cache-manifest")
async def get_cache_manifest(
    current_player: Annotated[CmsPlayer, Depends(get_current_player)],
    # Auth = token PLAYER → get_db_worker (BYPASSRLS), comme l'upload screenshot.
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> dict:
    """Manifeste des médias à mettre en cache localement (download manager BrightSign natif).

    Réutilise EXACTEMENT le payload de sync du player (mêmes variantes/cache_keys/URLs signées
    que ce que le renderer recevrait) et en extrait tout média cacheable. Le download manager
    (autorun.brs) télécharge chaque item sur SD via roUrlTransfer puis le sert en HTTP local ;
    le web player joue alors `http://127.0.0.1:<port>/media/<cache_key>` au lieu du flux MinIO.
    URLs MinIO signées (TTL 24h) → re-poller pour rafraîchir avant expiration.
    """
    from app.api.v1.endpoints.ws import _resolve_sync_payload  # import local (évite le cycle)

    payload = await _resolve_sync_payload(db, current_player.id_player)
    collected: dict[str, str] = {}
    _collect_cacheable(payload, collected)
    items = [{"cache_key": k, "url": u} for k, u in collected.items()]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }


async def _get_player_or_404(id_player: int, db: AsyncSession) -> CmsPlayer:
    player = (
        await db.execute(select(CmsPlayer).where(CmsPlayer.id_player == id_player))
    ).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return player


async def _enforce_user_scope(db: AsyncSession, current_user: CmsUser, id_player: int) -> None:
    """N8/G1 (audit 2026-07-27) : un admin de nœud de gouvernance ne voit que les écrans
    de ses sous-arbres. Hors périmètre = **404**, comme un cross-tenant — l'écran n'existe
    pas pour lui. Les rôles classiques (aucun nœud administré) et l'org-admin ne sont pas
    filtrés (`user_player_scope` renvoie None)."""
    from app.services.governance_scope import user_player_scope

    scope = await user_player_scope(db, current_user)
    if scope is not None and id_player not in scope:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")


async def _open_hours_per_day(db: AsyncSession, id_site: int | None) -> float:
    """Heures d'ouverture MOYENNES par jour du site (0 si fermé ce jour-là).

    Sert à traduire un taux « par cycle » en « par jour », qui est l'unité avec laquelle un
    responsable de magasin raisonne. Aucun horaire déclaré → 24 h (l'écran tourne en continu),
    et on le DIT à l'écran plutôt que de laisser croire à une mesure.
    """
    if id_site is None:
        return 24.0
    from app.models.organization import CmsSiteHours

    rows = (await db.execute(
        select(CmsSiteHours).where(CmsSiteHours.id_site == id_site)
    )).scalars().all()
    if not rows:
        return 24.0
    total = 0.0
    for r in rows:
        if r.is_closed or r.open_time is None or r.close_time is None:
            continue
        start = r.open_time.hour + r.open_time.minute / 60
        end = r.close_time.hour + r.close_time.minute / 60
        total += (end - start) if end > start else (24 - start + end)  # fenêtre nocturne
    return round(total / 7, 2) if total else 24.0


def _content_rates(program, open_hours_per_day: float) -> list[dict]:
    """Taux de passage par contenu sur le cycle compilé.

    « L'admin du nœud fait sa configuration et on lui montre le taux de passage des différents
    contenus » (Ed, 2026-07-26). Tout est déterministe : le cycle est connu, il suffit de
    compter. L'intervalle moyen est l'information la plus parlante — « toutes les 11 minutes »
    se comprend immédiatement, « 12 % d'antenne » beaucoup moins.
    """
    cycle_ms = program.cycle_duration_ms
    if not cycle_ms or not program.items:
        return []
    node_names = {l.id_node: l.node_name for l in program.layers}
    agg: dict[int, dict] = {}
    for it in program.items:
        e = agg.setdefault(it.id_media, {
            "id_media": it.id_media, "media_name": it.media_name, "id_node": it.id_node,
            "node_name": node_names.get(it.id_node, ""), "plays_per_cycle": 0, "airtime_ms": 0,
        })
        e["plays_per_cycle"] += 1
        e["airtime_ms"] += it.duration_ms
    cycles_per_day = (open_hours_per_day * 3600_000) / cycle_ms
    out = []
    for e in agg.values():
        plays = e["plays_per_cycle"]
        out.append({
            "id_media": e["id_media"],
            "media_name": e["media_name"],
            "id_node": e["id_node"],
            "node_name": e["node_name"],
            "plays_per_cycle": plays,
            "interval_ms": int(cycle_ms / plays),
            "plays_per_day": round(plays * cycles_per_day, 1),
            "airtime_pct": round(100 * e["airtime_ms"] / cycle_ms, 1),
        })
    # Le plus rare en tête : c'est celui sur lequel l'admin doit se poser une question.
    out.sort(key=lambda c: (c["plays_per_cycle"], c["media_name"]))
    return out


@router.get("/{id_player}/compiled-schedule")
async def get_compiled_schedule(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    at: str | None = None,
) -> dict:
    """Programme **compilé** toutes couches de gouvernance pour ce player (§18.4, lecture seule).

    Empile les schedules gouvernés des nœuds dont le player dépend (national→local), applique
    budgets/préemption/position via le moteur déterministe `governance_engine`. N'affecte PAS le
    sync live (`_resolve_sync_payload` est inchangé). Player hors gouvernance → couches vides.
    """
    from app.api.v1.endpoints.governance import _parse_at, diagnose_no_layers, gather_layers
    from app.services.governance_engine import compile_program
    from app.services.player_timezone import player_tz_name

    player = await _get_player_or_404(id_player, db)
    if player.id_org != current_user.id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    await _enforce_user_scope(db, current_user, id_player)

    # L'aperçu compilé doit montrer ce que CET écran diffuse à CET instant (P2.2) — donc
    # son heure locale. `at`, lui, est déjà exprimé dans l'heure locale de l'écran.
    now = _parse_at(at, tz_name=await player_tz_name(db, id_player))
    layers = await gather_layers(db, current_user.id_org, id_player, now)
    program = compile_program(layers, now)
    open_hours = await _open_hours_per_day(db, player.id_site)
    out = {
        "id_player": id_player,
        "resolved_at": now.isoformat(),
        "cycle_duration_ms": program.cycle_duration_ms,
        "turns": program.turns,
        "truncated": program.truncated,
        "open_hours_per_day": open_hours,
        "layers": [vars(r) for r in program.layers],
        "items": [vars(it) for it in program.items],
        "contents": _content_rates(program, open_hours),
    }
    # R10 : aucune couche → dire LAQUELLE des cinq causes s'applique. `compile_program`
    # rapporte toute couche d'entrée (même écartée), donc une sortie vide signifie
    # toujours que la collecte amont n'a rien trouvé.
    if not layers:
        diag = await diagnose_no_layers(db, current_user.id_org, id_player, now)
        out["empty_reason"] = diag["reason"]
        out["empty_detail"] = diag["detail"]
    return out


@router.get("/{id_player}/readiness")
async def get_player_readiness(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Prévision de sync-readiness (look-ahead Phase 3) : date estimée à laquelle le player
    aura téléchargé chaque média programmé à venir (selon débit + fenêtres), et si elle
    arrive avant la diffusion. Cross-tenant = 404."""
    from app.services.readiness import player_readiness

    player = await _get_player_or_404(id_player, db)
    if player.id_org != current_user.id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    await _enforce_user_scope(db, current_user, id_player)
    return await player_readiness(db, id_player)


@router.get("/{id_player}/inventory")
async def get_player_inventory(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Inventaire média du player : programme planifié (vidéo/image — toutes playlists/events/
    gouvernance le ciblant) croisé avec le cache réel remonté au heartbeat
    (`capabilities.store.cached_ids/downloading_ids`) → statut cached / downloading / missing.
    """
    from app.services.player_inventory import expected_media

    player = await _get_player_or_404(id_player, db)
    if player.id_org != current_user.id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    await _enforce_user_scope(db, current_user, id_player)

    store = (player.capabilities or {}).get("store") or {}
    cached = set(store.get("cached_ids") or [])
    downloading = set(store.get("downloading_ids") or [])

    expected = await expected_media(db, id_player)
    n_cached = n_dl = n_missing = 0
    items = []
    for m in expected:
        mid = m["id_media"]
        if mid in cached:
            st = "cached"; n_cached += 1
        elif mid in downloading:
            st = "downloading"; n_dl += 1
        else:
            st = "missing"; n_missing += 1
        items.append({**m, "status": st})
    order = {"missing": 0, "downloading": 1, "cached": 2}  # le plus actionnable en haut
    items.sort(key=lambda x: (order[x["status"]], (x["media_name"] or "").lower()))
    return {
        "id_player": id_player,
        "summary": {
            "expected": len(expected),
            "cached": n_cached,
            "downloading": n_dl,
            "missing": n_missing,
            "disk_used_ratio": store.get("disk_used_ratio"),
            "storage_pressure": bool(store.get("storage_pressure", False)),
            "reported": bool(store),  # le player a-t-il déjà remonté son cache ?
        },
        "items": items,
    }


@router.get("/stats", response_model=PlayerStatsRead)
async def get_player_stats(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerStatsRead:
    result = await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == current_user.id_org)
    )
    players = list(result.scalars().all())
    return PlayerStatsRead(
        total=len(players),
        online=sum(1 for p in players if p.status == "online"),
        offline=sum(1 for p in players if p.status == "offline"),
        warning=sum(1 for p in players if p.status in ("warning", "critical")),
    )


@router.get("/decode-report", response_model=list[PlayerDecodeHealthRead])
async def get_decode_report(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlayerDecodeHealthRead]:
    """Matrice de conformité décodage HW : par player, software vs hardware + alertes.

    Route fixe déclarée avant `/{id_player}` (sinon capturée comme id paramétré).
    """
    result = await db.execute(
        select(CmsPlayer)
        .where(CmsPlayer.id_org == current_user.id_org)
        .order_by(CmsPlayer.player_name)
    )
    players = list(result.scalars().all())
    return [
        PlayerDecodeHealthRead(
            id_player=p.id_player,
            player_name=p.player_name,
            player_os=p.player_os,
            status=p.status,
            **decode_health(p),
        )
        for p in players
    ]


@router.post("/", response_model=PlayerRead, status_code=status.HTTP_201_CREATED)
async def create_player(
    body: PlayerCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayer:
    player = CmsPlayer(
        id_org=current_user.id_org,
        id_site=body.id_site,
        player_name=body.player_name,
        player_os=body.player_os,
    )
    db.add(player)
    await db.commit()
    await db.refresh(player)
    return player


def _lan_group_label(p: CmsPlayer) -> str | None:
    """Libellé du groupe LAN Sync d'un player (None s'il n'est pas en groupe)."""
    cfg = p.display_config or {}
    if not cfg.get("lan_sync_enabled"):
        return None
    label = (cfg.get("lan_sync_group") or "").strip()
    return label or None


def _peer_dicts(members: list[CmsPlayer]) -> list[dict]:
    """Vue {display_config, player_os, capabilities} attendue par player_compat."""
    return [
        {"display_config": m.display_config, "player_os": m.player_os, "capabilities": m.capabilities}
        for m in members
    ]


def _eligibility_for(p: CmsPlayer, org_players: list[CmsPlayer]) -> dict:
    """{level, reason} de p compte tenu des AUTRES membres de son groupe LAN (homogénéité)."""
    label = _lan_group_label(p)
    peers = (
        _peer_dicts([m for m in org_players if m.id_player != p.id_player and _lan_group_label(m) == label])
        if label
        else []
    )
    return lan_sync_eligibility(p.display_config, p.player_os, p.capabilities, peers=peers)


def _with_screenshot(
    p: CmsPlayer,
    tag_filters: list[int] | None = None,
    eligibility: dict | None = None,
) -> PlayerRead:
    """Sérialise un player + URL signée de la dernière capture + filtres de tags (id_tag)."""
    out = PlayerRead.model_validate(p)
    if p.last_screenshot_at:
        out.screenshot_url = get_signed_url(f"screenshots/{p.id_player}/latest.png", ttl=3600)
    out.tag_filters = tag_filters or []
    out.lan_sync_eligibility = eligibility
    return out


@router.get("/", response_model=list[PlayerRead])
async def list_players(
    # Lecture seule : honore `players:read` comme le fait déjà sites.py — sans quoi
    # le menu « Players » (gaté players:read côté sidebar) menait droit à un 403
    # pour un rôle métier (ex. Planificateur), alors que « Sites » fonctionnait.
    # `require_admin_or_permission` plutôt que `require_permission` nu : l'org-admin
    # passe toujours, même s'il ne porte pas la permission seedée (cf. deps.py).
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlayerRead]:
    from app.services.governance_scope import user_player_scope

    stmt = (
        select(CmsPlayer)
        .where(CmsPlayer.id_org == current_user.id_org)
        .order_by(CmsPlayer.created_at.desc())
    )
    # N8/G1 : un admin de nœud ne liste que les écrans de ses sous-arbres.
    scope = await user_player_scope(db, current_user)
    if scope is not None:
        stmt = stmt.where(CmsPlayer.id_player.in_(scope or {-1}))
    players = list((await db.execute(stmt)).scalars().all())
    # Batch-load des filtres de tags par player (évite le N+1).
    tag_map: dict[int, list[int]] = {}
    if players:
        for pid, id_tag in (await db.execute(
            select(CmsPlayerTagFilter.id_player, CmsPlayerTagFilter.id_tag)
            .where(CmsPlayerTagFilter.id_player.in_([p.id_player for p in players]))
        )).all():
            tag_map.setdefault(pid, []).append(id_tag)
    return [
        _with_screenshot(p, tag_map.get(p.id_player, []), _eligibility_for(p, players))
        for p in players
    ]


@router.get("/{id_player}", response_model=PlayerRead)
async def get_player(
    id_player: int,
    # Lecture seule, alignée sur list_players et sites.py (players:read). Les
    # mutations (PUT/DELETE/commande/token/maintenance) restent org-admin.
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerRead:
    player = await _get_player_or_404(id_player, db)
    await _enforce_user_scope(db, current_user, id_player)
    tag_filters = [
        row[0] for row in (await db.execute(
            select(CmsPlayerTagFilter.id_tag).where(CmsPlayerTagFilter.id_player == id_player)
        )).all()
    ]
    org_players = list((await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == player.id_org)
    )).scalars().all())
    return _with_screenshot(player, tag_filters, _eligibility_for(player, org_players))


@router.put("/{id_player}", response_model=PlayerRead)
async def update_player(
    id_player: int,
    body: PlayerUpdate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerRead:
    player = await _get_player_or_404(id_player, db)
    if body.player_name is not None:
        player.player_name = body.player_name
    if body.player_os is not None:
        player.player_os = body.player_os
    site_changed = False
    if body.id_site is not None:
        site_changed = body.id_site != player.id_site
        player.id_site = body.id_site
    # Rôle audio/contenu (§XIX) — change la sortie audio et le mute contenu côté player
    role_changed = False
    if body.player_role is not None and body.player_role != player.player_role:
        player.player_role = body.player_role
        role_changed = True
    # Retail Media : exclusion des campagnes (écran caisse…) — force_sync pour que les
    # créatifs sortent/entrent de la boucle immédiatement.
    optout_changed = False
    if (body.campaigns_optout is not None
            and body.campaigns_optout != player.campaigns_optout):
        player.campaigns_optout = body.campaigns_optout
        optout_changed = True
    # Inventaire v2 (migr 0082) : plafond + tarif SOV — null EXPLICITE = revenir à
    # l'héritage site/org (model_fields_set), sans effet diffusion (pas de force_sync).
    if "campaign_max_occupancy_pct" in body.model_fields_set:
        player.campaign_max_occupancy_pct = body.campaign_max_occupancy_pct
    if "campaign_sov_daily_rate" in body.model_fields_set:
        player.campaign_sov_daily_rate = body.campaign_sov_daily_rate
    # display_config : accepte null (effacer) ou dict (mettre à jour)
    # On utilise model_fields_set pour distinguer "non fourni" de "explicitement null"
    display_config_changed = False
    if "display_config" in body.model_fields_set:
        new_cfg = body.display_config
        # Règle d'HOMOGÉNÉITÉ (cf. player_compat) : un groupe LAN Sync contenant un renderer
        # HW natif (mpv/ExoPlayer) doit être homogène (mêmes OS, renderer, matériel). Un groupe
        # hétérogène avec mpv/exo flip-flop → on REFUSE (409), au lieu de l'ancien « mpv ⟺ sync
        # mutuellement exclusifs ». force_chromium = dégrade ce player en Chromium pour réduire
        # l'hétérogénéité et tenter l'intégration.
        if isinstance(new_cfg, dict):
            new_cfg = dict(new_cfg)
            new_lan = bool(new_cfg.get("lan_sync_enabled"))
            new_group = (new_cfg.get("lan_sync_group") or "").strip() if new_lan else ""
            if new_lan and new_group:
                others = list((await db.execute(
                    select(CmsPlayer).where(
                        CmsPlayer.id_org == player.id_org,
                        CmsPlayer.id_player != player.id_player,
                    )
                )).scalars().all())
                peers = _peer_dicts([m for m in others if _lan_group_label(m) == new_group])
                elig = lan_sync_eligibility(new_cfg, player.player_os, player.capabilities, peers=peers)
                if elig["level"] == "block":
                    renderer = str(new_cfg.get("video_renderer") or "auto").lower()
                    if body.force_chromium and renderer in ("mpv", "exo"):
                        new_cfg["video_renderer"] = "auto"
                        elig = lan_sync_eligibility(
                            new_cfg, player.player_os, player.capabilities, peers=peers
                        )
                    if elig["level"] == "block":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "code": "LAN_SYNC_HETEROGENEOUS",
                                "message": (
                                    "A LAN Sync group with hardware decoding (mpv/ExoPlayer) must "
                                    "be homogeneous: same OS, renderer and hardware for all players."
                                ),
                            },
                        )
        player.display_config = new_cfg
        display_config_changed = True
    # Renommer un écran ou changer sa configuration d'affichage (résolution, rotation,
    # sync LAN, opt-out campagnes) change ce qu'on voit à l'antenne : on trace QUELS
    # champs ont bougé, pas leur contenu (un display_config complet noierait le journal).
    db.add(_player_audit(current_user, "update", player,
                         {"changes": sorted(body.model_fields_set)}))
    await db.commit()
    await db.refresh(player)
    if display_config_changed or role_changed or optout_changed:
        r = aioredis.from_url(settings.REDIS_URL)
        try:
            await r.publish(
                f"signflow:player:{id_player}:cmds",
                json.dumps({"type": "player:force_sync"}),
            )
        finally:
            await r.aclose()
    # Changement de site → alignement auto du fuseau OS sur celui du nouveau site
    # (best-effort, one-shot — cf. services/player_timezone).
    if site_changed:
        from app.services.player_timezone import align_player_timezone
        await align_player_timezone(db, player)
    tag_filters = [
        row[0] for row in (await db.execute(
            select(CmsPlayerTagFilter.id_tag).where(CmsPlayerTagFilter.id_player == id_player)
        )).all()
    ]
    org_players = list((await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == player.id_org)
    )).scalars().all())
    return _with_screenshot(player, tag_filters, _eligibility_for(player, org_players))


@router.delete("/{id_player}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_player(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    delete_pop: bool = Query(False),
) -> None:
    """Supprime le player. `delete_pop=true` supprime AUSSI ses preuves de diffusion (PoP) —
    sinon elles restent (cms_proof_of_play n'a pas de FK vers cms_players : orphelines mais
    conservées pour la conformité)."""
    player = await _get_player_or_404(id_player, db)
    if delete_pop:
        await db.execute(
            delete(CmsProofOfPlay).where(
                CmsProofOfPlay.id_player == id_player,
                CmsProofOfPlay.id_org == current_user.id_org,
            )
        )
    # Références SANS ON DELETE CASCADE côté DB → à retirer explicitement, sinon FK violation
    # (typiquement un vieux player : cibles de déploiement OTA + liens VOD).
    await db.execute(
        delete(CmsUpdateDeploymentTarget).where(CmsUpdateDeploymentTarget.id_player == id_player)
    )
    await db.execute(
        delete(CmsVodConfigPlayer).where(CmsVodConfigPlayer.id_player == id_player)
    )
    # Cibles GÉNÉRIQUES sans FK (target_type/target_id) : sans nettoyage, un player supprimé
    # reste ciblé à vie (constaté prod 2026-07-07, remonté par Ed : 8 cibles de schedules +
    # 1 event pointant des players disparus).
    await db.execute(
        delete(CmsScheduleTarget).where(
            CmsScheduleTarget.target_type == "player",
            CmsScheduleTarget.target_id == id_player,
        )
    )
    # Events : target_config JSONB → retirer l'id (les deux formes, hétérogène comprise).
    ghost_events = (await db.execute(
        select(CmsEvent).where(CmsEvent.id_org == current_user.id_org)
    )).scalars().all()
    for ev in ghost_events:
        cleaned = event_target_config_without(ev.target_config, "player", id_player)
        if cleaned is not None:
            ev.target_config = cleaned
    # Tracé AVANT la suppression : la ligne disparaît, le journal garde le nom de l'écran
    # et son site — « quel écran a été retiré du parc, par qui ? » doit avoir une réponse.
    db.add(_player_audit(current_user, "delete", player))
    await db.delete(player)
    await db.commit()


def _player_audit(
    user: CmsUser, action: str, player: CmsPlayer, detail: dict | None = None
) -> CmsAuditLog:
    """Trace d'un geste sur un écran (audit menu ⑪, 2026-07-31).

    🔑 C'est l'ASYMÉTRIE qui a mis sur la piste : la commande EN MASSE était journalisée —
    « un clic distrait coûte cher », dit son propre commentaire — mais pas la commande
    UNITAIRE, alors que redémarrer un écran de vitrine en pleine journée coûte le même prix.
    Même trou pour le renommage/la configuration d'affichage, la SUPPRESSION et la ROTATION
    DE JETON : trois gestes dont « qui, et quand ? » est la première question posée après coup.

    ⛔ Le nom de l'écran est conservé dans le détail (après suppression, la ligne disparaît
    mais le journal dit encore lequel) ; le JETON, lui, n'y entre jamais.
    """
    base = {"player_name": player.player_name, "id_site": player.id_site}
    base.update(detail or {})
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="player", resource_id=str(player.id_player), source="cms",
        detail=base,
    )


@router.post("/{id_player}/token", response_model=PlayerTokenResponse)
async def generate_player_token(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerTokenResponse:
    player = await _get_player_or_404(id_player, db)
    token, expires_at = create_player_token(id_player)
    player.player_token = token
    player.token_expires_at = expires_at
    # Régénération manuelle = l'ancien token est révoqué immédiatement (pas de fenêtre de grâce :
    # l'admin remet lui-même le nouveau token sur le device). On purge donc tout prev résiduel.
    player.player_token_prev = None
    player.player_token_prev_until = None
    db.add(_player_audit(current_user, "token_rotate", player,
                         {"expires_at": expires_at.isoformat() if expires_at else None}))
    await db.commit()
    return PlayerTokenResponse(player_token=token, expires_at=expires_at)


@router.post("/{id_player}/command", status_code=status.HTTP_202_ACCEPTED)
async def send_player_command(
    id_player: int,
    body: PlayerCommandRequest,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Publie une commande dans le canal Redis du player (fan-out WS)."""
    player = await _get_player_or_404(id_player, db)
    db.add(_player_audit(current_user, "command", player,
                         {"command": body.command, "params": body.params or {}}))
    await db.commit()
    redis = aioredis.from_url(settings.REDIS_URL)
    try:
        await redis.publish(
            f"signflow:player:{id_player}:cmds",
            json.dumps({
                "type": "player:command",
                "command": body.command,
                "params": body.params or {},
            }),
        )
    finally:
        await redis.aclose()
    return {"status": "sent"}


@router.post("/storage-check", response_model=StorageCheckResult)
async def storage_check(
    body: StorageCheckRequest,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Ce contenu tient-il sur ces écrans ? — à appeler AVANT d'affecter.

    Répond `ok` / `tight` / `insufficient` / `unknown` par écran. Le 2026-08-06, une
    playlist de 1,85 Go affectée à un stick de 4 Go a rempli son disque à 100 % et cassé
    Android : le frein embarqué empêche désormais la casse, mais seul ce contrôle prévient
    l'exploitant **au moment du geste**, quand corriger ne coûte rien.

    Lecture seule, sans effet de bord. Scopée à l'organisation ET au périmètre de
    gouvernance de l'appelant : un contrôle de capacité ne doit pas devenir un moyen
    d'apprendre qu'un écran existe ailleurs.
    """
    from app.services import storage_fit, targeting
    from app.services.governance_scope import user_player_scope

    ids = {int(p) for p in (body.id_players or [])}
    # Cibles canoniques résolues SERVEUR (même service que la diffusion) : le tiroir
    # d'affectation manipule « ce site », « ce groupe », pas des identifiants d'écran.
    if body.targets:
        ids |= await targeting.players_for_targets(
            db, current_user.id_org,
            [targeting.Target(target_type=t.target_type, target_id=t.target_id,
                              tag_expr=t.tag_expr or {}) for t in body.targets],
        )

    # Périmètre de gouvernance : un contrôle de capacité ne doit pas devenir un moyen
    # d'apprendre qu'un écran existe ailleurs.
    scope = await user_player_scope(db, current_user)
    if scope is not None:
        ids = {p for p in ids if p in scope}

    medias = {int(m) for m in (body.id_medias or [])}
    medias.update(await storage_fit.playlist_media_ids(db, body.id_playlist))

    return await storage_fit.assess(db, current_user.id_org, sorted(ids), sorted(medias))


@router.post("/commands", response_model=PlayerBulkCommandResult,
             status_code=status.HTTP_202_ACCEPTED)
async def send_bulk_player_command(
    body: PlayerBulkCommandRequest,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerBulkCommandResult:
    """Envoie une commande à PLUSIEURS players désignés par cibles canoniques.

    Cible = org | site | site_group | group | player | tags, résolue par
    `targeting.players_for_targets` (le même tuyau que campagnes/schedules/OTA — pas de
    résolveur maison, cf. l'audit du 2026-07-18).

    Les commandes DESTRUCTIVES exigent le PIN (`services/pin_guard.py`) : à l'échelle d'un
    parc, un clic distrait coûte cher. Toute exécution est journalisée dans l'audit avec la
    liste des players touchés — une action de masse doit rester explicable après coup.
    """
    if bulk_command_requires_pin(body.command):
        await verify_pin_or_raise(db, current_user, body.pin)

    targets = [
        Target(target_type=t.target_type, target_id=t.target_id, tag_expr=t.tag_expr)
        for t in body.targets
    ]
    player_ids = await players_for_targets(db, current_user.id_org, targets)
    if not player_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No player matches this target.",
        )

    redis = aioredis.from_url(settings.REDIS_URL)
    dispatched: list[int] = []
    try:
        msg = json.dumps({
            "type": "player:command",
            "command": body.command,
            "params": body.params or {},
        })
        for pid in sorted(player_ids):
            await redis.publish(f"signflow:player:{pid}:cmds", msg)
            dispatched.append(pid)
    finally:
        await redis.aclose()

    db.add(CmsAuditLog(
        id_org=current_user.id_org,
        id_user=current_user.id_user,
        action="player_bulk_command",
        resource_type="player",
        resource_id=None,
        source="cms",
        detail={
            "command": body.command,
            "params": body.params or {},
            "targets": [t.model_dump() for t in body.targets],
            "players": dispatched,
            "count": len(dispatched),
        },
    ))
    await db.commit()
    return PlayerBulkCommandResult(
        command=body.command, dispatched=dispatched, count=len(dispatched)
    )


@router.post("/{id_player}/maintenance", response_model=PlayerRead)
async def set_player_maintenance(
    id_player: int,
    body: PlayerMaintenanceRequest,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlayerRead:
    """Bascule le mode maintenance manuel d'un player — statut « collant » `maintenance`.

    ON  → status='maintenance', alertes vidées : distinct dans les dashboards, le moniteur de
          santé ne l'escalade plus et un heartbeat/reconnexion ne le repasse pas online
          (garde-fous ws.py). Utile pendant une intervention pour ne pas polluer les alertes.
    OFF → statut ré-évalué immédiatement : 'online' si vu il y a < 2 min, sinon 'offline' ;
          le prochain heartbeat / beat de santé affine ensuite (warning/critical…).
    """
    player = await _get_player_or_404(id_player, db)
    if body.enabled:
        player.status = "maintenance"
        player.alert_reasons = []
    else:
        last = player.last_seen_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        recent = last is not None and (
            datetime.now(timezone.utc) - last
        ).total_seconds() < 120
        player.status = "online" if recent else "offline"
    db.add(CmsAuditLog(
        id_org=current_user.id_org,
        id_user=current_user.id_user,
        action="player_maintenance_on" if body.enabled else "player_maintenance_off",
        resource_type="player",
        resource_id=str(id_player),
        detail={"player_name": player.player_name},
        source="cms",
    ))
    await db.commit()
    await db.refresh(player)
    tag_filters = [
        row[0] for row in (await db.execute(
            select(CmsPlayerTagFilter.id_tag).where(CmsPlayerTagFilter.id_player == id_player)
        )).all()
    ]
    org_players = list((await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_org == player.id_org)
    )).scalars().all())
    return _with_screenshot(player, tag_filters, _eligibility_for(player, org_players))


@router.post("/{id_player}/display/command", status_code=status.HTTP_202_ACCEPTED)
async def send_display_command(
    id_player: int,
    body: DisplayCommandRequest,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Envoie une commande à l'écran physique connecté au player (RS232/IP/HDMI-CEC).

    La commande est relayée via le canal Redis → WebSocket player.
    Le player l'exécute localement via display_control.js. Sans contrôleur configuré,
    power_on/power_off coupent/rétablissent la sortie vidéo du player lui-même
    (DPMS / wlr-randr / SC_MONITORPOWER / veille Android / SetPowerSaveMode BrightSign).

    Commandes : power_on, power_off, set_input, set_brightness, get_status.
    Params optionnels :
      - set_input  : {"source": "hdmi1"|"hdmi2"|"dp"|"pc"}
      - set_brightness : {"level": 0-100}
    """
    await _get_player_or_404(id_player, db)
    redis = aioredis.from_url(settings.REDIS_URL)
    try:
        await redis.publish(
            f"signflow:player:{id_player}:cmds",
            json.dumps({
                "type": "player:command",
                "command": "display_command",
                "params": {
                    "display_cmd": body.command,
                    **(body.params or {}),
                },
            }),
        )
    finally:
        await redis.aclose()
    return {"status": "sent", "display_cmd": body.command}


@router.post("/{id_player}/screenshot", status_code=status.HTTP_204_NO_CONTENT)
async def upload_player_screenshot(
    id_player: int,
    request: Request,
    current_player: Annotated[CmsPlayer, Depends(get_current_player)],
    # Auth = token PLAYER → on doit utiliser get_db_worker (BYPASSRLS). get_db dépend
    # de get_current_user (token access) et rejetterait le token player en 401.
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """Reçoit une capture envoyée par le player (auth token player) + horodate.

    La plupart des renderers envoient du PNG (capture canvas) ; le BrightSign envoie du JPEG
    (snapshot DWS = framebuffer composite). On stocke avec le vrai Content-Type pour que l'URL
    signée (drawer) serve l'image telle quelle. La clé reste `latest.png` (le nom d'objet est
    arbitraire pour MinIO) → GET/URL signée inchangés.
    """
    if current_player.id_player != id_player:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Player not authorized")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")
    ctype = request.headers.get("content-type", "image/png").split(";")[0].strip().lower()
    if ctype not in ("image/png", "image/jpeg", "image/webp"):
        ctype = "image/png"
    await upload_file(body, f"screenshots/{id_player}/latest.png", ctype)
    # UPDATE explicite : current_player appartient à la session de get_current_player.
    await db.execute(
        update(CmsPlayer)
        .where(CmsPlayer.id_player == id_player)
        .values(last_screenshot_at=datetime.now(timezone.utc))
    )
    await db.commit()


@router.get("/{id_player}/screenshot", response_class=StreamingResponse)
async def get_player_screenshot(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Stream la dernière capture du player directement depuis MinIO (PNG ou JPEG)."""
    await _get_player_or_404(id_player, db)
    try:
        content = await download_file(f"screenshots/{id_player}/latest.png")
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No screenshot available")
    # Détection par octets magiques (les BrightSign envoient du JPEG, les autres du PNG).
    media_type = "image/jpeg" if content[:3] == b"\xff\xd8\xff" else "image/png"
    return StreamingResponse(io.BytesIO(content), media_type=media_type)


@router.get("/{id_player}/current-state")
async def get_player_current_state(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Retourne l'état résolu du player à l'instant T :
    event actif, schedule actif, playlist active, nombre d'items.
    Utilise la même logique de priorité que le WebSocket (event > schedule > vide).
    """
    from app.api.v1.endpoints.ws import _local_now, _resolve_audio_baskets
    from app.services.player_timezone import player_tz_name
    from app.services.scheduling import schedule_active_at, time_rule_from_schedule

    player = await _get_player_or_404(id_player, db)
    # Heure du SITE de l'écran, comme la diffusion (P2.2) : cet endpoint dit « ce qui passe
    # à cet instant ». Sur l'horloge du serveur il annoncerait le créneau d'un autre fuseau.
    tz_name = await player_tz_name(db, id_player)
    now = _local_now(tz_name)

    # Groupes du player (+ groupes de sites via son site)
    group_ids: set[int] = set(
        (await db.execute(
            select(CmsPlayerGroupMember.id_group).where(
                CmsPlayerGroupMember.id_player == id_player
            )
        )).scalars().all()
    )
    site_group_ids = await player_site_group_ids(db, id_player)

    # ── Event actif correspondant à ce player ────────────────────────────────
    active_event_info = None
    active_events = (await db.execute(
        select(CmsEvent)
        # 🔴 Filtre d'ORGANISATION indispensable (trouvé le 2026-07-29 par un test qui
        # échouait « bizarrement »). Il manquait, et `event_target_matches` accepte une
        # cible `org` INCONDITIONNELLEMENT : un événement actif d'une autre organisation
        # s'affichait donc comme l'état courant d'un écran qui ne le diffuse pas.
        # ⚠️ Ne pas compter sur la RLS ici : mesuré en production, `cms_events` porte bien
        # une politique mais SANS `FORCE ROW LEVEL SECURITY`, et l'API se connecte avec le
        # rôle PROPRIÉTAIRE de la table — une politique ne s'applique pas au propriétaire
        # sans `FORCE`. Le filet qu'on croit avoir n'est pas tendu sur cette table.
        .where(CmsEvent.id_org == current_user.id_org)
        .where(CmsEvent.status == "active")
        .order_by(CmsEvent.priority.desc())
    )).scalars().all()

    matched_event: CmsEvent | None = None
    for ev in active_events:
        if event_target_matches(
            ev.target_config, id_player, group_ids, site_group_ids, id_site=player.id_site
        ):
            matched_event = ev
            break

    if matched_event:
        active_event_info = {
            "id_event":   matched_event.id_event,
            "event_name": matched_event.event_name,
            "priority":   matched_event.priority,
            "expires_at": matched_event.expires_at.isoformat() if matched_event.expires_at else None,
        }

    # ── Schedule actif (si pas d'event) ──────────────────────────────────────
    active_schedule_info = None
    matched_playlist_id: int | None = None

    if matched_event:
        matched_playlist_id = matched_event.id_playlist or None
    else:
        targets = list((await db.execute(
            select(CmsScheduleTarget).where(
                CmsScheduleTarget.target_type == "player",
                CmsScheduleTarget.target_id == id_player,
            )
        )).scalars().all())
        if group_ids:
            targets += list((await db.execute(
                select(CmsScheduleTarget).where(
                    CmsScheduleTarget.target_type == "player_group",
                    CmsScheduleTarget.target_id.in_(group_ids),
                )
            )).scalars().all())
        if site_group_ids:
            targets += list((await db.execute(
                select(CmsScheduleTarget).where(
                    CmsScheduleTarget.target_type == "site_group",
                    CmsScheduleTarget.target_id.in_(site_group_ids),
                )
            )).scalars().all())

        if targets:
            schedule_ids = [t.id_schedule for t in targets]
            schedules = (await db.execute(
                select(CmsSchedule)
                .where(
                    CmsSchedule.id_schedule.in_(schedule_ids),
                    CmsSchedule.is_active == True,   # noqa: E712
                    CmsSchedule.is_deleted == False,  # noqa: E712
                )
                .order_by(CmsSchedule.priority.desc())
            )).scalars().all()

            for sched in schedules:
                rules = (await db.execute(
                    select(CmsScheduleRule).where(
                        CmsScheduleRule.id_schedule == sched.id_schedule
                    )
                )).scalars().all()
                for r in rules:
                    if schedule_active_at(time_rule_from_schedule(r), sched.valid_from, sched.valid_until, now):
                        active_schedule_info = {
                            "id_schedule":   sched.id_schedule,
                            "schedule_name": sched.schedule_name,
                            "priority":      sched.priority,
                        }
                        matched_playlist_id = sched.id_playlist
                        break
                if active_schedule_info:
                    break

    # ── Playlist active ───────────────────────────────────────────────────────
    active_playlist_info = None
    pl = None
    if matched_playlist_id:
        pl = (await db.execute(
            select(CmsPlaylist).where(CmsPlaylist.id_playlist == matched_playlist_id)
        )).scalar_one_or_none()
        if pl:
            from app.services import document_media
            total = (await db.execute(
                select(func.sum(document_media.item_duration_sql()))
                # 🔴 `select_from` INDISPENSABLE : la sélection ne contient qu'une fonction
                # d'agrégat, donc aucune entité n'établit le côté GAUCHE de la jointure et
                # SQLAlchemy refuse de deviner (« Don't know how to join to CmsMedia »).
                # Résultat : `GET /players/{id}/current-state` répondait **500** dès que
                # l'écran avait une playlist active — le cas nominal. Introduit le
                # 2026-07-16 avec les documents multipages, mesuré en production le 29/07
                # (~20 erreurs/minute), invisible faute du moindre test sur cette route.
                .select_from(CmsPlaylistItem)
                .join(CmsMedia, CmsMedia.id_media == CmsPlaylistItem.id_media, isouter=True)
                .where(CmsPlaylistItem.id_playlist == matched_playlist_id)
            )).scalar_one_or_none()
            count = (await db.execute(
                select(func.count()).select_from(CmsPlaylistItem).where(
                    CmsPlaylistItem.id_playlist == matched_playlist_id
                )
            )).scalar_one()
            active_playlist_info = {
                "id_playlist":      pl.id_playlist,
                "playlist_name":    pl.playlist_name,
                "sync_mode":        pl.sync_mode,
                "item_count":       count,
                "total_duration_ms": total,
            }

    # ── Item en cours (wall_clock uniquement) ─────────────────────────────────
    current_item_info = None
    if pl and active_playlist_info and active_playlist_info.get("sync_mode") == "wall_clock":
        total_dur = active_playlist_info.get("total_duration_ms") or 0
        if total_dur > 0:
            from app.services.document_media import effective_duration_py
            pl_items = (await db.execute(
                select(CmsPlaylistItem, CmsMedia.media_name, CmsMedia.duration_ms,
                       CmsMedia.page_count)
                .join(CmsMedia, CmsPlaylistItem.id_media == CmsMedia.id_media, isouter=True)
                .where(CmsPlaylistItem.id_playlist == matched_playlist_id)
                .order_by(CmsPlaylistItem.sort_order)
            )).all()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elapsed_ms = int((now - midnight).total_seconds() * 1000) % total_dur
            running = 0
            for idx, (item, media_name, media_dur, media_pages) in enumerate(pl_items):
                item_dur = effective_duration_py(
                    item.duration_ms, media_dur, media_pages,
                    doc_pages=item.doc_pages, doc_page_count=item.doc_page_count,
                ) or 10000
                if running + item_dur > elapsed_ms:
                    current_item_info = {
                        "item_index":  idx,
                        "media_name":  media_name or "—",
                        "offset_ms":   elapsed_ms - running,
                        "duration_ms": item_dur,
                    }
                    break
                running += item_dur

    # ── Paniers audio actifs (players audio/mixed) ────────────────────────────
    active_audio = None
    if player.player_role in ("audio", "mixed"):
        baskets = await _resolve_audio_baskets(db, id_player, tz_name=tz_name)
        active_audio = [
            {
                "id_basket":   b["id_basket"],
                "basket_name": b["basket_name"],
                "basket_type": b["basket_type"],
                "mix_ratio":   b.get("mix_ratio"),
                "item_count":  len(b.get("items", [])),
            }
            for b in baskets
        ]

    return {
        "resolved_at":      now.isoformat(),
        "override_source":  "event" if matched_event else ("schedule" if active_schedule_info else "none"),
        "active_event":     active_event_info,
        "active_schedule":  active_schedule_info,
        "active_playlist":  active_playlist_info,
        "current_item":     current_item_info,
        "player_role":      player.player_role,
        "active_audio":     active_audio,
    }
