"""Tests Inventaire retail media v2 (migr 0082, demandes Ed 2026-07-11).

- Plafond d'occupation par player / par site (héritage player > site > 100) :
  les seuils du check deviennent relatifs au plafond ; le publish bloque au-delà.
- Tarif SOV par écran (héritage player > site > défaut org) → prix indicatif
  `estimate_campaign_price` (devis ; le prix contractuel reste le champ manuel).
- `occupancy_breakdown` : les campagnes qui occupent déjà les cibles.
- Settings org : GET/PUT sov_daily_rate (org-admin, champs partiels).
- Détail d'approbation §21 : other_campaigns + price exposés au reviewer.
"""

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.campaign import CmsAdvertiser, CmsCampaign, CmsCampaignTarget
from app.models.organization import CmsOrganization, CmsOrgAuthSettings, CmsSite
from app.models.player import CmsPlayer
from app.models.role import CmsPermission, CmsRole, CmsRolePermission, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services.campaign_inventory import (
    check_campaign_occupancy,
    estimate_campaign_price,
    occupancy_breakdown,
)
from app.services.targeting import Target


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="INV2", org_slug=f"inv2-{uuid.uuid4().hex[:8]}")
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


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    user = CmsUser(id_org=org.id_org, email=f"a-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    await db_session.flush()
    role = (await db_session.execute(select(CmsRole).where(
        CmsRole.role_key == "CMS_ORG_ADMIN"))).scalars().first()
    if role is None:
        role = CmsRole(id_org=None, role_key="CMS_ORG_ADMIN", label="admin", is_system=True)
        db_session.add(role)
        await db_session.flush()
    for op in ("read", "create", "edit", "publish"):
        await _grant(db_session, role, "campaigns", op)
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return user


def _headers(user: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


async def _site(db: AsyncSession, org: CmsOrganization, **kw) -> CmsSite:
    s = CmsSite(id_org=org.id_org, site_name=f"S-{uuid.uuid4().hex[:6]}", **kw)
    db.add(s)
    await db.commit()
    return s


async def _player(db: AsyncSession, org: CmsOrganization, site: CmsSite | None = None,
                  **kw) -> CmsPlayer:
    p = CmsPlayer(id_org=org.id_org, player_name=f"P-{uuid.uuid4().hex[:6]}",
                  player_os="linux", id_site=site.id_site if site else None, **kw)
    db.add(p)
    await db.commit()
    return p


async def _campaign(db: AsyncSession, org: CmsOrganization, sov: float,
                    status: str = "approved", days: int = 9) -> CmsCampaign:
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name=f"A-{uuid.uuid4().hex[:6]}")
    db.add(adv)
    await db.flush()
    camp = CmsCampaign(
        id_org=org.id_org, id_advertiser=adv.id_advertiser,
        campaign_name=f"C-{uuid.uuid4().hex[:6]}", status=status,
        starts_on=date.today(), ends_on=date.today() + timedelta(days=days),
        sov_percent=sov,
    )
    db.add(camp)
    await db.flush()
    db.add(CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                             target_type="org", target_id=None))
    await db.commit()
    return camp


_ORG_TARGET = [Target(target_type="org", target_id=None, tag_expr={})]


@pytest.mark.asyncio
async def test_player_cap_blocks_and_site_cap_inherits(
        db_session: AsyncSession, org: CmsOrganization):
    site = await _site(db_session, org, campaign_max_occupancy_pct=30)
    p_site = await _player(db_session, org, site)               # cap hérité 30
    p_own = await _player(db_session, org, site,
                          campaign_max_occupancy_pct=20)        # cap player prime
    p_free = await _player(db_session, org)                     # pas de plafond → 100

    res = await check_campaign_occupancy(
        db_session, org.id_org, starts_on=date.today(),
        ends_on=date.today() + timedelta(days=5),
        sov_percent=25.0, targets=_ORG_TARGET)
    rows = {r["id_player"]: r for r in res["players"]}
    assert rows[p_site.id_player]["cap_pct"] == 30.0
    assert rows[p_own.id_player]["cap_pct"] == 20.0
    assert rows[p_free.id_player]["cap_pct"] == 100.0
    # 25 % > plafond 20 du player → blocage ; et 25 ≥ 80 % de 30 → warning aussi
    assert res["blocked"] is True and res["warning"] is True

    # À 15 % : sous tous les plafonds mais 15 ≥ 80 % de 20 → warning sans blocage
    res = await check_campaign_occupancy(
        db_session, org.id_org, starts_on=date.today(),
        ends_on=date.today() + timedelta(days=5),
        sov_percent=16.0, targets=_ORG_TARGET)
    assert res["blocked"] is False and res["warning"] is True

    # Sans plafonds custom le comportement v1 est inchangé (warning 80/blocage 100)
    res = await check_campaign_occupancy(
        db_session, org.id_org, starts_on=date.today(),
        ends_on=date.today() + timedelta(days=5),
        sov_percent=16.0,
        targets=[Target(target_type="player", target_id=p_free.id_player, tag_expr={})])
    assert res["blocked"] is False and res["warning"] is False


@pytest.mark.asyncio
async def test_publish_blocked_by_player_cap(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    await _player(db_session, org, campaign_max_occupancy_pct=10)
    camp = await _campaign(db_session, org, sov=15.0, status="draft")
    # un créatif actif requis pour le publish
    from app.models.campaign import CmsCampaignCreative
    from app.models.media import CmsMedia
    m = CmsMedia(id_org=org.id_org, media_name="spot", media_type="video",
                 storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                 transcode_status="done", duration_ms=15_000)
    db_session.add(m)
    await db_session.flush()
    db_session.add(CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                                       id_media=m.id_media, duration_ms=15_000,
                                       approval_status="approved"))
    await db_session.commit()

    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 409 and "Overbooking" in r.json()["detail"]


@pytest.mark.asyncio
async def test_price_estimate_inheritance(
        db_session: AsyncSession, org: CmsOrganization):
    # Aucun tarif nulle part → pas de devis
    await _player(db_session, org)
    est = await estimate_campaign_price(
        db_session, org.id_org, targets=_ORG_TARGET, sov_percent=10.0,
        starts_on=date.today(), ends_on=date.today() + timedelta(days=9))
    assert est is None

    # org 0.1 ; site 0.2 ; player 0.5 — héritage player > site > org, 10 jours, SOV 10 %
    org.campaign_sov_daily_rate = 0.1
    site = await _site(db_session, org, campaign_sov_daily_rate=0.2)
    await _player(db_session, org, site)                                 # 0.2
    await _player(db_session, org, site, campaign_sov_daily_rate=0.5)    # 0.5
    await db_session.commit()
    est = await estimate_campaign_price(
        db_session, org.id_org, targets=_ORG_TARGET, sov_percent=10.0,
        starts_on=date.today(), ends_on=date.today() + timedelta(days=9))
    # p1 (org 0.1) + p2 (site 0.2) + p3 (player 0.5) = (0.1+0.2+0.5) × 10 % × 10 j = 80 €
    assert est is not None
    assert est["estimated_price"] == 80.0
    assert est["priced_players"] == 3 and est["unpriced_players"] == 0
    assert est["days"] == 10


@pytest.mark.asyncio
async def test_occupancy_breakdown_lists_existing_campaigns(
        db_session: AsyncSession, org: CmsOrganization):
    p = await _player(db_session, org)
    existing = await _campaign(db_session, org, sov=20.0)  # approved, cible org
    rows = await occupancy_breakdown(
        db_session, org.id_org, starts_on=date.today(),
        ends_on=date.today() + timedelta(days=3), pids={p.id_player})
    assert len(rows) == 1
    assert rows[0]["campaign_name"] == existing.campaign_name
    assert rows[0]["sov_percent"] == 20.0 and rows[0]["shared_players"] == 1
    # exclusion de soi-même (édition/approbation de la même campagne)
    rows = await occupancy_breakdown(
        db_session, org.id_org, starts_on=date.today(),
        ends_on=date.today() + timedelta(days=3), pids={p.id_player},
        exclude_id_campaign=existing.id_campaign)
    assert rows == []


@pytest.mark.asyncio
async def test_settings_rate_and_occupancy_check_price(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    # PUT partiel : le tarif seul, sans toucher l'approbation des créatifs
    r = await client.put("/api/v1/campaigns/settings", headers=_headers(admin),
                         json={"sov_daily_rate": 0.25})
    assert r.status_code == 200, r.text
    # subset, pas d'égalité stricte (la réponse gagne des champs — quotas 0084…)
    body = r.json()
    assert body["creative_approval"] is False and body["sov_daily_rate"] == 0.25
    r = await client.get("/api/v1/campaigns/settings", headers=_headers(admin))
    assert r.json()["sov_daily_rate"] == 0.25

    # occupancy-check renvoie le devis : 0.25 €/%/j × 10 points de SOV × 6 j
    # × 2 players = 30 €
    await _player(db_session, org)
    await _player(db_session, org)
    r = await client.post("/api/v1/campaigns/occupancy-check", headers=_headers(admin),
                          json={"starts_on": date.today().isoformat(),
                                "ends_on": (date.today() + timedelta(days=5)).isoformat(),
                                "sov_percent": 10.0,
                                "targets": [{"target_type": "org"}]})
    assert r.status_code == 200, r.text
    price = r.json()["price"]
    assert price["estimated_price"] == 30.0
    assert price["priced_players"] == 2 and price["days"] == 6
    assert all(row["cap_pct"] == 100.0 for row in r.json()["players"])

    # null explicite = désactiver le devis
    r = await client.put("/api/v1/campaigns/settings", headers=_headers(admin),
                         json={"sov_daily_rate": None})
    assert r.json()["sov_daily_rate"] is None


@pytest.mark.asyncio
async def test_approval_detail_includes_breakdown_and_price(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    from app.models.role import CmsRole as _CmsRole  # grants approvals au rôle admin
    role = (await db_session.execute(select(_CmsRole).where(
        _CmsRole.role_key == "CMS_ORG_ADMIN"))).scalars().first()
    for op in ("submit", "approve", "review"):
        await _grant(db_session, role, "approvals", op)
    await db_session.commit()

    org.campaign_sov_daily_rate = 0.5
    await _player(db_session, org)
    await _campaign(db_session, org, sov=20.0)               # occupe déjà la cible
    cand = await _campaign(db_session, org, sov=10.0, status="draft", days=3)
    await db_session.commit()

    r = await client.post("/api/v1/approvals/", headers=_headers(admin),
                          json={"resource_type": "campaign",
                                "resource_id": cand.id_campaign})
    assert r.status_code == 201, r.text
    r = await client.get(f"/api/v1/approvals/{r.json()['id_request']}",
                         headers=_headers(admin))
    info = r.json()["campaign"]
    assert len(info["other_campaigns"]) == 1
    assert info["other_campaigns"][0]["sov_percent"] == 20.0
    # 1 player × 0.5 × 10 % × 4 j = 20 €
    assert info["price"]["estimated_price"] == 20.0
