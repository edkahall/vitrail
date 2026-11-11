"""Tests VOD à la demande (Feature B, Phase 1) : configs + affectation + pré-cache.

- service `expected_media` : les médias d'une config VOD ciblant le player sont inclus
  (pré-cache), même hors playlist/schedule et sans subir le filtre tag (affectation explicite).
- endpoints CRUD `/vod` : création même-site, rejet site mixte, rejet média non-visuel,
  mise à jour des affectations, suppression, RBAC.
"""

import json
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.endpoints.vod as vod_module
from app.core.security import hash_password
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings, CmsSite
from app.models.player import CmsPlayer
from app.models.role import CmsPermission, CmsRole, CmsRolePermission, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.models.vod import CmsVodConfig, CmsVodConfigMedia, CmsVodConfigPlayer
from app.services.player_inventory import expected_media

_VOD_PERMS = [("vod", "create"), ("vod", "read"), ("vod", "edit"), ("vod", "delete"), ("vod", "trigger")]


@pytest_asyncio.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Le push force_sync (pré-cache) ne doit pas dépendre de Redis dans les tests."""
    async def _noop(_ids):
        return None
    monkeypatch.setattr(vod_module, "_push_force_sync", _noop)


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="VOD", org_slug=f"vod-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


async def _site(db, org, name):
    s = CmsSite(id_org=org.id_org, site_name=name)
    db.add(s)
    await db.flush()
    return s


async def _media(db, org, name, mtype="video"):
    m = CmsMedia(id_org=org.id_org, media_name=name, media_type=mtype,
                 mime_type=f"{mtype}/mp4", storage_key=f"k/{uuid.uuid4().hex}",
                 checksum_sha256=uuid.uuid4().hex)
    db.add(m)
    await db.flush()
    return m


async def _player(db, org, name, id_site=None, player_os="linux"):
    p = CmsPlayer(id_org=org.id_org, player_name=name, player_os=player_os, id_site=id_site)
    db.add(p)
    await db.flush()
    return p


@pytest_asyncio.fixture
async def auth(client: AsyncClient, db_session: AsyncSession, org: CmsOrganization) -> str:
    """Utilisateur avec toutes les permissions vod."""
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(id_org=org.id_org, email=f"vod-{uid}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    role = CmsRole(id_org=org.id_org, role_key=f"CMS_VOD_{uid}", label="Vod", is_system=False)
    db_session.add_all([user, role])
    await db_session.flush()
    for module, operation in _VOD_PERMS:
        await db_session.execute(
            pg_insert(CmsPermission)
            .values(module=module, operation=operation, label=f"{module}.{operation}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation"))
    await db_session.flush()
    for module, operation in _VOD_PERMS:
        perm = (await db_session.execute(select(CmsPermission).where(
            CmsPermission.module == module, CmsPermission.operation == operation))).scalar_one()
        db_session.add(CmsRolePermission(id_role=role.id_role, id_permission=perm.id_permission))
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    resp = await client.post("/api/v1/auth/login",
                             json={"email": user.email, "password": "Password1!"})
    return resp.json()["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ── Pré-cache (service expected_media) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_vod_media_in_expected_media(db_session, org):
    """Une config VOD pré-cache ses médias sur le player affecté (hors playlist/schedule)."""
    site = await _site(db_session, org, "Magasin A")
    player = await _player(db_session, org, "p1", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="Promo")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=vid.id_media, sort_order=0))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=player.id_player))
    await db_session.commit()

    exp = await expected_media(db_session, player.id_player)
    ids = {m["id_media"] for m in exp}
    assert vid.id_media in ids
    item = next(m for m in exp if m["id_media"] == vid.id_media)
    assert any("Promo" in r for r in item["refs"])


@pytest.mark.asyncio
async def test_vod_media_bypasses_tag_filter(db_session, org):
    """L'affectation VOD est explicite → pas soumise au filtre tag de réception du player."""
    from app.models.tag import CmsMediaTagValue, CmsPlayerTagFilter, CmsTagNamespace, CmsTagValue
    site = await _site(db_session, org, "S")
    player = await _player(db_session, org, "p", id_site=site.id_site)
    vid = await _media(db_session, org, "tagged.mp4", "video")
    # Tag "side=right" sur le média ET filtre "side=left" sur le player → normalement exclu.
    ns = CmsTagNamespace(id_org=org.id_org, namespace_key="side", label="side")
    db_session.add(ns)
    await db_session.flush()
    left = CmsTagValue(id_namespace=ns.id_namespace, tag_value="left", label="left")
    right = CmsTagValue(id_namespace=ns.id_namespace, tag_value="right", label="right")
    db_session.add_all([left, right])
    await db_session.flush()
    db_session.add(CmsMediaTagValue(id_media=vid.id_media, id_tag=right.id_tag))
    db_session.add(CmsPlayerTagFilter(id_player=player.id_player,
                                      id_namespace=ns.id_namespace, id_tag=left.id_tag))
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="V")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=vid.id_media))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=player.id_player))
    await db_session.commit()

    ids = {m["id_media"] for m in await expected_media(db_session, player.id_player)}
    assert vid.id_media in ids  # gardé malgré le tag opposé (affectation VOD explicite)


# ── CRUD ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_vod_same_site(client, db_session, org, auth):
    site = await _site(db_session, org, "Magasin")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    vid = await _media(db_session, org, "v.mp4", "video")
    img = await _media(db_session, org, "i.png", "image")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "Lancement", "id_medias": [vid.id_media, img.id_media],
        "id_players": [p1.id_player, p2.id_player]})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["vod_name"] == "Lancement"
    assert data["id_site"] == site.id_site
    assert data["site_name"] == "Magasin"
    assert {m["id_media"] for m in data["medias"]} == {vid.id_media, img.id_media}
    assert {p["id_player"] for p in data["players"]} == {p1.id_player, p2.id_player}


@pytest.mark.asyncio
async def test_create_vod_rejects_mixed_site(client, db_session, org, auth):
    s1 = await _site(db_session, org, "A")
    s2 = await _site(db_session, org, "B")
    p1 = await _player(db_session, org, "a", id_site=s1.id_site)
    p2 = await _player(db_session, org, "b", id_site=s2.id_site)
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_players": [p1.id_player, p2.id_player]})
    assert r.status_code == 400
    assert "site" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_vod_rejects_audio_media(client, db_session, org, auth):
    site = await _site(db_session, org, "A")
    p = await _player(db_session, org, "a", id_site=site.id_site)
    aud = await _media(db_session, org, "track.mp3", "audio")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [aud.id_media], "id_players": [p.id_player]})
    assert r.status_code == 400
    assert "visual" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_vod_replaces_assignments(client, db_session, org, auth):
    s1 = await _site(db_session, org, "A")
    s2 = await _site(db_session, org, "B")
    p1 = await _player(db_session, org, "a", id_site=s1.id_site)
    p2 = await _player(db_session, org, "b", id_site=s2.id_site)
    v1 = await _media(db_session, org, "v1.mp4", "video")
    v2 = await _media(db_session, org, "v2.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [v1.id_media], "id_players": [p1.id_player]})
    id_vod = r.json()["id_vod"]

    r = await client.put(f"/api/v1/vod/{id_vod}", headers=_h(auth), json={
        "id_medias": [v2.id_media], "id_players": [p2.id_player]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert {m["id_media"] for m in data["medias"]} == {v2.id_media}
    assert {p["id_player"] for p in data["players"]} == {p2.id_player}
    assert data["id_site"] == s2.id_site  # site recalculé sur la nouvelle affectation


@pytest.mark.asyncio
async def test_delete_vod(client, db_session, org, auth):
    site = await _site(db_session, org, "A")
    p = await _player(db_session, org, "a", id_site=site.id_site)
    v = await _media(db_session, org, "v.mp4", "video")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [v.id_media], "id_players": [p.id_player]})
    id_vod = r.json()["id_vod"]

    r = await client.delete(f"/api/v1/vod/{id_vod}", headers=_h(auth))
    assert r.status_code == 204
    # purge effective (media + players)
    assert (await db_session.execute(select(CmsVodConfigMedia).where(
        CmsVodConfigMedia.id_vod == id_vod))).first() is None
    r = await client.get(f"/api/v1/vod/{id_vod}", headers=_h(auth))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_build_vod_media_for_player(db_session, org, monkeypatch):
    """Le sync expose les médias VOD du player au panneau LAN-local (Phase 3)."""
    import app.api.v1.endpoints.ws as ws
    monkeypatch.setattr(ws, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    player = await _player(db_session, org, "p", id_site=site.id_site)
    other = await _player(db_session, org, "other", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="V")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=vid.id_media))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=player.id_player))
    await db_session.commit()

    out = await ws._build_vod_media(db_session, player.id_player)
    assert [m["id_media"] for m in out] == [vid.id_media]
    assert out[0]["media_name"] == "promo.mp4"
    assert out[0]["url"].startswith("https://m/")
    # un player NON affecté ne reçoit aucun média VOD
    assert await ws._build_vod_media(db_session, other.id_player) == []


@pytest.mark.asyncio
async def test_build_vod_lan_multi_target(db_session, org):
    """Le sync expose le secret LAN partagé + les pairs ciblables (trigger multi-cible autonome)."""
    import app.api.v1.endpoints.ws as ws
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "nuc", id_site=site.id_site)
    p2 = await _player(db_session, org, "aopen", id_site=site.id_site)
    chrome = await _player(db_session, org, "acer", id_site=site.id_site, player_os="chromeos")
    lone = await _player(db_session, org, "lone", id_site=site.id_site)
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="V")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=p1.id_player))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=p2.id_player))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=chrome.id_player))
    await db_session.commit()

    lan1 = await ws._build_vod_lan(db_session, p1.id_player)
    lan2 = await ws._build_vod_lan(db_session, p2.id_player)
    assert lan1 is not None and lan2 is not None
    # Les pairs ciblables = players de la config SAUF ChromeOS (pas de socket UDP en PWA).
    assert {p["id_player"] for p in lan1["peers"]} == {p1.id_player, p2.id_player}
    assert lan1["self_id"] == p1.id_player
    # Secret partagé par site → identique pour tous les players ciblables (signe/vérifie mutuellement).
    assert lan1["secret"] == lan2["secret"] and len(lan1["secret"]) == 64
    # ⚠️ RÈGLE CHANGÉE le 2026-07-19 (décision Ed) : un player hors config mais dont le SITE
    # porte des VOD reçoit désormais le secret — c'est un player de CONTRÔLE potentiel, il doit
    # pouvoir déclencher chez les autres sans être lui-même une cible. L'ancienne assertion
    # (`is None`) encodait la confusion des deux rôles qu'on corrige ici.
    lan_lone = await ws._build_vod_lan(db_session, lone.id_player)
    assert lan_lone is not None
    assert lan_lone["secret"] == lan1["secret"]      # même site → même secret
    # …mais il n'est PAS une cible : il n'apparaît pas dans les pairs des configs existantes.
    assert lone.id_player not in {p["id_player"] for p in lan1["peers"]}


class _FakeRedis:
    """Capture les messages publiés (canal → liste de payloads)."""
    published: dict[str, list[dict]] = {}

    async def publish(self, channel, msg):
        _FakeRedis.published.setdefault(channel, []).append(json.loads(msg))

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_play_now_dispatches_to_config_players(client, db_session, org, auth, monkeypatch):
    import app.services.media as media_svc
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://minio.test/{key}")

    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [vid.id_media], "id_players": [p1.id_player, p2.id_player]})
    id_vod = r.json()["id_vod"]

    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": vid.id_media, "mode": "loop"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "loop"
    assert set(data["dispatched"]) == {p1.id_player, p2.id_player}
    # Chaque player a reçu une commande play_now avec l'URL/cache résolus.
    for pid in (p1.id_player, p2.id_player):
        msgs = _FakeRedis.published[f"signflow:player:{pid}:cmds"]
        cmd = next(m for m in msgs if m.get("command") == "play_now")
        assert cmd["params"]["id_media"] == vid.id_media
        assert cmd["params"]["mode"] == "loop"
        assert cmd["params"]["url"].startswith("https://minio.test/")


@pytest.mark.asyncio
async def test_play_now_rejects_media_not_in_config(client, db_session, org, auth, monkeypatch):
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    site = await _site(db_session, org, "A")
    p = await _player(db_session, org, "a", id_site=site.id_site)
    v1 = await _media(db_session, org, "in.mp4", "video")
    v2 = await _media(db_session, org, "out.mp4", "video")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [v1.id_media], "id_players": [p.id_player]})
    id_vod = r.json()["id_vod"]

    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": v2.id_media})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_play_now_subset_targets(client, db_session, org, auth, monkeypatch):
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    import app.services.media as media_svc
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    vid = await _media(db_session, org, "v.mp4", "video")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [vid.id_media], "id_players": [p1.id_player, p2.id_player]})
    id_vod = r.json()["id_vod"]

    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": vid.id_media, "id_players": [p1.id_player]})
    assert r.status_code == 200
    assert r.json()["dispatched"] == [p1.id_player]  # p2 non ciblé


@pytest.mark.asyncio
async def test_vod_requires_permission(client, db_session, org):
    """Un utilisateur sans permission vod → 403."""
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(id_org=org.id_org, email=f"noperm-{uid}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    role = CmsRole(id_org=org.id_org, role_key=f"CMS_NOPERM_{uid}", label="NoPerm", is_system=False)
    db_session.add_all([user, role])
    await db_session.flush()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    tok = (await client.post("/api/v1/auth/login",
                             json={"email": user.email, "password": "Password1!"})).json()["access_token"]
    assert (await client.get("/api/v1/vod", headers=_h(tok))).status_code == 403


# ── Média « entrée HDMI » : switch ON/OFF de la source (VOD → cmd hdmi_input) ──

@pytest.mark.asyncio
async def test_create_vod_accepts_hdmi_media(client, db_session, org, auth):
    """Le pointeur hdmi_input est admis dans une config VOD (pas de fichier ni pré-cache)."""
    site = await _site(db_session, org, "A")
    p = await _player(db_session, org, "bs", id_site=site.id_site, player_os="brightsign")
    hdmi = await _media(db_session, org, "HDMI in", "hdmi_input")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [hdmi.id_media], "id_players": [p.id_player]})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_play_hdmi_switch_targets_capable_players_only(client, db_session, org, auth, monkeypatch):
    """Trigger d'un hdmi_input = commande `hdmi_input` start/stop, players à entrée HDMI seulement."""
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    site = await _site(db_session, org, "A")
    bs = await _player(db_session, org, "xt", id_site=site.id_site, player_os="brightsign")
    bs.capabilities = {"bs_model": "XT1144"}
    other = await _player(db_session, org, "nuc", id_site=site.id_site)  # pas d'entrée HDMI
    hdmi = await _media(db_session, org, "HDMI in", "hdmi_input")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [hdmi.id_media],
        "id_players": [bs.id_player, other.id_player]})
    id_vod = r.json()["id_vod"]

    # Switch ON : seul le player capable est ciblé, commande hdmi_input (pas play_now).
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": hdmi.id_media, "mode": "start"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id_media": hdmi.id_media, "mode": "start", "dispatched": [bs.id_player]}
    msgs = _FakeRedis.published[f"signflow:player:{bs.id_player}:cmds"]
    cmd = next(m for m in msgs if m.get("command") == "hdmi_input")
    # source + id_media = traçabilité (le renderer rapporte la session au switch OFF).
    assert cmd["params"] == {"mode": "start", "source": "vod", "id_media": hdmi.id_media}
    assert all(m.get("command") != "hdmi_input"
               for m in _FakeRedis.published.get(f"signflow:player:{other.id_player}:cmds", []))

    # Switch OFF.
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": hdmi.id_media, "mode": "stop"})
    assert r.status_code == 200
    assert r.json()["mode"] == "stop"
    stops = [m for m in _FakeRedis.published[f"signflow:player:{bs.id_player}:cmds"]
             if m.get("command") == "hdmi_input" and m["params"]["mode"] == "stop"]
    assert len(stops) == 1


@pytest.mark.asyncio
async def test_play_hdmi_without_capable_player_400(client, db_session, org, auth, monkeypatch):
    """Aucun player à entrée HDMI dans les cibles → 400 explicite (rien de dispatché)."""
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    site = await _site(db_session, org, "A")
    p = await _player(db_session, org, "nuc", id_site=site.id_site)
    hdmi = await _media(db_session, org, "HDMI in", "hdmi_input")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [hdmi.id_media], "id_players": [p.id_player]})
    id_vod = r.json()["id_vod"]
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": hdmi.id_media})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_build_vod_media_skips_hdmi_pointer(db_session, org, monkeypatch):
    """Le panneau LAN local ne reçoit PAS le pointeur hdmi_input (switch = trigger cloud only) —
    l'exposer ferait résoudre une rendition/URL sur un storage_key vide."""
    import app.api.v1.endpoints.ws as ws
    monkeypatch.setattr(ws, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    player = await _player(db_session, org, "p", id_site=site.id_site, player_os="brightsign")
    vid = await _media(db_session, org, "promo.mp4", "video")
    hdmi = await _media(db_session, org, "HDMI in", "hdmi_input")
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="V")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=hdmi.id_media, sort_order=0))
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=vid.id_media, sort_order=1))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=player.id_player))
    await db_session.commit()
    out = await ws._build_vod_media(db_session, player.id_player)
    assert [m["id_media"] for m in out] == [vid.id_media]


@pytest.mark.asyncio
async def test_play_hdmi_switch_writes_audit(client, db_session, org, auth, monkeypatch):
    """Le trigger VOD d'un switch HDMI est audité (qui, quand, mode, cibles)."""
    from app.models.audit_log import CmsAuditLog
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    site = await _site(db_session, org, "A")
    bs = await _player(db_session, org, "xt", id_site=site.id_site, player_os="brightsign")
    bs.capabilities = {"bs_model": "XT1144"}
    hdmi = await _media(db_session, org, "HDMI in", "hdmi_input")
    await db_session.commit()
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [hdmi.id_media], "id_players": [bs.id_player]})
    id_vod = r.json()["id_vod"]
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": hdmi.id_media, "mode": "start"})
    assert r.status_code == 200
    row = (await db_session.execute(select(CmsAuditLog).where(
        CmsAuditLog.action == "vod_hdmi_switch",
        CmsAuditLog.resource_id == str(id_vod)))).scalars().first()
    assert row is not None
    assert row.detail["mode"] == "start"
    assert row.detail["players"] == [bs.id_player]
    assert row.detail["id_media"] == hdmi.id_media


@pytest.mark.asyncio
async def test_audit_player_event_hdmi_session(db_session, org):
    """L'événement hdmi_session (émis par le renderer au switch OFF, y compris pour la page
    LOCALE /control que le serveur ne voit jamais passer) atterrit dans l'audit log."""
    import app.api.v1.endpoints.ws as ws
    from app.models.audit_log import CmsAuditLog
    site = await _site(db_session, org, "A")
    p = await _player(db_session, org, "xt", id_site=site.id_site, player_os="brightsign")
    await db_session.commit()
    await ws._audit_player_event(db_session, p.id_player, {
        "type": "player:event", "event": "hdmi_session", "source": "local",
        "id_media": None, "duration_ms": 12345,
        "started_at": "2026-07-08T10:00:00Z", "ended_at": "2026-07-08T10:00:12Z"})
    # ⚠️ Filtré sur CE player : sans ce scope, `.first()` attrapait la ligne d'un AUTRE
    # test dès que l'ordre d'exécution changeait (pytest-randomly), et le test échouait
    # sur une valeur qui ne lui appartenait pas. Diagnostiqué le 2026-07-31 — il passait
    # isolément et en ordre fixe, ce qui rendait le faux positif difficile à lire.
    row = (await db_session.execute(select(CmsAuditLog).where(
        CmsAuditLog.action == "hdmi_session",
        CmsAuditLog.id_org == org.id_org,
    ))).scalars().first()
    assert row is not None
    assert row.detail["source"] == "local"
    assert row.detail["duration_ms"] == 12345
    assert row.detail["id_player"] == p.id_player
    assert row.id_org == org.id_org


# ── Ciblage par SITE entier (cible DYNAMIQUE, migr 0096) ──────────────────────
#
# Le risque de cette feature n'est PAS le CRUD : c'est qu'une cible `site` soit acceptée
# côté CMS puis jamais honorée par les chemins PLAYER-CENTRIQUES (pré-cache, sync, LAN) —
# exactement le bug de l'audit du 2026-07-18. Les tests ci-dessous couvrent les deux sens.


@pytest.mark.asyncio
async def test_create_vod_target_site(client, db_session, org, auth):
    """Mode 'site' : la cible résolue = TOUS les players du site, sans liste explicite."""
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    await _player(db_session, org, "ailleurs", id_site=(await _site(db_session, org, "B")).id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "Site A", "target_mode": "site", "id_site": site.id_site,
        "id_medias": [vid.id_media]})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["target_mode"] == "site"
    # Les players du site sont RÉSOLUS dans la réponse ; celui de l'autre site est exclu.
    assert {p["id_player"] for p in data["players"]} == {p1.id_player, p2.id_player}
    # Aucune ligne explicite n'est écrite : la cible est recalculée à chaque lecture.
    rows = (await db_session.execute(select(CmsVodConfigPlayer).where(
        CmsVodConfigPlayer.id_vod == data["id_vod"]))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_target_site_is_dynamic(client, db_session, org, auth):
    """Coeur de la feature : un player AJOUTÉ au site après coup hérite de la config."""
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "Site A", "target_mode": "site", "id_site": site.id_site,
        "id_medias": [vid.id_media]})
    id_vod = r.json()["id_vod"]
    assert {p["id_player"] for p in r.json()["players"]} == {p1.id_player}

    # Nouveau player posé sur le site, SANS toucher à la config VOD.
    p2 = await _player(db_session, org, "nouveau", id_site=site.id_site)
    await db_session.commit()

    r = await client.get(f"/api/v1/vod/{id_vod}", headers=_h(auth))
    assert {p["id_player"] for p in r.json()["players"]} == {p1.id_player, p2.id_player}
    # …et il est réellement PRÉ-CACHÉ (chemin player-centrique, pas seulement l'affichage).
    assert vid.id_media in {m["id_media"] for m in await expected_media(db_session, p2.id_player)}


@pytest.mark.asyncio
async def test_target_site_precache_and_sync_paths(db_session, org, monkeypatch):
    """Les chemins player-centriques honorent le ciblage site : pré-cache + sync LAN + pairs."""
    import app.api.v1.endpoints.ws as ws
    monkeypatch.setattr(ws, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    outsider = await _player(db_session, org, "hors", id_site=(await _site(db_session, org, "B")).id_site)
    nosite = await _player(db_session, org, "sans-site", id_site=None)
    vid = await _media(db_session, org, "promo.mp4", "video")
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, target_mode="site", vod_name="V")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=vid.id_media))
    await db_session.commit()  # aucune ligne cms_vod_config_players

    # 1. pré-cache (expected_media) — le chemin qui décide de ce qui descend sur le device
    for p in (p1, p2):
        assert vid.id_media in {m["id_media"] for m in await expected_media(db_session, p.id_player)}
    for p in (outsider, nosite):
        assert vid.id_media not in {m["id_media"] for m in await expected_media(db_session, p.id_player)}

    # 2. payload de sync (panneau LAN local)
    assert [m["id_media"] for m in await ws._build_vod_media(db_session, p1.id_player)] == [vid.id_media]
    assert await ws._build_vod_media(db_session, outsider.id_player) == []

    # 3. pairs LAN : les deux players du site se voient mutuellement
    lan = await ws._build_vod_lan(db_session, p1.id_player)
    assert lan is not None
    assert {p["id_player"] for p in lan["peers"]} == {p1.id_player, p2.id_player}
    assert await ws._build_vod_lan(db_session, outsider.id_player) is None


@pytest.mark.asyncio
async def test_play_now_targets_whole_site(client, db_session, org, auth, monkeypatch):
    """Le trigger cloud dispatche à tous les players du site ciblé."""
    import app.services.media as media_svc
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://minio.test/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "S", "target_mode": "site", "id_site": site.id_site,
        "id_medias": [vid.id_media]})
    id_vod = r.json()["id_vod"]
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": vid.id_media, "mode": "once"})
    assert r.status_code == 200, r.text
    assert set(r.json()["dispatched"]) == {p1.id_player, p2.id_player}


@pytest.mark.asyncio
async def test_target_site_requires_valid_site(client, db_session, org, auth):
    """Un ciblage site sans site (ou avec un site étranger) est REFUSÉ, jamais avalé."""
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "target_mode": "site", "id_medias": [vid.id_media]})
    assert r.status_code == 400

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "target_mode": "site", "id_site": 999999,
        "id_medias": [vid.id_media]})
    assert r.status_code == 404

    # target_mode inconnu -> 422 (Literal), pas un ciblage vide silencieux.
    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "target_mode": "sites", "id_medias": [vid.id_media]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_switch_between_target_modes(client, db_session, org, auth):
    """Bascule players->site (purge la liste) et site->players (exige une liste explicite)."""
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    p2 = await _player(db_session, org, "b", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [vid.id_media], "id_players": [p1.id_player]})
    id_vod = r.json()["id_vod"]

    # players -> site : la cible s'élargit à tout le site, la liste explicite est purgée.
    r = await client.put(f"/api/v1/vod/{id_vod}", headers=_h(auth),
                         json={"target_mode": "site", "id_site": site.id_site})
    assert r.status_code == 200, r.text
    assert {p["id_player"] for p in r.json()["players"]} == {p1.id_player, p2.id_player}
    rows = (await db_session.execute(select(CmsVodConfigPlayer).where(
        CmsVodConfigPlayer.id_vod == id_vod))).scalars().all()
    assert rows == []

    # site -> players SANS liste : refusé (sinon cible vide en silence).
    r = await client.put(f"/api/v1/vod/{id_vod}", headers=_h(auth), json={"target_mode": "players"})
    assert r.status_code == 400

    r = await client.put(f"/api/v1/vod/{id_vod}", headers=_h(auth),
                         json={"target_mode": "players", "id_players": [p2.id_player]})
    assert r.status_code == 200, r.text
    assert {p["id_player"] for p in r.json()["players"]} == {p2.id_player}


# ── Takeover VOD : son au déclenchement + ducking + PoP + audit (2026-07-19) ───


@pytest.mark.asyncio
async def test_play_now_default_keeps_sound(client, db_session, org, auth, monkeypatch):
    """Sans `with_sound`, le payload garde le son (comportement historique préservé)."""
    import app.services.media as media_svc
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [vid.id_media], "id_players": [p1.id_player]})
    id_vod = r.json()["id_vod"]
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": vid.id_media, "mode": "once"})
    assert r.status_code == 200, r.text
    cmd = next(m for m in _FakeRedis.published[f"signflow:player:{p1.id_player}:cmds"]
               if m.get("command") == "play_now")
    assert cmd["params"]["with_sound"] is True


@pytest.mark.asyncio
async def test_play_now_muted_on_demand(client, db_session, org, auth, monkeypatch):
    """`with_sound=false` descend au player : c'est lui qui décide de muter le <video>."""
    import app.services.media as media_svc
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [vid.id_media], "id_players": [p1.id_player]})
    id_vod = r.json()["id_vod"]
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": vid.id_media, "mode": "once", "with_sound": False})
    assert r.status_code == 200, r.text
    cmd = next(m for m in _FakeRedis.published[f"signflow:player:{p1.id_player}:cmds"]
               if m.get("command") == "play_now")
    assert cmd["params"]["with_sound"] is False


@pytest.mark.asyncio
async def test_play_now_has_audio_flag(client, db_session, org, auth, monkeypatch):
    """`has_audio` reflète la metadata : un média SANS piste audio ne doit pas faire
    baisser la musique (sinon musique baissée + silence = vide à l'antenne)."""
    import app.services.media as media_svc
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    muet = await _media(db_session, org, "silencieux.mp4", "video")
    muet.media_metadata = {"container": "mp4", "video": {"codec": "h264"}}   # pas de clé 'audio'
    sonore = await _media(db_session, org, "sonore.mp4", "video")
    sonore.media_metadata = {"container": "mp4", "audio": {"codec": "aac"}}
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "id_medias": [muet.id_media, sonore.id_media],
        "id_players": [p1.id_player]})
    id_vod = r.json()["id_vod"]

    for media, expected in ((muet, False), (sonore, True)):
        _FakeRedis.published = {}
        r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                              json={"id_media": media.id_media, "mode": "once"})
        assert r.status_code == 200, r.text
        cmd = next(m for m in _FakeRedis.published[f"signflow:player:{p1.id_player}:cmds"]
                   if m.get("command") == "play_now")
        assert cmd["params"]["has_audio"] is expected, media.media_name


@pytest.mark.asyncio
async def test_play_now_writes_audit(client, db_session, org, auth, monkeypatch):
    """Le déclenchement VOD est AUDITÉ (il ne l'était que sur la branche HDMI) : sans ça,
    une diffusion ciblant un site entier ne laissait aucune trace serveur."""
    import app.services.media as media_svc
    from app.models.audit_log import CmsAuditLog
    _FakeRedis.published = {}
    monkeypatch.setattr(vod_module.aioredis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(media_svc, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    p1 = await _player(db_session, org, "a", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    await db_session.commit()

    r = await client.post("/api/v1/vod", headers=_h(auth), json={
        "vod_name": "X", "target_mode": "site", "id_site": site.id_site,
        "id_medias": [vid.id_media]})
    id_vod = r.json()["id_vod"]
    r = await client.post(f"/api/v1/vod/{id_vod}/play", headers=_h(auth),
                          json={"id_media": vid.id_media, "mode": "loop", "with_sound": False})
    assert r.status_code == 200, r.text

    row = (await db_session.execute(select(CmsAuditLog).where(
        CmsAuditLog.action == "vod_play_now", CmsAuditLog.id_org == org.id_org
    ))).scalars().first()
    assert row is not None
    assert row.detail["id_media"] == vid.id_media
    assert row.detail["mode"] == "loop"
    assert row.detail["with_sound"] is False
    assert row.detail["players"] == [p1.id_player]


@pytest.mark.asyncio
async def test_build_vod_media_exposes_has_audio(db_session, org, monkeypatch):
    """Le panneau LAN local reçoit `has_audio` : sans lui, un déclenchement LOCAL ferait
    baisser la musique même pour une vidéo muette (le player ne peut pas le deviner)."""
    import app.api.v1.endpoints.ws as ws
    monkeypatch.setattr(ws, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "A")
    player = await _player(db_session, org, "p", id_site=site.id_site)
    muet = await _media(db_session, org, "silencieux.mp4", "video")
    muet.media_metadata = {"container": "mp4", "video": {"codec": "h264"}}
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, target_mode="site", vod_name="V")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=muet.id_media))
    await db_session.commit()

    out = await ws._build_vod_media(db_session, player.id_player)
    assert len(out) == 1
    assert out[0]["has_audio"] is False


# ── Panneau de contrôle local : configs PILOTABLES vs médias JOUABLES (2026-07-19) ──


@pytest.mark.asyncio
async def test_vod_configs_scoped_per_config(db_session, org, monkeypatch):
    """Le panneau local reçoit les configs du SITE, chacune avec SA cible résolue —
    plus une soupe de tous les médias + tous les players (bug vu par Ed sur le NUC)."""
    import app.api.v1.endpoints.ws as ws
    monkeypatch.setattr(ws, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "Domicile")
    nuc = await _player(db_session, org, "NUC", id_site=site.id_site)
    rpi = await _player(db_session, org, "RPi", id_site=site.id_site)
    autre = await _player(db_session, org, "Autre", id_site=site.id_site)
    m_large = await _media(db_session, org, "large.mp4", "video")
    m_twingo = await _media(db_session, org, "twingo.mp4", "video")

    # Config A : tout le site. Config B : seulement NUC + RPi.
    a = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, target_mode="site", vod_name="A_Site")
    b = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="B_Twingo")
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=a.id_vod, id_media=m_large.id_media))
    db_session.add(CmsVodConfigMedia(id_vod=b.id_vod, id_media=m_twingo.id_media))
    db_session.add(CmsVodConfigPlayer(id_vod=b.id_vod, id_player=nuc.id_player))
    db_session.add(CmsVodConfigPlayer(id_vod=b.id_vod, id_player=rpi.id_player))
    await db_session.commit()

    cfgs = await ws._build_vod_configs(db_session, nuc)
    by_name = {c["vod_name"]: c for c in cfgs}
    assert set(by_name) == {"A_Site", "B_Twingo"}

    # Chaque config porte SES médias, pas ceux des autres.
    assert [m["id_media"] for m in by_name["A_Site"]["medias"]] == [m_large.id_media]
    assert [m["id_media"] for m in by_name["B_Twingo"]["medias"]] == [m_twingo.id_media]

    # Et SA cible résolue : B ne doit PAS lister « Autre » (c'est le bug d'affichage constaté).
    assert {p["id_player"] for p in by_name["A_Site"]["players"]} == {
        nuc.id_player, rpi.id_player, autre.id_player}
    assert {p["id_player"] for p in by_name["B_Twingo"]["players"]} == {
        nuc.id_player, rpi.id_player}


@pytest.mark.asyncio
async def test_control_player_pilots_without_being_targeted(db_session, org, monkeypatch):
    """Un player de CONTRÔLE non ciblé pilote quand même : il voit les configs du site et
    reçoit le secret LAN — mais ne peut RIEN jouer lui-même (vod_media vide)."""
    import app.api.v1.endpoints.ws as ws
    monkeypatch.setattr(ws, "get_signed_url", lambda key, ttl=86400: f"https://m/{key}")
    site = await _site(db_session, org, "Domicile")
    controle = await _player(db_session, org, "NUC-controle", id_site=site.id_site)
    cible = await _player(db_session, org, "Ecran", id_site=site.id_site)
    vid = await _media(db_session, org, "promo.mp4", "video")
    cfg = CmsVodConfig(id_org=org.id_org, id_site=site.id_site, vod_name="Sans le NUC")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add(CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=vid.id_media))
    db_session.add(CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=cible.id_player))
    await db_session.commit()

    # Il PILOTE : la config apparaît, avec sa cible (qui ne le contient pas).
    cfgs = await ws._build_vod_configs(db_session, controle)
    assert len(cfgs) == 1
    assert {p["id_player"] for p in cfgs[0]["players"]} == {cible.id_player}
    # Il a le secret LAN (sinon il ne pourrait rien émettre).
    assert (await ws._build_vod_lan(db_session, controle.id_player)) is not None
    # Mais il ne JOUE rien : aucun média pré-caché pour lui.
    assert await ws._build_vod_media(db_session, controle.id_player) == []


@pytest.mark.asyncio
async def test_no_vod_on_site_means_no_lan_secret(db_session, org):
    """Site sans aucune config VOD → pas de secret LAN (rien à piloter)."""
    import app.api.v1.endpoints.ws as ws
    site = await _site(db_session, org, "Vide")
    p = await _player(db_session, org, "seul", id_site=site.id_site)
    await db_session.commit()
    assert (await ws._build_vod_configs(db_session, p)) == []
    assert (await ws._build_vod_lan(db_session, p.id_player)) is None
