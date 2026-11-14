"""Worker « moteur Electron » (issue vitrail#5) — récupère un moteur officiel pour le catalogue.

Tâche longue (~100 Mo à télécharger puis à reconditionner) : jamais dans une requête HTTP.
L'état de chaque tâche est tenu dans Redis (`signflow:engine_fetch:<job>`, 24 h) pour que
l'interface puisse le suivre ; la ligne catalogue n'est créée qu'une fois l'archive stockée.
"""

from __future__ import annotations

import json
import logging
import tempfile

import httpx
import psycopg2
import redis

from app.core.config import settings
from app.services import engine_import, media
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

JOB_TTL_S = 24 * 3600


def job_key(job_id: str) -> str:
    return f"signflow:engine_fetch:{job_id}"


def set_job(job_id: str, **fields) -> None:
    r = redis.from_url(settings.REDIS_URL)
    try:
        cur = r.get(job_key(job_id))
        data = json.loads(cur) if cur else {}
        data.update(fields)
        r.set(job_key(job_id), json.dumps(data), ex=JOB_TTL_S)
    finally:
        r.close()


def _insert_version(version: str, family: str, sha256: str, size: int, notes: str) -> bool:
    """Ligne catalogue `track='engine'` en `beta` (D3 : banc d'abord, promote ensuite).
    Idempotent par la contrainte unique (track, version, model_family). True si créée."""
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO cms_player_update_versions "
            "(track, version, channel, model_family, download_url, sha256_checksum, "
            " storage_key, file_size, original_filename, release_notes) "
            "VALUES ('engine', %s, 'beta', %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT uq_cms_player_update_versions_track_version_family "
            "DO NOTHING RETURNING id_version",
            (version, family, engine_import.download_url(family, version), sha256,
             engine_import.storage_key(family, version), size,
             f"electron-{version}-{family}.tar.gz", notes),
        )
        created = cur.fetchone() is not None
        conn.commit()
        return created
    finally:
        conn.close()


@celery_app.task(name="app.workers.engine_worker.fetch_engine")
def fetch_engine(job_id: str, version: str, family: str) -> dict:
    set_job(job_id, state="downloading", version=version, family=family)
    try:
        with tempfile.TemporaryDirectory(prefix="sf-engine-") as wd:
            with httpx.Client(timeout=httpx.Timeout(20, read=120), follow_redirects=False) as c:
                pkg = engine_import.fetch_and_package(version, family, wd, c)
            set_job(job_id, state="storing")
            key = engine_import.storage_key(family, version)
            with open(pkg["tar_path"], "rb") as f:
                sha, size = media._stream_upload_sync(f, key, "application/gzip")
        notes = (f"Official Electron {version} ({pkg['zip_name']}, "
                 f"sha256 {pkg['zip_sha256']}), repackaged by the server.")
        created = _insert_version(version, family, sha, size, notes)
        set_job(job_id, state="done", created=created, sha256=sha, size=size)
        return {"job_id": job_id, "created": created}
    except engine_import.EngineImportError as exc:
        set_job(job_id, state="error", error=str(exc))
        return {"job_id": job_id, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — l'exploitant doit voir un échec, jamais un « en cours » éternel
        logger.exception("fetch_engine %s %s failed", version, family)
        set_job(job_id, state="error", error=f"Unexpected error: {exc.__class__.__name__}")
        return {"job_id": job_id, "error": "unexpected"}
