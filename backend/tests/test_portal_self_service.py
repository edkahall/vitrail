"""Tests portail annonceur SELF-SERVICE (S7 Phase C2, migr 0083).

Cycle complet : l'annonceur crée un brouillon (sites uniquement), uploade ses
créatifs (quotas 20/512 MiB, nés `pending`), voit la dispo AGRÉGÉE + le devis
(réponse strippée §3.2), soumet (§21, isolation cross-annonceur = 404) ;
le retailer approuve (auto-approve des créatifs) et publie. Un compte retailer
est refusé sur /portal (403) ; l'annonceur ne touche pas au brouillon d'un autre.
"""

import io
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.campaign import CmsAdvertiser, CmsCampaign, CmsCampaignCreative
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings, CmsSite
from app.models.role import CmsPermission, CmsRole, CmsRolePermission, CmsUserRoleAssignment
from app.models.user import CmsUser


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="PSS", org_slug=f"pss-{uuid.uuid4().hex[:8]}")
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


async def _role(db: AsyncSession, key: str, perms: list[tuple[str, str]]) -> CmsRole:
    role = (await db.execute(select(CmsRole).where(CmsRole.role_key == key))).scalars().first()
    if role is None:
        role = CmsRole(id_org=None, role_key=key, label=key, is_system=True)
        db.add(role)
        await db.flush()
    for module, op in perms:
        await _grant(db, role, module, op)
    return role


async def _advertiser_user(db: AsyncSession, org: CmsOrganization) -> tuple[CmsUser, CmsAdvertiser]:
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name=f"Brand-{uuid.uuid4().hex[:6]}")
    db.add(adv)
    await db.flush()
    role = await _role(db, "CMS_ADVERTISER",
                       [("campaigns", "read"), ("campaigns", "report"),
                        ("approvals", "submit")])
    user = CmsUser(id_org=org.id_org, email=f"adv-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True,
                   id_advertiser=adv.id_advertiser, full_name="Ad Vertiser")
    db.add(user)
    await db.flush()
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    return user, adv


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    role = await _role(db_session, "CMS_ORG_ADMIN",
                       [("campaigns", op) for op in ("read", "create", "edit", "publish")]
                       + [("approvals", op) for op in ("submit", "approve", "reject", "review")])
    user = CmsUser(id_org=org.id_org, email=f"adm-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return user


def _headers(user: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


async def _site(db: AsyncSession, org: CmsOrganization, **kw) -> CmsSite:
    s = CmsSite(id_org=org.id_org, site_name=f"S-{uuid.uuid4().hex[:6]}",
                latitude=48.85, longitude=2.35, **kw)
    db.add(s)
    await db.commit()
    return s


def _body(site: CmsSite, **kw) -> dict:
    return {
        "campaign_name": "Ma campagne", "starts_on": date.today().isoformat(),
        "ends_on": (date.today() + timedelta(days=9)).isoformat(),
        "sov_percent": 10, "site_ids": [site.id_site], **kw,
    }


@pytest.mark.asyncio
async def test_player_selection_setting_gates_screens(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization):
    """Sélection PAR ÉCRAN (migr 0089, réglage org défaut OFF) : OFF = les players ne
    sont ni listés ni ciblables (403) ; ON = listés dans /portal/sites et ciblables
    en cibles mixtes site+player (un opt-out reste introuvable = 404)."""
    from app.models.player import CmsPlayer

    user, _ = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)
    site2 = await _site(db_session, org)
    p1 = CmsPlayer(id_org=org.id_org, id_site=site.id_site, player_name="Entrance",
                   status="online")
    p_opt = CmsPlayer(id_org=org.id_org, id_site=site.id_site, player_name="Caisse",
                      status="online", campaigns_optout=True)
    db_session.add_all([p1, p_opt])
    await db_session.commit()

    # OFF (défaut) : pas de liste nominative, player_ids refusé
    r = await client.get("/api/v1/portal/sites", headers=_headers(user))
    assert all(s["players"] == [] for s in r.json())
    r = await client.get("/api/v1/portal/limits", headers=_headers(user))
    assert r.json()["player_selection"] is False
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site2, site_ids=[site2.id_site],
                                     player_ids=[p1.id_player]))
    assert r.status_code == 403

    # ON : players vendables listés (opt-out exclu), cibles mixtes acceptées
    org.campaign_portal_player_selection = True
    await db_session.commit()
    r = await client.get("/api/v1/portal/sites", headers=_headers(user))
    by_id = {s["id_site"]: s for s in r.json()}
    assert [p["player_name"] for p in by_id[site.id_site]["players"]] == ["Entrance"]
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site2, site_ids=[site2.id_site],
                                     player_ids=[p1.id_player]))
    assert r.status_code == 201, r.text
    targets = r.json()["targets"]
    assert {(t["target_type"], t["target_id"]) for t in targets} == {
        ("site", site2.id_site), ("player", p1.id_player)}
    # un opt-out n'est jamais ciblable ; aucune cible du tout = 422
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site2, site_ids=[], player_ids=[p_opt.id_player]))
    assert r.status_code == 404
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site2, site_ids=[], player_ids=[]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_creative_duration_guard(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    """Garde-fou boucle courte (migr 0089, décision Ed 12/07 : limite fixe réglable,
    défaut 30 s) : ajout CMS d'une vidéo trop longue = 422 ; publish re-vérifie
    (vidéos portail/import dont la durée arrive au transcodage) ; NULL = illimité."""
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Long")
    db_session.add(adv)
    await db_session.flush()
    camp = CmsCampaign(id_org=org.id_org, id_advertiser=adv.id_advertiser,
                       campaign_name="Loop", status="draft",
                       starts_on=date.today(), ends_on=date.today() + timedelta(days=5),
                       sov_percent=10.0)
    long_video = CmsMedia(id_org=org.id_org, media_name="corvane-90s", media_type="video",
                          storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                          transcode_status="done", duration_ms=90_000)
    db_session.add_all([camp, long_video])
    await db_session.commit()

    # défaut 30 s → 422 avec la durée dans le message
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/creatives",
                          headers=_headers(admin), json={"id_media": long_video.id_media})
    assert r.status_code == 422 and "90" in r.json()["detail"]

    # limite relevée → OK ; puis limite NULL explicite = illimité
    r = await client.put("/api/v1/campaigns/settings", headers=_headers(admin),
                         json={"creative_max_duration_s": 120})
    assert r.status_code == 200 and r.json()["creative_max_duration_s"] == 120
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/creatives",
                          headers=_headers(admin), json={"id_media": long_video.id_media})
    assert r.status_code == 201, r.text

    # publish re-vérifie : limite redescendue à 20 s → 422 explicite
    from app.models.campaign import CmsCampaignTarget
    db_session.add(CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                                     target_type="org", target_id=None))
    org.campaign_creative_max_duration_s = 20
    await db_session.commit()
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 422 and "corvane-90s" in r.json()["detail"]

    r = await client.put("/api/v1/campaigns/settings", headers=_headers(admin),
                         json={"creative_max_duration_s": None})
    assert r.status_code == 200 and r.json()["creative_max_duration_s"] is None
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_full_self_service_cycle(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    user, adv = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)
    from app.models.player import CmsPlayer
    db_session.add(CmsPlayer(id_org=org.id_org, player_name="P1", player_os="linux",
                             id_site=site.id_site))
    org.campaign_sov_daily_rate = 0.5
    await db_session.commit()

    # Sites pour la carte
    r = await client.get("/api/v1/portal/sites", headers=_headers(user))
    assert r.status_code == 200 and r.json()[0]["latitude"] == 48.85

    # Dispo agrégée + devis (réponse strippée : pas de players, pas de concurrents)
    r = await client.post("/api/v1/portal/occupancy-check", headers=_headers(user),
                          json={"starts_on": date.today().isoformat(),
                                "ends_on": (date.today() + timedelta(days=9)).isoformat(),
                                "sov_percent": 10, "site_ids": [site.id_site]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["availability"] == "ok"
    assert body["price"]["estimated_price"] == 50.0  # 0.5 × 10 pts × 10 j × 1 player
    assert "players" not in body and "other_campaigns" not in body

    # Création du brouillon (cibles = sites)
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site))
    assert r.status_code == 201, r.text
    camp = r.json()
    assert camp["status"] == "draft"
    assert len(camp["targets"]) == 1
    assert camp["targets"][0]["target_type"] == "site"
    assert camp["targets"][0]["target_id"] == site.id_site

    # Upload d'un créatif image → pending d'office, rangé au dossier annonceur
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = await client.post(
        f"/api/v1/portal/campaigns/{camp['id_campaign']}/creatives",
        headers=_headers(user),
        files={"file": ("visuel.png", io.BytesIO(png), "image/png")},
    )
    assert r.status_code == 201, r.text
    creative = r.json()
    assert creative["approval_status"] == "pending"
    assert creative["duration_ms"] == 10_000
    media = (await db_session.execute(select(CmsMedia).where(
        CmsMedia.id_media == creative["id_media"]))).scalar_one()
    adv_row = (await db_session.execute(select(CmsAdvertiser).where(
        CmsAdvertiser.id_advertiser == adv.id_advertiser))).scalar_one()
    await db_session.refresh(adv_row)
    assert media.id_folder == adv_row.id_folder and media.id_folder is not None

    # Soumission → demande §21 pending_review
    r = await client.post(f"/api/v1/portal/campaigns/{camp['id_campaign']}/submit",
                          headers=_headers(user))
    assert r.status_code == 201, r.text
    id_request = r.json()["id_request"]

    # Le brouillon soumis n'est plus modifiable tant que la demande vit ? Si :
    # il RESTE draft (le retailer peut demander des modifs) — mais une 2e soumission = 409
    r = await client.post(f"/api/v1/portal/campaigns/{camp['id_campaign']}/submit",
                          headers=_headers(user))
    assert r.status_code == 409

    # Côté retailer : la demande est dans la file avec créatifs + devis, approve
    r = await client.get(f"/api/v1/approvals/{id_request}", headers=_headers(admin))
    assert r.status_code == 200
    detail = r.json()
    assert detail["submitted_by_name"] == "Ad Vertiser"
    assert len(detail["creatives"]) == 1
    assert detail["campaign"]["price"]["estimated_price"] == 50.0
    r = await client.post(f"/api/v1/approvals/{id_request}/approve",
                          headers=_headers(admin), json={})
    assert r.status_code == 200

    # Créatif auto-approuvé → publish retailer OK → demande published
    cr = (await db_session.execute(select(CmsCampaignCreative).where(
        CmsCampaignCreative.id_campaign == camp["id_campaign"]))).scalar_one()
    await db_session.refresh(cr)
    assert cr.approval_status == "approved"
    r = await client.post(f"/api/v1/campaigns/{camp['id_campaign']}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/v1/approvals/{id_request}", headers=_headers(user))
    assert r.json()["status"] == "published"


@pytest.mark.asyncio
async def test_portal_gates_and_isolation(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    user_a, _ = await _advertiser_user(db_session, org)
    user_b, _ = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)

    # Retailer (non lié) → 403 sur le portail
    r = await client.get("/api/v1/portal/sites", headers=_headers(admin))
    assert r.status_code == 403

    # exclusive = SOV 100 ; site inexistant = 404 ; dates inversées = 422
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user_a),
                          json=_body(site, exclusive=True))
    assert r.status_code == 201 and float(r.json()["sov_percent"]) == 100.0
    id_a = r.json()["id_campaign"]
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user_a),
                          json={**_body(site), "site_ids": [999_999]})
    assert r.status_code == 404
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user_a),
                          json={**_body(site),
                                "ends_on": (date.today() - timedelta(days=2)).isoformat()})
    assert r.status_code == 422

    # Isolation : l'annonceur B ne voit/modifie pas le brouillon de A (404)
    r = await client.put(f"/api/v1/portal/campaigns/{id_a}", headers=_headers(user_b),
                         json=_body(site))
    assert r.status_code == 404
    r = await client.post(f"/api/v1/portal/campaigns/{id_a}/submit", headers=_headers(user_b))
    assert r.status_code == 404
    # ... y compris via l'endpoint GÉNÉRIQUE POST /approvals (scope service)
    r = await client.post("/api/v1/approvals/", headers=_headers(user_b),
                          json={"resource_type": "campaign", "resource_id": id_a})
    assert r.status_code == 404

    # Soumission sans créatif = 422
    r = await client.post(f"/api/v1/portal/campaigns/{id_a}/submit", headers=_headers(user_a))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_upload_quotas_and_types(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization):
    user, _ = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site))
    cid = r.json()["id_campaign"]

    # Type refusé (415)
    r = await client.post(f"/api/v1/portal/campaigns/{cid}/creatives",
                          headers=_headers(user),
                          files={"file": ("doc.pdf", io.BytesIO(b"%PDF"), "application/pdf")})
    assert r.status_code == 415

    # Quota 20 (on force le compteur en base pour ne pas uploader 20 fichiers)
    camp = (await db_session.execute(select(CmsCampaign).where(
        CmsCampaign.id_campaign == cid))).scalar_one()
    m = CmsMedia(id_org=org.id_org, media_name="bulk", media_type="image",
                 storage_key=f"t/{uuid.uuid4().hex}.png", checksum_sha256="c",
                 transcode_status="done")
    db_session.add(m)
    await db_session.flush()
    for _i in range(20):
        db_session.add(CmsCampaignCreative(
            id_org=org.id_org, id_campaign=camp.id_campaign, id_media=m.id_media,
            duration_ms=5000, approval_status="pending"))
    await db_session.commit()
    r = await client.post(f"/api/v1/portal/campaigns/{cid}/creatives",
                          headers=_headers(user),
                          files={"file": ("v.png", io.BytesIO(b"\x89PNG0000"), "image/png")})
    assert r.status_code == 409 and "Quota" in r.json()["detail"]


@pytest.mark.asyncio
async def test_org_quotas_configurables_et_limits(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization):
    """Quotas 0084 : le réglage org (GET/PUT /campaigns/settings) est appliqué à
    l'upload portail et exposé par GET /portal/limits (affichage UI)."""
    user, _ = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)
    org.campaign_max_creatives = 1
    org.campaign_creative_max_mb = 1
    await db_session.commit()

    r = await client.get("/api/v1/portal/limits", headers=_headers(user))
    assert r.status_code == 200
    # subset (pas d'exact : la réponse s'enrichit — 3e récidive du piège)
    body = r.json()
    assert body["max_creatives"] == 1 and body["creative_max_mb"] == 1

    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site))
    cid = r.json()["id_campaign"]

    # Fichier > 1 Mo → 413 avec la limite de l'org dans le message
    big = io.BytesIO(b"\x00" * (1024 * 1024 + 1))
    r = await client.post(f"/api/v1/portal/campaigns/{cid}/creatives",
                          headers=_headers(user),
                          files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413 and "1 MB" in r.json()["detail"]

    # Quota org = 1 créatif : on en force 1 en base → l'upload suivant = 409
    m = CmsMedia(id_org=org.id_org, media_name="one", media_type="image",
                 storage_key=f"t/{uuid.uuid4().hex}.png", checksum_sha256="c",
                 transcode_status="done")
    db_session.add(m)
    await db_session.flush()
    db_session.add(CmsCampaignCreative(
        id_org=org.id_org, id_campaign=cid, id_media=m.id_media,
        duration_ms=5000, approval_status="pending"))
    await db_session.commit()
    r = await client.post(f"/api/v1/portal/campaigns/{cid}/creatives",
                          headers=_headers(user),
                          files={"file": ("s.png", io.BytesIO(b"\x89PNG0"), "image/png")})
    assert r.status_code == 409 and "1 creatives max" in r.json()["detail"]


@pytest.mark.asyncio
async def test_site_lists_and_screen_counts(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization):
    """Retour Ed #11 : listes de sites par annonceur (upsert par nom, isolation
    cross-annonceur = 404) + screen_count strippé sur GET /portal/sites."""
    from app.models.player import CmsPlayer

    user_a, _ = await _advertiser_user(db_session, org)
    user_b, _ = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)
    db_session.add_all([
        CmsPlayer(id_org=org.id_org, player_name="SL1", player_os="linux",
                  id_site=site.id_site),
        CmsPlayer(id_org=org.id_org, player_name="SL2", player_os="linux",
                  id_site=site.id_site, campaigns_optout=True),
    ])
    await db_session.commit()

    # screen_count = écrans VENDABLES (opt-out déduit)
    r = await client.get("/api/v1/portal/sites", headers=_headers(user_a))
    assert r.status_code == 200
    assert next(s for s in r.json() if s["id_site"] == site.id_site)["screen_count"] == 1

    # créer une liste
    r = await client.post("/api/v1/portal/site-lists", headers=_headers(user_a),
                          json={"list_name": "IDF", "site_ids": [site.id_site]})
    assert r.status_code == 201, r.text
    lid = r.json()["id_list"]
    assert r.json()["site_ids"] == [site.id_site]

    # même nom = upsert (remplace, même id)
    r = await client.post("/api/v1/portal/site-lists", headers=_headers(user_a),
                          json={"list_name": "IDF", "site_ids": [site.id_site]})
    assert r.status_code == 201 and r.json()["id_list"] == lid

    # site inconnu (autre org / inexistant) = 422
    r = await client.post("/api/v1/portal/site-lists", headers=_headers(user_a),
                          json={"list_name": "Bad", "site_ids": [99999999]})
    assert r.status_code == 422

    # l'annonceur B ne voit pas les listes de A et ne peut pas les supprimer (404)
    r = await client.get("/api/v1/portal/site-lists", headers=_headers(user_b))
    assert r.status_code == 200 and r.json() == []
    r = await client.delete(f"/api/v1/portal/site-lists/{lid}", headers=_headers(user_b))
    assert r.status_code == 404

    # suppression par le propriétaire
    r = await client.delete(f"/api/v1/portal/site-lists/{lid}", headers=_headers(user_a))
    assert r.status_code == 204
    r = await client.get("/api/v1/portal/site-lists", headers=_headers(user_a))
    assert r.json() == []


@pytest.mark.asyncio
async def test_portal_audio_s3(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization):
    """S3 : les flags annonceur (migr 0095) gatent le type de campagne au portail.
    Audio OFF → 403 ; audio ON → occupancy STRIPPÉE en spots/h, création, kind immuable,
    créatif audio uniquement (415 sur un visuel)."""
    from app.models.player import CmsPlayer

    user, adv = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)
    # Player audio-capable rattaché au site (sinon audio_eligible_players le rejette).
    db_session.add(CmsPlayer(id_org=org.id_org, player_name="AUD", player_os="linux",
                             id_site=site.id_site, player_role="audio"))
    await db_session.commit()

    audio_occ = {"starts_on": date.today().isoformat(),
                 "ends_on": (date.today() + timedelta(days=9)).isoformat(),
                 "sov_percent": 10, "site_ids": [site.id_site],
                 "campaign_kind": "audio", "spots_per_hour": 6}

    # ── Audio NON autorisé (défaut) : limits sans 'audio', occupancy + create = 403 ──
    r = await client.get("/api/v1/portal/limits", headers=_headers(user))
    assert r.status_code == 200 and r.json()["allowed_kinds"] == ["visual"]
    r = await client.post("/api/v1/portal/occupancy-check", headers=_headers(user), json=audio_occ)
    assert r.status_code == 403
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site, campaign_kind="audio", spots_per_hour=6))
    assert r.status_code == 403

    # ── Autoriser l'audio sur l'annonceur ────────────────────────────────────────
    adv_row = (await db_session.execute(select(CmsAdvertiser).where(
        CmsAdvertiser.id_advertiser == adv.id_advertiser))).scalar_one()
    adv_row.allow_audio_campaigns = True
    await db_session.commit()

    r = await client.get("/api/v1/portal/limits", headers=_headers(user))
    assert set(r.json()["allowed_kinds"]) == {"visual", "audio"}

    # Occupancy audio STRIPPÉE : pas de players, spots/h réalisables présents
    r = await client.post("/api/v1/portal/occupancy-check", headers=_headers(user), json=audio_occ)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["availability"] == "ok"
    assert "players" not in body
    assert body["achievable_spots_per_hour"] == 12  # plafond org 12 - 0 servi

    # spots_per_hour manquant en audio = 422
    r = await client.post("/api/v1/portal/occupancy-check", headers=_headers(user),
                          json={**audio_occ, "spots_per_hour": None})
    assert r.status_code == 422

    # Création d'une campagne audio (pricing 'flat', spots_per_hour)
    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site, campaign_kind="audio", spots_per_hour=6))
    assert r.status_code == 201, r.text
    camp = r.json()
    cid = camp["id_campaign"]
    assert camp["campaign_kind"] == "audio" and camp["spots_per_hour"] == 6

    # Le type est immuable : update en 'visual' = 422
    r = await client.put(f"/api/v1/portal/campaigns/{cid}", headers=_headers(user),
                         json=_body(site, campaign_kind="visual"))
    assert r.status_code == 422

    # Un créatif VISUEL est refusé sur une campagne audio (415)
    r = await client.post(f"/api/v1/portal/campaigns/{cid}/creatives", headers=_headers(user),
                          files={"file": ("v.png", io.BytesIO(b"\x89PNG0"), "image/png")})
    assert r.status_code == 415

    # Un créatif AUDIO passe → media_type 'audio', durée posée au transcodage (None)
    r = await client.post(f"/api/v1/portal/campaigns/{cid}/creatives", headers=_headers(user),
                          files={"file": ("spot.mp3", io.BytesIO(b"ID3\x03\x00" + b"0" * 64), "audio/mpeg")})
    assert r.status_code == 201, r.text
    cr = r.json()
    assert cr["media_type"] == "audio" and cr["duration_ms"] is None


@pytest.mark.asyncio
async def test_portal_overview(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization):
    """Accueil portail : synthèse SCOPÉE à l'annonceur lié — compteurs de campagnes,
    players ciblés (statut effectif), Proof of Play 24 h, sites geo, activité."""
    from datetime import UTC, datetime
    from app.models.campaign import CmsCampaignTarget
    from app.models.player import CmsPlayer
    from app.models.pop import CmsProofOfPlay

    user, adv = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)  # a des coords (48.85, 2.35)
    player = CmsPlayer(id_org=org.id_org, player_name="OVW", player_os="linux",
                       id_site=site.id_site, status="online",
                       last_seen_at=datetime.now(UTC))
    media = CmsMedia(id_org=org.id_org, media_name="spotmedia", media_type="video",
                     storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                     transcode_status="done")
    db_session.add_all([player, media])
    await db_session.flush()
    camp = CmsCampaign(id_org=org.id_org, id_advertiser=adv.id_advertiser,
                       campaign_name="Live", status="approved",
                       starts_on=date.today() - timedelta(days=1),
                       ends_on=date.today() + timedelta(days=5),
                       sov_percent=10, pricing_model="sov")
    db_session.add(camp)
    await db_session.flush()
    db_session.add_all([
        CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                          target_type="site", target_id=site.id_site),
        CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                            id_media=media.id_media, approval_status="approved"),
        CmsProofOfPlay(id_org=org.id_org, id_player=player.id_player,
                       id_media=media.id_media, played_at=datetime.now(UTC),
                       duration_ms=5000, merkle_hash="mk1"),
    ])
    await db_session.commit()

    r = await client.get("/api/v1/portal/overview", headers=_headers(user))
    assert r.status_code == 200, r.text
    ov = r.json()
    assert ov["campaigns"]["total"] == 1 and ov["campaigns"]["active"] == 1
    assert ov["campaigns"]["visual"] == 1 and ov["campaigns"]["audio"] == 0
    # Player ciblé en ligne + PoP 24 h compté
    assert ov["players"]["total"] == 1 and ov["players"]["online"] == 1
    assert ov["proof_of_play_24h"] == 1
    # Site géolocalisé du player ciblé
    assert len(ov["sites"]) == 1 and ov["sites"][0]["id_site"] == site.id_site
    assert ov["sites"][0]["players_online"] == 1

    # Isolation : un autre annonceur ne voit rien de tout ça
    user_b, _ = await _advertiser_user(db_session, org)
    r = await client.get("/api/v1/portal/overview", headers=_headers(user_b))
    assert r.status_code == 200
    ob = r.json()
    assert ob["campaigns"]["total"] == 0 and ob["players"]["total"] == 0
    assert ob["proof_of_play_24h"] == 0 and ob["sites"] == []


@pytest.mark.asyncio
async def test_portal_decisions_bell(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser):
    """Retour Ed #8 : cloche des décisions du portail — non-vu serveur-side
    (submitter_seen_at), marquage « vu », et is_reviewer sur le dashboard (#7)."""
    user, _ = await _advertiser_user(db_session, org)
    site = await _site(db_session, org)

    r = await client.post("/api/v1/portal/campaigns", headers=_headers(user),
                          json=_body(site))
    cid = r.json()["id_campaign"]
    m = CmsMedia(id_org=org.id_org, media_name="bell", media_type="image",
                 storage_key=f"t/{uuid.uuid4().hex}.png", checksum_sha256="c",
                 transcode_status="done")
    db_session.add(m)
    await db_session.flush()
    db_session.add(CmsCampaignCreative(
        id_org=org.id_org, id_campaign=cid, id_media=m.id_media,
        duration_ms=5000, approval_status="pending"))
    await db_session.commit()

    r = await client.post(f"/api/v1/portal/campaigns/{cid}/submit", headers=_headers(user))
    assert r.status_code == 201, r.text
    rid = r.json()["id_request"]

    # Pas encore de décision
    r = await client.get("/api/v1/portal/decisions", headers=_headers(user))
    assert r.status_code == 200
    assert r.json() == {"unseen_count": 0, "items": []}

    # Le retailer approuve → 1 décision non vue
    r = await client.post(f"/api/v1/approvals/{rid}/approve", headers=_headers(admin),
                          json={})
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/portal/decisions", headers=_headers(user))
    body = r.json()
    assert body["unseen_count"] == 1
    assert body["items"][0]["status"] == "approved" and body["items"][0]["unseen"] is True
    assert body["items"][0]["id_campaign"] == cid

    # Marquer vu → badge à zéro, l'item reste listé
    r = await client.post("/api/v1/portal/decisions/seen", headers=_headers(user))
    assert r.status_code == 204
    r = await client.get("/api/v1/portal/decisions", headers=_headers(user))
    assert r.json()["unseen_count"] == 0
    assert r.json()["items"][0]["unseen"] is False

    # Cloche CMS (#7) : is_reviewer exposé par le dashboard
    r = await client.get("/api/v1/approvals/dashboard", headers=_headers(admin))
    assert r.status_code == 200 and r.json()["is_reviewer"] is True
    r = await client.get("/api/v1/approvals/dashboard", headers=_headers(user))
    assert r.status_code == 200 and r.json()["is_reviewer"] is False
