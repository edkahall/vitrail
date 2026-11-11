"""
Tests S7 Phase B1 — isolation INTRA-org du portail annonceur (migr 0080).

Un user lié à un annonceur (cms_users.id_advertiser, rôle CMS_ADVERTISER =
campaigns:read + campaigns:report) ne voit QUE les campagnes de SON annonceur :
  - liste filtrée, cross-annonceur = 404 (jamais 403 — même règle que le multi-tenant)
  - rapport de SA campagne OK, celui d'un concurrent = 404
  - endpoints org-wide (inventaire, occupancy, calendrier) = 403 « réservé au retailer »
  - liste annonceurs = le sien seul
  - écritures = 403 (RBAC : pas de campaigns:create/edit)
  - intents assistant campaign_status/report scopés par annonceur
Le retailer (permissions campagnes complètes, id_advertiser NULL) voit tout.
"""

import uuid
from datetime import date, timedelta

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.campaign import CmsAdvertiser, CmsCampaign
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsPermission, CmsRole, CmsRolePermission, CmsUserRoleAssignment
from app.models.user import CmsUser

# ── Fixtures ───────────────────────────────────────────────────────────────────

async def _ensure_perm(db: AsyncSession, module: str, op: str) -> int:
    await db.execute(
        pg_insert(CmsPermission)
        .values(module=module, operation=op, label=f"{module}.{op}")
        .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
    )
    await db.flush()
    return (await db.execute(select(CmsPermission.id_permission).where(
        CmsPermission.module == module, CmsPermission.operation == op))).scalar_one()


async def _user_with_perms(
    db: AsyncSession, id_org: int, perms: list[tuple[str, str]],
    id_advertiser: int | None = None,
) -> CmsUser:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=id_org, email=f"u-{uid}@portal.com",
        password_hash=hash_password("Password1!"), is_active=True,
        id_advertiser=id_advertiser,
    )
    db.add(user)
    await db.flush()
    role = CmsRole(id_org=id_org, role_key=f"R_{uid}", label="R", is_system=False)
    db.add(role)
    await db.flush()
    for module, op in perms:
        pid = await _ensure_perm(db, module, op)
        db.add(CmsRolePermission(id_role=role.id_role, id_permission=pid))
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    return user


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="PortalOrg", org_slug=f"portal-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


@pytest_asyncio.fixture
async def data(db_session: AsyncSession, org: CmsOrganization) -> dict:
    """2 annonceurs (Brightwell / Corvane) avec 1 campagne chacun."""
    d0 = date.today()
    adv_a = CmsAdvertiser(id_org=org.id_org, advertiser_name=f"Brightwell-{uuid.uuid4().hex[:6]}")
    adv_b = CmsAdvertiser(id_org=org.id_org, advertiser_name=f"Corvane-{uuid.uuid4().hex[:6]}")
    db_session.add_all([adv_a, adv_b])
    await db_session.flush()
    camp_a = CmsCampaign(id_org=org.id_org, id_advertiser=adv_a.id_advertiser,
                         campaign_name="Camp-A", starts_on=d0, ends_on=d0 + timedelta(days=10))
    camp_b = CmsCampaign(id_org=org.id_org, id_advertiser=adv_b.id_advertiser,
                         campaign_name="Camp-B", starts_on=d0, ends_on=d0 + timedelta(days=10))
    db_session.add_all([camp_a, camp_b])
    await db_session.commit()
    return {"adv_a": adv_a.id_advertiser, "adv_b": adv_b.id_advertiser,
            "camp_a": camp_a.id_campaign, "camp_b": camp_b.id_campaign}


@pytest_asyncio.fixture
async def adv_headers(db_session: AsyncSession, org: CmsOrganization, data: dict) -> dict:
    """User PORTAIL lié à l'annonceur A — permissions du rôle CMS_ADVERTISER (read+report)."""
    user = await _user_with_perms(
        db_session, org.id_org, [("campaigns", "read"), ("campaigns", "report")],
        id_advertiser=data["adv_a"],
    )
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}",
            "X-Test-User": str(user.id_user)}


@pytest_asyncio.fixture
async def retailer_headers(db_session: AsyncSession, org: CmsOrganization) -> dict:
    user = await _user_with_perms(
        db_session, org.id_org,
        [("campaigns", o) for o in ("create", "read", "edit", "delete", "publish", "report")],
    )
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


# ── Isolation lecture ──────────────────────────────────────────────────────────

async def test_advertiser_sees_only_own_campaigns(
    client: AsyncClient, adv_headers: dict, data: dict,
) -> None:
    rows = (await client.get("/api/v1/campaigns/", headers=adv_headers)).json()
    assert [c["id_campaign"] for c in rows] == [data["camp_a"]]

    # Le filtre client id_advertiser est IGNORÉ (scope forcé)
    rows = (await client.get(
        f"/api/v1/campaigns/?id_advertiser={data['adv_b']}", headers=adv_headers)).json()
    assert [c["id_campaign"] for c in rows] == [data["camp_a"]]

    r = await client.get(f"/api/v1/campaigns/{data['camp_a']}", headers=adv_headers)
    assert r.status_code == 200
    # Cross-annonceur = 404, jamais 403
    r = await client.get(f"/api/v1/campaigns/{data['camp_b']}", headers=adv_headers)
    assert r.status_code == 404
    r = await client.get(f"/api/v1/campaigns/{data['camp_b']}/creatives", headers=adv_headers)
    assert r.status_code == 404


async def test_advertiser_report_scoped(
    client: AsyncClient, adv_headers: dict, data: dict,
) -> None:
    r = await client.get(f"/api/v1/campaigns/{data['camp_a']}/report", headers=adv_headers)
    assert r.status_code == 200
    r = await client.get(f"/api/v1/campaigns/{data['camp_b']}/report", headers=adv_headers)
    assert r.status_code == 404


async def test_advertiser_blocked_on_org_wide_endpoints(
    client: AsyncClient, adv_headers: dict, data: dict,
) -> None:
    d0 = date.today()
    r = await client.get("/api/v1/campaigns/inventory", headers=adv_headers)
    assert r.status_code == 403
    r = await client.get(
        "/api/v1/campaigns/occupancy-daily",
        params={"date_from": d0.isoformat(), "date_to": d0.isoformat()},
        headers=adv_headers)
    assert r.status_code == 403
    r = await client.post("/api/v1/campaigns/occupancy-check", headers=adv_headers, json={
        "starts_on": d0.isoformat(), "ends_on": d0.isoformat(),
        "sov_percent": 10, "targets": [{"target_type": "org"}],
    })
    assert r.status_code == 403


async def test_advertiser_list_shows_only_own(
    client: AsyncClient, adv_headers: dict, data: dict,
) -> None:
    rows = (await client.get("/api/v1/advertisers/", headers=adv_headers)).json()
    assert [a["id_advertiser"] for a in rows] == [data["adv_a"]]


async def test_advertiser_cannot_write(
    client: AsyncClient, adv_headers: dict, data: dict,
) -> None:
    # RBAC : le rôle annonceur n'a ni create ni edit ni publish
    r = await client.post("/api/v1/campaigns/", headers=adv_headers, json={
        "campaign_name": "X", "id_advertiser": data["adv_a"],
        "starts_on": date.today().isoformat(), "ends_on": date.today().isoformat(),
        "sov_percent": 10, "targets": [{"target_type": "org"}],
    })
    assert r.status_code == 403
    r = await client.post(f"/api/v1/campaigns/{data['camp_a']}/status",
                          headers=adv_headers, json={"action": "publish"})
    assert r.status_code == 403


async def test_retailer_unaffected(
    client: AsyncClient, retailer_headers: dict, data: dict,
) -> None:
    rows = (await client.get("/api/v1/campaigns/", headers=retailer_headers)).json()
    assert {c["id_campaign"] for c in rows} == {data["camp_a"], data["camp_b"]}
    r = await client.get("/api/v1/campaigns/inventory", headers=retailer_headers)
    assert r.status_code == 200


# ── Gestion des accès portail (B2 — org-admin) ────────────────────────────────

@pytest_asyncio.fixture
async def org_admin_headers(db_session: AsyncSession, org: CmsOrganization) -> dict:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org, email=f"admin-{uid}@portal.com",
        password_hash=hash_password("Password1!"), is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.execute(
        pg_insert(CmsRole)
        .values(role_key="CMS_ORG_ADMIN", label="Org Admin", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    await db_session.flush()
    role = (await db_session.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return {"Authorization": f"Bearer {create_access_token(
        user.id_user, extra={'org': org.id_org, 'roles': ['CMS_ORG_ADMIN'], 'locale': 'en'})}"}


async def test_invite_list_revoke_portal_user(
    client: AsyncClient, org_admin_headers: dict, data: dict, db_session: AsyncSession,
) -> None:
    email = f"buyer-{uuid.uuid4().hex[:6]}@brightwell.com"
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": email, "full_name": "Jane Buyer"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["initial_password"]                      # mot de passe affiché une fois
    uid = body["user"]["id_user"]
    assert body["user"]["email"] == email

    # Lié + rôle CMS_ADVERTISER attribué
    db_session.expire_all()
    user = (await db_session.execute(
        select(CmsUser).where(CmsUser.id_user == uid))).scalar_one()
    assert user.id_advertiser == data["adv_a"]
    role_keys = (await db_session.execute(
        select(CmsRole.role_key)
        .join(CmsUserRoleAssignment, CmsUserRoleAssignment.id_role == CmsRole.id_role)
        .where(CmsUserRoleAssignment.id_user == uid)
    )).scalars().all()
    assert "CMS_ADVERTISER" in role_keys

    # Le compte fraîchement invité est isolé sur SON annonceur
    inv_headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    rows = (await client.get("/api/v1/campaigns/", headers=inv_headers)).json()
    assert [c["id_campaign"] for c in rows] == [data["camp_a"]]

    # Ré-inviter le MÊME email sur le MÊME annonceur = get-or-link idempotent
    # (retour Ed 11/07 — plus de 409 « email pris ») : compte actif → mdp INCHANGÉ
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": email},
    )
    assert r.status_code == 201, r.text
    assert r.json()["linked_existing"] is True
    assert r.json()["initial_password"] is None

    # Liste
    listed = (await client.get(
        f"/api/v1/advertisers/{data['adv_a']}/users", headers=org_admin_headers)).json()
    assert [u["id_user"] for u in listed] == [uid]

    # Révocation = désactivation (le login filtre is_active)
    r = await client.delete(
        f"/api/v1/advertisers/{data['adv_a']}/users/{uid}", headers=org_admin_headers)
    assert r.status_code == 204
    db_session.expire_all()
    user = (await db_session.execute(
        select(CmsUser).where(CmsUser.id_user == uid))).scalar_one()
    assert user.is_active is False

    # Ré-invitation d'un compte RÉVOQUÉ (le « supprimé » IAM = désactivé) → réactivé
    # + NOUVEAU mot de passe affiché une fois
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": email},
    )
    assert r.status_code == 201, r.text
    assert r.json()["linked_existing"] is True
    assert r.json()["initial_password"]
    db_session.expire_all()
    user = (await db_session.execute(
        select(CmsUser).where(CmsUser.id_user == uid))).scalar_one()
    assert user.is_active is True and user.id_advertiser == data["adv_a"]

    # Révocation avec PURGE (retour Ed 11/07) → délié → disparaît de la liste
    r = await client.delete(
        f"/api/v1/advertisers/{data['adv_a']}/users/{uid}?purge=true",
        headers=org_admin_headers)
    assert r.status_code == 204
    listed = (await client.get(
        f"/api/v1/advertisers/{data['adv_a']}/users", headers=org_admin_headers)).json()
    assert listed == []

    # Suppression via /settings/iam = désactive ET délie (plus de fantôme au drawer)
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": email},
    )
    assert r.status_code == 201  # re-rattaché (get-or-link)
    r = await client.delete(f"/api/v1/users/{uid}", headers=org_admin_headers)
    assert r.status_code == 204
    listed = (await client.get(
        f"/api/v1/advertisers/{data['adv_a']}/users", headers=org_admin_headers)).json()
    assert listed == []
    db_session.expire_all()
    user = (await db_session.execute(
        select(CmsUser).where(CmsUser.id_user == uid))).scalar_one()
    assert user.is_active is False and user.id_advertiser is None


async def test_invite_links_existing_user_with_guards(
    client: AsyncClient, org_admin_headers: dict, data: dict, db_session: AsyncSession,
    org: CmsOrganization,
) -> None:
    """Get-or-link (retour Ed 11/07 : user créé dans /settings/iam, impossible à lier ;
    « supprimé » IAM = désactivé → 409 email pris). Un user existant de l'org se
    RATTACHE : rôles internes remplacés par CMS_ADVERTISER, mdp inchangé si actif.
    Refus : autre annonceur (§3.1), compte admin, email d'une autre org."""
    id_org = org.id_org  # capturé avant les expire_all (instance fixture sinon expirée)
    # User interne existant (façon /settings/iam, rôle custom quelconque)
    plain = CmsUser(id_org=id_org, email=f"iam-{uuid.uuid4().hex[:6]}@demo.com",
                    password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(plain)
    await db_session.flush()
    role = CmsRole(id_org=id_org, role_key=f"VIEW-{uuid.uuid4().hex[:6]}",
                   label="viewer", is_system=False)
    db_session.add(role)
    await db_session.flush()
    db_session.add(CmsUserRoleAssignment(id_user=plain.id_user, id_role=role.id_role))
    await db_session.commit()
    plain_id, plain_email = plain.id_user, plain.email

    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": plain_email},
    )
    assert r.status_code == 201, r.text
    assert r.json()["linked_existing"] is True
    assert r.json()["initial_password"] is None  # compte actif → mdp conservé
    db_session.expire_all()
    linked = (await db_session.execute(
        select(CmsUser).where(CmsUser.id_user == plain_id))).scalar_one()
    assert linked.id_advertiser == data["adv_a"]
    role_keys = (await db_session.execute(
        select(CmsRole.role_key)
        .join(CmsUserRoleAssignment, CmsUserRoleAssignment.id_role == CmsRole.id_role)
        .where(CmsUserRoleAssignment.id_user == plain_id)
    )).scalars().all()
    assert role_keys == ["CMS_ADVERTISER"]  # rôles internes REMPLACÉS

    # Déjà lié à l'annonceur A → l'inviter chez B = 409 (1 compte = 1 annonceur)
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_b']}/users",
        headers=org_admin_headers, json={"email": plain_email},
    )
    assert r.status_code == 409

    # Compte administrateur → refus explicite
    admin_u = CmsUser(id_org=id_org, email=f"adm-{uuid.uuid4().hex[:6]}@demo.com",
                      password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(admin_u)
    await db_session.flush()
    admin_role = (await db_session.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=admin_u.id_user, id_role=admin_role.id_role))
    await db_session.commit()
    admin_email = admin_u.email
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": admin_email},
    )
    assert r.status_code == 409 and "administrator" in r.json()["detail"]

    # Email d'une AUTRE org → 409 générique (pas de rattachement cross-org)
    other_org = CmsOrganization(org_name="Other", org_slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other_org)
    await db_session.flush()
    foreign = CmsUser(id_org=other_org.id_org, email=f"ext-{uuid.uuid4().hex[:6]}@demo.com",
                      password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(foreign)
    await db_session.commit()
    foreign_email = foreign.email
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=org_admin_headers, json={"email": foreign_email},
    )
    assert r.status_code == 409


async def test_portal_user_management_requires_org_admin(
    client: AsyncClient, retailer_headers: dict, data: dict,
) -> None:
    r = await client.post(
        f"/api/v1/advertisers/{data['adv_a']}/users",
        headers=retailer_headers, json={"email": "x@y.com"},
    )
    assert r.status_code == 403


async def test_portal_branding_and_users_me(
    client: AsyncClient, adv_headers: dict, org: CmsOrganization, data: dict,
) -> None:
    r = await client.get("/api/v1/portal/branding", headers=adv_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["org_name"] == org.org_name
    assert body["advertiser_name"] and "Brightwell" in body["advertiser_name"]
    assert body["primary_color"].startswith("#")

    # users/me expose id_advertiser (redirect par rôle côté front)
    me = (await client.get("/api/v1/users/me", headers=adv_headers)).json()
    assert me["id_advertiser"] == data["adv_a"]


# ── Assistant : intents scopés ─────────────────────────────────────────────────

async def test_assistant_intents_scoped_by_advertiser(
    db_session: AsyncSession, org: CmsOrganization, data: dict,
) -> None:
    from app.services.assistant_tools import query_tenant_data

    user = await _user_with_perms(
        db_session, org.id_org, [("campaigns", "read"), ("campaigns", "report")],
        id_advertiser=data["adv_a"],
    )
    out = await query_tenant_data(db_session, org.id_org, "campaign_status",
                                  filters=None, id_user=user.id_user)
    assert [c["name"] for c in out["campaigns"]] == ["Camp-A"]

    # Une campagne concurrente nommée explicitement = introuvable
    out = await query_tenant_data(db_session, org.id_org, "campaign_status",
                                  filters={"campaign": "Camp-B"}, id_user=user.id_user)
    assert "error" in out
