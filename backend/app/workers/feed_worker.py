"""Worker Feed Fetcher (§XXV.3) — polling RSS/Atom → cms_feed_items.

Trois tâches (file `default`, worker celery-worker) :
  - `fetch_feed(id_feed)` : télécharge + parse (feedparser) → normalise en items → dédup
    `ON CONFLICT (id_feed, item_guid)` → met à jour le statut du feed → force_sync de l'org
    (les players qui affichent ce feed rechargent leurs items au prochain sync, offline-first).
  - `poll_due_feeds()` : enqueue les feeds arrivés à échéance (selon `polling_interval_s`).
  - `purge_old_feed_items()` : rétention — supprime les items affichés vieux de > 7 jours.

Pas de Vault/OAuth ici : la tranche 1 couvre rss | atom | google_news (flux publics). Les
socials (proxy OAuth) viendront via signflow-feed-proxy. Thumbnails : on garde `image_url`
d'origine (re-hébergement MinIO = évolution, cf. §25.6 SSRF).
"""

import io
import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3
import httpx
import psycopg2
import psycopg2.extras
from botocore.config import Config
from PIL import Image

from app.core.config import settings
from app.schemas.feed import PROXY_TYPES
from app.services import notifications
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_FETCH_BYTES = 8 * 1024 * 1024  # garde-fou : ne pas avaler un flux géant
_ERROR_THRESHOLD = 5                # N échecs consécutifs → status='error' + alerte
_ERROR_BACKOFF_MAX_EXP = 6          # backoff sur erreur : facteur max = 2^6 = 64× l'intervalle
_ERROR_BACKOFF_MAX_S = 6 * 3600     # plafond absolu du backoff (6 h) — un feed mort ne poll plus
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # plafond d'une vignette distante re-hébergée
_THUMB_MAX_W = 800                  # largeur max d'une vignette (ratio conservé)
_REHOST_LIMIT = 50                  # nb max de vignettes re-hébergées par passe

# Extension d'image dans une URL (pour accepter un <media:content>/<enclosure> SANS attribut type).
_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|bmp)(?:[?#]|$)", re.IGNORECASE)


def _looks_like_image_url(url: str) -> bool:
    return bool(url) and bool(_IMG_EXT_RE.search(url))


def _db_conn():
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=f"{'https' if settings.MINIO_USE_SSL else 'http'}://{settings.MINIO_ENDPOINT}",
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def _host_is_safe(host: str) -> bool:
    """Anti-SSRF (§25.6) : refuse un hôte qui résout vers une IP privée/loopback/réservée."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _fetch_image(url: str) -> bytes | None:
    """Télécharge une image distante avec garde anti-SSRF + plafond de taille + check de type.
    `follow_redirects=False` : un redirect pourrait pointer vers une IP privée (contournement SSRF)
    → on ne suit pas (les images redirigées ne sont pas re-hébergées, repli = URL distante)."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if not _host_is_safe(parsed.hostname):
        logger.info("rehost: hôte non sûr ignoré (%s)", parsed.hostname)
        return None
    try:
        headers = {"User-Agent": "Vitrail-FeedFetcher/1.0 (+https://github.com/edkahall/vitrail)"}
        with httpx.Client(timeout=15, follow_redirects=False, headers=headers) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                if not resp.headers.get("content-type", "").lower().startswith("image/"):
                    return None
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_IMAGE_BYTES:
                        logger.info("rehost: image trop grosse, ignorée (%s)", url[:80])
                        return None
                return bytes(buf)
    except Exception as exc:  # noqa: BLE001
        logger.info("rehost: téléchargement échec %s: %s", url[:80], exc)
        return None


_YT_CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
_YT_RSS = "https://www.youtube.com/feeds/videos.xml"


def _youtube_rss_url(raw: str) -> str:
    """Résout l'entrée utilisateur d'une chaîne YouTube vers son URL RSS publique.

    Accepte : l'URL RSS directe (videos.xml), une URL/ID de chaîne (.../channel/UC… ou UC…),
    une URL de playlist (list=…), ou une URL de handle/custom (@handle, /user/, /c/) — résolue
    en scrappant la page (extraction du channelId). Repli = l'URL telle quelle (feedparser
    tentera ; échec → feed marqué en erreur)."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    if "feeds/videos.xml" in raw:
        return raw
    # channel_id explicite (dans l'URL /channel/UC… ou collé brut)
    m = _YT_CHANNEL_ID_RE.search(raw)
    if m:
        return f"{_YT_RSS}?channel_id={m.group(0)}"
    # playlist
    mp = re.search(r"[?&]list=([0-9A-Za-z_-]+)", raw)
    if mp:
        return f"{_YT_RSS}?playlist_id={mp.group(1)}"
    # handle / custom / user : scrape la page pour extraire le channelId
    url = raw if raw.lower().startswith(("http://", "https://")) else f"https://www.youtube.com/{raw.lstrip('/')}"
    try:
        if not _host_is_safe(urlparse(url).hostname or ""):
            return raw
        headers = {"User-Agent": "Mozilla/5.0 (Vitrail-FeedFetcher)"}
        # Cookie SOCS=CAI : contourne le mur de consentement cookies YouTube (FR/EU) qui sinon
        # redirige vers consent.youtube.com (page SANS channelId). Sans lui, la résolution échoue.
        with httpx.Client(timeout=15, follow_redirects=True, headers=headers,
                          cookies={"SOCS": "CAI"}) as client:
            html = client.get(url).text
        m2 = re.search(r'"(?:externalId|channelId|browseId)":"(UC[0-9A-Za-z_-]{22})"', html) \
            or _YT_CHANNEL_ID_RE.search(html)
        if m2:
            cid = m2.group(1) if m2.lastindex else m2.group(0)
            return f"{_YT_RSS}?channel_id={cid}"
    except Exception as exc:  # noqa: BLE001
        logger.info("youtube resolve échec %s: %s", url[:80], exc)
    return raw


def _parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _fetch_via_proxy(feed_type: str, id_org: int, config: dict) -> tuple[list[dict], str, str]:
    """Interroge signflow-feed-proxy pour un feed social/financier/météo. Retourne
    (items normalisés, status, note). Le proxy normalise déjà au schéma FeedItem (incl. champs
    numériques data_chart) ; on ne fait que parser published_at et borner les guids."""
    url = f"{settings.FEED_PROXY_URL.rstrip('/')}/proxy/feed"
    with httpx.Client(timeout=float(config.get("timeout_s") or 20)) as client:
        resp = client.post(url, json={"type": feed_type, "org_id": id_org, "config": config})
        resp.raise_for_status()
        data = resp.json()
    items: list[dict] = []
    for it in (data.get("items") or []):
        guid = it.get("guid") or it.get("link_url") or it.get("title")
        if not guid:
            continue
        items.append({
            "guid": str(guid)[:2000],
            "title": it.get("title"),
            "description": it.get("description"),
            "content_html": None,
            "image_url": it.get("image_url"),
            "link_url": it.get("link_url"),
            "author": it.get("author"),
            "categories": it.get("categories"),
            "published_at": _parse_iso(it.get("published_at")),
            "value_num": it.get("value_num"),
            "value_unit": it.get("value_unit"),
            "change_pct": it.get("change_pct"),
            "series": it.get("series"),
        })
    return items, data.get("status", "ok"), data.get("note", "")


def _normalize_entry(entry) -> dict | None:
    """feedparser entry → dict FeedItem normalisé. None si pas de guid exploitable."""
    guid = (
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or entry.get("title")
    )
    if not guid:
        return None

    # Image : media_thumbnail / media_content / enclosures (premier image/*).
    # NB : certains flux (Le Monde) exposent l'image en <media:content> SANS attribut `type`
    # (juste url/width/height) → on l'accepte si le `medium`=image OU si l'URL a une extension
    # image. On évite ainsi de rater l'image tout en n'attrapant pas une vidéo (type/medium video).
    image_url = None
    for mt in (entry.get("media_thumbnail") or []):
        if mt.get("url"):
            image_url = mt["url"]
            break
    if not image_url:
        for mc in (entry.get("media_content") or []):
            url = mc.get("url")
            if not url:
                continue
            mtype = str(mc.get("type", "")).lower()
            medium = str(mc.get("medium", "")).lower()
            if mtype.startswith("image") or medium == "image" or \
                    (not mtype and not medium and _looks_like_image_url(url)):
                image_url = url
                break
    if not image_url:
        for enc in (entry.get("enclosures") or []):
            href = enc.get("href")
            if not href:
                continue
            etype = str(enc.get("type", "")).lower()
            if etype.startswith("image") or (not etype and _looks_like_image_url(href)):
                image_url = href
                break

    published_at = None
    pp = entry.get("published_parsed") or entry.get("updated_parsed")
    if pp:
        try:
            published_at = datetime(*pp[:6], tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            published_at = None

    categories = [t.get("term") for t in (entry.get("tags") or []) if t.get("term")]
    content_html = None
    if entry.get("content"):
        try:
            content_html = entry["content"][0].get("value")
        except Exception:  # noqa: BLE001
            content_html = None

    return {
        "guid": str(guid)[:2000],
        "title": (entry.get("title") or "").strip() or None,
        "description": (entry.get("summary") or "").strip() or None,
        "content_html": content_html,
        "image_url": image_url,
        "link_url": entry.get("link"),
        "author": entry.get("author"),
        "categories": categories or None,
        "published_at": published_at,
        # champs numériques data_chart — non applicables aux flux RSS/Atom
        "value_num": None, "value_unit": None, "change_pct": None, "series": None,
    }


@celery_app.task(name="app.workers.feed_worker.fetch_feed", bind=True, max_retries=2)
def fetch_feed(self, id_feed: int) -> dict:
    """Télécharge + parse un feed RSS/Atom, dédup les items, force_sync l'org."""
    import feedparser  # import local (lib lourde, seul ce worker en a besoin)

    conn = _db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            "SELECT id_org, feed_type, config, moderation_enabled, ai_summary_enabled, max_items "
            "FROM cms_datafeeds WHERE id_feed = %s AND is_deleted = false",
            (id_feed,),
        )
        feed = cur.fetchone()
        if not feed:
            return {"id_feed": id_feed, "skipped": "not found"}
        id_org = feed["id_org"]
        config = feed["config"] or {}
        url = config.get("url")
        moderation = feed["moderation_enabled"]
        timeout_s = int(config.get("timeout_s") or 10)
        feed_type = feed["feed_type"]
        ai_summary = bool(feed["ai_summary_enabled"])
        max_items = int(feed["max_items"] or 10)
    finally:
        conn.close()

    # Feeds servis par le proxy (socials/financiers/météo) → microservice signflow-feed-proxy
    # (au lieu du fetch+feedparser direct). Dégradation gracieuse : status != ok → on conserve les
    # items déjà en base (pas d'écran vide), on note la cause sans incrémenter les erreurs dures.
    if feed_type in PROXY_TYPES:
        try:
            items, pstatus, pnote = _fetch_via_proxy(feed_type, id_org, config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_feed %s proxy échec: %s", id_feed, exc)
            _mark_feed_error(id_feed, f"proxy: {str(exc)[:280]}")
            raise self.retry(exc=exc, countdown=60)
        if pstatus != "ok":
            _set_feed_note(id_feed, f"{pstatus}: {pnote}"[:480])
            logger.info("fetch_feed %s proxy status=%s note=%s", id_feed, pstatus, pnote)
            return {"id_feed": id_feed, "status": pstatus, "note": pnote, "inserted": 0}
    else:
        if not url:
            _mark_feed_error(id_feed, "config.url missing")
            return {"id_feed": id_feed, "error": "no url"}

        # Chaîne YouTube : l'utilisateur colle l'URL de la chaîne (/@handle, /channel/UC…) ou son
        # RSS → on résout vers l'URL RSS publique (videos.xml). feedparser parse ensuite le flux
        # vidéo comme un RSS standard (media:thumbnail = vignette, link = page watch).
        if feed_type == "youtube_channel":
            url = _youtube_rss_url(url)

        # Anti-SSRF (audit 2026-06-24) : l'URL du feed est fournie par l'utilisateur. Sans garde,
        # le worker fetchait n'importe quel hôte interne (metadata cloud, minio, litellm, redis…)
        # et exposait le contenu via les items du feed. On refuse les IP non publiques et on ne
        # suit PAS les redirections (un 3xx pourrait contourner la garde vers une IP interne).
        parsed_url = urlparse(url)
        _safe_scheme = parsed_url.scheme in ("http", "https")
        if not _safe_scheme or not _host_is_safe(parsed_url.hostname or ""):
            logger.warning("fetch_feed %s : hôte non autorisé (%s)", id_feed, parsed_url.hostname)
            _mark_feed_error(id_feed, "URL not allowed (internal/private host)")
            return {"id_feed": id_feed, "error": "unsafe host"}

        # Téléchargement contrôlé (timeout + plafond taille) puis parsing.
        try:
            headers = {"User-Agent": "Vitrail-FeedFetcher/1.0 (+https://github.com/edkahall/vitrail)"}
            with httpx.Client(timeout=timeout_s, follow_redirects=False, headers=headers) as client:
                resp = client.get(url)
                resp.raise_for_status()
                content = resp.content[:_MAX_FETCH_BYTES]
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_feed %s download échec: %s", id_feed, exc)
            _mark_feed_error(id_feed, f"download: {str(exc)[:300]}")
            raise self.retry(exc=exc, countdown=60)

        parsed = feedparser.parse(content)
        entries = parsed.entries or []
        items = [n for n in (_normalize_entry(e) for e in entries) if n]

    inserted = 0
    now = datetime.now(timezone.utc)
    mod_status = "pending" if moderation else "approved"
    conn = _db_conn()
    try:
        cur = conn.cursor()
        for it in items:
            series = it.get("series")
            cur.execute(
                """
                INSERT INTO cms_feed_items
                    (id_feed, item_guid, title, description, content_html, image_url,
                     link_url, author, categories, published_at, fetched_at, moderation_status,
                     value_num, value_unit, change_pct, series)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id_feed, item_guid) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    content_html = EXCLUDED.content_html, link_url = EXCLUDED.link_url,
                    author = EXCLUDED.author, categories = EXCLUDED.categories,
                    published_at = EXCLUDED.published_at,
                    value_num = COALESCE(EXCLUDED.value_num, cms_feed_items.value_num),
                    value_unit = COALESCE(EXCLUDED.value_unit, cms_feed_items.value_unit),
                    change_pct = COALESCE(EXCLUDED.change_pct, cms_feed_items.change_pct),
                    series = COALESCE(EXCLUDED.series, cms_feed_items.series),
                    image_url = EXCLUDED.image_url,
                    -- image d'origine changée → re-héberger la NOUVELLE vignette (sinon vignette
                    -- stale + jamais re-hostée car le rehost ne traite que thumbnail_s3 IS NULL)
                    thumbnail_s3 = CASE WHEN cms_feed_items.image_url IS DISTINCT FROM EXCLUDED.image_url
                                        THEN NULL ELSE cms_feed_items.thumbnail_s3 END,
                    -- description changée → invalider le résumé IA (régénéré au prochain passage)
                    summary_ai = CASE WHEN cms_feed_items.description IS DISTINCT FROM EXCLUDED.description
                                      THEN NULL ELSE cms_feed_items.summary_ai END
                  -- ne « compter » (rowcount → force_sync) QUE sur un vrai changement de contenu
                  -- (un guid réutilisé à contenu identique = no-op, pas de resync parasite chaque poll)
                  WHERE cms_feed_items.title        IS DISTINCT FROM EXCLUDED.title
                     OR cms_feed_items.description  IS DISTINCT FROM EXCLUDED.description
                     OR cms_feed_items.content_html IS DISTINCT FROM EXCLUDED.content_html
                     OR cms_feed_items.image_url    IS DISTINCT FROM EXCLUDED.image_url
                     OR cms_feed_items.link_url     IS DISTINCT FROM EXCLUDED.link_url
                     OR cms_feed_items.author       IS DISTINCT FROM EXCLUDED.author
                     OR cms_feed_items.published_at IS DISTINCT FROM EXCLUDED.published_at
                     OR EXCLUDED.value_num  IS DISTINCT FROM cms_feed_items.value_num
                     OR EXCLUDED.change_pct IS DISTINCT FROM cms_feed_items.change_pct
                     OR EXCLUDED.series     IS DISTINCT FROM cms_feed_items.series
                """,
                (
                    id_feed, it["guid"], it["title"], it["description"], it["content_html"],
                    it["image_url"], it["link_url"], it["author"], it["categories"],
                    it["published_at"], now, mod_status,
                    it.get("value_num"), it.get("value_unit"), it.get("change_pct"),
                    psycopg2.extras.Json(series) if series is not None else None,
                ),
            )
            inserted += cur.rowcount
        cur.execute(
            "UPDATE cms_datafeeds SET last_fetched_at = %s, last_error = NULL, "
            "consecutive_errors = 0, status = CASE WHEN status='error' THEN 'active' ELSE status END, "
            "updated_at = now() WHERE id_feed = %s",
            (now, id_feed),
        )
        conn.commit()
    finally:
        conn.close()

    # Résumé IA (§XXV.3) : si activé, on condense les items les plus récents AVANT le force_sync
    # → le sync embarque déjà les résumés (1er rendu correct, pas de re-render différé). Best-effort
    # (LiteLLM down → summary_ai reste NULL → repli sur la description d'origine côté player).
    if inserted and ai_summary:
        try:
            _summarize_items(id_feed, limit=max(max_items, 10))
        except Exception as exc:  # noqa: BLE001
            logger.info("fetch_feed %s : résumé IA ignoré: %s", id_feed, exc)

    # Resync graceful des players de l'org si de nouveaux items sont arrivés (un feed affiché
    # rechargera ses items au prochain sync — offline-first, comme une capture screenshot_server).
    # + re-hébergement des vignettes dans MinIO (re-sync à nouveau quand elles sont prêtes).
    if inserted:
        _force_sync_org(id_org)
        try:
            rehost_feed_thumbnails.delay(id_feed)
        except Exception:  # noqa: BLE001
            pass

    logger.info("fetch_feed %s : %d entrées, %d nouveaux items", id_feed, len(items), inserted)
    return {"id_feed": id_feed, "entries": len(items), "inserted": inserted}


@celery_app.task(name="app.workers.feed_worker.rehost_feed_thumbnails")
def rehost_feed_thumbnails(id_feed: int) -> dict:
    """Re-héberge dans MinIO les vignettes (`image_url`) des items d'un feed (anti-SSRF +
    redimension JPEG) et pose `thumbnail_s3`. Le sync sert alors une URL signée de NOTRE infra
    (fini le hotlink / CORS / mixed-content ; le player ne fetch plus d'URL arbitraire). Resync
    graceful de l'org quand des vignettes ont été posées."""
    conn = _db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            "SELECT id_org FROM cms_datafeeds WHERE id_feed = %s AND is_deleted = false", (id_feed,)
        )
        row = cur.fetchone()
        if not row:
            return {"id_feed": id_feed, "skipped": "not found"}
        id_org = row["id_org"]
        cur.execute(
            "SELECT id_item, image_url FROM cms_feed_items "
            "WHERE id_feed = %s AND image_url IS NOT NULL AND thumbnail_s3 IS NULL "
            "AND moderation_status <> 'rejected' "
            "ORDER BY published_at DESC NULLS LAST, fetched_at DESC LIMIT %s",
            (id_feed, _REHOST_LIMIT),
        )
        todo = cur.fetchall()
    finally:
        conn.close()

    if not todo:
        return {"id_feed": id_feed, "rehosted": 0}

    s3 = _s3()
    done: list[tuple[int, str]] = []
    for it in todo:
        raw = _fetch_image(it["image_url"])
        if not raw:
            continue
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if img.width > _THUMB_MAX_W:
                ratio = _THUMB_MAX_W / img.width
                img = img.resize((_THUMB_MAX_W, max(1, int(img.height * ratio))))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=82)
            data = out.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.info("rehost: image illisible item %s: %s", it["id_item"], exc)
            continue
        key = f"feed_thumbs/{id_feed}/{it['id_item']}.jpg"
        try:
            s3.put_object(
                Bucket=settings.MINIO_BUCKET_MEDIA, Key=key, Body=data, ContentType="image/jpeg"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("rehost: upload MinIO échec item %s: %s", it["id_item"], exc)
            continue
        done.append((it["id_item"], key))

    if done:
        conn = _db_conn()
        try:
            cur = conn.cursor()
            for id_item, key in done:
                cur.execute(
                    "UPDATE cms_feed_items SET thumbnail_s3 = %s WHERE id_item = %s", (key, id_item)
                )
            conn.commit()
        finally:
            conn.close()
        _force_sync_org(id_org)

    logger.info("rehost_feed_thumbnails %s : %d vignette(s) re-hébergée(s)", id_feed, len(done))
    return {"id_feed": id_feed, "rehosted": len(done)}


def _summarize_items(id_feed: int, limit: int = 10) -> int:
    """Génère le `summary_ai` (LiteLLM) des items récents qui n'en ont pas encore (hors rejetés).
    Synchrone et best-effort : un item dont le résumé échoue garde `summary_ai = NULL` (repli
    sur la description côté player). Retourne le nombre d'items résumés."""
    from app.services import ai_credentials, feed_ai

    conn = _db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id_org FROM cms_datafeeds WHERE id_feed = %s", (id_feed,))
        frow = cur.fetchone()
        cur.execute(
            "SELECT id_item, title, description, content_html FROM cms_feed_items "
            "WHERE id_feed = %s AND summary_ai IS NULL AND moderation_status <> 'rejected' "
            "ORDER BY published_at DESC NULLS LAST, fetched_at DESC LIMIT %s",
            (id_feed, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    # BYOK §XXVII : résoudre la clé Anthropic de l'org (rien d'auto). Sans clé → pas de résumé.
    if frow is None:
        return 0
    rconn = _db_conn()
    try:
        ai_key, ai_mode = ai_credentials.resolve_anthropic_for_worker(rconn, frow["id_org"])
    finally:
        rconn.close()
    if ai_mode is None:
        logger.info("_summarize_items %s : IA désactivée pour l'org (BYOK)", id_feed)
        return 0
    key_arg = ai_key if ai_mode == "org" else None  # platform → proxy (clé plateforme)

    done: list[tuple[int, str]] = []
    for it in rows:
        summary = feed_ai.summarize(
            it["title"], it["content_html"] or it["description"], api_key=key_arg
        )
        if summary:
            done.append((it["id_item"], summary))

    if done:
        conn = _db_conn()
        try:
            cur = conn.cursor()
            for id_item, summary in done:
                cur.execute(
                    "UPDATE cms_feed_items SET summary_ai = %s WHERE id_item = %s", (summary, id_item)
                )
            conn.commit()
        finally:
            conn.close()
    logger.info("_summarize_items %s : %d/%d résumé(s)", id_feed, len(done), len(rows))
    return len(done)


@celery_app.task(name="app.workers.feed_worker.summarize_feed_items")
def summarize_feed_items(id_feed: int) -> dict:
    """Résume les items d'un feed puis resync l'org. Déclenchée quand `ai_summary_enabled` passe à
    true sur un feed existant (les items déjà affichés gagnent un résumé sans attendre le prochain
    poll)."""
    conn = _db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            "SELECT id_org, max_items FROM cms_datafeeds "
            "WHERE id_feed = %s AND is_deleted = false",
            (id_feed,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"id_feed": id_feed, "skipped": "not found"}
    n = _summarize_items(id_feed, limit=max(int(row["max_items"] or 10), 10))
    if n:
        _force_sync_org(row["id_org"])
    return {"id_feed": id_feed, "summarized": n}


def _set_feed_note(id_feed: int, note: str) -> None:
    """Note informative (ex. needs_credentials) sans incrémenter les erreurs dures ni passer le feed
    en 'error' : on enregistre last_error + last_fetched_at (évite un re-poll immédiat)."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cms_datafeeds SET last_error = %s, last_fetched_at = now(), updated_at = now() "
            "WHERE id_feed = %s",
            (note[:500], id_feed),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def _mark_feed_error(id_feed: int, err: str) -> None:
    conn = _db_conn()
    row = None
    try:
        cur = conn.cursor()
        # `last_fetched_at = now()` : on a bien TENTÉ un fetch (comme le chemin succès) → le feed
        # respecte au moins son `polling_interval_s` au lieu d'être re-pollé à chaque beat, et son
        # backoff exponentiel (poll_due_feeds) se calcule à partir de cet instant. §25.6
        cur.execute(
            "UPDATE cms_datafeeds SET last_error = %s, consecutive_errors = consecutive_errors + 1, "
            "status = CASE WHEN consecutive_errors + 1 >= %s THEN 'error' ELSE status END, "
            "last_fetched_at = now(), updated_at = now() WHERE id_feed = %s "
            "RETURNING id_org, feed_name, consecutive_errors, status",
            (err[:500], _ERROR_THRESHOLD, id_feed),
        )
        row = cur.fetchone()  # (id_org, feed_name, consecutive_errors[nouveau], status[nouveau])
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        conn.close()
        return

    # Alerte à la TRANSITION vers 'error' (§XXII) — une seule fois par épisode : `consecutive_errors`
    # atteint EXACTEMENT le seuil (il incrémente de 1 par échec et est remis à 0 au 1er succès, donc
    # ne re-vaut le seuil qu'au début d'un nouvel épisode). Après le commit santé, best-effort : un
    # webhook injoignable ne doit pas casser le poll du feed.
    try:
        if row and int(row[2]) == _ERROR_THRESHOLD:
            notifications.dispatch_sync(conn, int(row[0]), "feed_error", {
                "id_feed": id_feed, "feed_name": row[1], "error": err[:200],
            })
    except Exception:  # noqa: BLE001
        logger.exception("dispatch feed_error failed (feed %s)", id_feed)
    finally:
        conn.close()


def _force_sync_org(id_org: int) -> None:
    try:
        import json
        import redis as _redis
        conn = _db_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id_player FROM cms_players WHERE id_org = %s", (id_org,))
            ids = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        if not ids:
            return
        r = _redis.from_url(settings.REDIS_URL)
        msg = json.dumps({"type": "player:force_sync", "preemptive": False})
        for pid in ids:
            r.publish(f"signflow:player:{pid}:cmds", msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("force_sync feed org %s échec: %s", id_org, exc)


@celery_app.task(name="app.workers.feed_worker.poll_due_feeds")
def poll_due_feeds() -> dict:
    """Enqueue les feeds arrivés à échéance. Intervalle effectif = `polling_interval_s` en régime
    nominal, **backoff exponentiel sur erreur** (× 2^consecutive_errors, plafonné) : un feed qui
    échoue est re-tenté de moins en moins souvent (2×, 4×, 8×… l'intervalle) au lieu d'être re-pollé
    en boucle. `GREATEST(polling_interval_s, …)` garantit qu'on ne RACCOURCIT jamais l'intervalle
    d'un feed à longue période. Un feed sain (consecutive_errors=0 → 2^0=1) garde son intervalle."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id_feed FROM cms_datafeeds
            WHERE is_deleted = false AND status <> 'paused'
              AND (last_fetched_at IS NULL
                   OR last_fetched_at + make_interval(secs => GREATEST(
                          polling_interval_s,
                          LEAST(
                              (polling_interval_s * POWER(2, LEAST(consecutive_errors, %s)))::bigint,
                              %s
                          )
                      )) <= now())
            """,
            (_ERROR_BACKOFF_MAX_EXP, _ERROR_BACKOFF_MAX_S),
        )
        ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    for id_feed in ids:
        fetch_feed.delay(id_feed)
    return {"enqueued": len(ids)}


@celery_app.task(name="app.workers.feed_worker.purge_old_feed_items")
def purge_old_feed_items() -> dict:
    """Rétention §25.6 : purge les items affichés vieux de > 7 jours."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM cms_feed_items "
            "WHERE is_shown = true AND last_shown_at IS NOT NULL "
            "AND last_shown_at < now() - interval '7 days'"
        )
        purged = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"purged": purged}
