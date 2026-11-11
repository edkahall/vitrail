"""Langue des alertes (§XXII) et de l'e-mail de modération (§XVIII) — 2026-11-11.

Règle : un message est rédigé dans la langue du profil de la personne à qui il est
destiné. Canal de notification → langue de son créateur, posée dans `config["locale"]` à
la création (canal antérieur sans langue → anglais). E-mail de modération → langue du
profil de CHAQUE modérateur. Repli : anglais.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.notification import CmsAlertRule, CmsNotificationChannel, CmsNotificationLog
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import governance_moderation, notifications
from app.services.doc_i18n import SUPPORTED_LOCALES


# ── Textes ─────────────────────────────────────────────────────────────────────────
def test_doc_texts_parity() -> None:
    for table in (notifications.DOC_TEXTS, governance_moderation.DOC_TEXTS):
        assert set(table) == set(SUPPORTED_LOCALES)
        for loc in SUPPORTED_LOCALES:
            assert set(table[loc]) == set(table["en"]), loc


def test_build_message_defaults_to_english_and_follows_locale() -> None:
    ctx = {"player_name": "NUC", "id_player": 1, "offline_min": 12}
    en_subj, en_body = notifications.build_message("player_offline", ctx)
    assert en_subj == "⚠️ Player offline: NUC"
    assert "has been offline for 12 min" in en_body
    # Témoin : la langue change bien le texte.
    fr_subj, fr_body = notifications.build_message("player_offline", ctx, locale="fr")
    assert fr_subj == "⚠️ Player hors ligne : NUC"
    assert "hors ligne depuis 12 min" in fr_body
    _, de_body = notifications.build_message("player_offline", ctx, locale="de")
    assert "NUC" in de_body and de_body != en_body and de_body != fr_body
    # Langue inconnue → anglais.
    assert notifications.build_message("player_offline", ctx, locale="xx")[0] == en_subj


def test_approval_message_each_locale_differs() -> None:
    ctx = {"resource_name": "Rentrée", "resource_type": "campaign", "resource_id": 3,
           "actor_name": "Ana", "comment": "ok", "due_date": "2026-12-01"}
    seen = {loc: notifications.build_message("approval_rejected", ctx, locale=loc)
            for loc in SUPPORTED_LOCALES}
    assert len({s for s, _ in seen.values()}) == len(SUPPORTED_LOCALES)
    assert all("Rentrée" in s and "Ana" in b and "ok" in b for s, b in seen.values())


def test_event_type_label_localized() -> None:
    assert notifications.EVENT_TYPES["player_offline"] == "Player offline (critical)"
    assert notifications.event_type_label("player_offline") == "Player offline (critical)"
    assert notifications.event_type_label("player_offline", "fr") == \
        "Player hors ligne (critique)"


def test_delivery_detail_in_channel_locale() -> None:
    ok, detail = notifications.deliver_to_channel("webhook", {"locale": "fr"}, "test",
                                                  "s", "b", {})
    assert not ok and detail == "url manquante"
    ok, detail = notifications.deliver_to_channel("webhook", {}, "test", "s", "b", {})
    assert not ok and detail == "missing url"


# ── Dispatch : message construit PAR CANAL ─────────────────────────────────────────
class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.inserts: list[tuple] = []

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("INSERT"):
            self.inserts.append(params)

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def test_dispatch_sync_builds_message_per_channel(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []

    def fake_deliver(ctype, config, event_type, subject, body, ctx, id_org=None,
                     id_rule=None):
        sent.append((config.get("locale"), subject))
        return True, "ok"

    monkeypatch.setattr(notifications, "deliver_to_channel", fake_deliver)
    conn = _FakeConn([
        (1, "webhook", {"url": "https://example.com/a", "locale": "fr"}),
        (2, "webhook", {"url": "https://example.com/b"}),               # canal ancien
        (3, "email", {"recipients": ["a@b.c"], "locale": "de"}),
    ])
    n = notifications.dispatch_sync(conn, 1, "player_offline",
                                    {"player_name": "NUC", "offline_min": 3})
    assert n == 3
    assert sent[0] == ("fr", "⚠️ Player hors ligne : NUC")
    assert sent[1] == (None, "⚠️ Player offline: NUC")
    assert sent[2][1] == notifications.build_message(
        "player_offline", {"player_name": "NUC"}, locale="de")[0]
    # Le journal garde le texte réellement envoyé, canal par canal.
    assert [p[3] for p in conn.cur.inserts] == [s for _, s in sent]


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="NotifI18n", org_slug=f"n18-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest.mark.asyncio
async def test_dispatch_async_logs_text_per_channel_locale(
        db_session: AsyncSession, org: CmsOrganization, monkeypatch) -> None:
    for name, cfg in (("fr", {"url": "https://example.com/fr", "locale": "fr"}),
                      ("old", {"url": "https://example.com/old"})):
        ch = CmsNotificationChannel(id_org=org.id_org, name=name, channel_type="webhook",
                                    config=cfg, enabled=True)
        db_session.add(ch)
        await db_session.flush()
        db_session.add(CmsAlertRule(id_org=org.id_org, event_type="feed_error",
                                    id_channel=ch.id_channel, enabled=True))
    await db_session.commit()

    payloads: list[dict] = []

    def fake_post(url, payload, secret="", event_type="", attempt=1):
        payloads.append(payload)
        return True, "HTTP 200", False

    monkeypatch.setattr(notifications, "webhook_post", fake_post)
    n = await notifications.dispatch_async(db_session, org.id_org, "feed_error",
                                           {"feed_name": "BFM", "id_feed": 7, "error": "x"})
    assert n == 2
    subjects = sorted(p["subject"] for p in payloads)
    assert subjects == ["⚠️ Feed en erreur : BFM", "⚠️ Feed error: BFM"]
    logs = (await db_session.execute(select(CmsNotificationLog).where(
        CmsNotificationLog.id_org == org.id_org))).scalars().all()
    assert sorted(lg.subject for lg in logs) == subjects


# ── Endpoints : la langue du créateur est posée sur le canal ───────────────────────
async def _admin(db_session: AsyncSession, org: CmsOrganization, locale: str) -> str:
    user = CmsUser(id_org=org.id_org, email=f"adm-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True, locale=locale)
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
        user.id_user, extra={"org": org.id_org, "roles": ["CMS_SUPER_ADMIN"]})


@pytest.mark.asyncio
async def test_channel_locale_from_creator_profile(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization) -> None:
    h = {"Authorization": f"Bearer {await _admin(db_session, org, 'de')}"}

    r = await client.post("/api/v1/notifications/channels", headers=h,
                          json={"name": "M", "channel_type": "email",
                                "config": {"recipients": ["ops@example.com"]}})
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["config"]["locale"] == "de"          # langue du profil du créateur, rendue
    id_channel = ch["id_channel"]

    # Mise à jour sans langue → conservée.
    r = await client.put(f"/api/v1/notifications/channels/{id_channel}", headers=h,
                         json={"config": {"recipients": ["noc@example.com"]}})
    assert r.status_code == 200 and r.json()["config"]["locale"] == "de"
    # Langue explicite → appliquée.
    r = await client.put(f"/api/v1/notifications/channels/{id_channel}", headers=h,
                         json={"config": {"recipients": ["noc@example.com"],
                                          "locale": "pt"}})
    assert r.json()["config"] == {"recipients": ["noc@example.com"], "locale": "pt"}

    # Création avec langue explicite → elle prime sur le profil.
    r = await client.post("/api/v1/notifications/channels", headers=h,
                          json={"name": "E", "channel_type": "email",
                                "config": {"recipients": ["a@example.com"],
                                           "locale": "es"}})
    assert r.json()["config"]["locale"] == "es"

    # Libellés des types d'événements dans la langue du demandeur.
    ets = (await client.get("/api/v1/notifications/event-types", headers=h)).json()
    label = next(e["label"] for e in ets["event_types"] if e["key"] == "player_offline")
    assert label == notifications.event_type_label("player_offline", "de")
    assert label != notifications.EVENT_TYPES["player_offline"]


@pytest.mark.asyncio
async def test_legacy_channel_without_locale_stays_english(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        monkeypatch) -> None:
    h = {"Authorization": f"Bearer {await _admin(db_session, org, 'fr')}"}
    ch = CmsNotificationChannel(id_org=org.id_org, name="old", channel_type="email",
                                config={"recipients": ["ops@example.com"]}, enabled=True)
    db_session.add(ch)
    await db_session.commit()

    sent: list[dict] = []

    def fake_send(*, to, subject, body, attachments=None):
        sent.append({"subject": subject})

    monkeypatch.setattr("app.services.email_delivery.send_email", fake_send)
    # La mise à jour d'un canal ancien ne lui invente pas la langue de l'éditeur.
    r = await client.put(f"/api/v1/notifications/channels/{ch.id_channel}", headers=h,
                         json={"config": {"recipients": ["ops@example.com"]}})
    assert "locale" not in r.json()["config"]
    r = await client.post(f"/api/v1/notifications/channels/{ch.id_channel}/test", headers=h)
    assert r.json()["ok"] is True
    assert sent[-1]["subject"] == "[Vitrail] 🔔 Vitrail alert (test)"


# ── E-mail de modération : langue de CHAQUE modérateur ─────────────────────────────
@pytest.mark.asyncio
async def test_moderation_email_in_each_moderator_locale(
        db_session: AsyncSession, org: CmsOrganization, monkeypatch) -> None:
    users = []
    for loc in ("fr", "de", "en", "fr"):
        u = CmsUser(id_org=org.id_org, email=f"m-{loc}-{uuid.uuid4().hex[:6]}@t.com",
                    password_hash=hash_password("Password1!"), is_active=True, locale=loc)
        db_session.add(u)
        users.append(u)
    await db_session.commit()

    async def fake_moderators(db, id_node):
        return [u.id_user for u in users]

    sent: list[dict] = []

    def fake_send(*, to, subject, body, attachments=None):
        sent.append({"to": sorted(to), "subject": subject, "body": body})

    monkeypatch.setattr(governance_moderation, "node_moderator_user_ids", fake_moderators)
    monkeypatch.setattr(governance_moderation, "send_email", fake_send)
    pl = SimpleNamespace(id_governance_node=1, playlist_name="Noël")
    submitter = SimpleNamespace(id_user=-1, full_name="Léa", email="lea@t.com",
                                locale="es")       # la langue du soumetteur n'entre pas en jeu
    await governance_moderation.notify_moderators_submitted(db_session, pl, submitter)

    by_subject = {s["subject"]: s for s in sent}
    assert len(sent) == 3                            # un envoi par langue
    fr = by_subject["Modération — la playlist « Noël » attend votre validation"]
    assert fr["to"] == sorted([users[0].email, users[3].email])
    assert fr["body"].startswith("Bonjour,\n\nLéa a soumis")
    en = by_subject["Moderation — playlist “Noël” awaits your approval"]
    assert en["to"] == [users[2].email]
    de = by_subject["Moderation — die Playlist „Noël“ wartet auf Ihre Freigabe"]
    assert de["to"] == [users[1].email]
