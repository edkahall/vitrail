"""Mises à jour contrôlées de l'OS (issue vitrail#6) — piste `os`, manifeste de banc, gardes.

Ce que ces tests verrouillent :
- un manifeste OS ne sort JAMAIS dans `update` ni `engine_update` (clé `os_update` seule), et sa
  directive porte la liste exacte des paquets ;
- un manifeste ne cible que les players de SA famille d'OS ; le déploiement s'arrête au premier
  échec ;
- le succès exige que le player remonte CE manifeste comme appliqué ;
- un relevé de banc n'entre au catalogue que s'il répond à une demande de l'exploitant, et sa
  liste de paquets suit la même grammaire stricte que le script root ;
- seul l'exploitant met à jour un banc, approuve ou supprime un manifeste ; un admin d'org
  déploie un manifeste approuvé sur ses écrans, une bêta sur un seul player ;
- une installation OS dispose de 3 h avant le délai d'échec (30 min pour les autres pistes).
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.player_version import CmsPlayerUpdateVersion, CmsUpdateDeployment
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import os_updates, ota
from app.services.targeting import Target

DEB12 = "debian-12-amd64"
PI13 = "raspios-13-arm64"
PKGS = [{"name": "libssl3", "arch": "amd64", "version": "3.0.15-1~deb12u1"},
        {"name": "tzdata", "arch": "all", "version": "2025b-0+deb12u1"}]


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="OsOrg", org_slug=f"os-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


async def _user_with_role(db: AsyncSession, org: CmsOrganization, role_key: str) -> CmsUser:
    user = CmsUser(id_org=org.id_org, email=f"{role_key.lower()}-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db.add(user)
    await db.flush()
    await db.execute(
        pg_insert(CmsRole).values(role_key=role_key, label=role_key, is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key"))
    await db.flush()
    role = (await db.execute(select(CmsRole).where(CmsRole.role_key == role_key))).scalar_one()
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    return user


@pytest_asyncio.fixture
async def super_admin(db_session: AsyncSession, org: CmsOrganization) -> tuple[CmsUser, str]:
    u = await _user_with_role(db_session, org, "CMS_SUPER_ADMIN")
    return u, create_access_token(
        u.id_user, extra={"org": org.id_org, "roles": ["CMS_SUPER_ADMIN"], "locale": "en"})


@pytest_asyncio.fixture
async def org_admin_token(db_session: AsyncSession, org: CmsOrganization, super_admin) -> str:
    """Admin d'org sur un serveur qui A un super-admin actif ⇒ PAS exploitant du serveur."""
    u = await _user_with_role(db_session, org, "CMS_ORG_ADMIN")
    return create_access_token(
        u.id_user, extra={"org": org.id_org, "roles": ["CMS_ORG_ADMIN"], "locale": "en"})


@pytest_asyncio.fixture(autouse=True)
async def _clean_ota_globals(db_session: AsyncSession):
    yield
    for tbl in ("cms_update_deployment_targets", "cms_update_deployments",
                "cms_player_update_versions"):
        await db_session.execute(text(f"DELETE FROM {tbl}"))
    await db_session.commit()


def _player(org: CmsOrganization, family: str | None = DEB12, name: str = "NUC",
            helper: bool = True, status: str = "online", applied: str | None = None) -> CmsPlayer:
    caps: dict = {"electron_version": "42.11.18"}
    if family is not None:
        caps["os_update"] = {"helper": helper, "family": family, "pending": 3, "security": 1,
                             "checked_at": "2026-11-15T10:00:00Z", "applied": applied}
    return CmsPlayer(id_org=org.id_org, player_name=name, player_os="linux-x64",
                     player_version="1.5.28", status=status, capabilities=caps)


def _manifest(family: str = DEB12, channel: str = "stable",
              version: str = "20261115-100000") -> CmsPlayerUpdateVersion:
    return CmsPlayerUpdateVersion(track="os", version=version, channel=channel,
                                  model_family=family, manifest={"packages": PKGS})


def _pg_conn():
    import psycopg2

    from tests.conftest import TEST_DATABASE_URL
    return psycopg2.connect(TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))


async def _dep_status(db: AsyncSession, id_deployment: int) -> str:
    db.expire_all()
    return (await db.execute(select(CmsUpdateDeployment.status).where(
        CmsUpdateDeployment.id_deployment == id_deployment))).scalar_one()


# ── 1. Séparation des pistes ─────────────────────────────────────────────────


async def test_os_directive_only_in_its_own_key_and_carries_the_packages(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org)
    ver = _manifest()
    db_session.add_all([p, ver])
    await db_session.commit()
    await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                [Target(target_type="org")], "immediate", {}, user.id_user)

    assert await ota.directive_for_player(db_session, p.id_player) is None
    assert await ota.directive_for_player(db_session, p.id_player, track=ota.ENGINE_TRACK) is None
    d = await ota.directive_for_player(db_session, p.id_player, track=ota.OS_TRACK)
    assert d and d["track"] == "os" and d["version"] == "20261115-100000"
    assert d["packages"] == ["libssl3:amd64=3.0.15-1~deb12u1", "tzdata:all=2025b-0+deb12u1"]


async def test_os_success_on_heartbeat_version_is_impossible(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    """La version du CODE remontée au heartbeat ne dit rien de l'OS."""
    user, _ = super_admin
    p = _player(org)
    ver = _manifest()
    db_session.add_all([p, ver])
    await db_session.commit()
    pid = p.id_player
    await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                [Target(target_type="org")], "immediate", {}, user.id_user)
    assert await ota.mark_success_if_updated(db_session, pid, "99.0.0") == []


# ── 2. Ciblage et arrêt au premier échec ─────────────────────────────────────


async def test_manifest_targets_only_its_os_family(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    nuc = _player(org, DEB12, "NUC")
    pi = _player(org, PI13, "RPi")
    old = _player(org, None, "Old player")   # ne remonte pas d'état OS
    ver = _manifest(DEB12)
    db_session.add_all([nuc, pi, old, ver])
    await db_session.commit()
    nuc_id = nuc.id_player
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    targets = await ota.deployment_targets(db_session, dep.id_deployment)
    assert [t.id_player for t in targets] == [nuc_id]
    assert dep.strategy_params.get("stop_on_failure") is True


async def test_os_deployment_pauses_at_first_failure(
    db_session: AsyncSession, org: CmsOrganization, super_admin, monkeypatch,
) -> None:
    from app.workers import ota_worker

    monkeypatch.setattr(ota_worker.notifications, "dispatch_sync", lambda *a, **k: None)
    user, _ = super_admin
    players = [_player(org, name=f"NUC{i}") for i in range(3)]
    ver = _manifest()
    db_session.add_all([*players, ver])
    await db_session.commit()
    a, b, _c = (pl.id_player for pl in players)
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    dep_id = dep.id_deployment
    await ota.report_target(db_session, a, dep_id, "failed", "obsolete: version not found")
    await db_session.commit()
    conn = _pg_conn()
    try:
        ota_worker._rollup(conn, dep_id)
    finally:
        conn.close()
    assert await _dep_status(db_session, dep_id) == "paused"
    assert await ota.directive_for_player(db_session, b, track=ota.OS_TRACK) is None


# ── 3. Succès : le player doit remonter CE manifeste ─────────────────────────


async def test_os_success_needs_the_applied_manifest(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org, applied="20261001-090000")
    ver = _manifest(version="20261115-100000")
    db_session.add_all([p, ver])
    await db_session.commit()
    pid = p.id_player
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    dep_id = dep.id_deployment
    assert (await ota.deployment_targets(db_session, dep_id))[0].previous_version == "20261001-090000"
    # Ancien manifeste encore remonté : `success` ignoré (le player le redira).
    assert not await ota.report_target(db_session, pid, dep_id, "success")
    caps = dict(p.capabilities)
    caps["os_update"] = {**caps["os_update"], "applied": "20261115-100000"}
    p.capabilities = caps
    await db_session.commit()
    assert await ota.report_target(db_session, pid, dep_id, "success")
    assert (await ota.deployment_targets(db_session, dep_id))[0].status == "success"


async def test_parc_shows_the_os_state(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    _, token = super_admin
    db_session.add_all([_player(org, name="NUC"), _player(org, None, name="Android")])
    await db_session.commit()
    r = await client.get("/api/v1/updates/parc", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    rows = {p["player_name"]: p for p in r.json()["players"]}
    assert rows["NUC"]["os_update"]["family"] == DEB12
    assert rows["NUC"]["os_update"]["pending"] == 3
    assert rows["NUC"]["os_update"]["security"] == 1
    assert rows["Android"]["os_update"] is None


# ── 4. Manifeste : forme stricte, demande obligatoire ─────────────────────────


@pytest.mark.parametrize("bad", [
    [{"name": "libssl3; rm -rf /", "arch": "amd64", "version": "1"}],
    [{"name": "libssl3", "arch": "amd64", "version": "1 2"}],
    [{"name": "libssl3", "arch": "", "version": "1"}],
    [{"name": "Libssl3", "arch": "amd64", "version": "1"}],
    [{"name": "libssl3", "arch": "amd64", "version": "1=2"}],
    [PKGS[0], PKGS[0]],
    "libssl3",
    [["libssl3", "amd64", "1"]],
])
def test_manifest_packages_follow_the_helper_grammar(bad) -> None:
    with pytest.raises(ota.OsManifestRejected):
        ota.validate_os_packages(bad)


def test_manifest_packages_are_normalised_and_sorted() -> None:
    out = ota.validate_os_packages(list(reversed(PKGS)) + [{"name": "a+b.c-d", "arch": "arm64",
                                                            "version": "1:2.3~rc1+deb12u1"}])
    assert [p["name"] for p in out] == ["a+b.c-d", "libssl3", "tzdata"]


async def test_register_manifest_creates_a_beta_version(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org)
    db_session.add(p)
    await db_session.commit()
    row = await ota.register_os_manifest(db_session, p.id_player, DEB12, PKGS, True, user.id_user)
    await db_session.commit()
    assert row.track == "os" and row.channel == "beta" and row.model_family == DEB12
    assert row.manifest["source_player"] == p.id_player and row.manifest["reboot_required"] is True
    assert len(row.manifest["packages"]) == 2
    # Rien n'a changé sur le banc : rien à approuver.
    assert await ota.register_os_manifest(db_session, p.id_player, DEB12, [], False, None) is None
    with pytest.raises(ota.OsManifestRejected):
        await ota.register_os_manifest(db_session, p.id_player, "Debian 12; x", PKGS, False, None)


class _FakeRedis:
    store: dict = {}
    published: list = []

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    async def get(self, k):
        return self.store.get(k)

    async def delete(self, k):
        return 1 if self.store.pop(k, None) is not None else 0

    async def publish(self, ch, msg):
        self.published.append((ch, json.loads(msg)))

    async def aclose(self):
        pass


async def test_bench_manifest_is_accepted_only_for_a_pending_request(monkeypatch) -> None:
    fake = _FakeRedis()
    fake.store, fake.published = {}, []
    monkeypatch.setattr(os_updates.aioredis, "from_url", lambda *_a, **_k: fake)
    # Aucune demande : refusé.
    assert await os_updates.take_bench(7, "abc") is None
    rid = await os_updates.request_bench(7, id_user=42)
    assert fake.published == [("signflow:player:7:cmds", {
        "type": "player:command", "command": "os_bench", "params": {"request_id": rid}})]
    # Mauvais player, mauvais identifiant : refusé, et la demande reste.
    assert await os_updates.take_bench(8, rid) is None
    assert await os_updates.take_bench(7, "nope") is None
    assert await os_updates.take_bench(7, "") is None
    # La bonne : acceptée UNE fois.
    assert (await os_updates.take_bench(7, rid))["id_user"] == 42
    assert await os_updates.take_bench(7, rid) is None


# ── 5. Gardes : l'exploitant du serveur ──────────────────────────────────────


async def test_only_the_operator_updates_a_bench(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
    org_admin_token: str, monkeypatch,
) -> None:
    sent: list = []

    async def fake_bench(id_player, id_user):
        sent.append(id_player)
        return "rid"

    monkeypatch.setattr(os_updates, "request_bench", fake_bench)
    ok = _player(org, name="Bench")
    no_helper = _player(org, name="NoHelper", helper=False)
    offline = _player(org, name="Off", status="offline")
    db_session.add_all([ok, no_helper, offline])
    await db_session.commit()
    _, op_token = super_admin

    def bench(token, pid):
        return client.post("/api/v1/updates/os/bench", json={"id_player": pid},
                           headers={"Authorization": f"Bearer {token}"})

    assert (await bench(org_admin_token, ok.id_player)).status_code == 403
    r = await bench(op_token, no_helper.id_player)
    assert r.status_code == 409 and "install-os-update.sh" in r.json()["detail"]
    assert (await bench(op_token, offline.id_player)).status_code == 409
    assert (await bench(op_token, 999999)).status_code == 404
    r = await bench(op_token, ok.id_player)
    assert r.status_code == 202 and r.json() == {"request_id": "rid"}
    assert sent == [ok.id_player]


async def test_org_admin_cannot_approve_or_delete_a_manifest(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
    org_admin_token: str,
) -> None:
    ver = _manifest(channel="beta")
    db_session.add(ver)
    await db_session.commit()
    vid = ver.id_version
    h = {"Authorization": f"Bearer {org_admin_token}"}
    assert (await client.post(f"/api/v1/updates/versions/{vid}/promote", headers=h)).status_code == 403
    assert (await client.delete(f"/api/v1/updates/versions/{vid}", headers=h)).status_code == 403
    _, op = super_admin
    r = await client.post(f"/api/v1/updates/versions/{vid}/promote",
                          headers={"Authorization": f"Bearer {op}"})
    assert r.status_code == 200 and r.json()["channel"] == "stable"
    assert r.json()["manifest"]["packages"][0]["name"] == "libssl3"


async def test_org_admin_deploys_approved_anywhere_but_beta_only_to_one_player(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, org_admin_token: str,
) -> None:
    p = _player(org)
    beta = _manifest(channel="beta", version="20261115-110000")
    stable = _manifest(channel="stable", version="20261115-100000")
    db_session.add_all([p, beta, stable])
    await db_session.commit()
    h = {"Authorization": f"Bearer {org_admin_token}"}

    def dep(ver, targets):
        return client.post("/api/v1/updates/deployments", headers=h, json={
            "id_version": ver.id_version, "strategy": "immediate", "strategy_params": {},
            "targets": targets})

    r = await dep(beta, [{"target_type": "org"}])
    assert r.status_code == 403 and "OS manifest" in r.json()["detail"]
    r = await dep(stable, [{"target_type": "org"}])
    assert r.status_code == 201, r.text
    # Une seule mise à jour OS à la fois par player.
    r = await dep(beta, [{"target_type": "player", "target_id": p.id_player}])
    assert r.status_code == 409, r.text


# ── 6. Délai d'échec : 3 h pour l'OS ─────────────────────────────────────────


async def test_os_install_gets_three_hours_before_timeout(
    db_session: AsyncSession, org: CmsOrganization, super_admin, monkeypatch,
) -> None:
    from app.workers import ota_worker

    monkeypatch.setattr(ota_worker.notifications, "dispatch_sync", lambda *a, **k: None)
    monkeypatch.setattr(ota_worker, "_db_conn", _pg_conn)
    user, _ = super_admin
    nuc = _player(org, name="NUC")
    code_player = _player(org, name="Code")
    os_ver = _manifest()
    code_ver = CmsPlayerUpdateVersion(track="player", version="1.5.29", model_family="linux",
                                      download_url="/x", sha256_checksum="a" * 64)
    db_session.add_all([nuc, code_player, os_ver, code_ver])
    await db_session.commit()
    nuc_id, code_id = nuc.id_player, code_player.id_player
    d_os = await ota.create_deployment(db_session, org.id_org, os_ver.id_version,
                                       [Target(target_type="player", target_id=nuc_id)],
                                       "immediate", {}, user.id_user)
    d_code = await ota.create_deployment(db_session, org.id_org, code_ver.id_version,
                                         [Target(target_type="player", target_id=code_id)],
                                         "immediate", {}, user.id_user)
    os_dep, code_dep = d_os.id_deployment, d_code.id_deployment
    for dep_id in (os_dep, code_dep):
        await db_session.execute(text(
            "UPDATE cms_update_deployment_targets SET status='installing', "
            "started_at = now() - interval '1 hour' WHERE id_deployment = :d"), {"d": dep_id})
    await db_session.commit()
    ota_worker.ota_timeout()
    db_session.expire_all()
    os_t = (await ota.deployment_targets(db_session, os_dep))[0]
    code_t = (await ota.deployment_targets(db_session, code_dep))[0]
    assert os_t.status == "installing"
    assert code_t.status == "failed"



# ── 7. Vagues + revue indépendante du 15/11 ──────────────────────────────────


async def _served(db: AsyncSession, pids: list[int], track: str = "os") -> list[int]:
    db.expire_all()
    return [pid for pid in pids if await ota.directive_for_player(db, pid, track=track)]


async def _set_applied(db: AsyncSession, pid: int, version: str) -> None:
    caps = (await db.execute(select(CmsPlayer.capabilities).where(CmsPlayer.id_player == pid))).scalar_one()
    caps = dict(caps)
    caps["os_update"] = {**caps["os_update"], "applied": version}
    await db.execute(text("UPDATE cms_players SET capabilities = CAST(:c AS jsonb) WHERE id_player = :p"),
                     {"c": json.dumps(caps), "p": pid})
    await db.commit()


async def test_os_deployment_goes_one_player_at_a_time_by_default(
    db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    """Sans vagues, « arrêt au premier échec » ne protégeait personne : tout le parc recevait la
    directive à la même minute. Défaut = 1 player à la fois, le suivant après la fin du précédent."""
    user, _ = super_admin
    offline = _player(org, name="Off", status="offline")
    db_session.add(offline)
    await db_session.commit()
    players = [_player(org, name=f"NUC{i}") for i in range(3)]
    ver = _manifest()
    db_session.add_all([*players, ver])
    await db_session.commit()
    a, b, c = (pl.id_player for pl in players)
    off = offline.id_player
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    dep_id = dep.id_deployment
    assert dep.strategy_params["wave_size"] == 1
    # Le hors-ligne (première cible) passe son tour : il ne bloque pas la vague.
    assert await _served(db_session, [off, a, b, c]) == [a]
    await ota.report_target(db_session, a, dep_id, "installing")
    await db_session.commit()
    assert await _served(db_session, [a, b, c]) == [a]          # a continue, b attend
    await _set_applied(db_session, a, "20261115-100000")
    assert await ota.report_target(db_session, a, dep_id, "success")
    await db_session.commit()
    assert await _served(db_session, [a, b, c]) == [b]          # au tour de b


async def test_wave_size_is_clamped_and_honoured(
    db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    user, _ = super_admin
    players = [_player(org, name=f"N{i}") for i in range(3)]
    ver = _manifest()
    db_session.add_all([*players, ver])
    await db_session.commit()
    pids = [pl.id_player for pl in players]
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate",
                                      {"wave_size": 2}, user.id_user)
    assert dep.strategy_params["wave_size"] == 2
    assert await _served(db_session, pids) == pids[:2]
    assert ota.clamp_wave("x") == 1 and ota.clamp_wave(0) == 1 and ota.clamp_wave(999) == ota.WAVE_MAX


async def test_engine_deployment_also_goes_in_waves(
    db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    user, _ = super_admin
    players = [CmsPlayer(id_org=org.id_org, player_name=f"R{i}", player_os="linux-arm64",
                         player_version="1.6.22", status="online",
                         capabilities={"electron_version": "42.11.18"}) for i in range(2)]
    ver = CmsPlayerUpdateVersion(track="engine", version="42.11.17", channel="stable",
                                 model_family="linux-arm64", download_url="/x", sha256_checksum="e" * 64)
    db_session.add_all([*players, ver])
    await db_session.commit()
    pids = [pl.id_player for pl in players]
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    assert dep.strategy_params["wave_size"] == 1
    assert await _served(db_session, pids, track="engine") == pids[:1]


async def test_code_deployment_is_not_limited_by_waves(
    db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    """Non-régression : la piste du CODE garde son comportement."""
    user, _ = super_admin
    players = [_player(org, name=f"C{i}") for i in range(2)]
    ver = CmsPlayerUpdateVersion(track="player", version="1.5.29", model_family="linux",
                                 download_url="/x", sha256_checksum="a" * 64)
    db_session.add_all([*players, ver])
    await db_session.commit()
    pids = [pl.id_player for pl in players]
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    assert "wave_size" not in dep.strategy_params
    db_session.expire_all()
    assert all([await ota.directive_for_player(db_session, p) for p in pids])


async def test_next_wave_is_nudged_after_a_transition(
    db_session: AsyncSession, org: CmsOrganization, super_admin, monkeypatch,
) -> None:
    from app.workers import ota_worker

    sent: list = []
    monkeypatch.setattr(ota_worker, "_publish_force_sync", lambda ids: sent.extend(ids))
    user, _ = super_admin
    players = [_player(org, name=f"W{i}") for i in range(2)]
    ver = _manifest()
    db_session.add_all([*players, ver])
    await db_session.commit()
    a, b = (pl.id_player for pl in players)
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    dep_id = dep.id_deployment
    await ota.report_target(db_session, a, dep_id, "installing")
    await db_session.commit()
    conn = _pg_conn()
    try:
        ota_worker._nudge_next_wave(conn, dep_id)
    finally:
        conn.close()
    assert sent == [b]


def test_manifest_rejects_a_trailing_newline() -> None:
    with pytest.raises(ota.OsManifestRejected):
        ota.validate_os_packages([{"name": "libssl3", "arch": "amd64", "version": "1.0\n"}])


async def test_bench_cannot_label_its_manifest_for_another_family(
    db_session: AsyncSession, org: CmsOrganization,
) -> None:
    p = _player(org, family=DEB12)
    db_session.add(p)
    await db_session.commit()
    with pytest.raises(ota.OsManifestRejected):
        await ota.register_os_manifest(db_session, p.id_player, PI13, PKGS, False, None)


async def test_second_bench_request_is_refused_while_one_is_pending(monkeypatch) -> None:
    fake = _FakeRedis()
    fake.store, fake.published = {}, []
    monkeypatch.setattr(os_updates.aioredis, "from_url", lambda *_a, **_k: fake)
    first = await os_updates.request_bench(7, id_user=1)
    with pytest.raises(os_updates.BenchPending):
        await os_updates.request_bench(7, id_user=1)
    assert (await os_updates.take_bench(7, first))["id_user"] == 1


async def test_org_admin_does_not_see_who_produced_a_manifest(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
    org_admin_token: str,
) -> None:
    ver = _manifest()
    ver.manifest = {"packages": PKGS, "source_player": 4, "requested_by": 1}
    db_session.add(ver)
    await db_session.commit()
    r = await client.get("/api/v1/updates/versions?track=os",
                         headers={"Authorization": f"Bearer {org_admin_token}"})
    assert r.status_code == 200, r.text
    m = r.json()[0]["manifest"]
    assert "source_player" not in m and "requested_by" not in m and len(m["packages"]) == 2
    _, op = super_admin
    r = await client.get("/api/v1/updates/versions?track=os", headers={"Authorization": f"Bearer {op}"})
    assert r.json()[0]["manifest"]["source_player"] == 4
