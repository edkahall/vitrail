"""Tests Retail Media S1 (§2.8, migr 0074) — annonceurs & campagnes CRUD.

- annonceurs : create/list (campaign_count) ; delete refusé (409) si campagne non terminée.
- campagnes : create avec cibles canoniques, update (remplacement des cibles),
  transitions de statut (publish exige créatif + cible ; pause/resume ; 409 invalide),
  statut effectif calculé par dates (approved → live/scheduled).
- créatifs : ajout (média org + type visuel ; image sans durée = 422), rotation pondérée.
- isolation : campagne d'une autre org = 404.
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
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="CampOrg", org_slug=f"camp-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


@pytest_asyncio.fixture
async def org_admin(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    """Utilisateur avec un rôle campagne complet (permissions campaigns:* explicites —
    la base de test n'a pas le seed 0002)."""
    from app.models.role import CmsPermission, CmsRolePermission
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org, email=f"adm-{uid}@camp.com",
        password_hash=hash_password("Password1!"), is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    role = CmsRole(id_org=org.id_org, role_key=f"CMS_CAMP_{uid}", label="Camp", is_system=False)
    db_session.add(role)
    await db_session.flush()
    for op in ("create", "read", "edit", "delete", "publish", "report"):
        await db_session.execute(
            pg_insert(CmsPermission)
            .values(module="campaigns", operation=op, label=f"campaigns.{op}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        )
        await db_session.flush()
        perm = (await db_session.execute(
            select(CmsPermission).where(
                CmsPermission.module == "campaigns", CmsPermission.operation == op)
        )).scalar_one()
        db_session.add(CmsRolePermission(id_role=role.id_role, id_permission=perm.id_permission))
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return user


@pytest_asyncio.fixture
def headers(org_admin: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(org_admin.id_user)}"}


@pytest_asyncio.fixture
async def media(db_session: AsyncSession, org: CmsOrganization) -> CmsMedia:
    m = CmsMedia(id_org=org.id_org, media_name="spot-30s", media_type="video",
                 storage_key="k", checksum_sha256="c" * 64, duration_ms=30_000)
    db_session.add(m)
    await db_session.commit()
    return m


async def _advertiser(client: AsyncClient, headers: dict, name: str = "Acme") -> dict:
    r = await client.post("/api/v1/advertisers/", headers=headers,
                          json={"advertiser_name": name, "contact_email": "buy@acme.com"})
    assert r.status_code == 201, r.text
    return r.json()


async def _campaign(client: AsyncClient, headers: dict, id_advertiser: int, **over) -> dict:
    body = {
        "id_advertiser": id_advertiser,
        "campaign_name": "Été Acme",
        "starts_on": date.today().isoformat(),
        "ends_on": (date.today() + timedelta(days=30)).isoformat(),
        "sov_percent": 10,
        "pricing_model": "sov",
        "price_amount": 1500,
        "targets": [{"target_type": "org"}],
    }
    body.update(over)
    r = await client.post("/api/v1/campaigns/", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_advertiser_crud_and_guarded_delete(client: AsyncClient, headers, org_admin):
    adv = await _advertiser(client, headers)
    camp = await _campaign(client, headers, adv["id_advertiser"])

    listed = (await client.get("/api/v1/advertisers/", headers=headers)).json()
    me = next(a for a in listed if a["id_advertiser"] == adv["id_advertiser"])
    assert me["campaign_count"] == 1

    # delete refusé tant qu'une campagne n'est pas terminée
    r = await client.delete(f"/api/v1/advertisers/{adv['id_advertiser']}", headers=headers)
    assert r.status_code == 409

    r = await client.post(f"/api/v1/campaigns/{camp['id_campaign']}/status",
                          headers=headers, json={"action": "cancel"})
    assert r.status_code == 200
    r = await client.delete(f"/api/v1/advertisers/{adv['id_advertiser']}", headers=headers)
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_campaign_create_update_targets(client: AsyncClient, headers, org_admin):
    adv = await _advertiser(client, headers, "Brandz")
    camp = await _campaign(client, headers, adv["id_advertiser"],
                           daily_hours={"start": "09:00", "end": "20:00"})
    assert camp["status"] == "draft"
    assert camp["advertiser_name"] == "Brandz"
    assert camp["targets"][0]["target_type"] == "org"
    assert camp["daily_hours"] == {"start": "09:00", "end": "20:00"}

    # update : remplacement des cibles + SOV
    r = await client.put(f"/api/v1/campaigns/{camp['id_campaign']}", headers=headers,
                         json={"sov_percent": 25,
                               "targets": [{"target_type": "tags",
                                            "tag_expr": {"zone": ["entree"]}}]})
    assert r.status_code == 200, r.text
    up = r.json()
    assert float(up["sov_percent"]) == 25
    assert up["targets"][0]["target_type"] == "tags"
    assert up["targets"][0]["tag_expr"] == {"zone": ["entree"]}

    # cible player sans target_id = 422 (validation schéma)
    r = await client.put(f"/api/v1/campaigns/{camp['id_campaign']}", headers=headers,
                         json={"targets": [{"target_type": "player"}]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_campaign_target_player_count(client: AsyncClient, headers, org_admin,
                                            db_session, org):
    """Pastille UI : `target_player_count` = players ciblés RÉSOLUS (union des cibles,
    players opt-out `campaigns_optout` déduits — même résolution que la diffusion)."""
    from app.models.player import CmsPlayer
    p1 = CmsPlayer(id_org=org.id_org, player_name="P-count-1", status="online")
    p2 = CmsPlayer(id_org=org.id_org, player_name="P-count-2", status="online",
                   campaigns_optout=True)
    db_session.add_all([p1, p2])
    await db_session.commit()
    adv = await _advertiser(client, headers, "CountCo")

    # cible org entière → tous les players éligibles de l'org (opt-out exclu)
    camp = await _campaign(client, headers, adv["id_advertiser"], campaign_name="Count-org")
    assert camp["target_player_count"] == 1

    # cible player explicite opt-out → 0 résolu
    r = await client.put(f"/api/v1/campaigns/{camp['id_campaign']}", headers=headers,
                         json={"targets": [{"target_type": "player",
                                            "target_id": p2.id_player}]})
    assert r.status_code == 200
    assert r.json()["target_player_count"] == 0

    # union player + org → dédupliqué, toujours 1
    r = await client.put(f"/api/v1/campaigns/{camp['id_campaign']}", headers=headers,
                         json={"targets": [{"target_type": "player",
                                            "target_id": p1.id_player},
                                           {"target_type": "org"}]})
    assert r.status_code == 200
    assert r.json()["target_player_count"] == 1
    listed = (await client.get("/api/v1/campaigns/", headers=headers)).json()
    me = next(c for c in listed if c["id_campaign"] == camp["id_campaign"])
    assert me["target_player_count"] == 1


@pytest.mark.asyncio
async def test_campaign_status_flow(client: AsyncClient, headers, org_admin, media):
    adv = await _advertiser(client, headers, "FlowCo")
    camp = await _campaign(client, headers, adv["id_advertiser"])
    cid = camp["id_campaign"]

    # publish sans créatif → 422
    r = await client.post(f"/api/v1/campaigns/{cid}/status", headers=headers,
                          json={"action": "publish"})
    assert r.status_code == 422

    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=headers,
                          json={"id_media": media.id_media})
    assert r.status_code == 201, r.text
    assert r.json()["media_name"] == "spot-30s"

    # publish OK → statut effectif live (aujourd'hui dans la fenêtre)
    r = await client.post(f"/api/v1/campaigns/{cid}/status", headers=headers,
                          json={"action": "publish"})
    assert r.status_code == 200
    assert r.json()["status"] == "live"

    # pause / resume
    r = await client.post(f"/api/v1/campaigns/{cid}/status", headers=headers,
                          json={"action": "pause"})
    assert r.json()["status"] == "paused"
    r = await client.post(f"/api/v1/campaigns/{cid}/status", headers=headers,
                          json={"action": "resume"})
    assert r.json()["status"] == "live"

    # transition invalide : publish depuis approved → 409
    r = await client.post(f"/api/v1/campaigns/{cid}/status", headers=headers,
                          json={"action": "publish"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_campaign_effective_status_scheduled(client: AsyncClient, headers,
                                                   org_admin, media):
    """Une campagne approuvée qui démarre demain = scheduled (pas live)."""
    adv = await _advertiser(client, headers, "Futur")
    camp = await _campaign(
        client, headers, adv["id_advertiser"],
        starts_on=(date.today() + timedelta(days=1)).isoformat(),
        ends_on=(date.today() + timedelta(days=10)).isoformat(),
    )
    cid = camp["id_campaign"]
    await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=headers,
                      json={"id_media": media.id_media})
    r = await client.post(f"/api/v1/campaigns/{cid}/status", headers=headers,
                          json={"action": "publish"})
    assert r.json()["status"] == "scheduled"


@pytest.mark.asyncio
async def test_creative_validation(client: AsyncClient, headers, org_admin,
                                   db_session, org):
    adv = await _advertiser(client, headers, "Crea")
    camp = await _campaign(client, headers, adv["id_advertiser"])
    cid = camp["id_campaign"]

    # média d'un type non diffusable → 422
    feed = CmsMedia(id_org=org.id_org, media_name="flux", media_type="feed_display",
                    storage_key="", checksum_sha256="f" * 64)
    db_session.add(feed)
    # image sans durée native ni duration_ms → 422
    img = CmsMedia(id_org=org.id_org, media_name="visuel", media_type="image",
                   storage_key="i", checksum_sha256="i" * 64)
    db_session.add(img)
    await db_session.commit()

    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=headers,
                          json={"id_media": feed.id_media})
    assert r.status_code == 422
    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=headers,
                          json={"id_media": img.id_media})
    assert r.status_code == 422
    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=headers,
                          json={"id_media": img.id_media, "duration_ms": 15000, "weight": 3})
    assert r.status_code == 201
    assert r.json()["weight"] == 3


@pytest.mark.asyncio
async def test_occupancy_check_and_overbook_block(client: AsyncClient, headers,
                                                  org_admin, media, db_session, org):
    """S2 : occupation SOV — check (current/projected, warning 80/blocage 100),
    publish refusé (409) en surbooking, accepté avec overbook_override."""
    from app.models.player import CmsPlayer
    player = CmsPlayer(id_org=org.id_org, player_name="P-inv", status="online")
    db_session.add(player)
    await db_session.commit()
    adv = await _advertiser(client, headers, "InvCo")
    tgt = [{"target_type": "player", "target_id": player.id_player}]

    # Campagne A 60 % approuvée sur le player
    a = await _campaign(client, headers, adv["id_advertiser"], sov_percent=60,
                        campaign_name="A-60", targets=tgt)
    await client.post(f"/api/v1/campaigns/{a['id_campaign']}/creatives", headers=headers,
                      json={"id_media": media.id_media})
    r = await client.post(f"/api/v1/campaigns/{a['id_campaign']}/status", headers=headers,
                          json={"action": "publish"})
    assert r.status_code == 200

    # check candidate 30 % → 90 % projeté = warning, pas bloqué
    chk = (await client.post("/api/v1/campaigns/occupancy-check", headers=headers, json={
        "starts_on": date.today().isoformat(),
        "ends_on": (date.today() + timedelta(days=30)).isoformat(),
        "sov_percent": 30, "targets": tgt,
    })).json()
    assert chk["warning"] is True and chk["blocked"] is False
    assert chk["players"][0]["current_pct"] == 60 and chk["players"][0]["projected_pct"] == 90
    assert chk["players"][0]["player_name"] == "P-inv"

    # check candidate 50 % → 110 % = bloqué
    chk = (await client.post("/api/v1/campaigns/occupancy-check", headers=headers, json={
        "starts_on": date.today().isoformat(),
        "ends_on": (date.today() + timedelta(days=30)).isoformat(),
        "sov_percent": 50, "targets": tgt,
    })).json()
    assert chk["blocked"] is True

    # Campagne B 50 % : publish → 409 surbooking ; avec override → 200
    b = await _campaign(client, headers, adv["id_advertiser"], sov_percent=50,
                        campaign_name="B-50", targets=tgt)
    await client.post(f"/api/v1/campaigns/{b['id_campaign']}/creatives", headers=headers,
                      json={"id_media": media.id_media})
    r = await client.post(f"/api/v1/campaigns/{b['id_campaign']}/status", headers=headers,
                          json={"action": "publish"})
    assert r.status_code == 409 and "Overbooking" in r.json()["detail"]
    r = await client.put(f"/api/v1/campaigns/{b['id_campaign']}", headers=headers,
                         json={"overbook_override": True})
    assert r.status_code == 200
    r = await client.post(f"/api/v1/campaigns/{b['id_campaign']}/status", headers=headers,
                          json={"action": "publish"})
    assert r.status_code == 200

    # inventaire : le player affiche 110 % vendus
    inv = (await client.get("/api/v1/campaigns/inventory", headers=headers)).json()
    row = next(x for x in inv if x["id_player"] == player.id_player)
    assert row["occupied_pct"] == 110

    # une campagne PAUSÉE libère l'inventaire
    await client.post(f"/api/v1/campaigns/{b['id_campaign']}/status", headers=headers,
                      json={"action": "pause"})
    inv = (await client.get("/api/v1/campaigns/inventory", headers=headers)).json()
    row = next(x for x in inv if x["id_player"] == player.id_player)
    assert row["occupied_pct"] == 60


@pytest.mark.asyncio
async def test_occupancy_daily_calendar(client: AsyncClient, headers, org_admin,
                                        media, db_session, org):
    """S6 : charge d'inventaire par JOUR (vue calendrier) — max sur les players de
    Σ SOV des campagnes approuvées actives ce jour-là ; fenêtre bornée 92 j."""
    from app.models.player import CmsPlayer
    player = CmsPlayer(id_org=org.id_org, player_name="P-cal", status="online")
    db_session.add(player)
    await db_session.commit()
    adv = await _advertiser(client, headers, "CalCo")
    tgt = [{"target_type": "player", "target_id": player.id_player}]
    d0 = date.today()

    async def _published(name: str, sov: int, start: date, end: date) -> None:
        c = await _campaign(client, headers, adv["id_advertiser"], sov_percent=sov,
                            campaign_name=name, targets=tgt,
                            starts_on=start.isoformat(), ends_on=end.isoformat())
        await client.post(f"/api/v1/campaigns/{c['id_campaign']}/creatives",
                          headers=headers, json={"id_media": media.id_media})
        r = await client.post(f"/api/v1/campaigns/{c['id_campaign']}/status",
                              headers=headers, json={"action": "publish"})
        assert r.status_code == 200, r.text

    await _published("Cal-A", 40, d0, d0 + timedelta(days=5))
    await _published("Cal-B", 30, d0 + timedelta(days=3), d0 + timedelta(days=10))

    r = await client.get(
        "/api/v1/campaigns/occupancy-daily",
        params={"date_from": d0.isoformat(),
                "date_to": (d0 + timedelta(days=11)).isoformat()},
        headers=headers)
    assert r.status_code == 200
    by_date = {row["date"]: row["max_pct"] for row in r.json()["days"]}
    assert by_date[d0.isoformat()] == 40                              # A seule
    assert by_date[(d0 + timedelta(days=3)).isoformat()] == 70        # A + B
    assert by_date[(d0 + timedelta(days=5)).isoformat()] == 70        # dernier jour A
    assert by_date[(d0 + timedelta(days=6)).isoformat()] == 30        # B seule
    assert by_date[(d0 + timedelta(days=11)).isoformat()] == 0        # rien

    # Fenêtre > 92 jours = 400
    r = await client.get(
        "/api/v1/campaigns/occupancy-daily",
        params={"date_from": d0.isoformat(),
                "date_to": (d0 + timedelta(days=120)).isoformat()},
        headers=headers)
    assert r.status_code == 400


def _import_order(**over) -> dict:
    body = {
        "version": 1,
        "advertiser": {"name": "ImportCo", "contact_email": "buy@importco.com"},
        "campaign": {
            "name": "Import Été", "starts_on": date.today().isoformat(),
            "ends_on": (date.today() + timedelta(days=15)).isoformat(),
            "sov_percent": 10, "pricing_model": "sov", "price_amount": 900,
        },
        "targets": [{"type": "org"}],
        "creatives": [{"url": "https://cdn.importco.com/spot-15s.mp4", "weight": 2}],
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_campaign_import_dry_run_and_import(client: AsyncClient, headers, org_admin,
                                                  db_session, org, monkeypatch):
    """S6 import JSON : dry-run (cibles par NOM, erreurs, occupation) puis import
    (annonceur créé + dossier, campagne draft, médias pending avec download_url,
    créatifs, download dispatché en Celery)."""
    from app.api.v1.endpoints import campaigns as campaigns_ep
    from app.models.media import CmsMedia
    from app.models.media_folder import CmsMediaFolder
    from app.models.player import CmsPlayer
    from app.workers import media_worker

    monkeypatch.setattr(campaigns_ep, "url_is_safe", lambda url, **kw: True)
    dispatched: list[int] = []
    monkeypatch.setattr(media_worker.download_remote_media, "delay",
                        lambda mid: dispatched.append(mid))

    player = CmsPlayer(id_org=org.id_org, player_name="P-import", status="online")
    db_session.add(player)
    await db_session.commit()

    # Dry-run : cible inconnue → erreur, pas de création
    bad = _import_order(targets=[{"type": "site_group", "name": "Nulle-Part"}])
    r = await client.post("/api/v1/campaigns/import/dry-run", headers=headers, json=bad)
    assert r.status_code == 200
    dry = r.json()
    assert dry["ok"] is False
    assert any("Nulle-Part" in e for e in dry["errors"])
    assert dry["targets"][0]["error"]

    # Dry-run OK : annonceur NOUVEAU, cible player par NOM, occupation calculée
    good = _import_order(targets=[{"type": "player", "name": "P-import"}])
    dry = (await client.post("/api/v1/campaigns/import/dry-run",
                             headers=headers, json=good)).json()
    assert dry["ok"] is True
    assert dry["advertiser_matched"] is False
    assert dry["targets"][0]["target_id"] == player.id_player
    assert dry["target_player_count"] == 1
    assert dry["creatives"][0]["media_type"] == "video"
    assert dry["occupancy"]["blocked"] is False

    # Image sans duration_ms → erreur dry-run + import 422
    img = _import_order(creatives=[{"url": "https://cdn.importco.com/visuel.jpg"}])
    dry = (await client.post("/api/v1/campaigns/import/dry-run",
                             headers=headers, json=img)).json()
    assert dry["ok"] is False and "duration_ms" in dry["errors"][0]
    r = await client.post("/api/v1/campaigns/import", headers=headers, json=img)
    assert r.status_code == 422

    # Import réel
    r = await client.post("/api/v1/campaigns/import", headers=headers, json=good)
    assert r.status_code == 201, r.text
    camp = r.json()
    assert camp["status"] == "draft"
    assert camp["advertiser_name"] == "ImportCo"
    assert camp["creative_count"] == 1
    assert camp["targets"][0]["target_type"] == "player"

    # Annonceur créé avec son dossier « Annonceurs/ImportCo » ; média pending rangé dedans
    from app.models.campaign import CmsAdvertiser
    adv = (await db_session.execute(select(CmsAdvertiser).where(
        CmsAdvertiser.id_org == org.id_org,
        CmsAdvertiser.advertiser_name == "ImportCo"))).scalar_one()
    assert adv.id_folder is not None
    folder = (await db_session.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_folder == adv.id_folder))).scalar_one()
    assert folder.folder_name == "ImportCo"
    media = (await db_session.execute(select(CmsMedia).where(
        CmsMedia.id_org == org.id_org, CmsMedia.source == "import"))).scalar_one()
    assert media.transcode_status == "pending"
    assert media.media_metadata["download_url"] == good["creatives"][0]["url"]
    assert media.id_folder == adv.id_folder
    assert media.media_name == "spot-15s.mp4"
    assert dispatched == [media.id_media]

    # Ré-import même annonceur → matché (pas de doublon)
    dry = (await client.post("/api/v1/campaigns/import/dry-run",
                             headers=headers, json=good)).json()
    assert dry["advertiser_matched"] is True


@pytest.mark.asyncio
async def test_campaign_import_via_api_key(client: AsyncClient, headers, org_admin,
                                           db_session, org, monkeypatch):
    """Import v2 par API (2026-07-12) : clé machine `sfi_` (scope campaigns:import,
    créée org-admin, valeur en clair renvoyée UNE fois) → dry-run + import SANS JWT ;
    clé inconnue/révoquée = 401."""
    from app.api.v1.endpoints import campaigns as campaigns_ep
    from app.workers import media_worker

    monkeypatch.setattr(campaigns_ep, "url_is_safe", lambda url, **kw: True)
    monkeypatch.setattr(media_worker.download_remote_media, "delay", lambda mid: None)

    # Vrai org-admin (la fixture `org_admin` du fichier = rôle campagne custom)
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.role import CmsUserRoleAssignment
    admin = CmsUser(id_org=org.id_org, email=f"oa-{uuid.uuid4().hex[:8]}@t.com",
                    password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(admin)
    await db_session.flush()
    await db_session.execute(
        pg_insert(CmsRole).values(role_key="CMS_ORG_ADMIN", label="OA", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key"))
    oa_role = (await db_session.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=admin.id_user, id_role=oa_role.id_role))
    await db_session.commit()
    ah = {"Authorization": f"Bearer {create_access_token(admin.id_user)}"}

    # Création de la clé (org-admin) — la valeur n'est renvoyée qu'à la création
    r = await client.post("/api/v1/campaigns/import-keys", headers=ah,
                          json={"label": "Agence Media+"})
    assert r.status_code == 201, r.text
    created = r.json()
    raw = created["key"]
    assert raw.startswith("sfi_")
    r = await client.get("/api/v1/campaigns/import-keys", headers=ah)
    assert [k["id_key"] for k in r.json()] == [created["id_key"]]
    assert r.json()[0]["key"] is None  # jamais re-exposée

    kh = {"Authorization": f"Bearer {raw}"}
    order = _import_order()
    dry = (await client.post("/api/v1/campaigns/import/dry-run", headers=kh,
                             json=order)).json()
    assert dry["advertiser_matched"] is False

    r = await client.post("/api/v1/campaigns/import", headers=kh, json=order)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"

    # Une clé inconnue / révoquée ne passe pas
    r = await client.post("/api/v1/campaigns/import/dry-run",
                          headers={"Authorization": "Bearer sfi_nimporte"}, json=order)
    assert r.status_code == 401
    r = await client.delete(f"/api/v1/campaigns/import-keys/{created['id_key']}",
                            headers=ah)
    assert r.status_code == 204
    r = await client.post("/api/v1/campaigns/import/dry-run", headers=kh, json=order)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_campaign_import_rejects_unsafe_url(client: AsyncClient, headers, org_admin):
    """Anti-SSRF : URL vers un hôte non public = erreur au dry-run (pas de fetch)."""
    order = _import_order(creatives=[{"url": "https://192.168.1.10:9000/spot.mp4"}])
    dry = (await client.post("/api/v1/campaigns/import/dry-run",
                             headers=headers, json=order)).json()
    assert dry["ok"] is False
    assert any("URL not allowed" in e for e in dry["errors"])


@pytest.mark.asyncio
async def test_campaign_cross_tenant_404(client: AsyncClient, headers, org_admin,
                                         db_session):
    """La campagne d'une autre org est invisible (404, jamais 403)."""
    other = CmsOrganization(org_name="Autre", org_slug=f"oth-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    from app.models.campaign import CmsAdvertiser, CmsCampaign
    adv = CmsAdvertiser(id_org=other.id_org, advertiser_name="X")
    db_session.add(adv)
    await db_session.flush()
    camp = CmsCampaign(id_org=other.id_org, id_advertiser=adv.id_advertiser,
                       campaign_name="Secret", starts_on=date.today(),
                       ends_on=date.today())
    db_session.add(camp)
    await db_session.commit()

    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_advertiser_folder_autocreated_and_renamed(client: AsyncClient, headers,
                                                         org_admin, db_session, org):
    """Dossier médiathèque « Annonceurs/<nom> » auto-créé à la création (migr 0075),
    renommé quand l'annonceur change de nom."""
    from app.models.media_folder import CmsMediaFolder
    adv = await _advertiser(client, headers, "Brightwell")
    assert adv["id_folder"], "dossier lié à la création"
    folder = (await db_session.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_folder == adv["id_folder"]))).scalar_one()
    assert folder.folder_name == "Brightwell" and folder.depth == 1
    root = (await db_session.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_folder == folder.parent_id))).scalar_one()
    assert root.system_key == "advertisers" and root.parent_id is None
    assert folder.path == f"{root.path}.{folder.id_folder}"

    # 2e annonceur → même racine des annonceurs
    adv2 = await _advertiser(client, headers, "Bosch")
    folder2 = (await db_session.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_folder == adv2["id_folder"]))).scalar_one()
    assert folder2.parent_id == root.id_folder

    # Renommage de l'annonceur → dossier renommé
    r = await client.put(f"/api/v1/advertisers/{adv['id_advertiser']}", headers=headers,
                         json={"advertiser_name": "Brightwell France"})
    assert r.status_code == 200
    await db_session.refresh(folder)
    assert folder.folder_name == "Brightwell France"


@pytest.mark.asyncio
async def test_advertiser_root_found_by_key_not_by_name(client: AsyncClient, headers,
                                                         org_admin, db_session, org):
    """La racine des annonceurs est repérée par `system_key` (migr 0131), jamais par son
    nom : un dossier utilisateur homonyme n'est pas réutilisé, et la racine système ne se
    renomme ni ne se déplace (son nom affiché est traduit par l'interface)."""
    from app.models.media_folder import CmsMediaFolder
    from app.models.role import CmsPermission, CmsRolePermission
    # Le rôle campagne de la fixture n'a pas les droits médiathèque : on les lui ajoute.
    id_role = (await db_session.execute(select(CmsUserRoleAssignment.id_role).where(
        CmsUserRoleAssignment.id_user == org_admin.id_user))).scalar_one()
    for op in ("read", "edit"):
        await db_session.execute(
            pg_insert(CmsPermission).values(module="media", operation=op, label=f"media.{op}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation"))
        perm = (await db_session.execute(select(CmsPermission).where(
            CmsPermission.module == "media", CmsPermission.operation == op))).scalar_one()
        db_session.add(CmsRolePermission(id_role=id_role, id_permission=perm.id_permission))
    await db_session.commit()
    # Témoin : un dossier UTILISATEUR portant exactement le nom de repli.
    r = await client.post("/api/v1/media/folders", headers=headers,
                          json={"folder_name": "Advertisers", "parent_id": None})
    assert r.status_code == 201, r.text
    user_root_id = r.json()["id_folder"]
    assert r.json()["system_key"] is None

    adv = await _advertiser(client, headers, "Keyed Co")
    folder = (await db_session.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_folder == adv["id_folder"]))).scalar_one()
    assert folder.parent_id != user_root_id
    root = (await db_session.execute(select(CmsMediaFolder).where(
        CmsMediaFolder.id_folder == folder.parent_id))).scalar_one()
    assert root.system_key == "advertisers"

    listed = (await client.get("/api/v1/media/folders", headers=headers)).json()
    assert {f["id_folder"]: f["system_key"] for f in listed}[root.id_folder] == "advertisers"

    r = await client.put(f"/api/v1/media/folders/{root.id_folder}", headers=headers,
                         json={"folder_name": "Sponsors"})
    assert r.status_code == 409, r.text
    r = await client.put(f"/api/v1/media/folders/{root.id_folder}", headers=headers,
                         json={"parent_id": user_root_id})
    assert r.status_code == 409, r.text
    # Le dossier de l'annonceur, lui, reste un dossier ordinaire.
    r = await client.put(f"/api/v1/media/folders/{folder.id_folder}", headers=headers,
                         json={"folder_name": "Keyed Co (2026)"})
    assert r.status_code == 200, r.text
