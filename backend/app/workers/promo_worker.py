"""Worker de rendu des visuels promo (Phase 1, §9 du plan).

Tourne dans le conteneur **celery-screenshot** (Playwright/Chromium, file `cms_screenshot`) :
`render_promo_image(id_job)` résout le template + le compositing HTML, rend le composite à la
**résolution cible exacte** via Playwright `set_content` (pas de réseau) → PNG → MinIO → cms_media
`generated_ai` → transcode (vignette + renditions) → job `ready`. 100% local (aucun appel fal).

Import Playwright LAZY (comme screenshot_worker) : les autres workers importent ce module sans
Chromium, ils ne consomment juste pas la file `cms_screenshot`.
"""

import base64
import io
import logging

import boto3
import psycopg2
from botocore.client import Config
from psycopg2.extras import Json

from app.core.config import settings
from app.services import brand_kit
from app.services.media import compute_sha256, make_storage_key
from app.services.promo import PromoError, conformity, fonts, org_fonts, render
from app.services.promo.templates import resolve_template
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


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


def _fail(id_job: int, reason: str) -> dict:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cms_generation_jobs SET status='failed', error_detail=%s, updated_at=now() "
            "WHERE id_job=%s", (reason[:500], id_job),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id_job": id_job, "status": "failed", "reason": reason}


def _media_data_uri(s3, conn, id_org: int, id_media, *, max_w: int | None = None) -> str | None:
    """Résout un cms_media image en data URI (embarqué dans le HTML → pas de réseau Playwright).

    `max_w` : downscale (fond) pour borner la taille base64 ; None = brut (produit, garde alpha)."""
    if not id_media:
        return None
    cur = conn.cursor()
    cur.execute(
        "SELECT storage_key, mime_type FROM cms_media "
        "WHERE id_media=%s AND id_org=%s AND media_type='image' AND is_deleted=false",
        (id_media, id_org),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    storage_key, mime = row[0], (row[1] or "image/png")
    data = s3.get_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key)["Body"].read()
    if max_w:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.convert("RGB").resize((max_w, max(1, int(img.height * ratio))))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=88)
                data, mime = buf.getvalue(), "image/jpeg"
        except Exception as exc:  # noqa: BLE001 — downscale best-effort
            logger.warning("promo: downscale fond échoué (%s), image brute", exc)
    b64 = base64.b64encode(data).decode()
    return f"data:{mime};base64,{b64}"


def _start_job(id_job: int, kind: str):
    """Marque le job 'processing' + renvoie (id_org, params, created_by) ou None si absent."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id_org, params, created_by FROM cms_generation_jobs "
            "WHERE id_job=%s AND kind=%s", (id_job, kind),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "UPDATE cms_generation_jobs SET status='processing', updated_at=now() WHERE id_job=%s",
            (id_job,),
        )
        conn.commit()
        return row
    finally:
        conn.close()


def _render_poster_png(id_org: int, p: dict) -> tuple[bytes, int, int, str]:
    """Rend l'affiche promo en PNG (compositing Jinja2→Playwright). (png, w, h, label).
    Lève PromoError si non conforme / template invalide (barrière serveur)."""
    template_id = p.get("template_id")
    fmt = p.get("format", "16:9")
    resolution = p.get("resolution", "fhd")
    inputs = p.get("inputs") or {}

    verdict = conformity.validate(template_id, fmt, inputs)
    if not verdict["valid"]:
        raise PromoError("conformity: " + "; ".join(e["message"] for e in verdict["errors"]))

    s3 = _s3()
    conn = _db_conn()
    try:
        # Catalogue de polices = built-in + polices custom de l'org (§12.7).
        catalog = fonts.builtin_catalog()
        catalog.update(org_fonts.org_font_catalog_sync(conn, id_org))
        # Charte de marque (P3.4) : celle de l'annonceur visé, sinon celle de l'org, sinon rien.
        # Elle est fusionnée AVANT la résolution pour que slots et calques voient la même palette.
        kit = brand_kit.resolve_brand_kit_sync(conn, id_org, p.get("id_advertiser"))
        resolved = resolve_template(
            template_id, fmt, resolution, inputs, font_catalog=catalog,
            theme_overrides=brand_kit.theme_overrides(kit, catalog))
        width, height = resolved["width"], resolved["height"]
        bg_uri = _media_data_uri(s3, conn, id_org, p.get("background_media_id"), max_w=width)
        product_uri = _media_data_uri(s3, conn, id_org, p.get("product_media_id"))
        # Calques image (Phase B) : injecte leur data-URI (downscalé à la largeur cible).
        for layer in resolved.get("layers") or []:
            if layer.get("type") == "image" and layer.get("media_id"):
                layer["data_uri"] = _media_data_uri(
                    s3, conn, id_org, layer["media_id"], max_w=width)
    finally:
        conn.close()

    loader = org_fonts.make_custom_loader(s3, settings.MINIO_BUCKET_MEDIA)
    font_faces_css = fonts.font_face_css(
        fonts.used_font_faces(resolved, catalog), catalog, custom_loader=loader)
    html = render.build_html(resolved, bg_uri, product_uri, font_faces_css=font_faces_css)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": width, "height": height},
                                  device_scale_factor=1)
        page = ctx.new_page()
        page.set_content(html, wait_until="load")
        try:
            page.evaluate("window.__fit && window.__fit()")
        except Exception:  # noqa: BLE001 — l'auto-fit s'exécute déjà au chargement
            pass
        page.wait_for_timeout(120)
        png = page.screenshot(type="png")
        browser.close()
    return png, width, height, resolved["label"]


def _insert_media(id_org, name, media_type, mime, storage_key, content, created_by, meta):
    """INSERT cms_media (pending) → id_media."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO cms_media (id_org, media_name, media_type, mime_type, storage_key, "
            "file_size_bytes, checksum_sha256, source, transcode_status, created_by, metadata, "
            "created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'generated_ai','pending',%s,%s, now(), now()) "
            "RETURNING id_media",
            (id_org, name, media_type, mime, storage_key, len(content),
             compute_sha256(content), created_by, Json(meta)),
        )
        return cur.fetchone()[0]
    finally:
        conn.commit()
        conn.close()


def _mark_ready(id_job: int, id_media: int) -> None:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cms_generation_jobs SET status='ready', id_media=%s, cost_usd=0, "
            "updated_at=now() WHERE id_job=%s", (id_media, id_job),
        )
        conn.commit()
    finally:
        conn.close()


@celery_app.task(name="app.workers.promo_worker.render_promo_image")
def render_promo_image(id_job: int) -> dict:
    row = _start_job(id_job, "promo_image")
    if row is None:
        return {"id_job": id_job, "status": "missing"}
    id_org, params, created_by = row
    p = params or {}
    try:
        png, width, height, label = _render_poster_png(id_org, p)
    except PromoError as exc:
        return _fail(id_job, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("render_promo_image %s: rendu échoué", id_job)
        return _fail(id_job, f"render failed: {exc.__class__.__name__}")

    name = ((p.get("name") or "").strip() or f"Promo {label}")[:120]
    storage_key = make_storage_key(id_org, f"{name}.png")
    try:
        _s3().put_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key,
                         Body=png, ContentType="image/png")
    except Exception as exc:  # noqa: BLE001
        return _fail(id_job, f"upload failed: {exc.__class__.__name__}")

    meta = {"promo": {"template_id": p.get("template_id"), "format": p.get("format"),
                      "resolution": p.get("resolution"), "width": width, "height": height,
                      "inputs": p.get("inputs") or {}}}
    id_media = _insert_media(id_org, name, "image", "image/png", storage_key, png, created_by, meta)
    _mark_ready(id_job, id_media)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(id_media)
    logger.info("render_promo_image %s → média %s (%sx%s)", id_job, id_media, width, height)
    return {"id_job": id_job, "status": "ready", "id_media": id_media}


# ── Vidéo : pipeline 2 étapes (Playwright n'a pas ffmpeg, et inversement) ─────────
# Étape 1 (file cms_screenshot) : rend l'affiche PNG → clé temp MinIO → dispatch l'anim.
# Étape 2 (file media) : ffmpeg (Ken Burns + fade) → MP4 → cms_media vidéo → transcode.

@celery_app.task(name="app.workers.promo_worker.render_promo_video")
def render_promo_video(id_job: int) -> dict:
    row = _start_job(id_job, "promo_video")
    if row is None:
        return {"id_job": id_job, "status": "missing"}
    id_org, params, _created_by = row
    p = params or {}
    try:
        png, width, height, _label = _render_poster_png(id_org, p)
    except PromoError as exc:
        return _fail(id_job, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("render_promo_video %s: rendu affiche échoué", id_job)
        return _fail(id_job, f"render failed: {exc.__class__.__name__}")

    tmp_key = f"promo-video-tmp/{id_org}/{id_job}.png"
    try:
        _s3().put_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=tmp_key,
                         Body=png, ContentType="image/png")
    except Exception as exc:  # noqa: BLE001
        return _fail(id_job, f"poster upload failed: {exc.__class__.__name__}")

    # Étape 2 sur la file 'media' (ffmpeg). Le job reste 'processing' d'ici là.
    animate_promo_video.delay(id_job, tmp_key, width, height)
    return {"id_job": id_job, "status": "processing", "stage": "animate"}


def _kenburns_cmd(in_png: str, out_mp4: str, w: int, h: int,
                  duration: float, animation: str) -> list:
    """Commande ffmpeg : anime une affiche fixe (Ken Burns + fade). Le texte reste net
    (cuit dans le PNG à la résolution cible)."""
    dur = max(3.0, min(15.0, float(duration or 8)))
    fps = 30
    nframes = int(dur * fps)
    zinc = 0.16 / max(1, nframes)          # atteint ~1.16x sur la durée
    scale_h = min(h * 2, 2160)             # upscale léger → zoompan plus fluide
    if animation == "static":
        vf = f"scale={w}:{h}"
    else:
        if animation == "zoom_out":
            z = f"if(eq(on,0),1.16,max(zoom-{zinc:.6f},1.0))"
        else:  # zoom_in (défaut)
            z = f"min(zoom+{zinc:.6f},1.16)"
        vf = (f"scale=-2:{scale_h},zoompan=z='{z}':"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={nframes}:s={w}x{h}:fps={fps}")
    fo = max(0.0, dur - 0.5)
    vf += f",fade=t=in:st=0:d=0.5,fade=t=out:st={fo:.2f}:d=0.5,format=yuv420p"
    return ["ffmpeg", "-y", "-loop", "1", "-i", in_png, "-t", f"{dur:.2f}", "-r", str(fps),
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out_mp4]


@celery_app.task(name="app.workers.promo_worker.animate_promo_video")
def animate_promo_video(id_job: int, tmp_key: str, width: int, height: int) -> dict:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id_org, params, created_by FROM cms_generation_jobs WHERE id_job=%s",
                    (id_job,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"id_job": id_job, "status": "missing"}
    id_org, params, created_by = row
    p = params or {}
    vopts = p.get("video") or {}
    s3 = _s3()

    def _cleanup_tmp():
        try:
            s3.delete_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=tmp_key)
        except Exception:  # noqa: BLE001
            pass

    import os
    import subprocess  # nosec B404
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="promovid_")
    in_png = os.path.join(tmpdir, "poster.png")
    out_mp4 = os.path.join(tmpdir, "out.mp4")
    try:
        obj = s3.get_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=tmp_key)["Body"].read()
        with open(in_png, "wb") as f:
            f.write(obj)
        cmd = _kenburns_cmd(in_png, out_mp4, width, height,
                            vopts.get("duration_s", 8), vopts.get("animation", "zoom_in"))
        res = subprocess.run(cmd, capture_output=True, timeout=180)  # nosec B603
        if res.returncode != 0 or not os.path.exists(out_mp4):
            logger.error("animate_promo_video %s ffmpeg: %s", id_job, res.stderr[-500:])
            _cleanup_tmp()
            return _fail(id_job, "video animation failed (ffmpeg)")
        with open(out_mp4, "rb") as f:
            mp4 = f.read()
    except Exception as exc:  # noqa: BLE001
        logger.exception("animate_promo_video %s", id_job)
        _cleanup_tmp()
        return _fail(id_job, f"animation failed: {exc.__class__.__name__}")
    finally:
        for fp in (in_png, out_mp4):
            try:
                os.path.exists(fp) and os.remove(fp)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    name = ((p.get("name") or "").strip() or "Promo video")[:120]
    storage_key = make_storage_key(id_org, f"{name}.mp4")
    try:
        s3.put_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key,
                      Body=mp4, ContentType="video/mp4")
    except Exception as exc:  # noqa: BLE001
        _cleanup_tmp()
        return _fail(id_job, f"video upload failed: {exc.__class__.__name__}")

    meta = {"promo": {"template_id": p.get("template_id"), "format": p.get("format"),
                      "resolution": p.get("resolution"), "kind": "video",
                      "video": vopts, "inputs": p.get("inputs") or {}}}
    id_media = _insert_media(id_org, name, "video", "video/mp4", storage_key, mp4, created_by, meta)
    _mark_ready(id_job, id_media)
    _cleanup_tmp()

    from app.workers.media_worker import transcode_media
    transcode_media.delay(id_media)
    logger.info("animate_promo_video %s → média %s", id_job, id_media)
    return {"id_job": id_job, "status": "ready", "id_media": id_media}
