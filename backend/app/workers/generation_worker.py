"""Worker de génération de contenu IA asynchrone (§ génération — vidéo/audio).

Un job `cms_generation_jobs` (pending) → ce worker résout la clé provider de l'org
(coffre BYOK, strict), appelle fal (file : submit → poll → download), crée le média
(source=generated_ai) + déclenche le transcode (renditions), puis marque le job ready.
DB via psycopg2 (signflow_worker / BYPASSRLS) et S3 via boto3 sync — comme media_worker.
Best-effort : toute erreur → job `failed` (error_detail), jamais de crash.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

import boto3
import psycopg2
from botocore.config import Config
from psycopg2.extras import Json

from app.core.config import settings
from app.services import spend
from app.services import ai_credentials
from app.services.generation import GenerationError, catalog, elevenlabs, fal, refs
from app.services.media import compute_sha256, make_storage_key
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MEDIA_TYPE = {"image": "image", "video": "video", "music": "audio", "voice": "audio"}
# Nom par défaut quand l'utilisateur n'en fournit pas (PLUS le prompt tronqué).
_DEFAULT_NAME = {"image": "AI image", "video": "AI video", "music": "AI music",
                 "voice": "AI voice"}


def _strip_audio(content: bytes) -> bytes:
    """Retire la piste audio d'un MP4 (copie vidéo, -an). Repli gracieux : inchangé si échec."""
    src = dst = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fin:
            fin.write(content)
            src = fin.name
        dst = f"{src}.muted.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-c", "copy", "-an", "-movflags", "+faststart", dst],
            check=True, capture_output=True, timeout=120,
        )
        with open(dst, "rb") as f:
            out = f.read()
        return out or content
    except Exception:  # noqa: BLE001
        logger.warning("strip_audio échec → audio conservé")
        return content
    finally:
        for pth in (src, dst):
            if pth and os.path.exists(pth):
                try:
                    os.remove(pth)
                except OSError:
                    pass


def _recompute_campaign(id_campaign: int | None) -> None:
    """Recalcule le statut + coût total d'une campagne depuis l'état de ses jobs (Phase 4)."""
    if not id_campaign:
        return
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, COALESCE(cost_usd, 0) FROM cms_generation_jobs WHERE id_campaign=%s",
            (id_campaign,),
        )
        rows = cur.fetchall()
        if not rows:
            return
        statuses = [r[0] for r in rows]
        total = sum(float(r[1]) for r in rows)
        if all(s in ("ready", "failed") for s in statuses):
            status = "ready" if any(s == "ready" for s in statuses) else "failed"
        else:
            status = "processing"
        cur.execute(
            "UPDATE cms_generation_campaigns SET status=%s, total_cost_usd=%s, updated_at=now() "
            "WHERE id_campaign=%s",
            (status, total, id_campaign),
        )
        conn.commit()
    finally:
        conn.close()


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=f"{'https' if settings.MINIO_USE_SSL else 'http'}://{settings.MINIO_ENDPOINT}",
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def _db_conn():
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def _fail(id_job: int, reason: str, id_campaign: int | None = None) -> dict:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cms_generation_jobs SET status='failed', error_detail=%s, updated_at=now() "
            "WHERE id_job=%s RETURNING id_spend",
            (reason[:500], id_job),
        )
        # ⚠️ On SOLDE, on ne relâche pas : un job qui échoue a très souvent déjà appelé le
        # fournisseur (timeout, post-traitement raté). Relâcher rendrait au client un crédit
        # qu'il a réellement dépensé. En cas de doute, on compte.
        row = cur.fetchone()
        if row and row[0]:
            cur.execute(spend.SETTLE_SQL, (None, row[0]))
        conn.commit()
    finally:
        conn.close()
    _recompute_campaign(id_campaign)
    return {"id_job": id_job, "status": "failed", "reason": reason}


@celery_app.task(name="app.workers.generation_worker.generate_content")
def generate_content(id_job: int) -> dict:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id_org, kind, provider, model, prompt, aspect_ratio, params, created_by, "
            "id_campaign FROM cms_generation_jobs WHERE id_job=%s",
            (id_job,),
        )
        row = cur.fetchone()
        if not row:
            return {"id_job": id_job, "status": "missing"}
        id_org, kind, provider, model, prompt, aspect_ratio, params, created_by, id_campaign = row
        cur.execute(
            "UPDATE cms_generation_jobs SET status='processing', updated_at=now() WHERE id_job=%s",
            (id_job,),
        )
        conn.commit()
        _recompute_campaign(id_campaign)
        api_key = ai_credentials.resolve_provider_key_sync(conn, id_org, provider)
    finally:
        conn.close()

    if not api_key:
        return _fail(id_job, "no_key", id_campaign)

    # ── Génération (bloquant) → content / content_type / cost_usd ─────────────
    p = params or {}
    # Images de référence (image→image / image→vidéo) résolues en data URIs.
    image_urls: list[str] = []
    ref_ids = p.get("reference_media_ids") or []
    if ref_ids:
        rconn = _db_conn()
        try:
            image_urls = refs.data_uris_for_media_sync(rconn, id_org, ref_ids)
        finally:
            rconn.close()
    try:
        if kind == "image":
            asset = fal.generate_image_sync(
                prompt=prompt, api_key=api_key, model=model,
                aspect_ratio=aspect_ratio or "16:9", params=p, image_urls=image_urls,
            )
            content, content_type, cost_usd = asset.content, asset.content_type, asset.cost_usd
        elif kind == "video":
            asset = fal.generate_video_sync(
                prompt=prompt, api_key=api_key, model=model,
                aspect_ratio=aspect_ratio, params=p, image_urls=image_urls,
            )
            content, content_type, cost_usd = asset.content, asset.content_type, asset.cost_usd
        elif kind in ("music", "voice"):
            if provider == "fal":
                asset = fal.generate_audio_sync(
                    kind=kind, prompt=prompt, api_key=api_key, model=model, params=p
                )
                content, content_type, cost_usd = asset.content, asset.content_type, asset.cost_usd
            elif provider == "elevenlabs":
                if kind == "voice":
                    content, content_type, cost_usd = elevenlabs.generate_voice(
                        text=prompt, api_key=api_key,
                        voice_id=p.get("voice_id"),
                        model_id=p.get("model_id", elevenlabs.DEFAULT_TTS_MODEL),
                    )
                else:
                    content, content_type, cost_usd = elevenlabs.generate_music(
                        prompt=prompt, api_key=api_key, params=p
                    )
            else:
                return _fail(id_job, f"unsupported_provider:{provider}", id_campaign)
        else:
            return _fail(id_job, f"unsupported_kind:{kind}", id_campaign)
    except GenerationError as exc:
        return _fail(id_job, str(exc), id_campaign)
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_content %s a échoué", id_job)
        return _fail(id_job, f"internal error: {exc.__class__.__name__}", id_campaign)

    # Retrait optionnel de la bande audio (vidéo) — fiable, agnostique du modèle.
    if kind == "video" and p.get("strip_audio"):
        content = _strip_audio(content)

    # Coût autoritatif via le catalogue (cohérent avec l'estimatif affiché à la création).
    cost_usd = catalog.estimate_cost(
        model, params=p, aspect_ratio=aspect_ratio,
        text=(prompt if kind == "voice" else None),
    ) or cost_usd

    # ── Stockage + média ──────────────────────────────────────────────────────
    media_type = _MEDIA_TYPE.get(kind, "video")
    ext = {"audio/mpeg": "mp3", "audio/wav": "wav", "video/mp4": "mp4"}.get(
        content_type, (content_type.split("/")[-1] or "bin")
    )
    # Nom fourni par l'utilisateur (UI-only dans params), sinon défaut générique (pas le prompt).
    name = ((p.get("name") or "").strip() or _DEFAULT_NAME.get(kind, "AI content"))[:120]
    storage_key = make_storage_key(id_org, f"{name}.{ext}")
    sha256 = compute_sha256(content)
    try:
        _s3().put_object(
            Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key,
            Body=content, ContentType=content_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(id_job, f"upload failed: {exc.__class__.__name__}", id_campaign)

    meta = {"generation": {
        "provider": provider, "model": model, "prompt": prompt,
        "aspect_ratio": aspect_ratio, "cost_usd": float(cost_usd),
    }}
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO cms_media (id_org, media_name, media_type, mime_type, storage_key, "
            "file_size_bytes, checksum_sha256, source, transcode_status, created_by, metadata, "
            "created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'generated_ai','pending',%s,%s, now(), now()) "
            "RETURNING id_media",
            (id_org, name, media_type, content_type, storage_key,
             len(content), sha256, created_by, Json(meta)),
        )
        id_media = cur.fetchone()[0]
        cur.execute(
            "UPDATE cms_generation_jobs SET status='ready', id_media=%s, cost_usd=%s, "
            "updated_at=now() WHERE id_job=%s RETURNING id_spend",
            (id_media, cost_usd, id_job),
        )
        # Solde la réservation au montant RÉEL (P3.2). Sans ça elle resterait à l'estimation
        # pour toujours : le plafond dériverait de la facture, dans les deux sens.
        row = cur.fetchone()
        if row and row[0]:
            cur.execute(spend.SETTLE_SQL, (float(cost_usd or 0), row[0]))
        conn.commit()
    finally:
        conn.close()

    _recompute_campaign(id_campaign)

    # Transcode (probe + thumbnail + renditions auto) — comme un upload normal.
    from app.workers.media_worker import transcode_media
    transcode_media.delay(id_media)

    logger.info("generate_content %s → média %s (%s)", id_job, id_media, kind)
    return {"id_job": id_job, "status": "ready", "id_media": id_media}
