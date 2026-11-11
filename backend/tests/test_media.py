"""
Tests Sprint 1.2 — Media Library + Stock Library (§XVI.2).

Couvre :
  - test_upload_image
  - test_list_media
  - test_get_media_returns_signed_url
  - test_delete_media_is_logical
  - test_upload_dispatches_transcode_task
  - test_stock_search_no_key_returns_503
  - test_stock_import
"""

import uuid
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from httpx import AsyncClient
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.models.media import CmsMedia, CmsMediaRendition
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import (
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser

# PNG 1×1 pixel minimal (image valide pour Pillow)
_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
    b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
    b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    # Slug unique par invocation — évite les conflits UNIQUE si la base de test
    # n'est pas vidée entre les tests.
    slug = f"media-{uuid.uuid4().hex[:8]}"
    o = CmsOrganization(org_name="MediaOrg", org_slug=slug)
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest_asyncio.fixture
async def media_user(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    """
    User avec permissions media:create/read/edit/delete via un custom role.
    On crée les permissions avec ON CONFLICT DO NOTHING pour éviter les
    violations de contrainte unique si elles existent déjà dans la base de test.
    """
    perms_data = [("media", "create"), ("media", "read"), ("media", "edit"), ("media", "delete")]
    perm_ids = []
    for module, operation in perms_data:
        await db_session.execute(
            pg_insert(CmsPermission)
            .values(module=module, operation=operation, label=f"{module}.{operation}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        )
    await db_session.flush()
    for module, operation in perms_data:
        result = await db_session.execute(
            select(CmsPermission).where(
                CmsPermission.module == module,
                CmsPermission.operation == operation,
            )
        )
        perm_ids.append(result.scalar_one().id_permission)

    role = CmsRole(
        role_key=f"TEST_MEDIA_{uuid.uuid4().hex[:6].upper()}",
        label="Test Media User",
        is_system=False,
        id_org=org.id_org,
    )
    db_session.add(role)
    await db_session.flush()

    for pid in perm_ids:
        db_session.add(CmsRolePermission(id_role=role.id_role, id_permission=pid))

    email = f"uploader.{uuid.uuid4().hex[:8]}@media.com"
    user = CmsUser(
        id_org=org.id_org,
        email=email,
        password_hash=hash_password("pass"),
    )
    db_session.add(user)
    await db_session.flush()

    await db_session.execute(
        pg_insert(CmsUserRoleAssignment)
        .values(id_user=user.id_user, id_role=role.id_role, is_deny=False)
        .on_conflict_do_nothing()
    )
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient, media_user: CmsUser) -> dict:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": media_user.email, "password": "pass"},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def minio_mock():
    """Mock MinIO via moto (S3-compatible). Intercepte tous les appels boto3."""
    with mock_aws():
        client = boto3.client(
            "s3",
            endpoint_url=f"http://{settings.MINIO_ENDPOINT}",
            aws_access_key_id=settings.MINIO_ACCESS_KEY,
            aws_secret_access_key=settings.MINIO_SECRET_KEY,
            region_name="us-east-1",
        )
        try:
            client.create_bucket(Bucket=settings.MINIO_BUCKET_MEDIA)
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
        yield client


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_image(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
) -> None:
    """Upload d'une image PNG — crée l'entrée cms_media avec transcode_status=pending."""
    with patch("app.workers.media_worker.transcode_media.delay"):
        resp = await client.post(
            "/api/v1/media/upload",
            files={"file": ("test.png", BytesIO(_PNG_1PX), "image/png")},
            headers=auth_headers,
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["media_type"] == "image"
    assert data["transcode_status"] == "pending"
    assert data["source"] == "upload"
    assert data.get("cdn_url_signed") is not None


@pytest.mark.asyncio
async def test_list_media(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """Liste paginée — exclut les médias supprimés logiquement."""
    uid = uuid.uuid4().hex[:8]
    db_session.add(CmsMedia(
        id_org=media_user.id_org,
        media_name=f"visible_{uid}.mp4",
        media_type="video",
        storage_key=f"{media_user.id_org}/visible_{uid}.mp4",
        checksum_sha256="aaa",
        transcode_status="done",
    ))
    db_session.add(CmsMedia(
        id_org=media_user.id_org,
        media_name=f"deleted_{uid}.mp4",
        media_type="video",
        storage_key=f"{media_user.id_org}/deleted_{uid}.mp4",
        checksum_sha256="bbb",
        transcode_status="done",
        is_deleted=True,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/media/", headers=auth_headers)
    assert resp.status_code == 200
    names = [m["media_name"] for m in resp.json()]
    assert f"visible_{uid}.mp4" in names
    assert f"deleted_{uid}.mp4" not in names


@pytest.mark.asyncio
async def test_get_media_returns_signed_url(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """GET /media/{id} retourne les métadonnées avec un cdn_url_signed non vide."""
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="photo.jpg",
        media_type="image",
        storage_key=f"{media_user.id_org}/{uuid.uuid4().hex}.jpg",
        checksum_sha256="abc123",
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    resp = await client.get(f"/api/v1/media/{media.id_media}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id_media"] == media.id_media
    assert data["cdn_url_signed"] is not None
    assert len(data["cdn_url_signed"]) > 0


@pytest.mark.asyncio
async def test_rename_media(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """PATCH /media/{id}/rename change le libellé (fichier inchangé) ; nom vide → 422."""
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="Vidéo IA",
        media_type="video",
        storage_key=f"{media_user.id_org}/{uuid.uuid4().hex}.mp4",
        checksum_sha256="abc123",
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)
    old_key = media.storage_key

    resp = await client.patch(
        f"/api/v1/media/{media.id_media}/rename",
        json={"media_name": "Promo rentrée 2026"}, headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["media_name"] == "Promo rentrée 2026"

    await db_session.refresh(media)
    assert media.media_name == "Promo rentrée 2026"
    assert media.storage_key == old_key  # le fichier ne bouge pas

    # nom vide (espaces) → 422
    bad = await client.patch(
        f"/api/v1/media/{media.id_media}/rename",
        json={"media_name": "   "}, headers=auth_headers,
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_download_media_returns_attachment_url(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """GET /media/{id}/download → URL pré-signée forçant le téléchargement (attachment + nom)."""
    from urllib.parse import unquote

    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="photo.jpg",
        media_type="image",
        storage_key=f"{media_user.id_org}/{uuid.uuid4().hex}.jpg",
        checksum_sha256="abc123",
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    resp = await client.get(f"/api/v1/media/{media.id_media}/download", headers=auth_headers)
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert "response-content-disposition" in url.lower()
    decoded = unquote(url)
    assert "attachment" in decoded
    assert "photo.jpg" in decoded


@pytest.mark.asyncio
async def test_download_media_404_without_file(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """Un média pointeur (feed/page web) sans storage_key → 404 (rien à télécharger)."""
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="Mon Feed",
        media_type="feed_display",
        storage_key="",          # sentinelle : média pointeur sans fichier (NOT NULL en base)
        checksum_sha256="",
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    resp = await client.get(f"/api/v1/media/{media.id_media}/download", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_media_is_logical(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """DELETE marque is_deleted=True (l'objet reste en DB) et retourne 404 ensuite."""
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="to_delete.jpg",
        media_type="image",
        storage_key=f"{media_user.id_org}/{uuid.uuid4().hex}.jpg",
        checksum_sha256="deadbeef",
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    with patch("app.workers.media_worker.delete_media_files.delay"):
        resp = await client.delete(f"/api/v1/media/{media.id_media}", headers=auth_headers)
    assert resp.status_code == 200

    # Toujours présent en DB, marqué supprimé
    result = await db_session.execute(
        select(CmsMedia).where(CmsMedia.id_media == media.id_media)
    )
    updated = result.scalar_one()
    assert updated.is_deleted is True

    # GET doit maintenant retourner 404 (is_deleted filtré côté applicatif)
    resp2 = await client.get(f"/api/v1/media/{media.id_media}", headers=auth_headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_delete_media_purges_rendition_rows(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """Le soft-delete d'un parent purge ses lignes cms_media_renditions (pas de FK CASCADE sur UPDATE)."""
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="parent_4k.mp4",
        media_type="video",
        storage_key=f"{media_user.id_org}/{uuid.uuid4().hex}.mp4",
        checksum_sha256="cafe",
        transcode_status="done",
        has_renditions=True,
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    db_session.add(CmsMediaRendition(
        id_parent=media.id_media, profile="fhd", width=1920, height=1080,
        codec="h264", storage_key=f"v/{media.id_media}.fhd.mp4", status="ready",
    ))
    await db_session.commit()

    with patch("app.workers.media_worker.delete_media_files.delay"):
        resp = await client.delete(f"/api/v1/media/{media.id_media}", headers=auth_headers)
    assert resp.status_code == 200

    remaining = (await db_session.execute(
        select(CmsMediaRendition).where(CmsMediaRendition.id_parent == media.id_media)
    )).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_replace_media_stages_without_disrupting_live_content(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """Replace = STAGING : l'ancien contenu (storage_key, renditions, objets MinIO) reste
    intact et diffusé ; on enregistre seulement pending_replace + staging_key et on
    re-dispatch le transcodage (la bascule atomique se fait en fin de tâche worker)."""
    from app.models.audit_log import CmsAuditLog

    old_key = f"{media_user.id_org}/old_{uuid.uuid4().hex}.mp4"
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="093_clip_4k.mp4",
        media_type="video",
        mime_type="video/mp4",
        storage_key=old_key,
        file_size_bytes=12345,
        checksum_sha256="old-sum",
        transcode_status="done",
        conformance_status="ready",
        has_renditions=True,
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)
    id_media = media.id_media

    db_session.add(CmsMediaRendition(
        id_parent=id_media, profile="fhd", width=1920, height=1080,
        codec="h264", storage_key=f"v/{id_media}.fhd.mp4", status="ready",
    ))
    await db_session.commit()

    with patch("app.workers.media_worker.transcode_media.delay") as mock_transcode:
        resp = await client.post(
            f"/api/v1/media/{id_media}/replace",
            files={"file": ("093_clip_4k.mkv", BytesIO(b"new-bytes-hevc"), "video/x-matroska")},
            headers=auth_headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id_media"] == id_media

    await db_session.refresh(media)
    # L'ancien contenu est INTACT (toujours diffusé) — seul un retraitement est signalé.
    assert media.storage_key == old_key
    assert media.checksum_sha256 == "old-sum"
    assert media.has_renditions is True
    assert media.transcode_status == "processing"

    # Le nouveau fichier est en staging dans metadata.pending_replace.
    pr = (media.media_metadata or {}).get("pending_replace")
    assert pr is not None and pr["staging_key"] and pr["staging_key"] != old_key
    assert old_key in pr["cleanup_keys"]

    # Renditions PAS encore purgées (elles servent encore l'ancien contenu).
    remaining = (await db_session.execute(
        select(CmsMediaRendition).where(CmsMediaRendition.id_parent == id_media)
    )).scalars().all()
    assert len(remaining) == 1

    mock_transcode.assert_called_once_with(id_media)

    audit = (await db_session.execute(
        select(CmsAuditLog).where(
            CmsAuditLog.action == "media_replace",
            CmsAuditLog.resource_id == str(id_media),
        )
    )).scalar_one_or_none()
    assert audit is not None
    assert audit.detail["staging_key"] == pr["staging_key"]


@pytest.mark.asyncio
async def test_replace_media_rejects_type_mismatch(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
    db_session: AsyncSession,
    media_user: CmsUser,
) -> None:
    """On ne peut pas remplacer une vidéo par une image (durées/rendu playlist en dépendent)."""
    media = CmsMedia(
        id_org=media_user.id_org,
        media_name="clip.mp4",
        media_type="video",
        storage_key=f"{media_user.id_org}/{uuid.uuid4().hex}.mp4",
        checksum_sha256="x",
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    resp = await client.post(
        f"/api/v1/media/{media.id_media}/replace",
        files={"file": ("img.png", BytesIO(_PNG_1PX), "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_dispatches_transcode_task(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
) -> None:
    """L'upload déclenche transcode_media.delay avec l'id_media du média créé."""
    with patch("app.workers.media_worker.transcode_media.delay") as mock_delay:
        resp = await client.post(
            "/api/v1/media/upload",
            files={"file": ("img.png", BytesIO(_PNG_1PX), "image/png")},
            headers=auth_headers,
        )
    assert resp.status_code == 201
    mock_delay.assert_called_once()
    id_media = resp.json()["id_media"]
    assert mock_delay.call_args.args == (id_media,)


@pytest.mark.asyncio
async def test_stock_search_no_key_returns_503(
    client: AsyncClient,
    auth_headers: dict,
) -> None:
    """Sans UNSPLASH_ACCESS_KEY, la recherche renvoie 503 (provider non configuré)
    pour que le front distingue « non configuré » d'un zéro résultat silencieux."""
    with patch.object(settings, "UNSPLASH_ACCESS_KEY", None):
        resp = await client.get(
            "/api/v1/media/stock/search?q=sunset&provider=unsplash",
            headers=auth_headers,
        )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "stock_provider_unconfigured"


@pytest.mark.asyncio
async def test_stock_import(
    client: AsyncClient,
    auth_headers: dict,
    minio_mock,
) -> None:
    """Import Unsplash — source='stock_unsplash', attribution_text et nom corrects."""
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = fake_jpeg
    mock_resp.headers = {"content-type": "image/jpeg"}

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=mock_resp)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_http)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_cm):
        with patch("app.workers.media_worker.transcode_media.delay"):
            # Garde anti-SSRF court-circuitée (sinon résolution DNS réelle → test non hermétique).
            with patch("app.api.v1.endpoints.media.url_is_safe", return_value=True):
                resp = await client.post(
                    "/api/v1/media/stock/import",
                    json={
                        "provider": "unsplash",
                        "provider_id": "abc123",
                        "full_url": "https://images.unsplash.com/photo-abc123",
                        "preview_url": "https://images.unsplash.com/photo-abc123?w=400",
                        "attribution_text": "Photo by John Doe on Unsplash",
                        "photographer": "John Doe",
                        "title": "Sunset over mountains",
                        "width": 4000,
                        "height": 2667,
                    },
                    headers=auth_headers,
                )

    assert resp.status_code == 201
    data = resp.json()
    assert data["source"] == "stock_unsplash"
    assert data["attribution_text"] == "Photo by John Doe on Unsplash"
    assert data["media_type"] == "image"
    assert data["media_name"] == "Sunset over mountains"


@pytest.mark.asyncio
async def test_stock_import_rejects_ssrf_url(
    client: AsyncClient,
    auth_headers: dict,
) -> None:
    """Régression sécurité (audit 2026-06-24) : `full_url` hors du domaine du provider
    (ici metadata cloud) doit être refusé en 400, sans aucun fetch."""
    resp = await client.post(
        "/api/v1/media/stock/import",
        json={
            "provider": "unsplash",
            "provider_id": "x",
            "full_url": "http://169.254.169.254/latest/meta-data/",
            "preview_url": "https://images.unsplash.com/x",
            "attribution_text": "x",
            "photographer": "x",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ── Génération d'image IA (§ génération de contenu — Phase 1, strict BYOK) ────────

@pytest.mark.asyncio
async def test_generate_image(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    minio_mock,
    monkeypatch,
) -> None:
    """Org avec clé fal → génère → cms_media source=generated_ai (fal mocké)."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key
    from app.services.generation import fal

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    async def _fake_generate(**kwargs):
        return fal.GeneratedImage(
            content=_PNG_1PX, content_type="image/png", width=1024, height=576, cost_usd=0.003
        )

    monkeypatch.setattr(fal, "generate_image", _fake_generate)

    with patch("app.workers.media_worker.transcode_media.delay"):
        resp = await client.post(
            "/api/v1/generation/image",
            json={"prompt": "un chat sur un canapé", "aspect_ratio": "16:9"},
            headers=auth_headers,
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["source"] == "generated_ai"
    assert data["media_type"] == "image"
    assert data["transcode_status"] == "pending"
    assert data.get("cdn_url_signed") is not None


@pytest.mark.asyncio
async def test_generate_image_without_key_is_400(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    monkeypatch,
) -> None:
    """Org sans clé fal → 400 explicite (strict BYOK, rien d'auto)."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    resp = await client.post(
        "/api/v1/generation/image",
        json={"prompt": "x", "aspect_ratio": "1:1"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_background_removal_edit_no_prompt(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    minio_mock,
    monkeypatch,
) -> None:
    """Détourage (birefnet) : modèle d'édition → AUCUN prompt requis, mais une réf exigée."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key
    from app.services.generation import fal, refs

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    # Sans référence → 422 (le modèle transforme une image existante).
    no_ref = await client.post(
        "/api/v1/generation/image",
        json={"prompt": "", "model": "fal-ai/birefnet"},
        headers=auth_headers,
    )
    assert no_ref.status_code == 422

    async def _fake_refs(_db, _org, _ids):
        return ["data:image/png;base64,AAAA"]

    async def _fake_generate(**kwargs):
        return fal.GeneratedImage(
            content=_PNG_1PX, content_type="image/png", width=512, height=512, cost_usd=0.01
        )

    monkeypatch.setattr(refs, "data_uris_for_media", _fake_refs)
    monkeypatch.setattr(fal, "generate_image", _fake_generate)

    with patch("app.workers.media_worker.transcode_media.delay"):
        resp = await client.post(
            "/api/v1/generation/image",
            json={"prompt": "", "model": "fal-ai/birefnet", "reference_media_ids": [1]},
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["source"] == "generated_ai"
    assert data["media_name"] == "Edited image"


@pytest.mark.asyncio
async def test_generate_video_creates_job(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /generation/video → job pending (202) + dispatch worker ; visible via GET /generation."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    with patch("app.workers.generation_worker.generate_content.delay") as mock_delay:
        resp = await client.post(
            "/api/v1/generation/video",
            json={"prompt": "un drone survole une plage", "aspect_ratio": "16:9"},
            headers=auth_headers,
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["kind"] == "video"
    assert data["provider"] == "fal"
    assert data["status"] == "pending"
    mock_delay.assert_called_once()

    lst = await client.get("/api/v1/generation?active=true", headers=auth_headers)
    assert lst.status_code == 200
    assert any(j["id_job"] == data["id_job"] for j in lst.json())


@pytest.mark.asyncio
async def test_generate_video_without_key_is_400(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    monkeypatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    resp = await client.post(
        "/api/v1/generation/video", json={"prompt": "x"}, headers=auth_headers
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_audio_fal_creates_job(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /generation/audio (provider fal) → job music pending."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    with patch("app.workers.generation_worker.generate_content.delay") as mock_delay:
        resp = await client.post(
            "/api/v1/generation/audio",
            json={"provider": "fal", "kind": "music", "prompt": "jazz lounge feutré"},
            headers=auth_headers,
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["kind"] == "music"
    assert data["provider"] == "fal"
    assert data["status"] == "pending"
    mock_delay.assert_called_once()


@pytest.mark.asyncio
async def test_generate_audio_elevenlabs_without_key_is_400(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Provider elevenlabs sans clé elevenlabs → 400 (même si une clé fal existe)."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")  # fal présent, pas elevenlabs
    await db_session.commit()

    resp = await client.post(
        "/api/v1/generation/audio",
        json={"provider": "elevenlabs", "kind": "voice", "prompt": "Bonjour et bienvenue"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ── Campagne one-shot (§ génération — Phase 4 : plan → fan-out) ─────────────────

@pytest.mark.asyncio
async def test_campaign_plan_and_execute(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """plan (repli sans clé Anthropic) → execute fan-out concepts × ratios → jobs image."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    # Plan : pas de clé Anthropic + user non super-admin → repli (1 concept = le brief).
    plan = await client.post(
        "/api/v1/generation/campaign/plan",
        json={"brief": "promo café d'automne, ambiance chaleureuse", "n_concepts": 2},
        headers=auth_headers,
    )
    assert plan.status_code == 200
    concepts = plan.json()["concepts"]
    assert len(concepts) == 2 and all(c["prompt"] for c in concepts)

    # Execute : 1 concept × 2 ratios = 2 jobs image (fan-out).
    with patch("app.workers.generation_worker.generate_content.delay") as mock_delay:
        resp = await client.post(
            "/api/v1/generation/campaign",
            json={
                "brief": "promo café d'automne",
                "ratios": ["16:9", "9:16"],
                "concepts": [{"title": "Café fumant", "prompt": "a steaming cup of coffee"}],
            },
            headers=auth_headers,
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["jobs_total"] == 2
    assert data["total_cost_usd"] and data["total_cost_usd"] > 0
    assert mock_delay.call_count == 2
    id_campaign = data["id_campaign"]

    # Les jobs de la campagne sont récupérables + sont des images.
    jobs = await client.get(f"/api/v1/generation?campaign={id_campaign}", headers=auth_headers)
    assert jobs.status_code == 200
    assert len(jobs.json()) == 2
    assert all(j["kind"] == "image" and j["id_campaign"] == id_campaign for j in jobs.json())

    # La campagne apparaît dans la liste.
    camps = await client.get("/api/v1/generation/campaigns", headers=auth_headers)
    assert camps.status_code == 200
    assert any(c["id_campaign"] == id_campaign for c in camps.json())


@pytest.mark.asyncio
async def test_campaign_multitype_fanout(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Campagne multi-type (image+vidéo+voix+musique) → 1 image + 1 vidéo + 1 voix + 1 musique."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    with patch("app.workers.generation_worker.generate_content.delay") as mock_delay:
        resp = await client.post(
            "/api/v1/generation/campaign",
            json={
                "brief": "lancement d'un nouveau soda artisanal",
                "types": ["image", "video", "voice", "music"],
                "ratios": ["16:9"],
                "concepts": [{"title": "Canette givrée", "prompt": "a frosty soda can"}],
                "video_prompt": "the can rotating slowly, bubbles rising",
                "voiceover": "Découvrez notre nouveau soda artisanal.",
                "music_prompt": "upbeat indie pop, summer vibe",
                "voice_provider": "fal", "music_provider": "fal",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["jobs_total"] == 4  # 1 image + 1 vidéo + 1 voix + 1 musique
    assert mock_delay.call_count == 4

    jobs = await client.get(
        f"/api/v1/generation?campaign={data['id_campaign']}", headers=auth_headers
    )
    kinds = sorted(j["kind"] for j in jobs.json())
    assert kinds == ["image", "music", "video", "voice"]


# ── Catalogue de génération + estimatif de coût (params avancés) ────────────────

@pytest.mark.asyncio
async def test_generation_catalog_filtered_by_keys(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Org avec clé fal seulement → catalogue ne propose QUE des modèles fal (pas elevenlabs),
    chaque modèle porte tier + pricing + params (pour l'UI dynamique + l'estimatif)."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    resp = await client.get("/api/v1/generation/catalog", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["providers"] == ["fal"]
    assert data["models"], "catalogue non vide"
    providers = {m["provider"] for m in data["models"]}
    assert providers == {"fal"}  # elevenlabs masqué (pas de clé)
    kinds = {m["kind"] for m in data["models"]}
    assert {"image", "video", "music", "voice"} <= kinds
    sample = data["models"][0]
    assert {"id", "label", "tier", "pricing", "params"} <= set(sample)


@pytest.mark.asyncio
async def test_image_to_image_requires_reference(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Un modèle image→image sans image de référence → 422 explicite (avant tout appel fal)."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "fal", "fal-test-key")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/generation/image",
        json={"prompt": "rendre plus lumineux", "model": "fal-ai/flux/dev/image-to-image"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_estimate_cost_modes() -> None:
    """Les modes de tarif (mégapixel / seconde / 1k chars / forfait) calculent un coût cohérent."""
    from app.services.generation import catalog

    # per_megapixel : prix × ~1 Mpx (FLUX schnell @ $0.003/Mpx)
    c = catalog.estimate_cost("fal-ai/flux/schnell", params={"aspect_ratio": "16:9"})
    assert 0.0025 <= c <= 0.005
    # résolution 'high' (×1.5 linéaire ≈ ×2.25 Mpx) coûte plus cher
    c_high = catalog.estimate_cost(
        "fal-ai/flux/schnell", params={"aspect_ratio": "16:9", "resolution": "high"}
    )
    assert c_high > c
    # per_second : Wan 2.5 à 0,15 $/s (facture réelle relevée le 2026-07-31 : 0,75 $/5 s
    # au défaut 720p du studio — l'ancien forfait supposé de 3 $ surestimait ×4)
    assert catalog.estimate_cost(
        "fal-ai/wan-25-preview/text-to-video", params={"duration": "8"}
    ) == pytest.approx(1.20)
    assert catalog.estimate_cost(
        "fal-ai/wan-25-preview/image-to-video", params={"duration": "5"}
    ) == pytest.approx(0.75)
    # per_video : LTX facturé par clip, pas à la seconde (tarif fal réel)
    assert catalog.estimate_cost(
        "fal-ai/ltx-video", params={"duration": "10"}
    ) == pytest.approx(0.02)
    # musique per_second : la durée change le coût (Stable Audio @ $0.0006/s × 30s)
    assert catalog.estimate_cost(
        "fal-ai/stable-audio", params={"seconds_total": 30}
    ) == pytest.approx(0.018)
    # per_1k_chars : ElevenLabs v3 via fal @ $0.10/1k × longueur du texte
    assert catalog.estimate_cost(
        "fal-ai/elevenlabs/tts/eleven-v3", text="a" * 500
    ) == pytest.approx(0.05, abs=0.001)
    # modèle inconnu → 0
    assert catalog.estimate_cost("inexistant/modele") == 0.0


# ── Assistant de rédaction de prompt + negative prompt ──────────────────────────

def test_prompt_assistant_coerce_and_negative() -> None:
    """_coerce : clamp des questions, one-shot force ready, negative retiré si non supporté."""
    from app.services.generation import prompt_assistant as pa

    # Modèle qui supporte le negative prompt (FLUX dev) vs pas (FLUX schnell).
    assert pa.supports_negative_for("fal-ai/flux/dev", "image") is True
    assert pa.supports_negative_for("fal-ai/flux/schnell", "image") is False
    assert pa.supports_negative_for(None, "image") is False

    # one-shot : jamais de question, negative conservé si supporté.
    r = pa._coerce(
        {"ready": False, "questions": ["a", "b", "c", "d"], "prompt": "a cat",
         "negative_prompt": "text, blurry", "notes": "ok"},
        supports_negative=True, one_shot=True,
    )
    assert r["ready"] is True and r["questions"] == []
    assert r["prompt"] == "a cat" and r["negative_prompt"] == "text, blurry"
    assert r["assisted"] is True

    # negative retiré si le modèle ne le supporte pas (évite un 422 fal).
    r2 = pa._coerce(
        {"ready": True, "prompt": "a dog", "negative_prompt": "blurry"},
        supports_negative=False, one_shot=True,
    )
    assert r2["negative_prompt"] is None

    # conversationnel : questions conservées (max 3), prompt nul tant que pas prêt.
    r3 = pa._coerce(
        {"ready": False, "questions": ["q1", "q2", "q3", "q4"], "prompt": None},
        supports_negative=True, one_shot=False,
    )
    assert r3["ready"] is False and r3["questions"] == ["q1", "q2", "q3"]
    assert r3["prompt"] is None


@pytest.mark.asyncio
async def test_prompt_assist_fallback_without_anthropic_key(
    client: AsyncClient,
    auth_headers: dict,
) -> None:
    """Pas de clé Anthropic + user non super-admin → repli brut (assisted=False), le prompt
    de l'utilisateur est renvoyé tel quel ; supports_negative_prompt reflète le modèle."""
    resp = await client.post(
        "/api/v1/generation/prompt-assist",
        json={"kind": "image", "model": "fal-ai/flux/dev",
              "mode": "one_shot", "intent": "un chat sur un canapé"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["assisted"] is False
    assert data["prompt"] == "un chat sur un canapé"  # repli = prompt brut, non assisté
    assert data["ready"] is True
    assert data["supports_negative_prompt"] is True  # FLUX dev supporte le negative

    # Un modèle sans support negative → flag à False.
    resp2 = await client.post(
        "/api/v1/generation/prompt-assist",
        json={"kind": "image", "model": "fal-ai/flux/schnell",
              "mode": "one_shot", "intent": "a cat"},
        headers=auth_headers,
    )
    assert resp2.json()["supports_negative_prompt"] is False


@pytest.mark.asyncio
async def test_prompt_assist_with_byok_uses_llm(
    client: AsyncClient,
    auth_headers: dict,
    org: CmsOrganization,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Org avec clé Anthropic (byok) → l'assistant est appelé ; son résultat est renvoyé."""
    from cryptography.fernet import Fernet

    from app.services.ai_credentials import upsert_org_key

    monkeypatch.setattr(settings, "CMS_VAULT_KEY", Fernet.generate_key().decode())
    await upsert_org_key(db_session, org.id_org, "anthropic", "sk-ant-test")
    await db_session.commit()

    canned = {
        "ready": True, "questions": [],
        "prompt": "a sleek soda can, frosty condensation, soft studio light, slow dolly in",
        "negative_prompt": "text, banner, watermark", "notes": "Prompt optimisé en anglais.",
        "assisted": True,
    }
    with patch(
        "app.services.generation.prompt_assistant.assist_prompt",
        new=AsyncMock(return_value=canned),
    ) as mock_assist:
        resp = await client.post(
            "/api/v1/generation/prompt-assist",
            json={"kind": "video", "model": "fal-ai/wan-25-preview/text-to-video",
                  "mode": "one_shot", "intent": "anime ma canette pour une pub"},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    assert mock_assist.await_count == 1
    # La clé byok est transmise à l'assistant.
    assert mock_assist.await_args.kwargs["byok_key"] == "sk-ant-test"
    data = resp.json()
    assert data["assisted"] is True
    assert data["prompt"] == canned["prompt"]
    assert data["negative_prompt"] == "text, banner, watermark"
    assert data["supports_negative_prompt"] is True


# ── Détection du media_type (MIME + repli extension, §XIX audio) ────────────────

def test_detect_media_type_extension_fallback() -> None:
    """Les .ogg/.opus/.flac mal typés par le navigateur sont détectés via l'extension."""
    from app.api.v1.endpoints.media import _detect_media_type as d

    # MIME ambigu (Ogg conteneur) → repli extension audio
    assert d("application/ogg", "track.ogg") == "audio"
    assert d("video/ogg", "track.ogg") == "audio"
    assert d("application/octet-stream", "song.opus") == "audio"
    assert d("application/octet-stream", "a.flac") == "audio"
    assert d("", "clip.m4a") == "audio"
    # .ogv reste vidéo
    assert d("application/octet-stream", "movie.ogv") == "video"
    # MIME fiable → préfixe (extension non requise)
    assert d("audio/mpeg", "x.mp3") == "audio"
    assert d("image/png", "x.png") == "image"
    # inconnu → None
    assert d("application/octet-stream", "weird.xyz") is None
    assert d("application/x-unknown", None) is None


# ── Barre de progression d'encodage (2026-08-03) ───────────────────────────────

@pytest.mark.asyncio
async def test_liste_expose_la_progression_d_un_encodage_en_cours(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    """La médiathèque sert l'avancement publié par le worker (Redis), pour les seuls
    médias réellement en traitement."""
    from app.services import encode_progress

    m = CmsMedia(
        id_org=org.id_org, media_name="enc.mp4", media_type="video", storage_key="k/enc.mp4",
        checksum_sha256="x" * 64, transcode_status="processing",
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    encode_progress.publish(
        m.id_media,
        encode_progress.payload(pct=42.0, eta_s=120, speed=1.5, phase="encode"),
    )
    try:
        resp = await client.get("/api/v1/media/", headers=auth_headers)
        assert resp.status_code == 200
        row = next(i for i in resp.json() if i["id_media"] == m.id_media)
        assert row["encode_progress"]["pct"] == 42.0
        assert row["encode_progress"]["eta_s"] == 120
        assert row["encode_progress"]["phase"] == "encode"

        # Détail : même information par le lien direct (le tiroir ouvert par URL ne doit
        # pas être moins informé que la liste — classe de défaut vue sur governed_readonly).
        d = await client.get(f"/api/v1/media/{m.id_media}", headers=auth_headers)
        assert d.status_code == 200
        assert d.json()["encode_progress"]["pct"] == 42.0
    finally:
        encode_progress.clear(m.id_media)


@pytest.mark.asyncio
async def test_pas_de_progression_sur_un_media_termine(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    """Une clé résiduelle (worker tué juste avant la fin) ne doit PAS ressusciter une barre
    sur un média déjà `done` : l'enrichissement ne regarde que les médias `processing`."""
    from app.services import encode_progress

    m = CmsMedia(
        id_org=org.id_org, media_name="fini.mp4", media_type="video", storage_key="k/fini.mp4",
        checksum_sha256="y" * 64, transcode_status="done",
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    encode_progress.publish(
        m.id_media, encode_progress.payload(pct=99.0, eta_s=1, speed=1.0, phase="encode")
    )
    try:
        resp = await client.get("/api/v1/media/", headers=auth_headers)
        row = next(i for i in resp.json() if i["id_media"] == m.id_media)
        assert row["encode_progress"] is None
    finally:
        encode_progress.clear(m.id_media)


# ── Décision d'encodage (étalonnage, bandes) ───────────────────────────────────

def _awaiting(org_id: int, band: str = "confirm", estimate: int = 2400) -> CmsMedia:
    return CmsMedia(
        id_org=org_id, media_name="lourd.mkv", media_type="video", storage_key="k/lourd.mkv",
        checksum_sha256="z" * 64, transcode_status="awaiting_decision",
        media_metadata={"encode_decision": {
            "band": band, "estimate_s": estimate, "measured_speed": 0.25,
            "width": 3840, "height": 2160, "encoder": "libx265", "resolved": False,
            "downscale": [{"profile": "fhd", "box": 1920, "estimate_s": estimate // 4},
                          {"profile": "hd720", "box": 1280, "estimate_s": estimate // 9}],
        }},
    )


@pytest.mark.asyncio
async def test_decision_lancer_remet_le_media_en_file(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    m = _awaiting(org.id_org)
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    with patch("app.workers.media_worker.transcode_media.delay") as delay:
        r = await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                              json={"action": "proceed"}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["transcode_status"] == "pending"
    delay.assert_called_once_with(m.id_media)

    await db_session.refresh(m)
    assert m.transcode_status == "pending"
    # `resolved` est ce qui empêche le worker de re-poser la même question en boucle.
    assert m.media_metadata["encode_decision"]["resolved"] is True
    assert m.media_metadata["encode_decision"]["downscale_box"] is None


@pytest.mark.asyncio
async def test_decision_definition_inferieure_transmet_le_cadre(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    m = _awaiting(org.id_org)
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    with patch("app.workers.media_worker.transcode_media.delay"):
        r = await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                              json={"action": "downscale", "profile": "fhd"},
                              headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["downscale_box"] == 1920
    await db_session.refresh(m)
    assert m.media_metadata["encode_decision"]["downscale_box"] == 1920


@pytest.mark.asyncio
async def test_decision_refuse_un_profil_non_propose(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    """Un profil inventé (ou plus grand que la source) produirait un encodage qui ne
    résout pas le problème — on n'accepte que ce qui a été réellement proposé."""
    m = _awaiting(org.id_org)
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    r = await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                          json={"action": "downscale", "profile": "uhd8k"},
                          headers=auth_headers)
    assert r.status_code == 422
    await db_session.refresh(m)
    assert m.transcode_status == "awaiting_decision"   # rien n'a bougé


@pytest.mark.asyncio
async def test_bande_haute_refusee_a_un_non_admin(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    """> 4 h : `media:edit` ne suffit pas. Engager la machine aussi longtemps est une
    décision d'exploitation — mais la sortie « définition inférieure » reste ouverte."""
    m = _awaiting(org.id_org, band="refuse", estimate=20_000)
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    r = await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                          json={"action": "proceed"}, headers=auth_headers)
    assert r.status_code == 403

    # ... et la voie de sortie fonctionne pour le même utilisateur.
    with patch("app.workers.media_worker.transcode_media.delay"):
        ok = await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                               json={"action": "downscale", "profile": "hd720"},
                               headers=auth_headers)
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_decision_sur_un_media_qui_n_attend_rien(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    m = CmsMedia(id_org=org.id_org, media_name="ok.mp4", media_type="video",
                 storage_key="k/ok.mp4", checksum_sha256="w" * 64, transcode_status="done")
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    r = await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                          json={"action": "proceed"}, headers=auth_headers)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_decision_tracee_dans_l_audit(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, org: CmsOrganization,
) -> None:
    """Motif récurrent des audits par menu : un geste qui engage doit laisser une trace."""
    from app.models.audit_log import CmsAuditLog

    m = _awaiting(org.id_org)
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    with patch("app.workers.media_worker.transcode_media.delay"):
        await client.post(f"/api/v1/media/{m.id_media}/encode-decision",
                          json={"action": "downscale", "profile": "fhd"}, headers=auth_headers)

    rows = (await db_session.execute(
        select(CmsAuditLog).where(
            CmsAuditLog.action == "media_encode_decision",
            CmsAuditLog.resource_id == str(m.id_media),
        )
    )).scalars().all()
    assert len(rows) == 1
    d = rows[0].detail
    # Le nom du média survit à sa suppression éventuelle, et l'estimation dit POURQUOI
    # la question a été posée.
    assert d["media_name"] == "lourd.mkv" and d["action"] == "downscale"
    assert d["band"] == "confirm" and d["downscale_box"] == 1920
