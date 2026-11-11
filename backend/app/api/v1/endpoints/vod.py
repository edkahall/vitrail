"""VOD à la demande (Feature B) — Phase 1 : configs + affectation + pré-cache.

GET    /vod                 — liste des configs de l'org
POST   /vod                 — créer une config (médias + cible players OU site)
GET    /vod/{id}            — détail (players = cible RÉSOLUE)
PUT    /vod/{id}            — modifier (nom/desc/médias/cible)
DELETE /vod/{id}            — supprimer

**Ciblage (migr 0096)** — deux modes, cf. `services/vod_targeting.py` (source unique) :
  - `target_mode='players'` : liste FIGÉE de players du même site (historique) ;
  - `target_mode='site'`    : TOUS les players du site, résolus à la lecture → un player
    posé sur le site plus tard hérite du pré-cache sans rouvrir la config.
Ne jamais recopier une requête sur `cms_vod_config_players` chez un appelant : le mode
'site' n'y écrit AUCUNE ligne, un tel clone rendrait la cible invisible en silence.

Le pré-cache est implicite : les médias d'une config VOD sont injectés dans
`player_inventory.expected_media` des players ciblés → le look-ahead (`_build_prefetch`)
les distribue automatiquement (offline-first content store). Tout changement d'affectation
publie `player:force_sync` aux players concernés (anciens ∪ nouveaux) pour relancer le
pré-cache immédiatement.

Phases 2-3 (trigger cloud `play_now` + LAN-local) viendront ensuite.
"""

import json
from functools import partial
from typing import Annotated

import anyio
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_permission
from app.models.audit_log import CmsAuditLog
from app.models.governance import CmsGovernanceNode
from app.models.media import CmsMedia
from app.models.organization import CmsSite
from app.models.player import CmsPlayer
from app.models.user import CmsUser
from app.models.vod import CmsVodConfig, CmsVodConfigMedia, CmsVodConfigPlayer
from app.services.governance_access import (
    assert_can_detach,
    assert_can_edit_node,
    content_visibility,
)
from app.services.governance_scope import out_of_scope_targets, user_player_scope
from app.services.vod_targeting import vod_player_ids
from app.schemas.vod import (
    PlayNowRequest,
    PlayNowResult,
    VodConfigCreate,
    VodConfigRead,
    VodConfigUpdate,
    VodMediaItem,
    VodPlayerItem,
)

router = APIRouter(tags=["vod"])

# Types réellement pré-cachables et rendus comme un visuel (cf. player_inventory._VISUAL).
_VISUAL_TYPES = ("video", "image", "document")
# Types admis dans une config VOD : les visuels + le pointeur « entrée HDMI » (pas de fichier,
# pas de pré-cache — le trigger devient un switch ON/OFF de la source, cf. play_vod_now).
_ALLOWED_TYPES = (*_VISUAL_TYPES, "hdmi_input")


async def _push_force_sync(player_ids: set[int]) -> None:
    """Relance le pré-cache : publie player:force_sync sur le canal de chaque player."""
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        payload = json.dumps({"type": "player:force_sync"})
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)
    finally:
        await r.aclose()


async def _validate_players(
    db: AsyncSession, id_org: int, player_ids: list[int]
) -> tuple[list[CmsPlayer], int | None]:
    """Charge les players de l'org et vérifie qu'ils partagent le MÊME id_site.

    Retourne (players, id_site_commun). Lève 400 si site mixte, 404 si un player est
    introuvable dans l'org.
    """
    if not player_ids:
        return [], None
    uniq = list(dict.fromkeys(player_ids))
    players = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_player.in_(uniq), CmsPlayer.id_org == id_org
        )
    )).scalars().all()
    found = {p.id_player for p in players}
    missing = set(uniq) - found
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player(s) not found in the organization: {sorted(missing)}",
        )
    sites = {p.id_site for p in players}
    if len(sites) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All players of a VOD config must belong to the same site (id_site).",
        )
    return players, (next(iter(sites)) if sites else None)


async def _validate_site(db: AsyncSession, id_org: int, id_site: int | None) -> int:
    """Vérifie qu'un ciblage 'site' porte bien un site EXISTANT de l'org.

    Un site absent/étranger donnerait une cible vide silencieuse — exactement le bug
    d'acceptation-sans-diffusion corrigé le 2026-07-18 ailleurs dans le produit.
    """
    if id_site is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Site targeting requires an id_site.",
        )
    exists = (await db.execute(
        select(CmsSite.id_site).where(CmsSite.id_site == id_site, CmsSite.id_org == id_org)
    )).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site not found in the organization: {id_site}",
        )
    return id_site


async def _validate_medias(
    db: AsyncSession, id_org: int, media_ids: list[int]
) -> list[CmsMedia]:
    """Charge les médias VISUELS (vidéo/image/document) de l'org, non supprimés/bloqués."""
    if not media_ids:
        return []
    uniq = list(dict.fromkeys(media_ids))
    medias = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media.in_(uniq),
            CmsMedia.id_org == id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
            CmsMedia.is_blocked == False,  # noqa: E712
        )
    )).scalars().all()
    by_id = {m.id_media: m for m in medias}
    missing = set(uniq) - set(by_id)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media not found in the organization: {sorted(missing)}",
        )
    bad = [m.id_media for m in medias if m.media_type not in _ALLOWED_TYPES]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Only visual media (video/image/document) and the HDMI input "
                    f"are supported in VOD: {sorted(bad)}"),
        )
    # Préserve l'ordre d'entrée.
    return [by_id[mid] for mid in uniq]


async def _enrich(db: AsyncSession, cfg: CmsVodConfig) -> VodConfigRead:
    """Construit la vue détaillée (médias + players + nom de site)."""
    media_rows = (await db.execute(
        select(CmsVodConfigMedia.id_media, CmsVodConfigMedia.sort_order,
               CmsMedia.media_name, CmsMedia.media_type)
        .outerjoin(CmsMedia, CmsMedia.id_media == CmsVodConfigMedia.id_media)
        .where(CmsVodConfigMedia.id_vod == cfg.id_vod)
        .order_by(CmsVodConfigMedia.sort_order, CmsVodConfigMedia.id_vod_media)
    )).all()
    medias = [
        VodMediaItem(id_media=mid, sort_order=so, media_name=name, media_type=mtype)
        for mid, so, name, mtype in media_rows
    ]

    # Cible RÉSOLUE (source unique) : liste explicite ou tous les players du site ciblé.
    pids = await vod_player_ids(db, cfg)
    player_rows = (await db.execute(
        select(CmsPlayer.id_player, CmsPlayer.player_name, CmsPlayer.status, CmsPlayer.id_site)
        .where(CmsPlayer.id_player.in_(pids))
        .order_by(CmsPlayer.player_name)
    )).all() if pids else []
    players = [
        VodPlayerItem(id_player=pid, player_name=name, player_status=st, id_site=site)
        for pid, name, st, site in player_rows
    ]

    site_name = None
    if cfg.id_site is not None:
        site_name = (await db.execute(
            select(CmsSite.site_name).where(CmsSite.id_site == cfg.id_site)
        )).scalar_one_or_none()

    return VodConfigRead(
        id_vod=cfg.id_vod,
        id_org=cfg.id_org,
        id_site=cfg.id_site,
        site_name=site_name,
        target_mode=cfg.target_mode,
        vod_name=cfg.vod_name,
        description=cfg.description,
        medias=medias,
        players=players,
        created_by=cfg.created_by,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


async def _current_player_ids(db: AsyncSession, cfg: CmsVodConfig) -> set[int]:
    """Cible résolue de la config (force_sync + dispatch play_now) — source unique.

    Prend la CONFIG et non un id_vod : en mode 'site' la cible dépend de `target_mode`/
    `id_site`, qu'un simple id ne porte pas.
    """
    return await vod_player_ids(db, cfg)


async def _validate_vod_node(db: AsyncSession, user: CmsUser, id_node: int | None) -> None:
    """Le nœud visé doit exister DANS l'org (cross-tenant = 404, jamais 403)."""
    if id_node is None:
        return
    ok = (await db.execute(
        select(CmsGovernanceNode.id_node).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Governance node not found")


def _get_or_404(cfg: CmsVodConfig | None) -> CmsVodConfig:
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VOD config not found")
    return cfg


async def _enforce_vod_targets_in_scope(
    db: AsyncSession, id_org: int, id_node: int | None,
    target_mode: str, id_site: int | None, player_ids: list[int],
) -> None:
    """Une VOD GOUVERNÉE ne vise que des écrans de son nœud (audit du 2026-07-29).

    🔴 Le trou que ça ferme : la phase 4 de la gouvernance avait contraint les cibles des
    ÉVÉNEMENTS à leur nœud, mais pas celles de la VOD. Rattacher une config VOD à un nœud
    n'était donc qu'une étiquette — elle continuait à pré-charger et à déclencher un plein
    écran sur n'importe quel écran de l'organisation. C'est exactement N3, laissé ouvert
    d'un seul côté.

    Miroir strict de `_enforce_event_targets_in_scope` : même service de périmètre, même
    forme de refus (422 qui NOMME la cible), un recouvrement partiel restant accepté.
    """
    if id_node is None:
        return
    node = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == id_org)
    )).scalar_one_or_none()
    if node is None:
        return
    if target_mode == "site":
        targets = [("site", id_site)] if id_site is not None else []
    else:
        targets = [("player", pid) for pid in player_ids]
    bad = await out_of_scope_targets(db, id_org, node, targets)
    if bad:
        names = ", ".join(b["target_name"] or f"#{b['target_id']}" for b in bad)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Target outside the scope of node '{node.node_name}': {names}.",
        )


async def _get_visible_vod(db: AsyncSession, user: CmsUser, id_vod: int) -> CmsVodConfig:
    """Config de l'org ET VISIBLE (D6/G4) — le lien direct ne doit pas contourner la liste.

    🔴 `GET /vod/{id}` et `POST /vod/{id}/play` ne filtraient QUE sur l'organisation : une
    config invisible dans la liste restait lisible par son identifiant, et surtout
    **déclenchable**. C'est le défaut « la liste filtre, le lien direct ment » déjà corrigé
    sur les templates (28/07) et les médias — ici il portait en plus une mise à l'antenne.
    """
    stmt = select(CmsVodConfig).where(
        CmsVodConfig.id_vod == id_vod, CmsVodConfig.id_org == user.id_org
    )
    gov_clause, _visible, _editable = await content_visibility(db, user)
    clause = gov_clause(CmsVodConfig.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    return _get_or_404((await db.execute(stmt)).scalar_one_or_none())


def _audit(user: CmsUser, action: str, cfg: CmsVodConfig, detail: dict | None = None) -> CmsAuditLog:
    """Traçabilité du CRUD VOD (audit 2026-07-29).

    Seul le DÉCLENCHEMENT était journalisé (`vod_play_now`, `vod_hdmi_switch` — les 27
    lignes mesurées en production). Créer une config, changer ses écrans cibles ou la
    supprimer ne laissait rien : basculer une config sur un SITE ENTIER, geste qui étend la
    diffusion à tous les écrans du magasin, était invisible après coup.
    """
    base = {"vod_name": cfg.vod_name, "target_mode": cfg.target_mode, "id_site": cfg.id_site}
    base.update(detail or {})
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type="vod_config", resource_id=str(cfg.id_vod), source="cms", detail=base,
    )


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("/vod", response_model=list[VodConfigRead])
async def list_vod_configs(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("vod", "read"))],
) -> list[VodConfigRead]:
    stmt = (
        select(CmsVodConfig)
        .where(CmsVodConfig.id_org == current_user.id_org)
        .order_by(CmsVodConfig.created_at.desc())
    )
    # Gouvernance §XVIII phase 4 : D6 (nœuds visibles) + G4 (les configs d'organisation
    # disparaissent pour un porteur de rôle de gouvernance).
    gov_clause, _visible, _editable = await content_visibility(db, current_user)
    clause = gov_clause(CmsVodConfig.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    rows = (await db.execute(stmt)).scalars().all()
    return [await _enrich(db, c) for c in rows]


@router.post("/vod", response_model=VodConfigRead, status_code=status.HTTP_201_CREATED)
async def create_vod_config(
    body: VodConfigCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("vod", "create"))],
) -> VodConfigRead:
    # Mode 'site' : le site est autoritaire, aucune ligne cms_vod_config_players n'est écrite
    # (la cible est recalculée à chaque lecture). Mode 'players' : site déduit des players.
    if body.target_mode == "site":
        players = []
        id_site = await _validate_site(db, current_user.id_org, body.id_site)
    else:
        players, id_site = await _validate_players(db, current_user.id_org, body.id_players)
    medias = await _validate_medias(db, current_user.id_org, body.id_medias)
    await _validate_vod_node(db, current_user, body.id_governance_node)
    await assert_can_edit_node(db, current_user, body.id_governance_node, "this VOD config")
    await _enforce_vod_targets_in_scope(
        db, current_user.id_org, body.id_governance_node,
        body.target_mode, id_site, [p.id_player for p in players],
    )

    cfg = CmsVodConfig(
        id_org=current_user.id_org,
        id_site=id_site,
        target_mode=body.target_mode,
        vod_name=body.vod_name,
        description=body.description,
        id_governance_node=body.id_governance_node,
        created_by=current_user.id_user,
    )
    db.add(cfg)
    await db.flush()

    for order, m in enumerate(medias):
        db.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=m.id_media, sort_order=order))
    for p in players:
        db.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=p.id_player))
    db.add(_audit(current_user, "create", cfg,
                  {"medias": len(medias), "players": len(players)}))
    await db.commit()
    await db.refresh(cfg)

    # force_sync sur la cible RÉSOLUE (en mode 'site' : tous les players du site).
    await _push_force_sync(await _current_player_ids(db, cfg))
    return await _enrich(db, cfg)


@router.get("/vod/{id_vod}", response_model=VodConfigRead)
async def get_vod_config(
    id_vod: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("vod", "read"))],
) -> VodConfigRead:
    cfg = await _get_visible_vod(db, current_user, id_vod)
    return await _enrich(db, cfg)


@router.put("/vod/{id_vod}", response_model=VodConfigRead)
async def update_vod_config(
    id_vod: int,
    body: VodConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("vod", "edit"))],
) -> VodConfigRead:
    cfg = await _get_visible_vod(db, current_user, id_vod)

    # Gouvernance §XVIII phase 4 : administrer le nœud ACTUEL, puis le nœud CIBLE en cas
    # de déplacement ; détacher est réservé à un nœud PARENT (N2).
    await assert_can_edit_node(db, current_user, cfg.id_governance_node, "this VOD config")
    if "id_governance_node" in body.model_fields_set:
        await _validate_vod_node(db, current_user, body.id_governance_node)
        await assert_can_edit_node(db, current_user, body.id_governance_node,
                                   "the destination node")
        if body.id_governance_node is None:
            await assert_can_detach(db, current_user, cfg.id_governance_node, "this VOD config")
        cfg.id_governance_node = body.id_governance_node

    # Cible AVANT mutation : les players qui perdent la config doivent être resynchronisés
    # (purge de leur pré-cache) autant que ceux qui la gagnent.
    affected: set[int] = await _current_player_ids(db, cfg)

    if body.vod_name is not None:
        cfg.vod_name = body.vod_name
    if body.description is not None:
        cfg.description = body.description

    mode = body.target_mode or cfg.target_mode
    if mode == "site":
        # Site autoritaire ; bascule players→site = on purge la liste explicite devenue morte.
        id_site = body.id_site if body.id_site is not None else cfg.id_site
        cfg.id_site = await _validate_site(db, current_user.id_org, id_site)
        cfg.target_mode = "site"
        await db.execute(delete(CmsVodConfigPlayer).where(CmsVodConfigPlayer.id_vod == id_vod))
    elif body.target_mode == "players" and body.id_players is None:
        # Bascule site→players sans liste : la cible deviendrait vide en silence.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Switching to player targeting requires id_players.",
        )
    elif body.id_players is not None:
        players, id_site = await _validate_players(db, current_user.id_org, body.id_players)
        cfg.id_site = id_site
        cfg.target_mode = "players"
        await db.execute(delete(CmsVodConfigPlayer).where(CmsVodConfigPlayer.id_vod == id_vod))
        for p in players:
            db.add(CmsVodConfigPlayer(id_vod=id_vod, id_player=p.id_player))

    if body.id_medias is not None:
        medias = await _validate_medias(db, current_user.id_org, body.id_medias)
        await db.execute(delete(CmsVodConfigMedia).where(CmsVodConfigMedia.id_vod == id_vod))
        for order, m in enumerate(medias):
            db.add(CmsVodConfigMedia(id_vod=id_vod, id_media=m.id_media, sort_order=order))

    # Périmètre du nœud vérifié APRÈS mutation de la cible : c'est la cible RÉSULTANTE qui
    # doit tenir dans le nœud, pas celle d'avant (sinon on validerait l'ancien état).
    await db.flush()
    await _enforce_vod_targets_in_scope(
        db, current_user.id_org, cfg.id_governance_node,
        cfg.target_mode, cfg.id_site,
        sorted(await _current_player_ids(db, cfg)) if cfg.target_mode != "site" else [],
    )

    db.add(_audit(current_user, "update", cfg,
                  {"champs": sorted(body.model_dump(exclude_unset=True).keys())}))
    await db.commit()
    await db.refresh(cfg)

    # Anciens ∪ nouveaux : les sortants purgent leur pré-cache, les entrants le construisent.
    await _push_force_sync(affected | await _current_player_ids(db, cfg))
    return await _enrich(db, cfg)


@router.delete("/vod/{id_vod}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vod_config(
    id_vod: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("vod", "delete"))],
) -> None:
    cfg = _get_or_404((await db.execute(
        select(CmsVodConfig).where(
            CmsVodConfig.id_vod == id_vod, CmsVodConfig.id_org == current_user.id_org
        )
    )).scalar_one_or_none())
    await assert_can_edit_node(db, current_user, cfg.id_governance_node, "this VOD config")

    affected = await _current_player_ids(db, cfg)
    # ON DELETE CASCADE purge media/players ; on supprime la config explicitement.
    await db.execute(delete(CmsVodConfigMedia).where(CmsVodConfigMedia.id_vod == id_vod))
    await db.execute(delete(CmsVodConfigPlayer).where(CmsVodConfigPlayer.id_vod == id_vod))
    db.add(_audit(current_user, "delete", cfg, {"ecrans_impactes": len(affected)}))
    await db.delete(cfg)
    await db.commit()

    await _push_force_sync(affected)


# ── Trigger CLOUD play_now (§Phase 2) ─────────────────────────────────────────

def _media_has_audio(media: CmsMedia) -> bool:
    """Le média porte-t-il une piste audio ? (metadata ffprobe, cf. media_metadata['audio'])

    Sert à décider du DUCKING côté player : baisser la musique de fond pour une vidéo
    MUETTE produirait « musique baissée + silence » = du vide à l'antenne (objection Ed
    2026-07-19). Metadata absente (média ancien/non sondé) → True : on préfère ducker à
    tort plutôt que laisser deux sources se superposer.
    """
    meta = media.media_metadata or {}
    if meta.get("audio"):
        return True
    # Distinguer « sondé, sans piste audio » de « jamais sondé » : ffprobe renseigne
    # toujours container/video/size_bytes. Metadata absente ou vide = inconnu → on suppose
    # du son (mieux vaut ducker à tort que laisser deux sources se superposer).
    probed = any(k in meta for k in ("container", "video", "size_bytes"))
    return not probed


async def _resolve_play_payload(db: AsyncSession, player: CmsPlayer, media: CmsMedia, mode: str,
                                with_sound: bool = True) -> dict:
    """Construit le payload de lecture ad-hoc pour ce player : variante (rendition)
    réellement servie + URL signée (repli si la lecture cache échoue). Réutilise la
    résolution de variante de `ws.py` (cohérent avec le sync/prefetch)."""
    from app.api.v1.endpoints import ws  # import tardif (évite tout cycle d'import au boot)
    from app.services.media import get_signed_url

    cache_key = media.storage_key
    if media.media_type == "video":
        target_height = await ws._player_target_height(db, player)
        # Le takeover VOD est rendu par un <video> Chromium (overlay), même sur un player mpv/exo →
        # browser_only=True force une variante navigateur-safe (jamais de HEVC/MKV injouable).
        cache_key = await ws._resolve_variant_key(
            db, media, target_height, None, True, ws._player_video_can_hevc(player),
        )
    try:
        url = await anyio.to_thread.run_sync(partial(get_signed_url, cache_key, 86400))
    except Exception:  # noqa: BLE001
        url = None
    return {
        "id_media": media.id_media,
        "media_type": media.media_type,
        # 'stop' DOIT passer tel quel (sinon le renderer rejoue la vidéo au lieu d'arrêter).
        "mode": mode if mode in ("once", "loop", "stop") else "once",
        "cache_key": cache_key,
        "cache_checksum": media.checksum_sha256 if cache_key == media.storage_key else None,
        "mime_type": media.mime_type,
        "url": url,
        "duration_ms": media.duration_ms,
        # Son choisi au déclenchement ; has_audio conditionne le ducking côté player.
        "with_sound": with_sound,
        "has_audio": _media_has_audio(media),
    }


@router.post("/vod/{id_vod}/play", response_model=PlayNowResult)
async def play_vod_now(
    id_vod: int,
    body: PlayNowRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    _: Annotated[None, Depends(require_permission("vod", "trigger"))],
) -> PlayNowResult:
    """Déclenche une lecture ad-hoc (takeover) du média sur les players de la config.

    Le média est résolu par player (rendition adaptative) puis poussé via `player:command`
    `play_now` sur Redis ; le renderer joue en takeover (once|loop) puis revient à la
    programmation. Le pré-cache (Phase 1) rend la lecture instantanée et locale.
    """
    # Garde 404 + objet conservé : la cible dépend de son target_mode/id_site.
    # 🔴 Filtre de VISIBILITÉ obligatoire ici : déclencher, c'est mettre un plein écran à
    # l'antenne. Sans lui, une config invisible en liste restait déclenchable par son
    # identifiant — le pire cas de la famille « la liste filtre, le lien direct ment ».
    cfg = await _get_visible_vod(db, current_user, id_vod)

    # Le média doit appartenir à la config.
    in_config = (await db.execute(select(CmsVodConfigMedia.id_vod_media).where(
        CmsVodConfigMedia.id_vod == id_vod, CmsVodConfigMedia.id_media == body.id_media))).first()
    if in_config is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This media does not belong to this VOD config.")
    media = (await db.execute(select(CmsMedia).where(
        CmsMedia.id_media == body.id_media, CmsMedia.id_org == current_user.id_org,
        CmsMedia.is_deleted == False, CmsMedia.is_blocked == False))).scalar_one_or_none()  # noqa: E712
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found or blocked.")

    # Cibles = players de la config, restreints au sous-ensemble demandé.
    config_player_ids = await _current_player_ids(db, cfg)
    # N8 — et au PÉRIMÈTRE D'ÉCRANS de l'appelant : un admin de nœud ne déclenche pas un
    # plein écran sur un écran qui, sur `/players/`, lui répond 404. `None` = pas de
    # restriction (org-admin, ou rôle classique), sémantique de `user_player_scope`.
    scope = await user_player_scope(db, current_user)
    if scope is not None:
        config_player_ids &= scope
    targets = config_player_ids if not body.id_players else (config_player_ids & set(body.id_players))
    if not targets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No valid target player in this config.")

    players = (await db.execute(select(CmsPlayer).where(CmsPlayer.id_player.in_(targets)))).scalars().all()

    # Média « entrée HDMI » : pas de lecture ad-hoc — le trigger est un SWITCH de source
    # (mode 'stop' = revenir à la playlist, tout autre mode = basculer sur l'entrée HDMI).
    # On réutilise la commande player `hdmi_input` (celle des boutons du drawer : bascule
    # immédiate, playlist suspendue, reprise wall-clock au stop). Cibles restreintes aux
    # players à entrée HDMI physique (miroir du gate de sync `_player_has_hdmi_input`).
    if media.media_type == "hdmi_input":
        from app.api.v1.endpoints.ws import _player_has_hdmi_input  # import tardif (évite cycle)
        capable = [p for p in players if _player_has_hdmi_input(p)]
        if not capable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No target player with a physical HDMI input (BrightSign XT/4K1142/XD1230).",
            )
        hdmi_mode = "stop" if body.mode == "stop" else "start"
        r = aioredis.from_url(settings.REDIS_URL)
        dispatched = []
        try:
            # source + id_media = traçabilité : le renderer les mémorise au switch ON et émet un
            # événement `hdmi_session` (durée réelle + PoP signé du média) au switch OFF.
            msg = json.dumps({"type": "player:command", "command": "hdmi_input",
                              "params": {"mode": hdmi_mode, "source": "vod",
                                         "id_media": media.id_media}})
            for player in capable:
                await r.publish(f"signflow:player:{player.id_player}:cmds", msg)
                dispatched.append(player.id_player)
        finally:
            await r.aclose()
        # Audit du DÉCLENCHEMENT (qui a cliqué, quand, quelles cibles) — la durée de session,
        # elle, est journalisée par le player au switch OFF (action `hdmi_session`).
        db.add(CmsAuditLog(
            id_org=current_user.id_org, id_user=current_user.id_user,
            action="vod_hdmi_switch", resource_type="vod", resource_id=str(id_vod),
            source="cms",
            detail={"mode": hdmi_mode, "id_media": media.id_media, "players": sorted(dispatched)},
        ))
        await db.commit()
        return PlayNowResult(id_media=media.id_media, mode=hdmi_mode, dispatched=sorted(dispatched))

    r = aioredis.from_url(settings.REDIS_URL)
    dispatched: list[int] = []
    try:
        for player in players:
            params = await _resolve_play_payload(db, player, media, body.mode, body.with_sound)
            msg = json.dumps({"type": "player:command", "command": "play_now", "params": params})
            await r.publish(f"signflow:player:{player.id_player}:cmds", msg)
            dispatched.append(player.id_player)
    finally:
        await r.aclose()

    # Audit du DÉCLENCHEMENT — il manquait totalement sur ce chemin (seule la branche HDMI
    # était tracée) : une diffusion VOD, désormais ciblable sur un SITE ENTIER, ne laissait
    # aucune trace serveur. La preuve de diffusion, elle, remonte en PoP §XXIV côté player.
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="vod_play_now", resource_type="vod", resource_id=str(id_vod),
        source="cms",
        detail={
            "id_media": media.id_media,
            "media_name": media.media_name,
            "mode": "loop" if body.mode == "loop" else "once",
            "with_sound": body.with_sound,
            "players": sorted(dispatched),
            "count": len(dispatched),
        },
    ))
    await db.commit()

    return PlayNowResult(id_media=media.id_media,
                         mode="loop" if body.mode == "loop" else "once",
                         dispatched=sorted(dispatched))
