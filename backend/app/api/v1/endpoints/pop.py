"""
Proof of Play — batch ingestion (§XXIV).

POST /pop/batch
  Auth    : token player (Bearer type=player)
  Merkle  : sha256(prev_hash + id_player + id_media + played_at + duration_ms)
             prev_hash initial = dernier hash connu en DB, ou "genesis:{id_player}"
"""

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, desc, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import (
    deny_demo,
    get_current_player,
    get_db,
    get_db_worker,
    require_admin_or_permission,
    require_org_admin,
)
from app.models.audio_basket import CmsAudioBasket, CmsAudioBasketItem
from app.models.audit_log import CmsAuditLog
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.pop_archive import CmsPopArchive
from app.models.user import CmsUser
from app.schemas.pop import (
    PopAnalyticsResponse,
    PopArchiveRow,
    PopArchivesResponse,
    PopArchiveVerifyResponse,
    PopBatchRequest,
    PopBatchResponse,
    PopCertificateResponse,
    PopDayStats,
    PopPlayRow,
    PopPlaysResponse,
    PopTopByTypeResponse,
    PopTopItem,
    PopTypeGroup,
    PopVerifyResponse,
)
from app.services import analytics, csv_safety, pop_archive, pop_counting, pop_proof, pop_screen
from app.services.media import download_file, get_signed_url
from app.services.doc_i18n import tr
from app.services.pop_report import DOC_TEXTS as POP_TEXTS
from app.services.pop_report import build_pop_report_pdf
from app.services.pop_report import csv_header as pop_csv_header

router = APIRouter(prefix="/pop", tags=["proof-of-play"])


def _next_hash(prev: str, id_player: int, id_media: int | None, played_at: str, duration_ms: int) -> str:
    return pop_proof.chain_hash(prev, id_player, id_media, played_at, duration_ms)


# Condition « preuve COMPTABILISÉE » — écran coupé (migr 0102) ou zone recouverte par un
# overlay plein écran (migr 0125). La preuve est conservée et reste dans la chaîne Merkle,
# mais ne compte pas comme de l'antenne. Définition unique : services/pop_counting.
# ⛔ NE JAMAIS l'ajouter aux chemins d'INTÉGRITÉ (/verify, chaîne du PDF, archivage) :
# la chaîne couvre toutes les lignes insérées, la filtrer ferait diverger le recalcul
# et produirait un faux « chaîne altérée ». Ces chemins lisent tout et n'excluent que
# des COMPTEURS.
_COUNTED = pop_counting.COUNTED


def _batch_proves_key(key_pem: str, logs, id_player: int) -> bool:
    """True si TOUTES les preuves SIGNÉES du lot vérifient contre `key_pem` — preuve que le
    device détient la clé privée correspondante. Un lot sans aucune signature renvoie False
    (rien à prouver). Sert au self-heal P0.1 : n'accepter une rotation de clé que si le lot
    démontre la possession de la nouvelle clé privée."""
    seen = False
    for e in logs:
        if not e.signature:
            continue
        seen = True
        msg = pop_proof.canonical_message(
            id_player, e.id_media, e.id_playlist,
            pop_proof.played_at_ms(e.played_at), e.duration_ms,
        )
        if not pop_proof.verify_signature(key_pem, e.signature, msg):
            return False
    return seen


@router.post("/batch", response_model=PopBatchResponse)
async def pop_batch(
    body: PopBatchRequest,
    player: Annotated[CmsPlayer, Depends(get_current_player)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> PopBatchResponse:
    if not body.logs:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="logs cannot be empty")

    # Semis de la chaîne = dernier hash INSÉRÉ (id_pop desc), PAS le played_at le plus récent.
    # La vérification recalcule la chaîne dans l'ordre id_pop ; semer par played_at divergeait dès
    # qu'une ligne au played_at max n'était pas la ligne à l'id_pop max (multi-zones : deux zones
    # émettent au même instant ; offline-first : flush d'un backlog antérieur) → faux « chaîne
    # altérée ». Écriture et vérif doivent ordonner la chaîne de façon identique (id_pop).
    last_row = await db.execute(
        select(CmsProofOfPlay.merkle_hash)
        .where(CmsProofOfPlay.id_player == player.id_player)
        .order_by(CmsProofOfPlay.id_pop.desc())
        .limit(1)
    )
    prev_hash = last_row.scalar_one_or_none() or f"genesis:{player.id_player}"

    # P2 — clé publique device : TOFU (enregistrée à la 1re remontée signée).
    # P0.1 self-heal — si le device renvoie une clé DIFFÉRENTE de celle enregistrée (seed
    # régénéré par un wipe storage : MAJ firmware, échange/reformatage SD), on accepte la
    # rotation UNIQUEMENT si le lot prouve la possession de la nouvelle clé privée (toutes ses
    # signatures vérifient contre la clé entrante). Sinon on garde l'ancienne clé → la boucle
    # ci-dessous rejette le lot (altération réelle, pas une rotation). Avant ce correctif, un
    # wipe device faisait rejeter SILENCIEUSEMENT tout le PoP (400, file qui monte) jusqu'au
    # reset manuel de pop_public_key. La rotation est horodatée + auditée (auditeur : la clé
    # a tourné le T). Compromis assumé : un détenteur du token player — déjà racine de
    # confiance, capable d'émettre des preuves NON signées — peut faire tourner la clé ; la
    # trace d'audit + l'horodatage rendent l'événement scrutable.
    effective_key = player.pop_public_key
    if body.pop_public_key and not effective_key:
        await db.execute(
            update(CmsPlayer)
            .where(CmsPlayer.id_player == player.id_player)
            .values(pop_public_key=body.pop_public_key)
        )
        effective_key = body.pop_public_key
    elif (
        body.pop_public_key
        and effective_key
        and body.pop_public_key != effective_key
        and _batch_proves_key(body.pop_public_key, body.logs, player.id_player)
    ):
        await db.execute(
            update(CmsPlayer)
            .where(CmsPlayer.id_player == player.id_player)
            .values(
                pop_public_key=body.pop_public_key,
                pop_key_rotated_at=datetime.now(timezone.utc),
            )
        )
        db.add(CmsAuditLog(
            id_org=player.id_org,
            action="pop_key_rotated",
            resource_type="player",
            resource_id=str(player.id_player),
            detail={"reason": "device_seed_changed", "self_heal": True},
            source="player",
        ))
        effective_key = body.pop_public_key

    # Écran programmé éteint à l'instant de la diffusion (migr 0102) : la preuve est
    # conservée et entre dans la chaîne Merkle, mais ne comptera jamais comme de
    # l'antenne. Visuel uniquement. Calculé UNE fois pour tout le lot.
    screen_off_flags = await pop_screen.resolve_screen_off(db, player, body.logs)

    records: list[CmsProofOfPlay] = []
    first_hash: str | None = None
    signed_count = 0
    screen_off_count = 0
    covered_count = 0

    for entry, screen_off in zip(body.logs, screen_off_flags):
        played_at_str = entry.played_at.astimezone(timezone.utc).isoformat()
        h = _next_hash(prev_hash, player.id_player, entry.id_media, played_at_str, entry.duration_ms)
        if first_hash is None:
            first_hash = h

        # P2 — vérification de signature. Une signature INVALIDE = altération → on refuse le lot.
        sig = entry.signature
        if sig and effective_key:
            msg = pop_proof.canonical_message(
                player.id_player, entry.id_media, entry.id_playlist,
                pop_proof.played_at_ms(entry.played_at), entry.duration_ms,
            )
            if not pop_proof.verify_signature(effective_key, sig, msg):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid proof signature (proof rejected)",
                )
            signed_count += 1
        else:
            sig = None  # player legacy / pas de clé → stocké non signé

        records.append(
            CmsProofOfPlay(
                id_org=player.id_org,
                id_player=player.id_player,
                id_playlist=entry.id_playlist,
                id_media=entry.id_media,
                played_at=entry.played_at,
                duration_ms=entry.duration_ms,
                merkle_hash=h,
                signature=sig,
                screen_off=screen_off,
                # Déclaré par le player (migr 0125) : une zone de template multi-zones
                # recouverte par un spot plein écran ou un takeover VOD au moment du
                # démarrage du média. Le serveur ne peut pas le recalculer — chaque zone
                # a son propre wall-clock, il n'existe aucune timeline globale à rejouer.
                covered=bool(entry.covered),
            )
        )
        if screen_off:
            screen_off_count += 1
        if entry.covered:
            covered_count += 1
        prev_hash = h

    db.add_all(records)
    await db.commit()

    return PopBatchResponse(
        inserted=len(records),
        first_hash=first_hash,  # type: ignore[arg-type]
        last_hash=prev_hash,
        signed=signed_count,
        screen_off=screen_off_count,
        covered=covered_count,
    )


@router.get("/analytics", response_model=PopAnalyticsResponse)
async def pop_analytics(
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(7, ge=1, le=90),
) -> PopAnalyticsResponse:
    """Agrégats Proof of Play sur les N derniers jours (org isolée via RLS + filtre explicite).

    **Périmètre de gouvernance (D1 étendue, 31/07)** : ces agrégats portent sur la diffusion
    réelle du parc — hors périmètre = rien.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    id_org = current_user.id_org
    _scope = await analytics.scoped_report_players(db, current_user)
    _scope_cond = [CmsProofOfPlay.id_player.in_(_scope)] if _scope is not None else []

    # Totaux
    total_row = (await db.execute(
        select(
            func.count(CmsProofOfPlay.id_pop).label("cnt"),
            func.coalesce(func.sum(CmsProofOfPlay.duration_ms), 0).label("dur"),
        ).where(
            CmsProofOfPlay.id_org == id_org,
            CmsProofOfPlay.played_at >= since,
            _COUNTED,
            *_scope_cond,
        )
    )).one()

    # Plays par jour
    _day = func.date_trunc(literal_column("'day'"), CmsProofOfPlay.played_at)
    day_rows = (await db.execute(
        select(
            _day.label("day"),
            func.count(CmsProofOfPlay.id_pop).label("cnt"),
            func.coalesce(func.sum(CmsProofOfPlay.duration_ms), 0).label("dur"),
        )
        .where(
            CmsProofOfPlay.id_org == id_org,
            CmsProofOfPlay.played_at >= since,
            _COUNTED,
            *_scope_cond,
        )
        .group_by(_day)
        .order_by(_day)
    )).all()

    # Top 5 médias par nombre de lectures
    media_rows = (await db.execute(
        select(
            CmsProofOfPlay.id_media,
            CmsMedia.media_name,
            func.count(CmsProofOfPlay.id_pop).label("cnt"),
        )
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media, isouter=True)
        .where(
            CmsProofOfPlay.id_org == id_org,
            CmsProofOfPlay.played_at >= since,
            CmsProofOfPlay.id_media.is_not(None),
            _COUNTED,
            *_scope_cond,
        )
        .group_by(CmsProofOfPlay.id_media, CmsMedia.media_name)
        .order_by(desc("cnt"))
        .limit(5)
    )).all()

    return PopAnalyticsResponse(
        period_days=days,
        total_plays=total_row.cnt,
        total_duration_ms=total_row.dur,
        plays_by_day=[
            PopDayStats(
                date=row.day.strftime("%Y-%m-%d") if hasattr(row.day, "strftime") else str(row.day)[:10],
                count=row.cnt,
                duration_ms=row.dur,
            )
            for row in day_rows
        ],
        top_media=[
            PopTopItem(
                id=row.id_media,
                name=row.media_name or f"#{row.id_media}",
                count=row.cnt,
            )
            for row in media_rows
        ],
    )


@router.get("/top-by-type", response_model=PopTopByTypeResponse)
async def pop_top_by_type(
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(3, ge=1, le=10),
) -> PopTopByTypeResponse:
    """Top N contenus les plus diffusés par type de média sur les N derniers jours.

    Périmètre de gouvernance (D1 étendue) : le palmarès ne doit pas révéler ce qui passe
    sur les écrans d'un autre nœud.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    _scope = await analytics.scoped_report_players(db, current_user)
    _scope_cond = [CmsProofOfPlay.id_player.in_(_scope)] if _scope is not None else []
    rows = (await db.execute(
        select(
            CmsProofOfPlay.id_media,
            CmsMedia.media_name,
            CmsMedia.media_type,
            func.count(CmsProofOfPlay.id_pop).label("cnt"),
        )
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media)
        .where(
            CmsProofOfPlay.id_org == current_user.id_org,
            CmsProofOfPlay.played_at >= since,
            CmsProofOfPlay.id_media.is_not(None),
            _COUNTED,
            *_scope_cond,
        )
        .group_by(CmsProofOfPlay.id_media, CmsMedia.media_name, CmsMedia.media_type)
        .order_by(desc("cnt"))
    )).all()

    # Regroupe par type, garde le top `limit` de chaque (rows déjà triées par count desc).
    by_type: dict[str, list[PopTopItem]] = {}
    for row in rows:
        bucket = by_type.setdefault(row.media_type or "other", [])
        if len(bucket) < limit:
            bucket.append(PopTopItem(id=row.id_media, name=row.media_name or f"#{row.id_media}", count=row.cnt))

    return PopTopByTypeResponse(
        period_days=days,
        groups=[PopTypeGroup(media_type=mt, items=items) for mt, items in by_type.items()],
    )


# ── P1 : vérification de chaîne + attestation certifiée ─────────────────────────

async def _player_keys(db: AsyncSession, id_org: int) -> dict[int, str | None]:
    """id_player → clé publique PoP (pour vérifier les signatures de l'org)."""
    rows = (await db.execute(
        select(CmsPlayer.id_player, CmsPlayer.pop_public_key).where(CmsPlayer.id_org == id_org)
    )).all()
    return {r.id_player: r.pop_public_key for r in rows}


def _recompute_hash(prev: str, id_player: int, row) -> str:
    played_at_str = row.played_at.astimezone(timezone.utc).isoformat()
    return _next_hash(prev, id_player, row.id_media, played_at_str, row.duration_ms)


@router.get("/verify", response_model=PopVerifyResponse)
async def pop_verify(
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=365),
) -> PopVerifyResponse:
    """Vérifie l'intégrité du Proof of Play de l'org sur N jours : (a) la chaîne de hash est-elle
    continue et recalculable, (b) les signatures Ed25519 sont-elles valides. Merkle root agrégé."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    keys = await _player_keys(db, current_user.id_org)
    rows = (await db.execute(
        select(CmsProofOfPlay).where(
            CmsProofOfPlay.id_org == current_user.id_org,
            CmsProofOfPlay.played_at >= since,
        ).order_by(CmsProofOfPlay.id_player, CmsProofOfPlay.id_pop)
    )).scalars().all()

    chain_valid, broken_at = True, None
    signed = unsigned = sig_invalid = 0
    prev_by_player: dict[int, str] = {}
    for r in rows:
        prev = prev_by_player.get(r.id_player, f"genesis:{r.id_player}")
        if _recompute_hash(prev, r.id_player, r) != r.merkle_hash and chain_valid:
            chain_valid, broken_at = False, r.id_pop
        prev_by_player[r.id_player] = r.merkle_hash
        if r.signature:
            key = keys.get(r.id_player)
            msg = pop_proof.canonical_message(
                r.id_player, r.id_media, r.id_playlist,
                pop_proof.played_at_ms(r.played_at), r.duration_ms,
            )
            if key and pop_proof.verify_signature(key, r.signature, msg):
                signed += 1
            else:
                sig_invalid += 1
        else:
            unsigned += 1

    # Fenêtre vérifiable : après DROP des partitions archivées, la chaîne ne se recompose
    # qu'à partir du premier jour non archivé (le froid est attesté par son merkle_root).
    chain_from = (await db.execute(
        select(func.max(CmsPopArchive.period_end)).where(
            CmsPopArchive.id_org == current_user.id_org)
    )).scalar_one_or_none()

    return PopVerifyResponse(
        period_days=days, total=len(rows), chain_valid=chain_valid, broken_at_id=broken_at,
        signed=signed, unsigned=unsigned, signature_invalid=sig_invalid,
        merkle_root=pop_proof.merkle_root([r.merkle_hash for r in rows]),
        chain_from=chain_from.isoformat() if chain_from else None,
    )


@router.get("/certificate", response_model=PopCertificateResponse)
async def pop_certificate(
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date = Query(..., alias="from"),
    until: date = Query(...),
) -> PopCertificateResponse:
    """Attestation certifiée de diffusion sur [from, until] : Merkle root global + tête de chaîne
    et compteurs signés/non-signés par player. Artefact vérifiable à fournir à un annonceur."""
    start = datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(until, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    rows = (await db.execute(
        select(CmsProofOfPlay).where(
            CmsProofOfPlay.id_org == current_user.id_org,
            CmsProofOfPlay.played_at >= start,
            CmsProofOfPlay.played_at < end,
        ).order_by(CmsProofOfPlay.id_player, CmsProofOfPlay.id_pop)
    )).scalars().all()

    # La chaîne et le Merkle root portent sur TOUTES les lignes (sinon l'attestation ne
    # serait plus recalculable par un tiers) ; seuls les COMPTEURS d'antenne excluent les
    # preuves non vues — écran coupé (migr 0102) ou zone recouverte (migr 0125) — exposées
    # à part pour que le certificat reste explicite plutôt que silencieusement différent.
    per_player: dict[int, dict] = {}
    total_screen_off = 0
    total_covered = 0
    for r in rows:
        p = per_player.setdefault(
            r.id_player, {"id_player": r.id_player, "plays": 0, "screen_off": 0,
                          "covered": 0, "chain_head": None, "signed": 0, "unsigned": 0}
        )
        p["chain_head"] = r.merkle_hash  # dernier hash vu = tête de chaîne du player sur la période
        # Une preuve peut cumuler les deux causes (écran éteint ET zone recouverte) : on la
        # compte dans chacune — ce sont des diagnostics, pas une partition — mais on ne la
        # déduit qu'UNE fois de l'antenne, d'où le compteur `not_counted` séparé.
        not_counted = False
        if r.screen_off:
            p["screen_off"] += 1
            total_screen_off += 1
            not_counted = True
        if r.covered:
            p["covered"] += 1
            total_covered += 1
            not_counted = True
        if not_counted:
            continue
        p["plays"] += 1
        p["signed" if r.signature else "unsigned"] += 1

    return PopCertificateResponse(
        id_org=current_user.id_org,
        from_date=from_.isoformat(),
        until_date=until.isoformat(),
        total_plays=sum(p["plays"] for p in per_player.values()),
        total_screen_off=total_screen_off,
        total_covered=total_covered,
        merkle_root=pop_proof.merkle_root([r.merkle_hash for r in rows]),
        players=list(per_player.values()),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Consultation / export par player (menu Rapports → Proof of Play) ─────────────

def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.SIGNFLOW_TZ)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def _day_bounds_utc(from_d: date, until_d: date) -> tuple[datetime, datetime]:
    """Bornes [début from, fin until] en UTC, jours définis en SIGNFLOW_TZ (until inclus)."""
    tz = _tz()
    start = datetime.combine(from_d, time.min, tzinfo=tz)
    end = datetime.combine(until_d + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# Définition UNIQUE dans `services/csv_safety` : la fonction vivait en trois copies
# (pop, bi_export, reports) qui se citaient l'une l'autre en commentaire. L'alias est
# conservé pour ne rien changer aux appels de ce fichier.
_csv_safe = csv_safety.csv_safe


# Résolution des filtres de cible : source UNIQUE partagée avec les rapports §XXIV
# (`analytics.resolve_player_ids`, qui délègue au targeting canonique). pop.py en avait
# un clone à l'identique — donc les deux ignoraient `site_group` (audit ciblage 2026-07-18).
_resolve_player_ids = analytics.resolve_player_ids


def _media_type_cond(media_type: str | None):
    """Condition SQL du filtre « type de média » de /plays et /plays.csv (None = pas de filtre).

    Nécessite la jointure CmsMedia (LEFT : une preuve dont le média a disparu reste comptée
    côté « visual », jamais perdue silencieusement). Familles audio (retour Ed 17/07 — le
    panier ambiance noyait la lecture du rapport) : la preuve ne stocke pas le panier, on
    classe par APPARTENANCE du média aux paniers — `audio_ambiance` = média présent dans au
    moins un panier ambiance ; `audio_standard` = le reste de l'audio (musique/jingle/message/
    alerte/fallback). Un média à la fois en panier ambiance ET musique compte côté ambiance.
    """
    is_ambiance = (
        select(CmsAudioBasketItem.id_item)
        .join(CmsAudioBasket, CmsAudioBasket.id_basket == CmsAudioBasketItem.id_basket)
        .where(
            CmsAudioBasketItem.id_media == CmsProofOfPlay.id_media,
            CmsAudioBasket.basket_type == "ambiance",
        )
    ).exists()
    if media_type == "audio":
        return CmsMedia.media_type == "audio"
    if media_type == "audio_ambiance":
        return (CmsMedia.media_type == "audio") & is_ambiance
    if media_type == "audio_standard":
        return (CmsMedia.media_type == "audio") & ~is_ambiance
    if media_type == "visual":
        return func.coalesce(CmsMedia.media_type, "") != "audio"
    return None


@router.get("/plays", response_model=PopPlaysResponse)
async def pop_plays(
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date = Query(..., alias="from"),
    until: date = Query(...),
    id_players: Annotated[list[int], Query()] = [],
    id_group: int | None = Query(None),
    id_site: int | None = Query(None),
    id_site_group: int | None = Query(None),
    media_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PopPlaysResponse:
    """Liste paginée des preuves de diffusion (filtre player(s)/groupe/site + période).

    `media_type` : `audio` = preuves des paniers audio §XIX (S1 PoP audio) ; `audio_standard`
    / `audio_ambiance` = sous-familles par appartenance aux paniers (cf. `_media_type_cond`) ;
    `visual` = tout le reste (image/vidéo/document/…, médias supprimés inclus). Absent = tout."""
    start, end = _day_bounds_utc(from_, until)
    # Périmètre de gouvernance (D1 étendue au menu Rapports, 31/07) : le Proof of Play
    # NOMME l'écran et le contenu — c'est la preuve de diffusion du réseau. Le filtre
    # demandé est intersecté avec le périmètre (`id_players` vient du client).
    player_ids = await analytics.scoped_report_players(
        db, current_user, id_players, id_group, id_site, id_site_group)

    conds = [
        CmsProofOfPlay.id_org == current_user.id_org,
        CmsProofOfPlay.played_at >= start,
        CmsProofOfPlay.played_at < end,
    ]
    if player_ids is not None:
        conds.append(CmsProofOfPlay.id_player.in_(player_ids))

    media_cond = _media_type_cond(media_type)

    count_q = select(func.count(CmsProofOfPlay.id_pop)).where(*conds)
    if media_cond is not None:
        count_q = count_q.join(
            CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media, isouter=True
        ).where(media_cond)
    total = (await db.execute(count_q)).scalar_one()

    rows_q = (
        select(
            CmsProofOfPlay.id_pop, CmsProofOfPlay.id_player, CmsPlayer.player_name,
            CmsProofOfPlay.id_media, CmsMedia.media_name, CmsProofOfPlay.played_at,
            CmsProofOfPlay.duration_ms, CmsProofOfPlay.signature, CmsProofOfPlay.merkle_hash,
            CmsMedia.media_type, CmsProofOfPlay.screen_off, CmsProofOfPlay.covered,
        )
        .join(CmsPlayer, CmsPlayer.id_player == CmsProofOfPlay.id_player, isouter=True)
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media, isouter=True)
        .where(*conds)
        .order_by(CmsProofOfPlay.played_at.desc(), CmsProofOfPlay.id_pop.desc())
        .limit(limit).offset(offset)
    )
    if media_cond is not None:
        rows_q = rows_q.where(media_cond)
    rows = (await db.execute(rows_q)).all()

    return PopPlaysResponse(
        total=total, limit=limit, offset=offset,
        rows=[
            PopPlayRow(
                id_pop=r.id_pop, id_player=r.id_player, player_name=r.player_name,
                id_media=r.id_media, media_name=r.media_name, played_at=r.played_at,
                duration_ms=r.duration_ms, signed=r.signature is not None, merkle_hash=r.merkle_hash,
                is_document=(r.media_type == "document"),
                screen_off=bool(r.screen_off),
                covered=bool(r.covered),
            )
            for r in rows
        ],
    )


@router.get(
    "/plays.csv",
    dependencies=[Depends(require_admin_or_permission("reports", "export"))],
)
async def pop_plays_csv(
    current_user: Annotated[CmsUser, Depends(deny_demo)],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date = Query(..., alias="from"),
    until: date = Query(...),
    id_players: Annotated[list[int], Query()] = [],
    id_group: int | None = Query(None),
    id_site: int | None = Query(None),
    id_site_group: int | None = Query(None),
    media_type: str | None = Query(None),
) -> Response:
    """Export CSV des preuves (mêmes filtres que /plays, sans pagination)."""
    start, end = _day_bounds_utc(from_, until)
    # Périmètre de gouvernance (D1 étendue au menu Rapports, 31/07) : le Proof of Play
    # NOMME l'écran et le contenu — c'est la preuve de diffusion du réseau. Le filtre
    # demandé est intersecté avec le périmètre (`id_players` vient du client).
    player_ids = await analytics.scoped_report_players(
        db, current_user, id_players, id_group, id_site, id_site_group)

    conds = [
        CmsProofOfPlay.id_org == current_user.id_org,
        CmsProofOfPlay.played_at >= start,
        CmsProofOfPlay.played_at < end,
    ]
    if player_ids is not None:
        conds.append(CmsProofOfPlay.id_player.in_(player_ids))
    csv_media_cond = _media_type_cond(media_type)
    if csv_media_cond is not None:
        conds.append(csv_media_cond)

    rows = (await db.execute(
        select(
            CmsProofOfPlay.id_player, CmsPlayer.player_name, CmsMedia.media_name,
            CmsProofOfPlay.played_at, CmsProofOfPlay.duration_ms,
            CmsProofOfPlay.signature, CmsProofOfPlay.merkle_hash,
            CmsProofOfPlay.screen_off, CmsProofOfPlay.covered,
        )
        .join(CmsPlayer, CmsPlayer.id_player == CmsProofOfPlay.id_player, isouter=True)
        .join(CmsMedia, CmsMedia.id_media == CmsProofOfPlay.id_media, isouter=True)
        .where(*conds)
        .order_by(CmsProofOfPlay.played_at.desc(), CmsProofOfPlay.id_pop.desc())
    )).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    # Ecran_coupe / Zone_recouverte : la ligne est exportée (preuve à valeur de trace) mais
    # signale qu'elle n'entre dans aucun total d'antenne. Les deux causes sont côte à côte —
    # cet export est ouvert par un humain dans un tableur, l'en-tête est là pour le guider.
    # (⚠️ Le CSV du connecteur BI, lui, est un contrat MACHINE : on y ajoute en FIN de
    # colonnes, cf. bi_export.)
    # Langue du profil du demandeur (en-têtes + oui/non) ; défaut anglais.
    loc = current_user.locale
    writer.writerow(pop_csv_header(loc))
    yes, no = tr(POP_TEXTS, loc, "yes"), tr(POP_TEXTS, loc, "no")
    for r in rows:
        writer.writerow([
            _csv_safe(r.player_name), _csv_safe(r.media_name),
            r.played_at.astimezone(timezone.utc).isoformat(),
            round((r.duration_ms or 0) / 1000),
            yes if r.signature else no,
            yes if r.screen_off else no,
            yes if r.covered else no,
            _csv_safe(r.merkle_hash),
        ])
    filename = f"proof_of_play_{from_.isoformat()}_{until.isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/report.pdf",
    dependencies=[Depends(require_admin_or_permission("reports", "export"))],
)
async def pop_report_pdf(
    current_user: Annotated[CmsUser, Depends(deny_demo)],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_: date = Query(..., alias="from"),
    until: date = Query(...),
    id_players: Annotated[list[int], Query()] = [],
    id_group: int | None = Query(None),
    id_site: int | None = Query(None),
    id_site_group: int | None = Query(None),
) -> Response:
    """Document PDF unique et organisé : résumé global + une section par player (intégrité de
    chaîne, signatures, Merkle root). Sélection libre / par groupe / par site."""
    start, end = _day_bounds_utc(from_, until)
    # Périmètre de gouvernance (D1 étendue au menu Rapports, 31/07) : le Proof of Play
    # NOMME l'écran et le contenu — c'est la preuve de diffusion du réseau. Le filtre
    # demandé est intersecté avec le périmètre (`id_players` vient du client).
    player_ids = await analytics.scoped_report_players(
        db, current_user, id_players, id_group, id_site, id_site_group)

    conds = [
        CmsProofOfPlay.id_org == current_user.id_org,
        CmsProofOfPlay.played_at >= start,
        CmsProofOfPlay.played_at < end,
    ]
    if player_ids is not None:
        conds.append(CmsProofOfPlay.id_player.in_(player_ids))

    rows = (await db.execute(
        select(CmsProofOfPlay).where(*conds)
        .order_by(CmsProofOfPlay.id_player, CmsProofOfPlay.id_pop)
    )).scalars().all()

    keys = await _player_keys(db, current_user.id_org)
    names = {
        r.id_player: r.player_name for r in (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name).where(
                CmsPlayer.id_org == current_user.id_org)
        )).all()
    }
    org_name = (await db.execute(
        select(CmsOrganization.org_name).where(CmsOrganization.id_org == current_user.id_org)
    )).scalar_one_or_none() or f"Org #{current_user.id_org}"

    # Agrégat par player (intégrité de chaîne + signatures, calqué sur /pop/verify scopé player).
    # ⚠️ La chaîne se recalcule sur TOUTES les preuves — y compris celles jouées écran coupé,
    # sinon le recalcul divergerait et le PDF conclurait à tort « chaîne altérée ». Seuls les
    # compteurs d'antenne (plays / duration_sec) les excluent ; elles sont comptées à part.
    per: dict[int, dict] = {}
    prev_by_player: dict[int, str] = {}
    total_screen_off = 0
    total_covered = 0
    for r in rows:
        p = per.setdefault(r.id_player, {
            "id_player": r.id_player, "player_name": names.get(r.id_player) or f"Player #{r.id_player}",
            "plays": 0, "duration_sec": 0, "screen_off": 0, "covered": 0,
            "signed": 0, "unsigned": 0, "signature_invalid": 0,
            "chain_valid": True, "chain_head": None, "hashes": [],
        })
        # Deux causes de non-comptage, qui peuvent se cumuler sur la même preuve : on les
        # diagnostique séparément, on ne retire la ligne de l'antenne qu'une fois.
        if r.screen_off:
            p["screen_off"] += 1
            total_screen_off += 1
        if r.covered:
            p["covered"] += 1
            total_covered += 1
        if not (r.screen_off or r.covered):
            p["plays"] += 1
            p["duration_sec"] += round((r.duration_ms or 0) / 1000)
        p["chain_head"] = r.merkle_hash
        p["hashes"].append(r.merkle_hash)
        prev = prev_by_player.get(r.id_player, f"genesis:{r.id_player}")
        if p["chain_valid"] and _recompute_hash(prev, r.id_player, r) != r.merkle_hash:
            p["chain_valid"] = False
        prev_by_player[r.id_player] = r.merkle_hash
        if r.signature:
            msg = pop_proof.canonical_message(
                r.id_player, r.id_media, r.id_playlist,
                pop_proof.played_at_ms(r.played_at), r.duration_ms,
            )
            key = keys.get(r.id_player)
            if key and pop_proof.verify_signature(key, r.signature, msg):
                p["signed"] += 1
            else:
                p["signature_invalid"] += 1
        else:
            p["unsigned"] += 1

    players_report = [
        {**{k: v for k, v in p.items() if k != "hashes"},
         "merkle_root": pop_proof.merkle_root(p["hashes"])}
        for p in per.values()
    ]

    loc = current_user.locale
    if id_group:
        scope = tr(POP_TEXTS, loc, "scope_group", id=id_group)
    elif id_site:
        scope = tr(POP_TEXTS, loc, "scope_site", id=id_site)
    elif player_ids is not None:
        scope = tr(POP_TEXTS, loc, "scope_selected", n=len(player_ids))
    else:
        scope = tr(POP_TEXTS, loc, "scope_all")

    report = {
        "org_name": org_name,
        "from_date": from_.isoformat(),
        "until_date": until.isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        # Somme des compteurs par player, JAMAIS `len(rows) - total_screen_off` : une preuve
        # peut porter les deux causes et serait alors déduite deux fois.
        "total_plays": sum(p["plays"] for p in per.values()),
        "total_screen_off": total_screen_off,
        "total_covered": total_covered,
        "merkle_root": pop_proof.merkle_root([r.merkle_hash for r in rows]),
        "scope_label": scope,
        "players": players_report,
    }
    pdf = build_pop_report_pdf(report, locale=loc)
    filename = f"proof_of_play_{from_.isoformat()}_{until.isoformat()}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/players/{id_player}", status_code=status.HTTP_200_OK)
async def purge_player_pop(
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Purge TOUTES les preuves de diffusion (PoP) d'un player, SANS supprimer le player.
    Destructif (org-admin, RLS, audité). La clé publique du device (`pop_public_key`) est
    conservée : le player continue de signer, la chaîne repart proprement au prochain lot."""
    player = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_player == id_player,
            CmsPlayer.id_org == current_user.id_org,
        )
    )).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player not found")

    res = await db.execute(
        delete(CmsProofOfPlay).where(
            CmsProofOfPlay.id_player == id_player,
            CmsProofOfPlay.id_org == current_user.id_org,
        )
    )
    deleted = res.rowcount or 0
    db.add(CmsAuditLog(
        id_org=current_user.id_org,
        id_user=current_user.id_user,
        action="pop_purged",
        resource_type="player",
        resource_id=str(id_player),
        detail={"deleted": deleted},
        source="cms",
    ))
    await db.commit()
    return {"deleted": deleted}


# ── Archives (froid MinIO) — menu Rapports → Proof of Play → onglet Archives ──────

@router.get("/archives", response_model=PopArchivesResponse)
async def pop_archives(
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PopArchivesResponse:
    """Liste des périodes de Proof of Play archivées hors base (index `cms_pop_archives`)."""
    total = (await db.execute(
        select(func.count(CmsPopArchive.id_archive)).where(
            CmsPopArchive.id_org == current_user.id_org)
    )).scalar_one()
    rows = (await db.execute(
        select(CmsPopArchive)
        .where(CmsPopArchive.id_org == current_user.id_org)
        .order_by(CmsPopArchive.period_start.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return PopArchivesResponse(
        total=total,
        rows=[
            PopArchiveRow(
                id_archive=a.id_archive,
                period_start=a.period_start.isoformat(),
                period_end=a.period_end.isoformat(),
                row_count=a.row_count,
                merkle_root=a.merkle_root,
                has_pdf=bool(a.pdf_key),
                bytes_csv=a.bytes_csv,
                players=a.players or [],
                created_at=a.created_at,
            )
            for a in rows
        ],
    )


async def _get_archive_or_404(db: AsyncSession, id_org: int, id_archive: int) -> CmsPopArchive:
    archive = (await db.execute(
        select(CmsPopArchive).where(
            CmsPopArchive.id_archive == id_archive,
            CmsPopArchive.id_org == id_org,
        )
    )).scalar_one_or_none()
    if archive is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="archive not found")
    return archive


@router.get("/archives/{id_archive}/download")
async def pop_archive_download(
    id_archive: int,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "export"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    kind: str = Query("csv", pattern="^(csv|pdf)$"),
) -> dict[str, str]:
    """URL pré-signée forçant le téléchargement du CSV.gz (lignes brutes) ou du PDF
    d'attestation d'une archive. 404 si le PDF n'a pas été archivé."""
    archive = await _get_archive_or_404(db, current_user.id_org, id_archive)
    if kind == "pdf":
        if not archive.pdf_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pdf not archived")
        name = f"proof_of_play_{archive.period_start.isoformat()}.pdf"
        return {"url": get_signed_url(archive.pdf_key, download_name=name)}
    name = f"proof_of_play_{archive.period_start.isoformat()}.csv.gz"
    return {"url": get_signed_url(archive.csv_key, download_name=name)}


@router.post("/archives/{id_archive}/verify", response_model=PopArchiveVerifyResponse)
async def pop_archive_verify(
    id_archive: int,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("reports", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PopArchiveVerifyResponse:
    """Vérifie l'intégrité d'une archive hors ligne : re-télécharge le CSV.gz, recalcule le
    merkle_root + le nombre de lignes, compare aux valeurs stockées dans l'index."""
    archive = await _get_archive_or_404(db, current_user.id_org, id_archive)
    data = await download_file(archive.csv_key)
    computed_root, computed_count = pop_archive.merkle_root_from_csv_gz(data)
    ok = (computed_root == archive.merkle_root) and (computed_count == archive.row_count)
    return PopArchiveVerifyResponse(
        id_archive=id_archive,
        ok=ok,
        stored_merkle_root=archive.merkle_root,
        computed_merkle_root=computed_root,
        stored_row_count=archive.row_count,
        computed_row_count=computed_count,
    )
