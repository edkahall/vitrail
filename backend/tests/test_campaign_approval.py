"""Tests Retail Media S5 (§2.8) — approbation des créatifs, optionnelle par org.

- GET/PUT /campaigns/settings (org-admin) : flag `creative_approval` (défaut OFF).
- Flag ON : nouveau créatif = `pending` ; publish 422 tant qu'aucun créatif approuvé ;
  approve/reject (RBAC approvals:approve/reject §21 — 1er consommateur réel) ;
  gate diffusion campaign_ad_slots (seuls les `approved` sortent).
- Flag OFF : comportement historique inchangé (créatif né `approved`).
"""

import uuid
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.campaign import CmsAdvertiser, CmsCampaign, CmsCampaignCreative, CmsCampaignTarget
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.role import (
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser
from app.services.campaign_delivery import campaign_ad_slots


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="RMA", org_slug=f"rma-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


async def _grant(db: AsyncSession, role: CmsRole, module: str, op: str) -> None:
    await db.execute(
        pg_insert(CmsPermission)
        .values(module=module, operation=op, label=f"{module}.{op}")
        .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation"))
    await db.flush()
    perm = (await db.execute(select(CmsPermission).where(
        CmsPermission.module == module, CmsPermission.operation == op))).scalar_one()
    await db.execute(
        pg_insert(CmsRolePermission)
        .values(id_role=role.id_role, id_permission=perm.id_permission)
        .on_conflict_do_nothing())


async def _user(db: AsyncSession, org: CmsOrganization, role_key: str,
                perms: list[tuple[str, str]], *, is_system: bool = True) -> CmsUser:
    user = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db.add(user)
    await db.flush()
    role = (await db.execute(select(CmsRole).where(
        CmsRole.role_key == role_key))).scalars().first()
    if role is None:
        role = CmsRole(id_org=None if is_system else org.id_org, role_key=role_key,
                       label=role_key, is_system=is_system)
        db.add(role)
        await db.flush()
    for module, op in perms:
        await _grant(db, role, module, op)
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    return user


@pytest_asyncio.fixture
async def admin_headers(db_session: AsyncSession, org: CmsOrganization) -> dict:
    user = await _user(db_session, org, "CMS_ORG_ADMIN",
                       [("campaigns", op) for op in ("read", "create", "edit", "publish")]
                       + [("approvals", "approve"), ("approvals", "reject")])
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


@pytest_asyncio.fixture
async def manager_headers(db_session: AsyncSession, org: CmsOrganization) -> dict:
    """Rôle custom SANS permissions approvals → la revue doit répondre 403."""
    user = await _user(db_session, org, f"CAMPMGR-{uuid.uuid4().hex[:6]}",
                       [("campaigns", op) for op in ("read", "create", "edit", "publish")],
                       is_system=False)
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


async def _campaign_with_target(db: AsyncSession, org: CmsOrganization) -> CmsCampaign:
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name=f"Acme-{uuid.uuid4().hex[:6]}")
    db.add(adv)
    await db.flush()
    camp = CmsCampaign(
        id_org=org.id_org, id_advertiser=adv.id_advertiser, campaign_name="Été",
        status="draft", starts_on=date.today() - timedelta(days=1),
        ends_on=date.today() + timedelta(days=7), sov_percent=10.0,
    )
    db.add(camp)
    await db.flush()
    db.add(CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                             target_type="org", target_id=None))
    await db.commit()
    return camp


async def _media(db: AsyncSession, org: CmsOrganization, name: str = "spot") -> CmsMedia:
    m = CmsMedia(id_org=org.id_org, media_name=name, media_type="video",
                 storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                 transcode_status="done", duration_ms=15_000)
    db.add(m)
    await db.commit()
    return m


@pytest.mark.asyncio
async def test_settings_default_off_and_org_admin_toggle(
        client: AsyncClient, admin_headers: dict):
    r = await client.get("/api/v1/campaigns/settings", headers=admin_headers)
    assert r.status_code == 200 and r.json()["creative_approval"] is False
    # Quotas d'upload portail (0084) : défauts 20 créatifs / 1024 Mo
    assert r.json()["max_creatives"] == 20 and r.json()["creative_max_mb"] == 1024

    r = await client.put("/api/v1/campaigns/settings", headers=admin_headers,
                         json={"creative_approval": True})
    assert r.status_code == 200 and r.json()["creative_approval"] is True

    r = await client.get("/api/v1/campaigns/settings", headers=admin_headers)
    assert r.json()["creative_approval"] is True

    # Quotas configurables (retour Ed #6) — update partiel, l'autre champ inchangé
    r = await client.put("/api/v1/campaigns/settings", headers=admin_headers,
                         json={"max_creatives": 5, "creative_max_mb": 256})
    assert r.status_code == 200
    assert r.json()["max_creatives"] == 5 and r.json()["creative_max_mb"] == 256
    assert r.json()["creative_approval"] is True
    # Bornes : 0 refusé (422 pydantic)
    r = await client.put("/api/v1/campaigns/settings", headers=admin_headers,
                         json={"max_creatives": 0})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_flag_off_creative_born_approved(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin_headers: dict):
    camp = await _campaign_with_target(db_session, org)
    m = await _media(db_session, org)
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/creatives",
                          headers=admin_headers, json={"id_media": m.id_media})
    assert r.status_code == 201, r.text
    assert r.json()["approval_status"] == "approved"
    # publish passe directement (comportement historique)
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=admin_headers, json={"action": "publish"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_flag_on_pending_publish_gate_then_review(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin_headers: dict):
    await client.put("/api/v1/campaigns/settings", headers=admin_headers,
                     json={"creative_approval": True})
    camp = await _campaign_with_target(db_session, org)
    m = await _media(db_session, org)

    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/creatives",
                          headers=admin_headers, json={"id_media": m.id_media})
    assert r.status_code == 201, r.text
    creative = r.json()
    assert creative["approval_status"] == "pending"

    # publish refusé tant qu'aucun créatif approuvé
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=admin_headers, json={"action": "publish"})
    assert r.status_code == 422 and "approved" in r.json()["detail"]

    # approve (RBAC approvals:approve) → publish passe
    r = await client.post(f"/api/v1/campaigns/creatives/{creative['id_creative']}/approve",
                          headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["approval_status"] == "approved"
    assert r.json()["reviewed_at"] is not None
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=admin_headers, json={"action": "publish"})
    assert r.status_code == 200, r.text

    # reject en cours de diffusion → statut rejected (retrait au prochain sync)
    r = await client.post(f"/api/v1/campaigns/creatives/{creative['id_creative']}/reject",
                          headers=admin_headers, json={"note": "logo flou"})
    assert r.status_code == 200 and r.json()["approval_status"] == "rejected"


@pytest.mark.asyncio
async def test_review_requires_approvals_permission(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin_headers: dict, manager_headers: dict):
    camp = await _campaign_with_target(db_session, org)
    m = await _media(db_session, org)
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/creatives",
                          headers=manager_headers, json={"id_media": m.id_media})
    assert r.status_code == 201
    id_creative = r.json()["id_creative"]
    for action in ("approve", "reject"):
        r = await client.post(f"/api/v1/campaigns/creatives/{id_creative}/{action}",
                              headers=manager_headers)
        assert r.status_code == 403, f"{action} devrait être 403 sans permission approvals"


@pytest.mark.asyncio
async def test_delivery_gate_only_approved_when_flag_on(
        db_session: AsyncSession, org: CmsOrganization):
    player = CmsPlayer(id_org=org.id_org, player_name="P-appr", player_os="linux")
    db_session.add(player)
    await db_session.flush()
    camp = await _campaign_with_target(db_session, org)
    camp.status = "approved"
    camp.sov_percent = 30.0  # ≥ 2 slots/cycle → la rotation couvre les 2 créatifs
    m_ok = await _media(db_session, org, "spot-ok")
    m_pending = await _media(db_session, org, "spot-pending")
    db_session.add_all([
        CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                            id_media=m_ok.id_media, duration_ms=15_000,
                            approval_status="approved"),
        CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                            id_media=m_pending.id_media, duration_ms=15_000,
                            approval_status="pending"),
    ])
    org.campaign_creative_approval = True
    await db_session.commit()

    slots = await campaign_ad_slots(db_session, org.id_org, player.id_player,
                                    datetime.now(), home_duration_ms=120_000)
    assert slots and {s.id_media for s in slots} == {m_ok.id_media}

    # Flag OFF → l'approbation n'est plus enforced (pending redevient diffusable)
    org.campaign_creative_approval = False
    await db_session.commit()
    slots = await campaign_ad_slots(db_session, org.id_org, player.id_player,
                                    datetime.now(), home_duration_ms=120_000)
    assert {s.id_media for s in slots} == {m_ok.id_media, m_pending.id_media}
