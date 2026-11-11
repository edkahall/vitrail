"""Tests Retail Media S4 (§2.8) — rapport annonceur.

- build_campaign_report : PoP agrégés (passages/signés/antenne), SOV servi = antenne
  créatifs / antenne totale des players ciblés, répartitions player/jour/créatif,
  CPM indicatif = passages × cpm_rate / 1000.
- endpoints : GET /report (RBAC campaigns:report), GET /report.pdf (%PDF, white-label),
  POST /share (org-admin) → lien public /public/share/{token} type campaign qui sert
  le rapport (pas le dashboard) ; lien exécutif intact.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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
from app.models.pop import CmsProofOfPlay
from app.models.role import (
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser
from app.services.campaign_report import build_campaign_report

TZ = ZoneInfo("Europe/Paris")


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="RMR", org_slug=f"rmr-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


@pytest_asyncio.fixture
async def org_admin(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(id_org=org.id_org, email=f"rmr-{uid}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    await db_session.flush()
    # Rôle système org-admin (require_org_admin) + permissions campaigns.
    role = (await db_session.execute(select(CmsRole).where(
        CmsRole.role_key == "CMS_ORG_ADMIN"))).scalars().first()
    if role is None:
        role = CmsRole(id_org=None, role_key="CMS_ORG_ADMIN", label="Org admin", is_system=True)
        db_session.add(role)
        await db_session.flush()
    for op in ("read", "report", "publish"):
        await db_session.execute(
            pg_insert(CmsPermission)
            .values(module="campaigns", operation=op, label=f"campaigns.{op}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation"))
        await db_session.flush()
        perm = (await db_session.execute(select(CmsPermission).where(
            CmsPermission.module == "campaigns", CmsPermission.operation == op))).scalar_one()
        await db_session.execute(
            pg_insert(CmsRolePermission)
            .values(id_role=role.id_role, id_permission=perm.id_permission)
            .on_conflict_do_nothing())
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return user


@pytest_asyncio.fixture
def headers(org_admin: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(org_admin.id_user)}"}


async def _scene(db: AsyncSession, org: CmsOrganization):
    """Campagne approuvée (SOV 20 %, CPM 5) ciblant un player + PoP :
    6 passages créatif (5 signés, 27 s) + 54 passages « maison » (30 s) hier/aujourd'hui."""
    player = CmsPlayer(id_org=org.id_org, player_name="P-report", player_os="linux")
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Brightwell")
    db.add_all([player, adv])
    await db.flush()
    creative = CmsMedia(id_org=org.id_org, media_name="spot-brightwell", media_type="video",
                        storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                        transcode_status="done", duration_ms=27_000)
    home = CmsMedia(id_org=org.id_org, media_name="maison", media_type="video",
                    storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="h",
                    transcode_status="done", duration_ms=30_000)
    db.add_all([creative, home])
    await db.flush()
    camp = CmsCampaign(
        id_org=org.id_org, id_advertiser=adv.id_advertiser, campaign_name="Été Brightwell",
        status="approved", starts_on=date.today() - timedelta(days=1),
        ends_on=date.today() + timedelta(days=7),
        sov_percent=20.0, pricing_model="sov", price_amount=1500, cpm_rate=5.0,
    )
    db.add(camp)
    await db.flush()
    db.add_all([
        CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                            id_media=creative.id_media, duration_ms=27_000),
        CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                          target_type="player", target_id=player.id_player),
    ])
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    pops = []
    # 3 passages créatif hier (signés) + 3 aujourd'hui (2 signés)
    for i, (at, signed) in enumerate([(yesterday, True)] * 3 + [(now, True), (now, True),
                                                                (now, False)]):
        pops.append(CmsProofOfPlay(
            id_org=org.id_org, id_player=player.id_player, id_media=creative.id_media,
            played_at=at - timedelta(minutes=i), duration_ms=27_000,
            merkle_hash="m" * 64, signature="sig" if signed else None))
    # 54 passages maison (dénominateur SOV servi)
    for i in range(54):
        pops.append(CmsProofOfPlay(
            id_org=org.id_org, id_player=player.id_player, id_media=home.id_media,
            played_at=now - timedelta(minutes=i + 10), duration_ms=30_000,
            merkle_hash="m" * 64, signature="sig"))
    db.add_all(pops)
    await db.commit()
    return camp, player, creative


@pytest.mark.asyncio
async def test_build_campaign_report_aggregates(db_session, org):
    camp, player, creative = await _scene(db_session, org)
    report = await build_campaign_report(db_session, camp, TZ)

    assert report["campaign"]["campaign_name"] == "Été Brightwell"
    assert report["campaign"]["advertiser_name"] == "Brightwell"
    t = report["totals"]
    assert t["plays"] == 6 and t["signed_plays"] == 5
    assert t["airtime_ms"] == 6 * 27_000
    assert t["players_reached"] == 1 and t["players_targeted"] == 1

    # SOV servi = 162 s créatif / (162 + 1620) s total ≈ 9,1 %
    assert report["sov"]["sold_pct"] == 20.0
    assert report["sov"]["served_pct"] == pytest.approx(9.1, abs=0.1)

    # CPM indicatif = 6 × 5 / 1000
    assert report["amount"]["cpm_indicative"] == pytest.approx(0.03)
    assert report["amount"]["amount"] == 1500.0

    assert report["per_player"][0]["id_player"] == player.id_player
    assert report["per_player"][0]["plays"] == 6
    assert report["per_creative"] == [{
        "id_media": creative.id_media, "media_name": "spot-brightwell",
        "plays": 6, "airtime_ms": 6 * 27_000}]
    assert len(report["per_day"]) == 2  # hier + aujourd'hui
    assert sum(d["plays"] for d in report["per_day"]) == 6


@pytest.mark.asyncio
async def test_report_endpoints(client: AsyncClient, db_session, org, headers, org_admin):
    camp, *_ = await _scene(db_session, org)

    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}/report", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["plays"] == 6

    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}/report.pdf", headers=headers)
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    # nom de fichier dans la langue du profil : anglais par défaut, français si fr
    assert "campaign_report_" in r.headers["content-disposition"]
    org_admin.locale = "fr"
    await db_session.commit()
    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}/report.pdf", headers=headers)
    assert r.status_code == 200
    assert "rapport_campagne_" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_report_site_hierarchy_and_pdf_levels(client: AsyncClient, db_session, org, headers):
    """Retour Ed #3 : per_site / per_site_group agrégés (SOV moyen par niveau, site
    multi-groupe compté une fois, players sans site sous id_site null) + PDF ?level=."""
    from app.models.organization import CmsSite
    from app.models.site_group import CmsSiteGroup, CmsSiteGroupMember

    s1 = CmsSite(id_org=org.id_org, site_name="Paris")
    s2 = CmsSite(id_org=org.id_org, site_name="Lyon")
    db_session.add_all([s1, s2])
    await db_session.flush()
    grp = CmsSiteGroup(id_org=org.id_org, group_name="Nord")
    db_session.add(grp)
    await db_session.flush()
    db_session.add(CmsSiteGroupMember(
        id_site_group=grp.id_site_group, id_site=s1.id_site, id_org=org.id_org))

    p1 = CmsPlayer(id_org=org.id_org, player_name="P1", player_os="linux", id_site=s1.id_site)
    p2 = CmsPlayer(id_org=org.id_org, player_name="P2", player_os="linux", id_site=s1.id_site)
    p3 = CmsPlayer(id_org=org.id_org, player_name="P3", player_os="linux", id_site=s2.id_site)
    p4 = CmsPlayer(id_org=org.id_org, player_name="P4", player_os="linux")  # sans site
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Corvane")
    db_session.add_all([p1, p2, p3, p4, adv])
    await db_session.flush()
    creative = CmsMedia(id_org=org.id_org, media_name="spot", media_type="video",
                        storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                        transcode_status="done", duration_ms=27_000)
    home = CmsMedia(id_org=org.id_org, media_name="maison", media_type="video",
                    storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="h",
                    transcode_status="done", duration_ms=27_000)
    db_session.add_all([creative, home])
    await db_session.flush()
    camp = CmsCampaign(
        id_org=org.id_org, id_advertiser=adv.id_advertiser, campaign_name="Hiérarchie",
        status="approved", starts_on=date.today() - timedelta(days=1),
        ends_on=date.today() + timedelta(days=7), sov_percent=15.0,
        pricing_model="flat", price_amount=100,
    )
    db_session.add(camp)
    await db_session.flush()
    db_session.add_all([
        CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                            id_media=creative.id_media, duration_ms=27_000),
        CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                          target_type="org", target_id=None),
    ])
    now = datetime.now(UTC)
    pops = []
    for player, n_creative in ((p1, 2), (p2, 1), (p3, 3), (p4, 1)):
        for i in range(n_creative):
            pops.append(CmsProofOfPlay(
                id_org=org.id_org, id_player=player.id_player, id_media=creative.id_media,
                played_at=now - timedelta(minutes=i), duration_ms=27_000,
                merkle_hash="m" * 64, signature="sig"))
        for i in range(10):  # antenne « maison » = dénominateur SOV
            pops.append(CmsProofOfPlay(
                id_org=org.id_org, id_player=player.id_player, id_media=home.id_media,
                played_at=now - timedelta(minutes=i + 30), duration_ms=27_000,
                merkle_hash="m" * 64, signature="sig"))
    db_session.add_all(pops)
    await db_session.commit()

    report = await build_campaign_report(db_session, camp, TZ)
    assert all("id_site" in p for p in report["per_player"])

    sites = {s["site_name"]: s for s in report["per_site"]}
    assert sites["Paris"]["plays"] == 3 and sites["Paris"]["players_reached"] == 2
    assert sites["Lyon"]["plays"] == 3 and sites["Lyon"]["players_reached"] == 1
    assert sites[None]["plays"] == 1  # players sans site
    # SOV moyen Paris = 3×27 / ((2+10 + 1+10)×27) = 3/23
    assert sites["Paris"]["sov_served_pct"] == pytest.approx(round(100 * 3 / 23, 1), abs=0.1)
    # tri par passages décroissants
    assert report["per_site"][0]["plays"] >= report["per_site"][-1]["plays"]

    groups = report["per_site_group"]
    assert len(groups) == 1 and groups[0]["group_name"] == "Nord"
    assert groups[0]["plays"] == 3 and groups[0]["site_ids"] == [s1.id_site]
    assert groups[0]["players_reached"] == 2

    # PDF aux 3 granularités + niveau inconnu = 422
    for lvl in ("player", "site", "site_group"):
        r = await client.get(
            f"/api/v1/campaigns/{camp.id_campaign}/report.pdf?level={lvl}", headers=headers)
        assert r.status_code == 200 and r.content[:4] == b"%PDF", lvl
    r = await client.get(
        f"/api/v1/campaigns/{camp.id_campaign}/report.pdf?level=bogus", headers=headers)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_campaign_share_link_public_view(client: AsyncClient, db_session, org, headers):
    camp, *_ = await _scene(db_session, org)

    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/share", headers=headers,
                          json={"label": "Lien Brightwell"})
    assert r.status_code == 201, r.text
    share = r.json()
    assert share["url"].startswith("/share/")

    # Vue publique SANS login → rapport de campagne (pas le dashboard exécutif).
    r = await client.get(f"/api/v1/public/share/{share['token']}")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["report_type"] == "campaign"
    assert view["dashboard"] is None
    assert view["campaign_report"]["totals"]["plays"] == 6
    assert view["campaign_report"]["campaign"]["campaign_name"] == "Été Brightwell"

    # Listé côté campagne + accès journalisé.
    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}/shares", headers=headers)
    assert r.status_code == 200
    assert r.json()[0]["access_count"] == 1


@pytest.mark.asyncio
async def test_delete_campaign_revokes_share_links(client: AsyncClient, db_session, org, headers):
    """Nettoyage complet à la suppression : liens annonceur révoqués + /share public 404."""
    from app.models.report import CmsReportShareLink

    camp, *_ = await _scene(db_session, org)
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/share", headers=headers,
                          json={"label": "Lien à révoquer"})
    assert r.status_code == 201
    token = r.json()["token"]

    # La permission delete manque à la fixture → l'accorder (même rôle org-admin)
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.models.role import CmsPermission, CmsRole, CmsRolePermission
    role = (await db_session.execute(sa_select(CmsRole).where(
        CmsRole.role_key == "CMS_ORG_ADMIN"))).scalars().first()
    await db_session.execute(
        pg_insert(CmsPermission)
        .values(module="campaigns", operation="delete", label="campaigns.delete")
        .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation"))
    await db_session.flush()
    perm = (await db_session.execute(sa_select(CmsPermission).where(
        CmsPermission.module == "campaigns",
        CmsPermission.operation == "delete"))).scalar_one()
    await db_session.execute(
        pg_insert(CmsRolePermission)
        .values(id_role=role.id_role, id_permission=perm.id_permission)
        .on_conflict_do_nothing())
    await db_session.commit()

    r = await client.delete(f"/api/v1/campaigns/{camp.id_campaign}", headers=headers)
    assert r.status_code == 204

    link = (await db_session.execute(sa_select(CmsReportShareLink).where(
        CmsReportShareLink.token == token))).scalar_one()
    await db_session.refresh(link)
    assert link.revoked is True
    # Le lien public ne sert plus rien
    r = await client.get(f"/api/v1/public/share/{token}")
    assert r.status_code == 404
