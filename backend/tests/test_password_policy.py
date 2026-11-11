"""
Tests politique de mot de passe (audit sécurité 2026-06-25).

- Unitaires `check_password` : longueur, plancher absolu, exigences de complexité.
- Intégration : POST /users refuse un mot de passe faible (422) et accepte un fort (201).
"""

import uuid

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services.password_policy import check_password


# ── Unitaires (purs) ─────────────────────────────────────────────────────────

def test_check_password_too_short():
    missing = check_password("Ab1!", min_len=10, policy={})
    assert any("characters" in m for m in missing)


def test_check_password_absolute_floor():
    # Même si l'org met min_len=2, le plancher dur de 8 s'applique.
    missing = check_password("Ab1!def", min_len=2, policy={})  # 7 chars
    assert any("8 characters" in m for m in missing)
    assert check_password("Ab1!defg", min_len=2, policy={}) == []  # 8 chars → ok


def test_check_password_complexity_requirements():
    policy = {"uppercase": True, "digit": True, "special": True, "lowercase": True}
    missing = check_password("aaaaaaaaaa", min_len=10, policy=policy)
    assert "an uppercase letter" in missing
    assert "a digit" in missing
    assert "a special character" in missing
    assert "a lowercase letter" not in missing


def test_check_password_strong_passes():
    policy = {"uppercase": True, "digit": True, "special": False}
    assert check_password("Password12", min_len=10, policy=policy) == []


# ── Fixtures intégration ─────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    slug = f"pwpol-org-{uuid.uuid4().hex[:8]}"
    o = CmsOrganization(org_name="PwPol Org", org_slug=slug)
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))  # défaut min 10 + uppercase + digit
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, org: CmsOrganization) -> str:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org,
        email=f"admin-{uid}@pwpol-test.com",
        password_hash=hash_password("Password1!"),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.execute(
        pg_insert(CmsRole)
        .values(role_key="CMS_ORG_ADMIN", label="Org Admin", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    await db_session.flush()
    role = (
        await db_session.execute(select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))
    ).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return create_access_token(
        user.id_user,
        extra={"org": org.id_org, "roles": ["CMS_ORG_ADMIN"], "locale": "en"},
    )


# ── Intégration ──────────────────────────────────────────────────────────────

async def test_create_user_rejects_weak_password(client: AsyncClient, admin_token: str) -> None:
    email = f"weak-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(
        "/api/v1/users",
        json={"email": email, "password": "weak"},  # trop court + pas de maj/chiffre
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422
    assert "Password" in resp.json()["detail"]


async def test_create_user_accepts_strong_password(client: AsyncClient, admin_token: str) -> None:
    email = f"strong-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(
        "/api/v1/users",
        json={"email": email, "password": "Password12"},  # 10 chars, maj + chiffre
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201
