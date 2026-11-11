"""Tests Notifications (§XXII) — canaux webhook, règles, test, journal."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services.notifications import build_message


def test_build_message() -> None:
    subj, body = build_message(
        "player_offline", {"player_name": "NUC", "id_player": 1, "offline_min": 12})
    assert "NUC" in subj and "12 min" in body
    subj2, _ = build_message("test", {})
    assert "test" in subj2.lower()
    subj3, body3 = build_message(
        "feed_error", {"feed_name": "BFM", "id_feed": 7, "error": "download: timeout"})
    assert "BFM" in subj3 and "timeout" in body3


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="NotifOrg", org_slug=f"notif-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, org: CmsOrganization) -> str:
    user = CmsUser(id_org=org.id_org, email=f"adm-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    await db_session.flush()
    await db_session.execute(
        pg_insert(CmsRole).values(role_key="CMS_SUPER_ADMIN", label="Super", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    await db_session.flush()
    role = (await db_session.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_SUPER_ADMIN"))).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return create_access_token(
        user.id_user, extra={"org": org.id_org, "roles": ["CMS_SUPER_ADMIN"], "locale": "fr"})


async def test_notifications_flow(client: AsyncClient, admin_token: str) -> None:
    h = {"Authorization": f"Bearer {admin_token}"}

    # URL invalide → 400
    bad = await client.post("/api/v1/notifications/channels", headers=h,
                            json={"name": "X", "config": {"url": "pas-une-url"}})
    assert bad.status_code == 400

    # Canal webhook valide. ⚠️ L'hôte doit être RÉSOLVABLE et public : depuis l'audit du
    # menu ⑬ (2026-07-31), l'URL passe par `url_is_safe` — l'ancien `hooks.example.com`
    # n'existe pas et est désormais refusé, à raison (un webhook vers un hôte inexistant
    # n'aurait de toute façon jamais rien livré).
    ch = await client.post("/api/v1/notifications/channels", headers=h,
                           json={"name": "Slack", "config": {"url": "https://example.com/x"}})
    assert ch.status_code == 201
    id_channel = ch.json()["id_channel"]

    # Règle : player_offline → ce canal
    rule = await client.post("/api/v1/notifications/rules", headers=h,
                             json={"event_type": "player_offline", "id_channel": id_channel})
    assert rule.status_code == 201
    # Type d'événement inconnu → 400
    r400 = await client.post("/api/v1/notifications/rules", headers=h,
                             json={"event_type": "bogus", "id_channel": id_channel})
    assert r400.status_code == 400

    # Test du canal (webhook mocké) → journalisé
    # ⚠️ `webhook_post` rend un TRIPLET depuis P1.7 : (ok, détail, réessayable). Le
    # 3ᵉ terme dit si l'échec vaut un renvoi — un 4xx ne vaut jamais un renvoi.
    with patch("app.api.v1.endpoints.notifications.notifications.webhook_post",
               return_value=(True, "HTTP 200", False)):
        tr = await client.post(f"/api/v1/notifications/channels/{id_channel}/test", headers=h)
    assert tr.status_code == 200 and tr.json()["ok"] is True
    log = (await client.get("/api/v1/notifications/log", headers=h)).json()
    assert any(entry["event_type"] == "test" and entry["status"] == "sent" for entry in log)

    # event-types exposés
    ets = (await client.get("/api/v1/notifications/event-types", headers=h)).json()
    assert any(e["key"] == "player_offline" for e in ets["event_types"])

    # Suppression du canal → cascade sur la règle
    dc = await client.delete(f"/api/v1/notifications/channels/{id_channel}", headers=h)
    assert dc.status_code == 204
    assert (await client.get("/api/v1/notifications/rules", headers=h)).json() == []


async def test_email_channel(client: AsyncClient, admin_token: str) -> None:
    """Canal EMAIL §XXII (2026-07-12) : validation des destinataires, test SMTP mocké,
    dispatch réel via une règle (deliver_to_channel route par type)."""
    from app.services import notifications as notif_svc

    h = {"Authorization": f"Bearer {admin_token}"}

    # destinataires invalides → 400 ; type inconnu → 400
    bad = await client.post("/api/v1/notifications/channels", headers=h,
                            json={"name": "M", "channel_type": "email",
                                  "config": {"recipients": ["pas-un-email"]}})
    assert bad.status_code == 400
    bad = await client.post("/api/v1/notifications/channels", headers=h,
                            json={"name": "M", "channel_type": "sms", "config": {}})
    assert bad.status_code == 400

    ch = await client.post("/api/v1/notifications/channels", headers=h,
                           json={"name": "Astreinte", "channel_type": "email",
                                 "config": {"recipients": ["ops@example.com",
                                                           " noc@example.com "]}})
    assert ch.status_code == 201
    body = ch.json()
    assert body["channel_type"] == "email"
    assert body["config"]["recipients"] == ["ops@example.com", "noc@example.com"]
    id_channel = body["id_channel"]

    # Test du canal : send_email mocké (importé DANS email_send → patch à la source)
    sent: list[dict] = []
    def fake_send(*, to, subject, body, attachments=None):
        sent.append({"to": to, "subject": subject, "body": body})
    with patch("app.services.email_delivery.send_email", fake_send):
        tr = await client.post(f"/api/v1/notifications/channels/{id_channel}/test", headers=h)
        assert tr.status_code == 200 and tr.json()["ok"] is True
        assert sent[0]["to"] == ["ops@example.com", "noc@example.com"]
        assert sent[0]["subject"].startswith("[Vitrail]")

        # Dispatch réel : règle player_offline → email envoyé + journalisé
        rule = await client.post("/api/v1/notifications/rules", headers=h,
                                 json={"event_type": "player_offline",
                                       "id_channel": id_channel})
        assert rule.status_code == 201
        ok, detail = notif_svc.deliver_to_channel(
            "email", {"recipients": ["ops@example.com"], "locale": "fr"}, "player_offline",
            *notif_svc.build_message("player_offline",
                                     {"player_name": "NUC", "offline_min": 5}, locale="fr"),
            {})
        assert ok and "1 destinataire" in detail
        assert "NUC" in sent[-1]["subject"]
