"""
Endpoints Media Library + Stock Library (§XVI.2).

POST  /media/upload          — upload multipart → cms_media + task Celery
GET   /media                 — liste paginée (filtres : media_type, source, q)
GET   /media/stock/search    — recherche Unsplash / Pexels
POST  /media/stock/import    — import stock → cms_media
GET   /media/{id_media}      — détail + URL signée TTL 1h
DELETE /media/{id_media}     — suppression logique + cleanup MinIO async
"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.deps import get_db, require_permission
from app.core.ssrf import url_is_safe
from app.models.audit_log import CmsAuditLog
from app.models.event import CmsEvent
from app.models.feed import CmsDatafeed
from app.models.governance import CmsGovernanceNode
from app.models.media import CmsMedia, CmsMediaRendition
from app.models.player import CmsPlayer
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.user import CmsUser
from app.schemas.media import (
    DocumentPageRead,
    DocumentPagesResult,
    HdmiInputCreate,
    IsrcLookupResult,
    MediaEmbargoResult,
    MediaEmbargoUpdate,
    MediaEncodeDecision,
    MediaEncodeDecisionResult,
    MediaEncodeModeUpdate,
    MediaGovernanceNodeUpdate,
    MediaListItem,
    MediaPassthroughAck,
    MediaPassthroughAckResult,
    MediaQuarantineRequest,
    MediaQuarantineResult,
    MediaRead,
    MediaRenameUpdate,
    MediaRightsUpdate,
    MediaStats,
    RenditionGenerateRequest,
    RenditionRead,
    StockImportRequest,
    StockSearchResult,
)
from app.services import encode_progress
from app.services import rbac
from app.services import media_deletion
from app.services import media_usage as media_usage_service
from app.services.governance_access import (
    editable_node_ids,
    media_visibility,
    user_can_detach_from_node,
    user_can_edit_node,
    user_is_governance_scoped,
    visible_node_ids,
)
from app.services.media import (
    compute_sha256,
    get_signed_url,
    make_storage_key,
    stream_upload,
    upload_file,
)

router = APIRouter(prefix="/media", tags=["media"])


# ── Gouvernance §XVIII du CONTENU — médias (R14/R15, D9) ─────────────────────────
async def _enforce_media_node_edit(db: AsyncSession, user: CmsUser, media: CmsMedia) -> None:
    """403 si l'utilisateur n'administre pas le nœud du média (NULL = libre). D9 :
    un responsable local ne modifie/supprime pas un média national."""
    if not await user_can_edit_node(db, user, media.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer this media's governance node",
        )


async def _validate_media_node_org(db: AsyncSession, user: CmsUser, id_node: int | None) -> None:
    if id_node is None:
        return
    node = (await db.execute(
        select(CmsGovernanceNode.id_node).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Governance node not found")

# Domaines autorisés pour l'import stock (anti-SSRF) : on ne télécharge que depuis les CDN
# du provider choisi, jamais une URL arbitraire fournie par le client.
_STOCK_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "unsplash": ("unsplash.com",),
    "pexels": ("pexels.com",),
}

# Prefix → media_type mapping. Keys ending with "/" are prefix-matched; others are exact.
_MIME_TO_TYPE: dict[str, str] = {
    "image/": "image",
    "video/": "video",
    "audio/": "audio",
    "application/pdf": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "text/html": "html_package",
    "application/zip": "html_package",
}


# Repli par extension — les navigateurs/OS mappent mal certains conteneurs.
# Ex: un .ogg AUDIO est souvent annoncé "application/ogg" ou "video/ogg" (Ogg est
# un conteneur pouvant porter de la vidéo) → le préfixe "audio/" ne matche pas.
_EXT_TO_TYPE: dict[str, str] = {
    # audio
    ".mp3": "audio", ".aac": "audio", ".wav": "audio", ".ogg": "audio", ".oga": "audio",
    ".opus": "audio", ".flac": "audio", ".m4a": "audio", ".weba": "audio", ".wma": "audio",
    # vidéo
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video", ".avi": "video",
    ".m4v": "video", ".ogv": "video", ".wmv": "video",
    # image
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image",
    ".svg": "image", ".bmp": "image", ".avif": "image",
    # document
    ".pdf": "document", ".ppt": "document", ".pptx": "document",
    # package HTML
    ".zip": "html_package", ".html": "html_package", ".htm": "html_package",
}

# MIME ambigus/peu fiables → on privilégie l'extension dans ces cas.
_AMBIGUOUS_MIME = {"", "application/octet-stream", "application/ogg", "video/ogg", "application/x-ogg"}


def _detect_media_type(content_type: str, filename: str | None = None) -> str | None:
    """Détermine le media_type CMS depuis le MIME, avec repli par extension.

    Le MIME du navigateur est peu fiable pour .ogg/.opus/.flac (souvent annoncés
    application/ogg ou octet-stream) → on retombe sur l'extension du fichier.
    """
    # 1. MIME exact / préfixe — sauf si ambigu
    if content_type not in _AMBIGUOUS_MIME:
        if content_type in _MIME_TO_TYPE:
            return _MIME_TO_TYPE[content_type]
        for prefix, mtype in _MIME_TO_TYPE.items():
            if prefix.endswith("/") and content_type.startswith(prefix):
                return mtype
    # 2. Repli par extension du fichier
    if filename and "." in filename:
        ext = filename[filename.rfind("."):].lower()
        if ext in _EXT_TO_TYPE:
            return _EXT_TO_TYPE[ext]
    return None


# ── Upload ─────────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def upload_media(
    file: UploadFile,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    not_before_utc: Optional[str] = Form(None),
    embargo_event_id: Optional[int] = Form(None),
    encode_mode: Optional[str] = Form("auto"),
) -> MediaRead:
    """Upload un fichier vers MinIO en streaming (multipart ≥8MB), crée l'entrée cms_media."""
    if not file.filename and not file.size:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    content_type = file.content_type or "application/octet-stream"
    media_type = _detect_media_type(content_type, file.filename)
    if media_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type: {content_type}. "
                "Accepted types: images (JPEG, PNG, GIF, WebP, SVG), "
                "videos (MP4, MOV, WebM), audio (MP3, AAC, WAV), "
                "documents (PDF, PPTX), HTML packages (ZIP, HTML)."
            ),
        )
    storage_key = make_storage_key(current_user.id_org, file.filename or "upload")

    sha256, file_size = await stream_upload(file.file, storage_key, content_type)

    parsed_embargo: Optional[datetime] = None
    if not_before_utc:
        try:
            parsed_embargo = datetime.fromisoformat(not_before_utc.replace("Z", "+00:00"))
        except ValueError:
            pass

    media = CmsMedia(
        id_org=current_user.id_org,
        media_name=file.filename or storage_key,
        media_type=media_type,
        mime_type=content_type,
        storage_key=storage_key,
        file_size_bytes=file_size,
        checksum_sha256=sha256,
        source="upload",
        transcode_status="pending",
        # passthrough = conserver le fichier tel quel (vidéo/audio seulement) ; sinon auto
        encode_mode="passthrough" if (encode_mode == "passthrough" and media_type in ("video", "audio")) else "auto",
        created_by=current_user.id_user,
        not_before_utc=parsed_embargo,
        embargo_event_id=embargo_event_id,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    # Dispatch tâche de transcodage
    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)

    result = MediaRead.model_validate(media)
    result.cdn_url_signed = get_signed_url(storage_key)
    return result


# ── Liste ──────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=MediaStats)
async def media_stats(
    _: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    source: Optional[str] = Query(None),
    id_folder: Optional[int] = Query(None, description="Filter on this folder"),
    unfiled: bool = Query(False, description="Unfiled media (id_folder IS NULL)"),
) -> MediaStats:
    """Comptes par type sur TOUTE la bibliothèque (indépendant de la pagination)."""
    stmt = select(CmsMedia.media_type, func.count()).where(CmsMedia.is_deleted == False)  # noqa: E712
    if source:
        stmt = stmt.where(CmsMedia.source == source)
    if unfiled:
        stmt = stmt.where(CmsMedia.id_folder.is_(None))
    elif id_folder is not None:
        stmt = stmt.where(CmsMedia.id_folder == id_folder)
    stmt = stmt.group_by(CmsMedia.media_type)
    rows = (await db.execute(stmt)).all()
    by_type = {media_type: count for media_type, count in rows}
    return MediaStats(total=sum(by_type.values()), by_type=by_type)


@router.get("/", response_model=list[MediaListItem])
async def list_media(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    media_type: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    id_folder: Optional[int] = Query(None, description="Filter on this folder"),
    subtree: bool = Query(False, description="With id_folder: include subfolders"),
    unfiled: bool = Query(False, description="Unfiled media (id_folder IS NULL)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
) -> list[MediaListItem]:
    # Gouvernance §XVIII (D6) : lecture filtrée — médias d'org (nœud NULL) + nœuds visibles
    # (éditables + ancêtres). None = org-admin → aucun filtre. Décision d'Ed 2026-07-29 : la
    # médiathèque non gouvernée reste LISIBLE pour les rôles scopés (cf. `media_visibility`) —
    # ils la programment sans pouvoir la modifier.
    gov_clause, visible, editable = await media_visibility(db, current_user)
    scoped = await user_is_governance_scoped(db, current_user)
    stmt = select(CmsMedia).where(CmsMedia.is_deleted == False)  # noqa: E712
    _clause = gov_clause(CmsMedia.id_governance_node)
    if _clause is not None:
        stmt = stmt.where(_clause)
    if media_type:
        stmt = stmt.where(CmsMedia.media_type == media_type)
    if source:
        stmt = stmt.where(CmsMedia.source == source)
    if q:
        stmt = stmt.where(CmsMedia.media_name.ilike(f"%{q}%"))
    if unfiled:
        stmt = stmt.where(CmsMedia.id_folder.is_(None))
    elif id_folder is not None and subtree:
        # Sous-arbre du dossier (path matérialisé) — ex. picker de créatifs par annonceur.
        from app.models.media_folder import CmsMediaFolder
        folder = (await db.execute(
            select(CmsMediaFolder).where(CmsMediaFolder.id_folder == id_folder)
        )).scalar_one_or_none()
        if folder is None:
            return []
        sub_ids = (await db.execute(
            select(CmsMediaFolder.id_folder).where(
                CmsMediaFolder.path.like(f"{folder.path}.%"))
        )).scalars().all()
        stmt = stmt.where(CmsMedia.id_folder.in_([id_folder, *sub_ids]))
    elif id_folder is not None:
        stmt = stmt.where(CmsMedia.id_folder == id_folder)
    stmt = stmt.order_by(CmsMedia.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    rows = list(result.scalars())
    # Récupère les noms d'events pour les médias avec embargo_event_id
    event_ids = {m.embargo_event_id for m in rows if m.embargo_event_id}
    event_names: dict[int, str] = {}
    if event_ids:
        ev_rows = (await db.execute(
            select(CmsEvent.id_event, CmsEvent.event_name).where(
                CmsEvent.id_event.in_(event_ids)
            )
        )).all()
        event_names = {r.id_event: r.event_name for r in ev_rows}
    # Tags par média (§XVII) — batché pour les badges de la Media Library
    from app.models.tag import CmsMediaTagValue
    media_ids = [m.id_media for m in rows]
    tags_by_media: dict[int, list[int]] = {}
    if media_ids:
        for mid, id_tag in (await db.execute(
            select(CmsMediaTagValue.id_media, CmsMediaTagValue.id_tag)
            .where(CmsMediaTagValue.id_media.in_(media_ids))
        )).all():
            tags_by_media.setdefault(mid, []).append(id_tag)
    items = [MediaListItem.model_validate(m) for m in rows]
    for item, m in zip(items, rows):
        if item.thumbnail_url:
            item.thumbnail_url = get_signed_url(item.thumbnail_url, ttl=3600)
        if item.embargo_event_id:
            item.embargo_event_name = event_names.get(item.embargo_event_id)
        item.tags = tags_by_media.get(item.id_media, [])
        # Taille du fichier réellement servi (après re-encode), persistée dans le metadata.
        item.served_size_bytes = (m.media_metadata or {}).get("size_bytes")
        if m.transcode_status == "awaiting_decision":
            item.encode_decision = (m.media_metadata or {}).get("encode_decision")
        # Média pointeur de feed → expose id_feed pour ouvrir le bon éditeur côté front.
        if m.media_type == "feed_display":
            item.id_feed = (m.media_metadata or {}).get("id_feed")
        # Gouvernance D6 : lecture seule si gouverné par un nœud non éditable (le front grise
        # les actions). editable None = org-admin → jamais lecture seule.
        # ⚠️ Un média NON gouverné est lecture seule pour un rôle scopé : il le VOIT depuis
        # le 2026-07-29 mais ne l'édite pas. Sans ce cas, la liste annoncerait « éditable »
        # sur toute la médiathèque commune et chaque geste finirait en 403 — c'est
        # exactement la classe de mensonge que `governed_readonly` existe pour éviter.
        gov = m.id_governance_node
        item.governed_readonly = bool(
            gov is not None and editable is not None and gov not in editable
        ) or (gov is None and scoped)
    # Avancement des encodages en cours. 🔑 On ne demande Redis QUE pour les médias
    # effectivement en traitement : dans le cas courant (rien n'encode) la liste est vide
    # et `read_many` ne fait aucun appel — la médiathèque est une des pages les plus
    # sollicitées du produit, elle ne doit pas payer une fonctionnalité qui ne sert que
    # pendant quelques minutes après un upload.
    busy = [i.id_media for i in items if i.transcode_status == "processing"]
    if busy:
        progress = await encode_progress.read_many(busy)
        for item in items:
            item.encode_progress = progress.get(item.id_media)
    return items


# ── Embargo ─────────────────────────────────────────────────────────────────────

async def _fully_embargoed_playlists(
    db: AsyncSession, id_media: int, effective_embargo: datetime | None
) -> list[dict]:
    """Retourne les playlists dont TOUS les items seront embargués pendant la période donnée."""
    if not effective_embargo or effective_embargo <= datetime.now(UTC):
        return []
    # Playlists contenant ce média
    pids = list((await db.execute(
        select(CmsPlaylistItem.id_playlist)
        .where(CmsPlaylistItem.id_media == id_media)
        .distinct()
    )).scalars().all())
    if not pids:
        return []

    now_utc = datetime.now(UTC)
    affected = []
    for pid in pids:
        # Tous les médias de la playlist
        medias = list((await db.execute(
            select(CmsMedia)
            .join(CmsPlaylistItem, CmsPlaylistItem.id_media == CmsMedia.id_media)
            .where(CmsPlaylistItem.id_playlist == pid, CmsMedia.is_deleted == False)  # noqa: E712
        )).scalars().all())
        if not medias:
            continue
        # Un item non-embargué suffit à « sauver » la playlist
        all_locked = True
        for m in medias:
            m_emb = m.not_before_utc
            if m.embargo_event_id:
                ev = (await db.execute(
                    select(CmsEvent).where(CmsEvent.id_event == m.embargo_event_id)
                )).scalar_one_or_none()
                m_emb = (ev.not_before_utc if ev else None) or m_emb
            if not m_emb or m_emb <= now_utc:
                all_locked = False
                break
        if all_locked:
            pl = (await db.execute(
                select(CmsPlaylist).where(CmsPlaylist.id_playlist == pid)
            )).scalar_one_or_none()
            if pl:
                affected.append({"id_playlist": pid, "playlist_name": pl.playlist_name})
    return affected


@router.patch("/{id_media}/embargo", response_model=MediaEmbargoResult)
async def update_media_embargo(
    id_media: int,
    body: MediaEmbargoUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaEmbargoResult:
    """Met à jour l'embargo d'un média et retourne les playlists à risque.

    Garde de nœud (D9) : l'embargo ÉTEINT la diffusion d'un média — c'est une des
    mutations les plus lourdes de conséquence, et elle rechargeait le média à la main,
    sans passer par `_get_media_editable` comme ses voisines (audit du menu Média,
    2026-07-28).
    """
    media = await _get_media_editable(db, current_user, id_media)
    media.not_before_utc = body.not_before_utc
    media.embargo_event_id = body.embargo_event_id
    media.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(media)

    # Embargo effectif pour le calcul de risque
    effective_embargo: datetime | None = body.not_before_utc
    if body.embargo_event_id:
        ev = (await db.execute(
            select(CmsEvent).where(CmsEvent.id_event == body.embargo_event_id)
        )).scalar_one_or_none()
        effective_embargo = (ev.not_before_utc if ev else None) or body.not_before_utc

    affected = await _fully_embargoed_playlists(db, id_media, effective_embargo)

    item = MediaListItem.model_validate(media)
    if item.thumbnail_url:
        item.thumbnail_url = get_signed_url(item.thumbnail_url, ttl=3600)
    if media.embargo_event_id:
        ev2 = (await db.execute(
            select(CmsEvent).where(CmsEvent.id_event == media.embargo_event_id)
        )).scalar_one_or_none()
        item.embargo_event_name = ev2.event_name if ev2 else None
    return MediaEmbargoResult(media=item, affected_playlists=affected)


# ── Droits d'auteur SACEM (§XIX.6) ───────────────────────────────────────────────

_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def _normalize_isrc(raw: str | None) -> str | None:
    """Normalise un ISRC (retire tirets/espaces, majuscules). None si vide, lève si invalide."""
    if raw is None:
        return None
    cleaned = raw.replace("-", "").replace(" ", "").upper()
    if not cleaned:
        return None
    if not _ISRC_RE.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid ISRC — expected format: CC-XXX-YY-NNNNN (12 characters)",
        )
    return cleaned


@router.patch("/{id_media}/rights", response_model=MediaRead)
async def update_media_rights(
    id_media: int,
    body: MediaRightsUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Met à jour l'ISRC + les métadonnées de droits d'un média (déclaration SACEM/SOCAN).

    Garde de nœud (D9) — cette route rechargeait aussi le média à la main.
    """
    media = await _get_media_editable(db, current_user, id_media)
    media.isrc = _normalize_isrc(body.isrc)
    media.rights_metadata = body.rights_metadata or {}
    # naïf UTC : compatible colonne naïve (tests create_all) ET timestamptz (prod migr 0003)
    media.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()
    await db.refresh(media)
    result = MediaRead.model_validate(media)
    if media.storage_key:
        result.cdn_url_signed = get_signed_url(media.storage_key)
    return result


async def _get_media_or_404(db: AsyncSession, id_media: int) -> CmsMedia:
    media = (await db.execute(
        select(CmsMedia).where(CmsMedia.id_media == id_media, CmsMedia.is_deleted == False)  # noqa: E712
    )).scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return media


async def _get_media_editable(db: AsyncSession, user: CmsUser, id_media: int) -> CmsMedia:
    """Charge un média + enforce la garde de nœud (édition/suppression). D9.

    Passe par la VISIBILITÉ d'abord : un média hors périmètre doit répondre 404 (il
    n'existe pas pour cet utilisateur) plutôt que 403 (« il existe, mais pas pour vous »).
    """
    media = await _get_media_visible(db, user, id_media)
    await _enforce_media_node_edit(db, user, media)
    return media


async def _get_media_visible(db: AsyncSession, user: CmsUser, id_media: int) -> CmsMedia:
    """Charge un média en appliquant la VISIBILITÉ de gouvernance (D6). Hors périmètre =
    **404**, comme un cross-tenant : le média n'existe pas pour cet utilisateur.

    Pourquoi ça compte : `GET /media/` filtre par `media_visibility`, mais les routes
    unitaires rechargeaient le média par son seul identifiant — un lien direct
    contournait donc le filtre de la liste. C'est exactement le trou que `playlists.py`
    ferme déjà pour son propre détail, avec le même raisonnement.
    """
    gov_clause, _visible, _editable = await media_visibility(db, user)
    stmt = select(CmsMedia).where(
        CmsMedia.id_media == id_media, CmsMedia.is_deleted == False,  # noqa: E712
    )
    clause = gov_clause(CmsMedia.id_governance_node)
    if clause is not None:  # None = org-admin, aucun filtre
        stmt = stmt.where(clause)
    media = (await db.execute(stmt)).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    return media


@router.get("/usage-summary")
async def media_usage_summary(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    ids: str = Query(..., description="Comma-separated IDs"),
) -> dict:
    """« Combien d'écrans ? » pour un LOT de médias — affiché en permanence sur les
    vignettes (demande d'Ed). Le DÉTAIL nommé reste servi par `/usage`, au survol.

    Les identifiants sont filtrés par la VISIBILITÉ de gouvernance avant tout calcul : on
    ne renseigne pas sur un média qu'on n'a pas le droit de voir, même par un compteur.
    """
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:500]
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Invalid IDs")
    if not wanted:
        return {}

    gov_clause, _visible, _editable = await media_visibility(db, current_user)
    stmt = select(CmsMedia.id_media).where(
        CmsMedia.id_media.in_(wanted),
        CmsMedia.id_org == current_user.id_org,
        CmsMedia.is_deleted == False,  # noqa: E712
    )
    clause = gov_clause(CmsMedia.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    allowed = list((await db.execute(stmt)).scalars().all())
    return await media_usage_service.usage_summary(db, current_user.id_org, allowed)


@router.get("/{id_media}/pages", response_model=DocumentPagesResult)
async def list_document_pages(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentPagesResult:
    """Pages converties d'un document multipage (§3.2) — visionneuse du drawer
    médiathèque + aperçu de l'éditeur de playlist. URLs signées TTL 1 h (aperçu ;
    la diffusion passe par le payload de sync, pas par cet endpoint)."""
    media = await _get_media_visible(db, current_user, id_media)
    doc = (media.media_metadata or {}).get("document") or {}
    pages_meta = doc.get("pages") or []
    pages: list[DocumentPageRead] = []
    for i, p in enumerate(pages_meta, start=1):
        key = p.get("key") if isinstance(p, dict) else None
        if not key:
            continue
        pages.append(DocumentPageRead(
            page=i, url=get_signed_url(key, 3600),
            width=p.get("width"), height=p.get("height"),
        ))
    return DocumentPagesResult(
        id_media=id_media,
        page_count=len(pages),
        truncated=bool(doc.get("truncated")),
        pages=pages,
    )


def _reprocess(media: CmsMedia) -> None:
    """Marque le média à retraiter et relance la tâche Celery (probe + conformité)."""
    media.transcode_status = "pending"
    media.updated_at = datetime.now(UTC).replace(tzinfo=None)


@router.patch("/{id_media}/encode-mode", response_model=MediaRead)
async def update_media_encode_mode(
    id_media: int,
    body: MediaEncodeModeUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Bascule le mode d'encodage (auto|passthrough) + relance le traitement (§XVI.2).

    `passthrough` conserve le fichier tel quel (cas mur d'images) ; `auto` ré-encode.
    La conformité (CFR/GOP/faststart) est recalculée à chaque retraitement.
    """
    if body.encode_mode not in ("auto", "passthrough"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid encode_mode (auto|passthrough)",
        )
    media = await _get_media_editable(db, current_user, id_media)
    if media.media_type not in ("video", "audio"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="encode_mode only applies to video/audio media",
        )
    media.encode_mode = body.encode_mode
    _reprocess(media)
    await db.commit()
    await db.refresh(media)
    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)
    result = MediaRead.model_validate(media)
    if media.storage_key:
        result.cdn_url_signed = get_signed_url(media.storage_key)
    return result


@router.post("/{id_media}/reanalyze", response_model=MediaRead)
async def reanalyze_media(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Relance le traitement (re-probe + recalcul de conformité) sans changer le mode."""
    media = await _get_media_editable(db, current_user, id_media)
    _reprocess(media)
    await db.commit()
    await db.refresh(media)
    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)
    result = MediaRead.model_validate(media)
    if media.storage_key:
        result.cdn_url_signed = get_signed_url(media.storage_key)
    return result


@router.patch("/{id_media}/rename", response_model=MediaRead)
async def rename_media(
    id_media: int,
    body: MediaRenameUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Renomme le libellé d'un média (pas le fichier ni l'extension réelle)."""
    name = body.media_name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty name"
        )
    media = await _get_media_editable(db, current_user, id_media)
    old_name = media.media_name
    media.media_name = name[:255]
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_rename", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"from": old_name, "to": media.media_name},
    ))
    await db.commit()
    await db.refresh(media)
    result = MediaRead.model_validate(media)
    if media.storage_key:
        result.cdn_url_signed = get_signed_url(media.storage_key)
    return result


@router.patch("/{id_media}/governance-node", response_model=MediaRead)
async def assign_media_governance_node(
    id_media: int,
    body: MediaGovernanceNodeUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Rattache/détache un média d'un nœud de gouvernance §XVIII (D9).

    Il faut administrer le nœud ACTUEL du média (gardé par la dépendance) ET le nœud CIBLE :
    on ne fait pas entrer un média dans un nœud qu'on n'administre pas, ni sortir un média
    d'un nœud national qu'on n'administre pas."""
    media = await _get_media_editable(db, current_user, id_media)
    await _validate_media_node_org(db, current_user, body.id_governance_node)
    if not await user_can_edit_node(db, current_user, body.id_governance_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer the target governance node",
        )
    # N2 : détacher (→ NULL) fait sortir le média de la gouvernance — ancêtre STRICT ou
    # org-admin seulement, même règle que playlists et programmes.
    if body.id_governance_node is None and not await user_can_detach_from_node(
        db, current_user, media.id_governance_node
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator of a parent node can detach "
                   "this media from governance",
        )
    old = media.id_governance_node
    media.id_governance_node = body.id_governance_node
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_governance_node", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"from": old, "to": body.id_governance_node},
    ))
    await db.commit()
    await db.refresh(media)
    result = MediaRead.model_validate(media)
    if media.storage_key:
        result.cdn_url_signed = get_signed_url(media.storage_key)
    return result


@router.post("/{id_media}/replace", response_model=MediaRead)
async def replace_media(
    id_media: int,
    file: UploadFile,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    encode_mode: Optional[str] = Form(None),
) -> MediaRead:
    """Remplace le FICHIER d'un média en conservant le même `id_media`.

    Toutes les références (playlists, programmes, événements, paniers) pointent toujours
    vers ce média : le remplacement est transparent côté planification. Le type de média
    doit rester identique (on ne remplace pas une vidéo par une image — les durées/rendu
    playlist en dépendent).

    **Staging atomique** : le nouveau fichier est mis dans une clé de *staging* ; le
    contenu ACTUELLEMENT diffusé (storage_key, renditions, objets MinIO) reste intact et
    en ligne pendant tout le transcodage. C'est seulement à la FIN du transcodage que le
    worker bascule atomiquement le storage_key vers la nouvelle version, purge les anciens
    objets MinIO + lignes renditions, régénère les renditions et notifie les players
    (force_sync graceful). Les players ne voient donc JAMAIS de fichier brut/illisible
    (ex. .mkv) ni de contenu manquant pendant le remplacement.

    Cas d'usage : ré-encoder un lot de vidéos (ex. 4K H.264 → HEVC) sans recâbler les
    playlists ni créer de doublons, sans interrompre la diffusion.
    """
    media = await _get_media_editable(db, current_user, id_media)

    if not file.filename and not file.size:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    content_type = file.content_type or "application/octet-stream"
    new_type = _detect_media_type(content_type, file.filename)
    if new_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {content_type}.",
        )
    if new_type != media.media_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The replacement must keep the same media type "
                f"(expected: {media.media_type}, received: {new_type})."
            ),
        )

    # Clés des objets MinIO de l'ANCIENNE version, à purger UNE FOIS la bascule effectuée
    # par le worker (jamais avant : elles servent encore la diffusion en cours).
    cleanup_keys = [media.storage_key]
    if media.thumbnail_url:
        cleanup_keys.append(media.thumbnail_url)
    cleanup_keys += [
        k for (k,) in (await db.execute(
            select(CmsMediaRendition.storage_key).where(
                CmsMediaRendition.id_parent == id_media
            )
        )).all() if k
    ]

    # Le nouveau fichier va dans une clé de STAGING — le storage_key servi n'est PAS touché.
    staging_key = make_storage_key(current_user.id_org, file.filename or "upload")
    sha256, file_size = await stream_upload(file.file, staging_key, content_type)

    chosen_mode = (
        encode_mode if (encode_mode in ("auto", "passthrough") and new_type in ("video", "audio"))
        else media.encode_mode
    )
    # On marque le remplacement en attente dans la colonne `metadata` (pas de migration) :
    # le worker le détecte, transcode le staging, puis bascule atomiquement. On NE touche
    # NI au storage_key/dimensions/renditions/conformité (l'ancien contenu reste diffusé),
    # on signale juste un retraitement en cours via transcode_status='processing'.
    media.media_metadata = {
        **(media.media_metadata or {}),
        "pending_replace": {
            "staging_key": staging_key,
            "cleanup_keys": cleanup_keys,
            "encode_mode": chosen_mode,
            "checksum": sha256,
            "size": file_size,
            "mime": content_type,
        },
    }
    media.transcode_status = "processing"
    media.updated_at = datetime.now(UTC).replace(tzinfo=None)

    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_replace", resource_type="media", resource_id=str(id_media),
        source="cms", detail={
            "media_name": media.media_name,
            "old_storage_key": media.storage_key,
            "staging_key": staging_key,
            "new_size_bytes": file_size,
        },
    ))
    await db.commit()
    await db.refresh(media)

    # Transcodage du staging → bascule atomique + purge + force_sync EN FIN de tâche
    # (cf. transcode_media : branche pending_replace). Aucun force_sync immédiat ici.
    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)

    result = MediaRead.model_validate(media)
    if media.storage_key:
        result.cdn_url_signed = get_signed_url(media.storage_key)
    return result


@router.post("/{id_media}/passthrough-ack", response_model=MediaPassthroughAckResult)
async def acknowledge_passthrough(
    id_media: int,
    body: MediaPassthroughAck,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaPassthroughAckResult:
    """Trace l'acquittement d'un verdict de conformité 🟡/🔴 dans cms_audit_log.

    « Le client ne pourra pas dire qu'il ne savait pas » : l'utilisateur reconnaît
    avoir vu les raisons (codec/bitrate/VFR/GOP/faststart) avant de diffuser tel quel.
    """
    media = await _get_media_editable(db, current_user, id_media)
    conf = (media.media_metadata or {}).get("conformance") or {}
    now = datetime.now(UTC).replace(tzinfo=None)
    db.add(CmsAuditLog(
        id_org=current_user.id_org,
        id_user=current_user.id_user,
        action="media_passthrough_ack",
        resource_type="media",
        resource_id=str(id_media),
        source="cms",
        detail={
            "media_name": media.media_name,
            "encode_mode": media.encode_mode,
            "conformance_status": media.conformance_status,
            "reasons": conf.get("reasons", []),
            "note": body.note,
        },
    ))
    await db.commit()
    return MediaPassthroughAckResult(
        id_media=id_media,
        conformance_status=media.conformance_status,
        acknowledged_at=now,
    )


@router.post("/{id_media}/encode-decision", response_model=MediaEncodeDecisionResult)
async def decide_encode(
    id_media: int,
    body: MediaEncodeDecision,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaEncodeDecisionResult:
    """Tranche un encodage que l'étalonnage a jugé trop long (§ bandes, décision Ed 03/08).

    Trois bandes mesurées par le worker sur la VRAIE source :
      · < 20 min      → on lance, cette route n'est jamais sollicitée ;
      · 20 min – 4 h  → l'utilisateur décide (lancer, ou encoder en définition inférieure) ;
      · > 4 h         → refus. Seul un org-admin peut passer outre — mais la voie normale
        reste d'encoder en définition inférieure, ce qui transforme le refus en choix.
    """
    media = await _get_media_editable(db, current_user, id_media)
    if media.transcode_status != "awaiting_decision":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This media is not awaiting an encoding decision",
        )
    meta = dict(media.media_metadata or {})
    pending = dict(meta.get("encode_decision") or {})
    band = pending.get("band")
    options = {o.get("profile"): o for o in (pending.get("downscale") or []) if isinstance(o, dict)}

    downscale_box: int | None = None
    if body.action == "downscale":
        opt = options.get(body.profile or "")
        if not opt:
            # On n'accepte QUE les profils réellement proposés : un profil inventé (ou plus
            # grand que la source) produirait un encodage qui ne résout pas le problème.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown profile for this media (offered: {', '.join(options) or 'none'})",
            )
        downscale_box = int(opt["box"])
    elif band == "refuse":
        # Bande 3 : la permission media:edit ne suffit pas. Lancer un encodage de plus de
        # 4 h engage la machine pour tout le monde — c'est une décision d'exploitation.
        roles = await rbac.get_user_role_keys(db, current_user.id_user)
        if not {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Estimated encoding exceeds the limit: reserved for an organization "
                       "administrator, or choose a lower resolution",
            )

    meta["encode_decision"] = {
        **pending,
        "resolved": True,
        "action": body.action,
        "downscale_box": downscale_box,
        "decided_by": current_user.id_user,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    media.media_metadata = meta
    media.transcode_status = "pending"
    # Traçabilité : c'est une décision d'exploitation (temps machine engagé, ou qualité
    # dégradée volontairement). Le motif récurrent des audits par menu était justement
    # l'absence de trace sur les gestes qui engagent.
    db.add(CmsAuditLog(
        id_org=current_user.id_org,
        id_user=current_user.id_user,
        action="media_encode_decision",
        resource_type="media",
        resource_id=str(id_media),
        source="cms",
        detail={
            "media_name": media.media_name,
            "band": band,
            "estimate_s": pending.get("estimate_s"),
            "action": body.action,
            "profile": body.profile,
            "downscale_box": downscale_box,
        },
    ))
    await db.commit()

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)

    return MediaEncodeDecisionResult(
        id_media=id_media,
        transcode_status="pending",
        downscale_box=downscale_box,
        estimate_s=(options.get(body.profile or "") or {}).get("estimate_s")
        if body.action == "downscale" else pending.get("estimate_s"),
    )


# ── Quarantaine / kill-switch (§ Q5) ─────────────────────────────────────────────

async def _broadcast_org(db: AsyncSession, id_org: int, message: dict) -> None:
    """Publie un message sur le canal Redis de chaque player de l'org."""
    player_ids = [r for (r,) in (await db.execute(
        select(CmsPlayer.id_player).where(CmsPlayer.id_org == id_org)
    )).all()]
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    payload = json.dumps(message)
    async with r:
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)


@router.post("/{id_media}/quarantine", response_model=MediaQuarantineResult)
async def quarantine_media(
    id_media: int,
    body: MediaQuarantineRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaQuarantineResult:
    """Kill-switch : bloque immédiatement la diffusion d'un média sur TOUS les players.

    Enforced server-side (le média ne part dans aucun player:sync) + force_sync.
    Tracé dans cms_audit_log (dimension légale). Cas « 1000 €/passage ».
    """
    media = await _get_media_editable(db, current_user, id_media)
    now = datetime.now(UTC)
    media.is_blocked = True
    media.block_reason = body.reason
    media.blocked_at = now
    media.blocked_by = current_user.id_user
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_quarantine", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"media_name": media.media_name, "reason": body.reason},
    ))
    await db.commit()
    # preemptive=True : retrait IMMÉDIAT (pas à la prochaine frontière d'item) — kill-switch.
    await _broadcast_org(db, current_user.id_org,
                         {"type": "player:force_sync", "preemptive": True})
    return MediaQuarantineResult(
        id_media=id_media, is_blocked=True, block_reason=media.block_reason, blocked_at=now,
    )


@router.post("/{id_media}/unquarantine", response_model=MediaQuarantineResult)
async def unquarantine_media(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaQuarantineResult:
    """Lève la quarantaine : le média redevient diffusable + force_sync."""
    media = await _get_media_editable(db, current_user, id_media)
    media.is_blocked = False
    media.block_reason = None
    media.blocked_at = None
    media.blocked_by = None
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_unquarantine", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"media_name": media.media_name},
    ))
    await db.commit()
    await _broadcast_org(db, current_user.id_org, {"type": "player:force_sync"})
    return MediaQuarantineResult(id_media=id_media, is_blocked=False)


@router.post("/{id_media}/purge", status_code=status.HTTP_202_ACCEPTED)
async def purge_media(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Ordonne à tous les players d'effacer ce média de leur cache local (best-effort).

    Le contenu reste dans MinIO (source) ; seule la copie locale des players est purgée.
    À combiner avec la quarantaine pour un retrait légal complet. Tracé dans l'audit.
    """
    media = await _get_media_editable(db, current_user, id_media)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_purge", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"media_name": media.media_name},
    ))
    await db.commit()
    await _broadcast_org(db, current_user.id_org,
                         {"type": "player:purge_media", "id_media": id_media})
    return {"id_media": id_media, "purge_dispatched": True}


@router.get("/isrc-lookup", response_model=IsrcLookupResult)
async def isrc_lookup(
    _: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    isrc: str = Query(..., min_length=5),
) -> IsrcLookupResult:
    """Lookup best-effort des métadonnées d'un enregistrement via MusicBrainz (gratuit, ouvert).

    Renvoie ``found=false`` si l'ISRC est inconnu ou si MusicBrainz est injoignable —
    le formulaire reste éditable manuellement.
    """
    norm = _normalize_isrc(isrc)
    if norm is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ISRC required")
    empty = IsrcLookupResult(found=False, isrc=norm)
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            resp = await http.get(
                f"https://musicbrainz.org/ws/2/isrc/{norm}",
                params={"fmt": "json", "inc": "artist-credits+releases"},
                headers={"User-Agent": "Vitrail/1.0 (signage rights reporting)"},
            )
        if resp.status_code != 200:
            return empty
        data = resp.json()
        recordings = data.get("recordings") or []
        if not recordings:
            return empty
        rec = recordings[0]
        artists = rec.get("artist-credit") or []
        composer = artists[0].get("name") if artists and isinstance(artists[0], dict) else None
        releases = rec.get("releases") or []
        album = releases[0].get("title") if releases else None
        year = None
        if releases and releases[0].get("date"):
            try:
                year = int(str(releases[0]["date"])[:4])
            except (ValueError, TypeError):
                year = None
        return IsrcLookupResult(
            found=True,
            isrc=norm,
            title=rec.get("title"),
            composer=composer,
            album=album,
            year=year,
        )
    except (httpx.HTTPError, ValueError, KeyError):
        return empty


# ── Stock Library ──────────────────────────────────────────────────────────────

@router.get("/stock/search", response_model=list[StockSearchResult])
async def stock_search(
    _: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    q: str = Query(..., min_length=1),
    provider: str = Query("unsplash"),
    per_page: int = Query(20, ge=1, le=50),
) -> list[StockSearchResult]:
    """Recherche dans Unsplash ou Pexels.

    Renvoie 503 (detail=`stock_provider_unconfigured`) si la clé API du fournisseur
    sélectionné n'est pas posée côté serveur — le front distingue ainsi « non configuré »
    d'un « zéro résultat » silencieux.
    """
    if provider == "unsplash":
        if not settings.UNSPLASH_ACCESS_KEY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="stock_provider_unconfigured",
            )
        return await _unsplash_search(q, per_page)
    if provider == "pexels":
        if not settings.PEXELS_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="stock_provider_unconfigured",
            )
        return await _pexels_search(q, per_page)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="provider must be 'unsplash' or 'pexels'",
    )


@router.post("/stock/import", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def stock_import(
    body: StockImportRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Télécharge un asset stock, le stocke dans MinIO avec attribution légale."""
    if body.provider not in _STOCK_HOST_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provider must be 'unsplash' or 'pexels'",
        )

    # Anti-SSRF (audit 2026-06-24) : `full_url` est fourni par le client → sans garde, le serveur
    # fetchait n'importe quelle URL (metadata cloud, services internes) et renvoyait le corps via
    # URL signée. On exige HTTPS + un domaine du provider + une IP publique, et on n'suit PAS les
    # redirections (un 3xx pourrait pointer vers une IP interne).
    if not await run_in_threadpool(
        url_is_safe,
        body.full_url,
        schemes=("https",),
        allowed_host_suffixes=_STOCK_HOST_SUFFIXES[body.provider],
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source URL not allowed",
        )

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        resp = await http.get(body.full_url)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not download the stock asset",
        )

    content = resp.content
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    media_type = _detect_media_type(content_type) or "image"
    ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
    filename = f"{body.provider_id}.{ext}"

    storage_key = make_storage_key(current_user.id_org, filename)
    sha256 = compute_sha256(content)

    await upload_file(content, storage_key, content_type)

    source = f"stock_{body.provider}"
    media = CmsMedia(
        id_org=current_user.id_org,
        media_name=body.title or filename,
        media_type=media_type,
        mime_type=content_type,
        storage_key=storage_key,
        file_size_bytes=len(content),
        width=body.width,
        height=body.height,
        checksum_sha256=sha256,
        source=source,
        attribution_text=body.attribution_text,
        transcode_status="pending",
        created_by=current_user.id_user,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)

    result = MediaRead.model_validate(media)
    result.cdn_url_signed = get_signed_url(storage_key)
    return result


# ── Média pointeur « Entrée HDMI » ─────────────────────────────────────────────

@router.post("/hdmi-input", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def create_hdmi_input_media(
    payload: HdmiInputCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Média pointeur « Entrée HDMI » : aucun fichier — programmé dans une playlist, l'item
    bascule le player sur son entrée HDMI physique pendant sa durée (BrightSign XT/4K1142/
    XD1230). Le sync ne sert l'item qu'aux players capables ; les autres l'ignorent."""
    media = CmsMedia(
        id_org=current_user.id_org,
        media_name=payload.name,
        media_type="hdmi_input",
        mime_type="application/x-hdmi-input",
        storage_key="",          # sentinel : pas de fichier (NOT NULL en base)
        checksum_sha256="",      # idem
        source="hdmi",
        media_metadata={"input": "hdmi"},
        transcode_status="done",  # pas de transcodage → directement diffusable
        is_encrypted=False,
        conformance_status="ready",
        created_by=current_user.id_user,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)
    return MediaRead.model_validate(media)


# ── Détail ─────────────────────────────────────────────────────────────────────

@router.get("/{id_media}", response_model=MediaRead)
async def get_media(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    media = await _get_media_visible(db, current_user, id_media)
    out = MediaRead.model_validate(media)
    # `governed_readonly` n'était calculé que par la LISTE : le tiroir ouvert par lien
    # direct s'affichait donc éditable sur un média que l'utilisateur ne peut pas éditer,
    # et tous ses champs partaient vers un 403 (audit du menu Média, 2026-07-28).
    out.governed_readonly = not await user_can_edit_node(db, current_user, media.id_governance_node)
    # Médias pointeurs (hdmi_input / url_display / feed_display) : storage_key = "" (sentinelle,
    # pas de fichier) → ne PAS signer (botocore rejette une clé vide → 500).
    out.cdn_url_signed = get_signed_url(media.storage_key) if media.storage_key else None
    if out.thumbnail_url:
        out.thumbnail_url = get_signed_url(media.thumbnail_url, ttl=3600)
    if media.transcode_status == "processing":
        out.encode_progress = (await encode_progress.read_many([id_media])).get(id_media)
    if media.transcode_status == "awaiting_decision":
        out.encode_decision = (media.media_metadata or {}).get("encode_decision")
    return out


@router.get("/{id_media}/download")
async def download_media(
    id_media: int,
    current: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """URL pré-signée forçant le TÉLÉCHARGEMENT du fichier (Content-Disposition attachment).

    Renvoie le nom du média, complété de l'extension réelle du fichier servi si besoin.
    404 pour les médias pointeurs (feed_display / url_display) qui n'ont pas de fichier.
    """
    # Mode démo (§II) : pas de téléchargement des contenus (GET → non couvert par DemoGuard).
    if current.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="demo_blocked")
    # Visibilité de gouvernance : télécharger le fichier est la forme la plus complète de
    # « lire », elle ne peut pas être plus permissive que la liste.
    media = await _get_media_visible(db, current, id_media)
    if not media.storage_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    name = media.media_name or "download"
    ext = media.storage_key.rsplit(".", 1)[-1] if "." in media.storage_key else ""
    if ext and not name.lower().endswith(f".{ext.lower()}"):
        name = f"{name}.{ext}"
    return {"url": get_signed_url(media.storage_key, download_name=name)}


# ── Renditions adaptatives (« artefacts » vidéo) ────────────────────────────────

# Profils de rendition supportés → arête max du cadre englobant (cf. media_worker).
_RENDITION_BOXES = {"fhd": 1920, "hd720": 1280}


@router.get("/{id_media}/renditions", response_model=list[RenditionRead])
async def list_renditions(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RenditionRead]:
    """Liste les artefacts (renditions) d'un média parent — section du drawer média."""
    await _get_media_visible(db, current_user, id_media)
    rows = (await db.execute(
        select(CmsMediaRendition)
        .where(CmsMediaRendition.id_parent == id_media)
        .order_by(CmsMediaRendition.profile)
    )).scalars().all()
    return [RenditionRead.model_validate(r) for r in rows]


@router.post(
    "/{id_media}/renditions",
    response_model=RenditionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_rendition_endpoint(
    id_media: int,
    body: RenditionGenerateRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RenditionRead:
    """Génère (ou régénère) une rendition d'un média vidéo (opt-in par média).

    Idempotent par (parent, profile) : régénère l'artefact existant. Le ré-encodage
    est asynchrone (Celery) ; la ligne est renvoyée à l'état `pending`/`processing`.
    """
    media = await _get_media_editable(db, current_user, id_media)
    if media.media_type != "video":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Renditions only apply to videos")
    from app.workers.media_worker import RENDITION_PROFILES
    prof = RENDITION_PROFILES.get(body.profile)
    if prof is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Unknown rendition profile: {body.profile}")
    # Profil de DOWNSCALE (même codec h264) : refus si la source n'est pas plus grande que
    # la cible (artefact inutile). Profil de TRANSCODE codec (ex. hevc_4k, même résolution) :
    # la valeur est le changement de codec → pas de contrainte de taille. Les profils ROTÉS
    # (transpose, box None) sont exclus du contrôle : leur valeur est la ROTATION, pas la
    # taille — et `<= None` serait un TypeError (500) sur rot_*_fhd, dont le codec est h264.
    if (prof.get("transpose") is None and prof["codec"] == "h264"
            and media.width and media.height
            and max(media.width, media.height) <= prof["box"]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The source is not larger than the requested profile",
        )

    rend = (await db.execute(
        select(CmsMediaRendition).where(
            CmsMediaRendition.id_parent == id_media,
            CmsMediaRendition.profile == body.profile,
        )
    )).scalar_one_or_none()
    if rend is None:
        rend = CmsMediaRendition(id_parent=id_media, profile=body.profile, status="pending")
        db.add(rend)
    else:
        rend.status = "pending"
        rend.error_detail = None
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_rendition_generate", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"media_name": media.media_name, "profile": body.profile},
    ))
    await db.commit()
    await db.refresh(rend)

    from app.workers.media_worker import generate_rendition
    generate_rendition.delay(id_media, body.profile)
    return RenditionRead.model_validate(rend)


@router.delete(
    "/{id_media}/renditions/{id_rendition}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_rendition(
    id_media: int,
    id_rendition: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Supprime un artefact (DB + objet MinIO). Le parent reste intact ; les players
    repassent automatiquement à l'originale (ou à une autre rendition) au prochain sync."""
    await _get_media_editable(db, current_user, id_media)
    rend = (await db.execute(
        select(CmsMediaRendition).where(
            CmsMediaRendition.id_rendition == id_rendition,
            CmsMediaRendition.id_parent == id_media,
        )
    )).scalar_one_or_none()
    if rend is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rendition not found")

    storage_key = rend.storage_key
    await db.delete(rend)
    # Recalcule has_renditions sur le parent (reste vrai s'il reste un artefact prêt).
    still = (await db.execute(
        select(CmsMediaRendition).where(
            CmsMediaRendition.id_parent == id_media,
            CmsMediaRendition.id_rendition != id_rendition,
            CmsMediaRendition.status == "ready",
        )
    )).first()
    await db.execute(
        update(CmsMedia).where(CmsMedia.id_media == id_media).values(has_renditions=bool(still))
    )
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="media_rendition_delete", resource_type="media", resource_id=str(id_media),
        source="cms", detail={"profile": rend.profile},
    ))
    await db.commit()

    if storage_key:
        from app.workers.media_worker import delete_media_files
        delete_media_files.delay(storage_key)


# ── Impact & suppression logique (avec cascade) ─────────────────────────────────

def _public_impact(impact: dict) -> dict:
    """Retire les clés internes (`_…`) avant exposition au front."""
    return {k: v for k, v in impact.items() if not k.startswith("_")}


@router.get("/{id_media}/usage")
async def media_usage(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Où ce média est-il utilisé — playlists, programmes, événements — et sur quels
    ÉCRANS diffuse-t-il en ce moment (demande d'Ed, audit du menu Média 2026-07-28).

    Lecture seule. Même filtre de visibilité que la liste : on ne renseigne pas sur un
    média qu'on n'a pas le droit de voir.
    """
    media = await _get_media_visible(db, current_user, id_media)
    return await media_usage_service.usage(db, media)


@router.get("/{id_media}/deletion-impact")
async def media_deletion_impact(
    id_media: int,
    _: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Arbre d'impact de la suppression d'un média (playlists/programmes/événements/
    paniers + état de diffusion en cours/futur/passé). Lecture seule — alimente la
    confirmation avant suppression."""
    media = (await db.execute(
        select(CmsMedia).where(CmsMedia.id_media == id_media, CmsMedia.is_deleted == False)  # noqa: E712
    )).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    return _public_impact(await media_deletion.analyze(db, media))


@router.delete("/{id_media}")
async def delete_media(
    id_media: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Supprime un média + ses renditions, et applique la cascade : retrait des playlists,
    suppression des playlists devenues vides, des programmes orphelins, et annulation des
    événements concernés (cf. media_deletion). Renvoie l'impact réellement appliqué.

    Garde de nœud (D9) : c'était la route la plus destructive du module et la SEULE
    mutation lourde qui ne la posait pas — alors que `DELETE /{id}/renditions/{id}`,
    bien moins conséquent, la posait. Un responsable local pouvait donc effacer un média
    national **et sa cascade** (retrait des playlists, suppression des programmes
    devenus orphelins, annulation d'événements), à l'exact opposé de l'intention D9
    documentée plus haut dans ce fichier (audit du menu Média, 2026-07-28).
    """
    media = await _get_media_editable(db, current_user, id_media)
    id_org = media.id_org
    storage_key, thumbnail_url = media.storage_key, media.thumbnail_url

    # Clés MinIO des artefacts (renditions) à purger en même temps que l'originale.
    rendition_keys = [
        k for (k,) in (await db.execute(
            select(CmsMediaRendition.storage_key).where(
                CmsMediaRendition.id_parent == id_media
            )
        )).all() if k
    ]
    # Pages converties d'un document multipage (§3.2) : mêmes artefacts, même purge.
    rendition_keys += [
        p.get("key")
        for p in (((media.media_metadata or {}).get("document") or {}).get("pages") or [])
        if isinstance(p, dict) and p.get("key")
    ]

    # Cascade (playlists/programmes/événements) — calcule l'impact ET applique les écritures.
    impact = await media_deletion.cascade_delete(db, media)

    await db.execute(
        update(CmsMedia).where(CmsMedia.id_media == id_media).values(is_deleted=True)
    )
    # Média pointeur d'un feed → soft-delete aussi le datafeed sous-jacent, sinon il reste
    # pollé indéfiniment (orphelin). Symétrique de DELETE /feeds/{id}.
    if media.media_type == "feed_display":
        fid = (media.media_metadata or {}).get("id_feed")
        if fid:
            await db.execute(
                update(CmsDatafeed).where(CmsDatafeed.id_feed == int(fid)).values(is_deleted=True)
            )
    # Le soft-delete (UPDATE) ne déclenche pas le FK CASCADE → purger explicitement les lignes
    # `cms_media_renditions` (sinon orphelines une fois les objets MinIO supprimés).
    await db.execute(
        delete(CmsMediaRendition).where(CmsMediaRendition.id_parent == id_media)
    )
    await db.commit()

    from app.workers.media_worker import delete_media_files
    # url_display / feed_display = médias pointeurs sans fichier MinIO → rien à purger.
    if media.media_type not in ("url_display", "feed_display"):
        delete_media_files.delay(storage_key, thumbnail_url, rendition_keys)

    # Le contenu diffusé a changé (playlists/programmes modifiés) → resync graceful des players.
    if impact["summary"]["playlists_total"] or impact["summary"]["events_cancelled"]:
        await _broadcast_org(db, id_org, {"type": "player:force_sync", "preemptive": False})

    return _public_impact(impact)


# ── Helpers Stock API ──────────────────────────────────────────────────────────

async def _unsplash_search(q: str, per_page: int) -> list[StockSearchResult]:
    if not settings.UNSPLASH_ACCESS_KEY:
        return []
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(
            "https://api.unsplash.com/search/photos",
            params={"query": q, "per_page": per_page},
            headers={"Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}"},
        )
    if resp.status_code != 200:
        return []
    data = resp.json()
    results = []
    for photo in data.get("results", []):
        user = photo.get("user", {})
        results.append(StockSearchResult(
            provider="unsplash",
            provider_id=photo["id"],
            title=photo.get("alt_description"),
            description=photo.get("description"),
            preview_url=photo["urls"]["small"],
            full_url=photo["urls"]["full"],
            attribution_text=f"Photo by {user.get('name', 'Unknown')} on Unsplash",
            photographer=user.get("name", "Unknown"),
            photographer_url=user.get("links", {}).get("html"),
            width=photo.get("width"),
            height=photo.get("height"),
        ))
    return results


async def _pexels_search(q: str, per_page: int) -> list[StockSearchResult]:
    if not settings.PEXELS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(
            "https://api.pexels.com/v1/search",
            params={"query": q, "per_page": per_page},
            headers={"Authorization": settings.PEXELS_API_KEY},
        )
    if resp.status_code != 200:
        return []
    data = resp.json()
    results = []
    for photo in data.get("photos", []):
        photographer = photo.get("photographer", "Unknown")
        results.append(StockSearchResult(
            provider="pexels",
            provider_id=str(photo["id"]),
            title=photo.get("alt"),
            description=None,
            preview_url=photo["src"]["medium"],
            full_url=photo["src"]["original"],
            attribution_text=f"Photo by {photographer} on Pexels",
            photographer=photographer,
            photographer_url=photo.get("photographer_url"),
            width=photo.get("width"),
            height=photo.get("height"),
        ))
    return results
