"""Tests — décodage matériel auto-détecté & remonté au serveur (pré-Sprint 2.7).

Couvre :
  - decode_health : unknown / hardware / software / sf_hw_decode=off
  - media_decode_risk : green / amber / red
  - os_family
  - endpoint GET /players/decode-report (org-scoped, route fixe avant /{id})
  - persistance de capabilities via PlayerRead
"""

import uuid

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services.player_capabilities import (
    SW_CEILING_LABEL,
    decode_health,
    media_decode_risk,
    os_family,
)


class _StubPlayer:
    def __init__(self, capabilities=None, player_os="linux-x64"):
        self.capabilities = capabilities
        self.player_os = player_os


# ── decode_health (pur) ──────────────────────────────────────────────────────

def test_decode_health_unknown_when_no_caps() -> None:
    h = decode_health(_StubPlayer(capabilities=None))
    assert h["effective_decode"] == "unknown"
    assert h["severity"] == "unknown"
    assert h["method"] is None
    assert h["warnings"]  # message « non remontées »


def test_decode_health_hardware_active() -> None:
    h = decode_health(_StubPlayer(capabilities={
        "hw_decode": "vaapi", "hw_decode_active": True,
        "gpu_vendor": "intel", "gpu_model": "UHD 600",
    }))
    assert h["effective_decode"] == "hardware"
    assert h["severity"] == "ok"
    assert h["method"] == "vaapi"
    assert h["warnings"] == []
    assert h["gpu_vendor"] == "intel"


def test_decode_health_software_with_capable_gpu() -> None:
    """GPU VA-API détecté mais Chromium n'active pas le HW (cas Gemini Lake/Electron)."""
    h = decode_health(_StubPlayer(capabilities={
        "hw_decode": "vaapi", "hw_decode_active": False,
    }))
    assert h["effective_decode"] == "software"
    assert h["severity"] == "warning"
    assert any("is NOT" in w or "software" in w for w in h["warnings"])
    assert any(SW_CEILING_LABEL in w for w in h["warnings"])


def test_decode_health_chromeos_native_is_hardware() -> None:
    """ChromeOS PWA : décodage natif transparent → matériel sans hw_decode_active.

    Le bridge navigateur remonte hw_decode='chromium-native' (pas de drapeau actif
    mesurable). On le classe matériel → la sélection de rendition n'est plus « heureuse
    par défaut » mais bornée par la définition de l'écran. Cf. backlog ChromeOS UI CMS.
    """
    h = decode_health(_StubPlayer(
        capabilities={"hw_decode": "chromium-native", "platform": "chromeos",
                      "display_modes": ["3840x2160"]},
        player_os="chromeos",
    ))
    assert h["effective_decode"] == "hardware"
    assert h["severity"] == "ok"
    assert h["method"] == "chromium-native"
    assert h["warnings"] == []


def test_decode_health_sf_mode_off() -> None:
    h = decode_health(_StubPlayer(capabilities={
        "hw_decode": "d3d11", "hw_decode_active": True, "sf_hw_decode": "off",
    }))
    assert h["effective_decode"] == "software"  # override gagne sur active=True
    assert any("manually disabled" in w for w in h["warnings"])


# ── media_decode_risk (pur) ──────────────────────────────────────────────────

def test_media_risk_green_hw_4k() -> None:
    p = _StubPlayer(capabilities={"hw_decode": "vaapi", "hw_decode_active": True})
    r = media_decode_risk(p, 3840, 2160, 60, "h264")
    assert r["verdict"] == "green"


def test_media_risk_green_software_within_ceiling() -> None:
    p = _StubPlayer(capabilities={"hw_decode": "none", "hw_decode_active": False})
    r = media_decode_risk(p, 1920, 1080, 30, "h264")
    assert r["verdict"] == "green"


def test_media_risk_amber_software_fhd60() -> None:
    p = _StubPlayer(capabilities={"hw_decode": "none", "hw_decode_active": False})
    r = media_decode_risk(p, 1920, 1080, 60, "h264")
    assert r["verdict"] == "amber"


def test_media_risk_red_software_4k60() -> None:
    p = _StubPlayer(capabilities={"hw_decode": "none", "hw_decode_active": False})
    r = media_decode_risk(p, 3840, 2160, 60, "h264")
    assert r["verdict"] == "red"


def test_media_risk_red_unsupported_codec() -> None:
    p = _StubPlayer(capabilities={"hw_decode": "vaapi", "hw_decode_active": True})
    r = media_decode_risk(p, 1920, 1080, 30, "prores")
    assert r["verdict"] == "red"


def test_os_family() -> None:
    assert os_family("linux-arm64") == "linux"
    assert os_family("windows-x64") == "windows"
    assert os_family("android-arm64") == "android"
    assert os_family(None) == "unknown"


# ── Endpoint /players/decode-report ──────────────────────────────────────────

@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="CapOrg", org_slug=f"cap-org-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, org: CmsOrganization) -> str:
    user = CmsUser(
        id_org=org.id_org,
        email=f"cap-admin-{uuid.uuid4().hex[:8]}@test.com",
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
        user.id_user, extra={"org": org.id_org, "roles": ["CMS_ORG_ADMIN"], "locale": "en"}
    )


async def test_decode_report_endpoint(
    client: AsyncClient, admin_token: str, db_session: AsyncSession
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.post(
        "/api/v1/players/",
        json={"player_name": f"cap-{uuid.uuid4().hex[:8]}", "player_os": "linux-x64"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id_player"]

    # Le player remonte ses capacités (simulé : écriture directe).
    await db_session.execute(
        update(CmsPlayer)
        .where(CmsPlayer.id_player == pid)
        .values(capabilities={"hw_decode": "vaapi", "hw_decode_active": False,
                              "gpu_vendor": "intel"})
    )
    await db_session.commit()

    # PlayerRead expose capabilities
    detail = await client.get(f"/api/v1/players/{pid}", headers=headers)
    assert detail.json()["capabilities"]["gpu_vendor"] == "intel"

    # decode-report : route fixe résolue (pas capturée par /{id_player}) + verdict software
    report = await client.get("/api/v1/players/decode-report", headers=headers)
    assert report.status_code == 200, report.text
    row = next(r for r in report.json() if r["id_player"] == pid)
    assert row["effective_decode"] == "software"
    assert row["severity"] == "warning"
    assert row["warnings"]
