"""
Tests Lot 4a — Droits d'auteur SACEM (§XIX.6).

Couvre :
  - PATCH /media/{id}/rights (ISRC + rights_metadata, validation ISRC)
  - GET /reports/rights (agrégation Proof of Play, JSON + CSV)
"""

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings, CmsSite
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.role import (
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="RightsOrg", org_slug=f"rights-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest_asyncio.fixture
async def auth_token(client: AsyncClient, db_session: AsyncSession, org: CmsOrganization) -> str:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org,
        email=f"rights-{uid}@test.com",
        password_hash=hash_password("Password1!"),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    role = CmsRole(id_org=org.id_org, role_key=f"CMS_RIGHTS_{uid}", label="Rights", is_system=False)
    db_session.add(role)
    await db_session.flush()

    for module, operation in [
        ("media", "read"), ("media", "edit"),
        ("reports", "read"), ("reports", "export"),
    ]:
        await db_session.execute(
            pg_insert(CmsPermission)
            .values(module=module, operation=operation, label=f"{module}.{operation}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        )
        await db_session.flush()
        perm = (await db_session.execute(
            select(CmsPermission).where(
                CmsPermission.module == module, CmsPermission.operation == operation
            )
        )).scalar_one()
        db_session.add(CmsRolePermission(id_role=role.id_role, id_permission=perm.id_permission))

    await db_session.flush()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password1!"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_update_media_rights(
    client: AsyncClient, auth_token: str, db_session: AsyncSession, org: CmsOrganization
) -> None:
    headers = {"Authorization": f"Bearer {auth_token}"}
    media = CmsMedia(
        id_org=org.id_org, media_name="track.ogg", media_type="audio",
        storage_key=f"test/{uuid.uuid4().hex}.ogg", checksum_sha256=uuid.uuid4().hex,
        transcode_status="done",
    )
    db_session.add(media)
    await db_session.commit()
    await db_session.refresh(media)

    # ISRC avec tirets → normalisé sans tirets ; metadata stockée.
    resp = await client.patch(
        f"/api/v1/media/{media.id_media}/rights",
        json={"isrc": "FR-Z03-26-00001", "rights_metadata": {
            "composer": "Daft Punk", "publisher": "EMI", "rights_org": "SACEM"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["isrc"] == "FRZ032600001"
    assert data["rights_metadata"]["composer"] == "Daft Punk"

    # ISRC invalide → 422.
    bad = await client.patch(
        f"/api/v1/media/{media.id_media}/rights",
        json={"isrc": "NOPE", "rights_metadata": {}}, headers=headers,
    )
    assert bad.status_code == 422


async def test_rights_report_json_and_csv(
    client: AsyncClient, auth_token: str, db_session: AsyncSession, org: CmsOrganization
) -> None:
    headers = {"Authorization": f"Bearer {auth_token}"}

    site = CmsSite(id_org=org.id_org, site_name="Store 1")
    db_session.add(site)
    await db_session.flush()
    player = CmsPlayer(id_org=org.id_org, id_site=site.id_site, player_name="P1")
    media = CmsMedia(
        id_org=org.id_org, media_name="Anthem", media_type="audio",
        storage_key=f"test/{uuid.uuid4().hex}.ogg", checksum_sha256=uuid.uuid4().hex,
        transcode_status="done", isrc="FRZ032600002",
        rights_metadata={"composer": "Composer X", "publisher": "Pub Y", "rights_org": "SACEM"},
    )
    db_session.add_all([player, media])
    await db_session.flush()

    played = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
    for i in range(3):
        db_session.add(CmsProofOfPlay(
            id_org=org.id_org, id_player=player.id_player, id_media=media.id_media,
            played_at=played, duration_ms=60000, merkle_hash=f"h{i}",
        ))
    await db_session.commit()

    # JSON
    resp = await client.get(
        "/api/v1/reports/rights",
        params={"from": "2026-04-01", "until": "2026-04-30", "rights_org": "SACEM"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_plays"] == 3
    assert body["total_sec"] == 180
    row = next(r for r in body["rows"] if r["isrc"] == "FRZ032600002")
    assert row["plays"] == 3
    assert row["total_sec"] == 180
    assert row["players"] == 1
    assert row["sites"] == 1
    assert row["composer"] == "Composer X"

    # CSV
    csv_resp = await client.get(
        "/api/v1/reports/rights",
        params={"from": "2026-04-01", "until": "2026-04-30", "format": "csv"},
        headers=headers,
    )
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "FRZ032600002" in csv_resp.text
    assert "Composer X" in csv_resp.text

    # Hors période → vide
    empty = await client.get(
        "/api/v1/reports/rights",
        params={"from": "2026-05-01", "until": "2026-05-31"}, headers=headers,
    )
    assert empty.status_code == 200
    assert empty.json()["total_plays"] == 0


async def test_rights_csv_neutralizes_formula_injection(
    client: AsyncClient, auth_token: str, db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Un nom de média commençant par '=' ne doit pas sortir tel quel dans le CSV
    (sinon formule exécutée à l'ouverture Excel/Calc). Audit sécurité 2026-06-25."""
    headers = {"Authorization": f"Bearer {auth_token}"}
    site = CmsSite(id_org=org.id_org, site_name="Store X")
    db_session.add(site)
    await db_session.flush()
    player = CmsPlayer(id_org=org.id_org, id_site=site.id_site, player_name="PX")
    media = CmsMedia(
        id_org=org.id_org, media_name='=HYPERLINK("http://evil")', media_type="audio",
        storage_key=f"test/{uuid.uuid4().hex}.ogg", checksum_sha256=uuid.uuid4().hex,
        transcode_status="done", isrc="FRZ032600003",
    )
    db_session.add_all([player, media])
    await db_session.flush()
    db_session.add(CmsProofOfPlay(
        id_org=org.id_org, id_player=player.id_player, id_media=media.id_media,
        played_at=datetime(2026, 4, 15, 12, 0, tzinfo=UTC), duration_ms=60000, merkle_hash="hx",
    ))
    await db_session.commit()

    csv_resp = await client.get(
        "/api/v1/reports/rights",
        params={"from": "2026-04-01", "until": "2026-04-30", "format": "csv"},
        headers=headers,
    )
    assert csv_resp.status_code == 200
    # La valeur est préfixée d'une apostrophe → plus de '=' en tête de cellule.
    assert '\'=HYPERLINK' in csv_resp.text
    assert ',=HYPERLINK' not in csv_resp.text


async def test_rights_csv_follows_profile_locale(
    client: AsyncClient, auth_token: str, db_session: AsyncSession, org: CmsOrganization
) -> None:
    """En-têtes du CSV droits = langue du profil du demandeur (défaut anglais)."""
    from sqlalchemy import update
    headers = {"Authorization": f"Bearer {auth_token}"}
    params = {"from": "2026-04-01", "until": "2026-04-30", "format": "csv"}
    en = await client.get("/api/v1/reports/rights", params=params, headers=headers)
    assert en.status_code == 200
    assert en.text.splitlines()[0].startswith("ISRC,Title,Composer")

    await db_session.execute(update(CmsUser).where(CmsUser.id_org == org.id_org).values(locale="de"))
    await db_session.commit()
    de = await client.get("/api/v1/reports/rights", params=params, headers=headers)
    first = de.text.splitlines()[0]
    assert first.startswith("ISRC,Titel,Komponist") and "Title" not in first
    assert first.isascii() and " " not in first
