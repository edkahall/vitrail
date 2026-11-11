"""
Tests §XXIV — Executive Dashboard (Analytics).

Couvre :
  - compute_health_score : fonction pure (pondérations, pénalité alertes, flotte vide)
  - GET /analytics/executive : KPIs flotte, conformité, top contenus, série rollup
  - gate reports:read : 403 sans la permission
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.analytics import CmsNetworkMetricsDaily
from app.models.media import CmsMedia
from app.models.notification import CmsNotificationLog
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
from app.services.analytics import compute_health_score, roi_estimate

# ── Fonction pure ────────────────────────────────────────────────────────────

def test_compute_health_score_perfect():
    # Tout online + frais + conforme, aucune alerte → 100.
    assert compute_health_score(
        total=10, online=10, fresh=10, conforming=10, open_alerts=0) == 100


def test_compute_health_score_all_down():
    assert compute_health_score(
        total=5, online=0, fresh=0, conforming=0, open_alerts=0) == 0


def test_compute_health_score_empty_fleet_is_none():
    assert compute_health_score(
        total=0, online=0, fresh=0, conforming=0, open_alerts=0) is None


def test_compute_health_score_alert_penalty_capped():
    # Flotte parfaite mais beaucoup d'alertes : pénalité plafonnée à 20 → 80.
    assert compute_health_score(
        total=4, online=4, fresh=4, conforming=4, open_alerts=99) == 80


def test_compute_health_score_weighted_partial():
    # online=1/2 (0.45), conforme=0/2 (0.30), frais=2/2 (0.25) → 100*(0.225+0+0.25)=47.5 → 48.
    assert compute_health_score(
        total=2, online=1, fresh=2, conforming=0, open_alerts=0) == 48


# ── Endpoint ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="AnalyticsOrg", org_slug=f"analytics-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


async def _make_user(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
    perms: list[tuple[str, str]],
) -> str:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org, email=f"a-{uid}@test.com",
        password_hash=hash_password("Password1!"), is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    role = CmsRole(id_org=org.id_org, role_key=f"CMS_A_{uid}", label="A", is_system=False)
    db_session.add(role)
    await db_session.flush()
    for module, operation in perms:
        await db_session.execute(
            pg_insert(CmsPermission)
            .values(module=module, operation=operation, label=f"{module}.{operation}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        )
        await db_session.flush()
        perm = (await db_session.execute(
            select(CmsPermission).where(
                CmsPermission.module == module, CmsPermission.operation == operation)
        )).scalar_one()
        db_session.add(CmsRolePermission(id_role=role.id_role, id_permission=perm.id_permission))
    await db_session.flush()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password1!"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_executive_dashboard(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    token = await _make_user(client, db_session, org, [("reports", "read")])
    headers = {"Authorization": f"Bearer {token}"}

    site = CmsSite(id_org=org.id_org, site_name="Store A")
    db_session.add(site)
    await db_session.flush()

    now = datetime.now(UTC)
    p_online = CmsPlayer(
        id_org=org.id_org, id_site=site.id_site, player_name="ON",
        status="online", last_seen_at=now)
    p_warning = CmsPlayer(
        id_org=org.id_org, id_site=site.id_site, player_name="WARN",
        status="warning", last_seen_at=now)
    p_offline = CmsPlayer(
        id_org=org.id_org, id_site=site.id_site, player_name="OFF", status="offline")
    media = CmsMedia(
        id_org=org.id_org, media_name="Spot A", media_type="video",
        storage_key=f"test/{uuid.uuid4().hex}.mp4", checksum_sha256=uuid.uuid4().hex,
        transcode_status="done")
    db_session.add_all([p_online, p_warning, p_offline, media])
    await db_session.flush()

    # 2 diffusions récentes (< 24h) par le player online → conforme.
    for i in range(2):
        db_session.add(CmsProofOfPlay(
            id_org=org.id_org, id_player=p_online.id_player, id_media=media.id_media,
            played_at=now - timedelta(hours=1), duration_ms=5000, merkle_hash=f"h{i}"))
    # 1 alerte récente.
    db_session.add(CmsNotificationLog(
        id_org=org.id_org, event_type="player_offline", status="sent"))
    # 1 point de rollup pour aujourd'hui.
    db_session.add(CmsNetworkMetricsDaily(
        id_org=org.id_org, metric_date=now.date(), players_total=3, players_online=1,
        players_warning=1, players_offline=1, conformity_pct=33.3, alerts_active=1,
        plays_count=2, play_seconds=10, health_score=42))
    await db_session.commit()

    resp = await client.get("/api/v1/analytics/executive", headers=headers)
    assert resp.status_code == 200, resp.text
    d = resp.json()

    assert d["fleet"] == {
        "total": 3, "online": 1, "warning": 1, "offline": 1, "maintenance": 0}
    assert d["plays_count"] == 2
    assert d["play_seconds"] == 10
    # conformité = 1 player diffusant / 3 total → 33.3
    assert d["conformity_pct"] == 33.3
    assert d["alerts_active"] == 1
    assert d["health_score"] is not None
    # top contenu = le spot diffusé
    assert any(t["id_media"] == media.id_media and t["plays"] == 2 for t in d["top_content"])
    # série = le point de rollup
    assert len(d["series"]) == 1
    assert d["series"][0]["health_score"] == 42
    assert d["series"][0]["online_pct"] == round(100 / 3, 1)
    # uptime = moyenne du online_pct de la série
    assert d["uptime_pct"] == round(100 / 3, 1)


async def test_executive_requires_reports_read(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    # Un compte avec seulement media:read n'accède pas au dashboard exécutif.
    token = await _make_user(client, db_session, org, [("media", "read")])
    resp = await client.get(
        "/api/v1/analytics/executive", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_specialized_dashboards(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    token = await _make_user(client, db_session, org, [("reports", "read")])
    h = {"Authorization": f"Bearer {token}"}

    site = CmsSite(id_org=org.id_org, site_name="Store NOC")
    db_session.add(site)
    await db_session.flush()
    p_on = CmsPlayer(id_org=org.id_org, id_site=site.id_site, player_name="P-on",
                     status="online", player_os="linux", player_version="2.1.0")
    p_off = CmsPlayer(id_org=org.id_org, id_site=site.id_site, player_name="P-off",
                      status="offline", player_os="android")
    media = CmsMedia(id_org=org.id_org, media_name="Clip", media_type="video",
                     storage_key=f"test/{uuid.uuid4().hex}.mp4",
                     checksum_sha256=uuid.uuid4().hex, transcode_status="done")
    db_session.add_all([p_on, p_off, media])
    await db_session.flush()
    now = datetime.now(UTC)
    for i in range(3):
        db_session.add(CmsProofOfPlay(
            id_org=org.id_org, id_player=p_on.id_player, id_media=media.id_media,
            played_at=now - timedelta(hours=2), duration_ms=4000, merkle_hash=f"h{i}"))
    await db_session.commit()

    # NOC : compte + lignes enrichies (site résolu)
    r = await client.get("/api/v1/analytics/noc", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 2 and d["online"] == 1 and d["offline"] == 1
    row = next(x for x in d["rows"] if x["player_name"] == "P-on")
    assert row["site_name"] == "Store NOC" and row["player_os"] == "linux"

    # Contenu : perf par média (plays + reach)
    r = await client.get("/api/v1/analytics/content", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total_plays"] == 3
    cr = next(x for x in d["rows"] if x["id_media"] == media.id_media)
    assert cr["plays"] == 3 and cr["players"] == 1 and cr["sites"] == 1


async def test_geo_and_audience(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    token = await _make_user(client, db_session, org, [("reports", "read")])
    h = {"Authorization": f"Bearer {token}"}

    placed = CmsSite(id_org=org.id_org, site_name="Lyon", latitude=45.764, longitude=4.8357)
    noloc = CmsSite(id_org=org.id_org, site_name="SansCoord")
    db_session.add_all([placed, noloc])
    await db_session.flush()
    player = CmsPlayer(id_org=org.id_org, id_site=placed.id_site, player_name="P", status="online")
    media = CmsMedia(id_org=org.id_org, media_name="Spot", media_type="video",
                     storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256=uuid.uuid4().hex,
                     transcode_status="done")
    db_session.add_all([player, media])
    await db_session.flush()
    now = datetime.now(UTC)
    for i in range(4):
        db_session.add(CmsProofOfPlay(
            id_org=org.id_org, id_player=player.id_player, id_media=media.id_media,
            played_at=now - timedelta(hours=1), duration_ms=900000, merkle_hash=f"g{i}"))
    await db_session.commit()

    # GEO : site placé + comptage non placés
    r = await client.get("/api/v1/analytics/geo", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["unplaced"] == 1
    s = next(x for x in d["sites"] if x["site_name"] == "Lyon")
    assert s["latitude"] == 45.764 and s["players_online"] == 1

    # AUDIENCE : impressions estimées (heures diffusées × contacts/h), 24 heures
    r = await client.get("/api/v1/analytics/audience", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["by_hour"]) == 24
    assert d["total_est_impressions"] > 0           # 4×15min = 1h × 50 = ~50
    assert any(row["site_name"] == "Lyon" for row in d["by_site"])


# ── ROI + branding + PDF + share links ───────────────────────────────────────

def test_roi_estimate():
    r = roi_estimate(
        screens=10, uptime_pct=100, days=30, hours_per_day=12,
        contacts_per_hour=50, cpm_eur=5, paper_eur_per_month=50)
    # impressions = 10 × 12 × 30 × 1.0 × 50 = 180000
    assert r["impressions"] == 180000
    # valeur réseau = 180000 / 1000 × 5 = 900
    assert r["network_value_eur"] == 900.0
    # économie papier = 10 × 50 × (30/30) = 500
    assert r["paper_savings_eur"] == 500.0
    assert r["total_roi_eur"] == 1400.0
    # uptime 50% divise les contacts par 2
    r2 = roi_estimate(screens=10, uptime_pct=50, days=30, hours_per_day=12,
                      contacts_per_hour=50, cpm_eur=5, paper_eur_per_month=50)
    assert r2["impressions"] == 90000


async def _admin_token(db_session: AsyncSession, org: CmsOrganization,
                       locale: str = "en") -> str:
    user = CmsUser(id_org=org.id_org, email=f"adm-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True, locale=locale)
    db_session.add(user)
    await db_session.flush()
    # CMS_SUPER_ADMIN : bypass RBAC (require_permission) + accepté par require_org_admin.
    # Les org-admin réels ont les permissions seedées (0002_iam), absentes en test.
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


async def test_branding_and_pdf(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    token = await _admin_token(db_session, org)
    h = {"Authorization": f"Bearer {token}"}

    # défaut créé à la volée
    r = await client.get("/api/v1/analytics/branding", headers=h)
    assert r.status_code == 200
    assert r.json()["primary_color"] == "#4875B4"
    assert r.json()["has_logo"] is False

    # mise à jour (couleur validée, titre, hypothèse ROI)
    r = await client.put("/api/v1/analytics/branding", headers=h,
                         json={"report_title": "Rapport Direction", "primary_color": "#2E7D32",
                               "roi_cpm_eur": 8})
    assert r.status_code == 200, r.text
    assert r.json()["report_title"] == "Rapport Direction"
    assert r.json()["primary_color"] == "#2E7D32"
    assert r.json()["roi_cpm_eur"] == 8.0

    # couleur invalide rejetée
    bad = await client.put("/api/v1/analytics/branding", headers=h,
                          json={"primary_color": "red"})
    assert bad.status_code == 422

    # PDF généré
    r = await client.get("/api/v1/analytics/executive/report.pdf", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_compute_next_run():
    from datetime import UTC
    from zoneinfo import ZoneInfo

    from app.services.report_render import compute_next_run
    tz = ZoneInfo("Europe/Paris")
    after = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)  # samedi 12h Paris
    # weekly lundi (dow=0) 7h → lundi 6 juil 07h Paris (= 05:00 UTC en été)
    w = compute_next_run(frequency="weekly", send_hour=7, send_dow=0, send_dom=1,
                         tz=tz, after=after).astimezone(tz)
    assert (w.month, w.day, w.hour, w.weekday()) == (7, 6, 7, 0)
    # monthly le 1er → 1 août (le 1 juil est passé)
    m = compute_next_run(frequency="monthly", send_hour=7, send_dow=1, send_dom=1,
                         tz=tz, after=after).astimezone(tz)
    assert (m.month, m.day, m.hour) == (8, 1, 7)


async def test_report_schedule_crud(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    token = await _admin_token(db_session, org)
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post("/api/v1/analytics/report-schedules", headers=h,
                         json={"label": "Direction", "frequency": "monthly", "send_dom": 1,
                               "send_hour": 7, "period_days": 30,
                               "recipients": ["dir@example.com"]})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["frequency"] == "monthly" and s["next_run_at"] is not None
    sid = s["id_schedule"]

    assert len((await client.get("/api/v1/analytics/report-schedules", headers=h)).json()) == 1

    # send-now : SMTP absent en test → 502 + history 'failed'
    r = await client.post(f"/api/v1/analytics/report-schedules/{sid}/send-now", headers=h)
    assert r.status_code == 502
    hist = (await client.get("/api/v1/analytics/report-history", headers=h)).json()
    assert hist and hist[0]["status"] == "failed"

    assert (await client.delete(
        f"/api/v1/analytics/report-schedules/{sid}", headers=h)).status_code == 204


async def test_share_link_lifecycle(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    token = await _admin_token(db_session, org)
    h = {"Authorization": f"Bearer {token}"}

    # création
    r = await client.post("/api/v1/analytics/shares", headers=h,
                         json={"label": "Direction", "period_days": 30})
    assert r.status_code == 201, r.text
    share = r.json()
    stok = share["token"]
    assert share["url"] == f"/share/{stok}"
    id_share = share["id_share"]

    # vue publique SANS auth
    r = await client.get(f"/api/v1/public/share/{stok}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_name"] == org.org_name
    assert "dashboard" in body and "roi" in body

    # l'accès a été journalisé
    r = await client.get("/api/v1/analytics/shares", headers=h)
    assert r.json()[0]["access_count"] >= 1

    # révocation → vue publique 404
    r = await client.delete(f"/api/v1/analytics/shares/{id_share}", headers=h)
    assert r.status_code == 200 and r.json()["revoked"] is True
    r = await client.get(f"/api/v1/public/share/{stok}")
    assert r.status_code == 404

    # token bidon → 404
    r = await client.get("/api/v1/public/share/inexistant")
    assert r.status_code == 404


async def test_share_link_password_and_ip(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Options avancées §24.4 : mot de passe (401/403/200) + IP allowlist (404 fail-closed)."""
    token = await _admin_token(db_session, org)
    h = {"Authorization": f"Bearer {token}"}

    # ── mot de passe ──
    r = await client.post("/api/v1/analytics/shares", headers=h,
                          json={"label": "Protégé", "password": "s3cret!"})
    assert r.status_code == 201, r.text
    share = r.json()
    assert share["has_password"] is True
    stok = share["token"]

    r = await client.get(f"/api/v1/public/share/{stok}")
    assert r.status_code == 401 and r.json()["detail"] == "password_required"
    r = await client.get(f"/api/v1/public/share/{stok}",
                         headers={"X-Share-Password": "faux"})
    assert r.status_code == 403 and r.json()["detail"] == "invalid_password"
    r = await client.get(f"/api/v1/public/share/{stok}",
                         headers={"X-Share-Password": "s3cret!"})
    assert r.status_code == 200, r.text

    # ── allowlist IP (X-Forwarded-For = 1re valeur, comme derrière Caddy) ──
    r = await client.post("/api/v1/analytics/shares", headers=h,
                          json={"label": "IP", "ip_allowlist": ["81.2.3.4", "10.0.0.0/8"]})
    assert r.status_code == 201, r.text
    assert r.json()["ip_allowlist"] == ["81.2.3.4/32", "10.0.0.0/8"]
    stok2 = r.json()["token"]

    r = await client.get(f"/api/v1/public/share/{stok2}",
                         headers={"X-Forwarded-For": "9.9.9.9"})
    assert r.status_code == 404  # hors allowlist → ne révèle pas l'existence du lien
    r = await client.get(f"/api/v1/public/share/{stok2}",
                         headers={"X-Forwarded-For": "10.1.2.3"})
    assert r.status_code == 200
    r = await client.get(f"/api/v1/public/share/{stok2}",
                         headers={"X-Forwarded-For": "81.2.3.4, 172.19.0.1"})
    assert r.status_code == 200  # 1re valeur = client réel

    # CIDR invalide → 422
    r = await client.post("/api/v1/analytics/shares", headers=h,
                          json={"ip_allowlist": ["pas-une-ip"]})
    assert r.status_code == 422


async def test_bi_keys_and_exports(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Connecteurs BI §24.7 : cycle de vie clé sfr_ + les 3 flux CSV + les deux modes d'auth."""
    token = await _admin_token(db_session, org)
    h = {"Authorization": f"Bearer {token}"}

    # création — la valeur en clair n'est renvoyée qu'à la création
    r = await client.post("/api/v1/analytics/bi-keys", headers=h, json={"label": "Power BI"})
    assert r.status_code == 201, r.text
    key = r.json()
    raw = key["key"]
    assert raw and raw.startswith("sfr_")
    r = await client.get("/api/v1/analytics/bi-keys", headers=h)
    assert len(r.json()) == 1 and r.json()[0]["key"] is None

    # export par clé API en query param (mode connecteur BI web)
    r = await client.get(f"/api/v1/analytics/export/pop.csv?api_key={raw}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("played_at_utc,")
    for feed in ("network-metrics", "content"):
        r = await client.get(f"/api/v1/analytics/export/{feed}.csv?api_key={raw}")
        assert r.status_code == 200, (feed, r.text)

    # export par clé API en Bearer + par JWT reports:read
    r = await client.get("/api/v1/analytics/export/pop.csv",
                         headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    r = await client.get("/api/v1/analytics/export/pop.csv", headers=h)
    assert r.status_code == 200

    # sans auth / clé bidon → 401 ; période > 366 j → 400
    assert (await client.get("/api/v1/analytics/export/pop.csv")).status_code == 401
    assert (await client.get(
        "/api/v1/analytics/export/pop.csv?api_key=sfr_bidon")).status_code == 401
    r = await client.get(
        f"/api/v1/analytics/export/pop.csv?api_key={raw}&from=2020-01-01&until=2026-01-01")
    assert r.status_code == 400

    # révocation → la clé ne passe plus
    r = await client.delete(f"/api/v1/analytics/bi-keys/{key['id_key']}", headers=h)
    assert r.status_code == 204
    assert (await client.get(
        f"/api/v1/analytics/export/pop.csv?api_key={raw}")).status_code == 401


# ── Langue du rapport exécutif (profil du destinataire, défaut anglais) ────────

_EXEC_PAYLOAD = {
    "from_date": "2026-10-01", "until_date": "2026-10-31", "health_score": 90,
    "uptime_pct": 99.5, "conformity_pct": 97.0, "alerts_active": 1,
    "plays_count": 12345, "play_seconds": 7200,
    "fleet": {"online": 3, "warning": 1, "offline": 0, "maintenance": 0, "total": 4},
    "top_content": [{"media_name": "Promo", "id_media": 1, "plays": 4321, "seconds": 600}],
}
_EXEC_ROI = {"impressions": 90000, "network_value_eur": 1450.0, "paper_savings_eur": 600.0,
             "total_roi_eur": 2050.0, "screens": 4, "hours_per_day": 12.5,
             "contacts_per_hour": 50.0, "cpm_eur": 5.0, "paper_eur_per_month": 50.0,
             "uptime_pct": 99.5}


def _exec_pdf(monkeypatch, **kw) -> bytes:
    from reportlab import rl_config

    from app.services.report_pdf import build_executive_pdf
    monkeypatch.setattr(rl_config, "pageCompression", 0)
    return build_executive_pdf(org_name="Org", payload=_EXEC_PAYLOAD, branding={},
                               roi=_EXEC_ROI, **kw)


def test_executive_pdf_default_english(monkeypatch) -> None:
    en = _exec_pdf(monkeypatch)
    assert b"Key indicators" in en and b"Fleet status" in en and b"Executive report" in en
    assert b"12,345" in en and b"2026-10-01" in en
    assert b"Indicateurs cl" not in en


def test_executive_pdf_german_and_french(monkeypatch) -> None:
    de = _exec_pdf(monkeypatch, locale="de")
    assert b"Kennzahlen" in de and b"Flottenstatus" in de and b"Management-Bericht" in de
    assert b"12.345" in de and b"01.10.2026" in de
    assert b"Key indicators" not in de
    fr = _exec_pdf(monkeypatch, locale="fr-FR")
    assert b"Indicateurs cl" in fr and b"Statut de la flotte" in fr and b"01/10/2026" in fr


def test_executive_email_texts_follow_locale() -> None:
    from datetime import date

    from app.services.report_render import executive_email, pdf_filename
    f, u = date(2026, 10, 1), date(2026, 10, 31)
    subj, body, fname = executive_email("Org", f, u, None)
    assert "Executive report" in subj and body.startswith("Hello,")
    assert fname == "executive_report_2026-10-01_2026-10-31.pdf"
    subj, body, fname = executive_email("Org", f, u, "fr")
    assert "Rapport exécutif" in subj and body.startswith("Bonjour,") and "01/10/2026" in body
    assert fname == "rapport_executif_2026-10-01_2026-10-31.pdf"
    subj, body, _ = executive_email("Org", f, u, "de", test=True)
    assert "Management-Bericht" in subj and "01.10.2026" in body
    for loc in ("en", "fr", "es", "pt", "de"):
        assert pdf_filename(f, u, loc).isascii()


async def test_executive_pdf_download_in_requester_language(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
) -> None:
    for loc, prefix in (("en", "executive_report_"), ("de", "management_bericht_"),
                        ("fr", "rapport_executif_")):
        token = await _admin_token(db_session, org, locale=loc)
        r = await client.get("/api/v1/analytics/executive/report.pdf",
                             headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert f'filename="{prefix}' in r.headers["content-disposition"], loc


async def test_scheduled_report_uses_creator_language(
    db_session: AsyncSession, org: CmsOrganization, monkeypatch
) -> None:
    from zoneinfo import ZoneInfo

    from app.models.report import CmsReportSchedule
    from app.services import email_delivery, report_render
    from app.workers import report_worker

    creator = CmsUser(id_org=org.id_org, email=f"cr-{uuid.uuid4().hex[:8]}@t.com",
                      password_hash=hash_password("Password1!"), is_active=True, locale="de")
    db_session.add(creator)
    await db_session.flush()

    seen: dict = {}
    real_render = report_render.render_executive_pdf

    async def _render(*a, **kw):
        seen["render_locale"] = kw.get("locale")
        return await real_render(*a, **kw)

    def _send(*, to, subject, body, attachments):
        seen.update(subject=subject, body=body, fname=attachments[0][0])

    monkeypatch.setattr(report_render, "render_executive_pdf", _render)
    monkeypatch.setattr(email_delivery, "send_email", _send)

    tz = ZoneInfo("UTC")
    sched = CmsReportSchedule(id_org=org.id_org, recipients=["dir@example.com"],
                              period_days=7, frequency="weekly", send_hour=7, send_dow=0,
                              send_dom=1, include_roi=True, created_by=creator.id_user)
    db_session.add(sched)
    await db_session.flush()
    assert await report_worker._deliver(db_session, sched, tz) == "sent"
    assert seen["render_locale"] == "de"
    assert "Management-Bericht" in seen["subject"] and seen["body"].startswith("Guten Tag")
    assert seen["fname"].startswith("management_bericht_")

    # créateur inconnu (compte supprimé → created_by NULL) → anglais
    sched.created_by = None
    assert await report_worker._deliver(db_session, sched, tz) == "sent"
    assert seen["render_locale"] is None
    assert "Executive report" in seen["subject"]
    assert seen["fname"].startswith("executive_report_")
