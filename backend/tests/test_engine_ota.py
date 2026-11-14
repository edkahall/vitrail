"""OTA du moteur Electron (issue vitrail#5) — piste `engine`, récupération officielle, gardes.

Ce que ces tests verrouillent :
- une version moteur ne sort JAMAIS dans la clé historique `update` (un player actuel la
  prendrait pour un bundle de code et extrairait le moteur par-dessus son application) ;
- le succès est constaté par ÉGALITÉ de la version d'Electron remontée (une descente compte) ;
- `rolled_back` est un statut remontable et terminal ; un déploiement moteur s'ARRÊTE au premier
  échec, et la reprise ne s'arrête plus que sur un échec NOUVEAU ;
- seul l'exploitant du serveur récupère, promeut, supprime ou déploie un moteur ;
- le moteur est vérifié contre SHASUMS256.txt, reconditionné avec ses droits d'exécution, et
  téléchargé uniquement depuis les hôtes autorisés, redirections comprises.
"""
from __future__ import annotations

import io
import os
import tarfile
import uuid
import zipfile

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_player_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.player_version import (
    CmsPlayerUpdateVersion,
    CmsUpdateDeployment,
    CmsUpdateDeploymentTarget,
)
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import engine_import, ota
from app.services.targeting import Target

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="EngOrg", org_slug=f"eng-{uuid.uuid4().hex[:8]}")
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


def _player(org: CmsOrganization, os_: str = "linux-arm64", electron: str | None = "42.11.18",
            name: str = "RPi") -> CmsPlayer:
    caps = {"electron_version": electron} if electron else {}
    return CmsPlayer(id_org=org.id_org, player_name=name, player_os=os_,
                     player_version="1.6.22", status="online", capabilities=caps)


def _engine_version(version: str = "42.11.17", family: str = "linux-arm64",
                    channel: str = "beta") -> CmsPlayerUpdateVersion:
    return CmsPlayerUpdateVersion(
        track="engine", version=version, channel=channel, model_family=family,
        download_url=engine_import.download_url(family, version),
        sha256_checksum="e" * 64, storage_key=engine_import.storage_key(family, version))


# ── 1. Séparation des pistes : le moteur ne sort jamais dans `update` ─────────


async def test_engine_directive_never_in_legacy_update_key(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org)
    ver = _engine_version()
    db_session.add_all([p, ver])
    await db_session.commit()
    await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                [Target(target_type="org")], "immediate", {}, user.id_user)

    # La clé historique (piste player + firmware) ne voit RIEN.
    assert await ota.directive_for_player(db_session, p.id_player) is None
    # La clé moteur porte la directive.
    d = await ota.directive_for_player(db_session, p.id_player, track=ota.ENGINE_TRACK)
    assert d and d["track"] == "engine" and d["version"] == "42.11.17"
    assert d["download_url"] == "/api/v1/updates/engines/linux-arm64/42.11.17"


async def test_player_directive_never_in_engine_key(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org)
    ver = CmsPlayerUpdateVersion(track="player", version="1.6.23", model_family="rpi",
                                 download_url="/api/v1/updates/bundles/rpi/1.6.23",
                                 sha256_checksum="a" * 64)
    db_session.add_all([p, ver])
    await db_session.commit()
    await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                [Target(target_type="org")], "immediate", {}, user.id_user)
    assert await ota.directive_for_player(db_session, p.id_player, track=ota.ENGINE_TRACK) is None
    assert (await ota.directive_for_player(db_session, p.id_player))["version"] == "1.6.23"


async def test_engine_targets_only_their_platform_and_record_current_electron(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    rpi = _player(org, "linux-arm64", "42.11.18", "RPi")
    nuc = _player(org, "linux-x64", "42.11.18", "NUC")
    win = _player(org, "windows", "42.11.18", "AOpen")
    ver = _engine_version(family="linux-arm64")
    db_session.add_all([rpi, nuc, win, ver])
    await db_session.commit()
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    tgts = await ota.deployment_targets(db_session, dep.id_deployment)
    assert [t.id_player for t in tgts] == [rpi.id_player]
    # Base du retour arrière = l'Electron courant, pas la version du code (1.6.22).
    assert tgts[0].previous_version == "42.11.18"
    # D3 : arrêt au premier échec posé d'office pour un moteur…
    assert dep.strategy_params.get("stop_on_failure") is True


async def test_engine_stop_on_failure_can_be_refused_explicitly(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    ver = _engine_version()
    db_session.add_all([_player(org), ver])
    await db_session.commit()
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate",
                                      {"stop_on_failure": False}, user.id_user)
    assert dep.strategy_params["stop_on_failure"] is False


# ── 2. Succès constaté par la version d'Electron ─────────────────────────────


async def test_engine_success_is_exact_electron_version(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org)
    ver = _engine_version("42.11.17")   # DESCENTE depuis 42.11.18
    db_session.add_all([p, ver])
    await db_session.commit()
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)

    # La version du CODE ne dit rien du moteur.
    assert await ota.mark_success_if_updated(db_session, p.id_player, "99.0.0") == []
    # Toujours l'ancien moteur : pas de succès.
    assert await ota.mark_engine_success(db_session, p.id_player, "42.11.18") == []
    # Plus haut que la cible : ce n'est PAS la cible.
    assert await ota.mark_engine_success(db_session, p.id_player, "43.0.0") == []
    assert await ota.mark_engine_success(db_session, p.id_player, None) == []
    # Exactement la cible (une descente) : succès.
    assert await ota.mark_engine_success(db_session, p.id_player, "42.11.17") == [dep.id_deployment]
    tgt = (await ota.deployment_targets(db_session, dep.id_deployment))[0]
    assert tgt.status == "success"


async def test_engine_version_of_reads_capabilities() -> None:
    assert ota.engine_version_of({"electron_version": "42.11.18"}) == "42.11.18"
    assert ota.engine_version_of({}) is None
    assert ota.engine_version_of(None) is None
    assert ota.engine_version_of("garbage") is None  # type: ignore[arg-type]


# ── 3. rolled_back + arrêt au premier échec ───────────────────────────────────


async def test_rolled_back_is_reportable_and_terminal(
    db_session: AsyncSession, org: CmsOrganization, super_admin
) -> None:
    user, _ = super_admin
    p = _player(org)
    ver = _engine_version()
    db_session.add_all([p, ver])
    await db_session.commit()
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    assert "rolled_back" in ota.REPORTABLE_STATUSES
    assert await ota.report_target(db_session, p.id_player, dep.id_deployment, "installing")
    assert await ota.report_target(db_session, p.id_player, dep.id_deployment, "rolled_back",
                                   "no render within 300 s")
    tgt = (await ota.deployment_targets(db_session, dep.id_deployment))[0]
    assert tgt.status == "rolled_back"
    assert tgt.completed_at is not None and "300 s" in (tgt.error_message or "")
    # Terminal : plus de directive, et un report tardif est ignoré.
    assert await ota.directive_for_player(db_session, p.id_player, track="engine") is None
    assert not await ota.report_target(db_session, p.id_player, dep.id_deployment, "success")


def test_ws_accepts_the_shared_reportable_statuses() -> None:
    """ws.py doit filtrer `update:status` par la liste partagée (sinon `rolled_back` serait
    jeté en silence, comme avant)."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "v1",
                            "endpoints", "ws.py"), encoding="utf-8").read()
    assert "st not in ota.REPORTABLE_STATUSES" in src
    assert 'payload["engine_update"]' in src
    assert "mark_engine_success" in src


def _pg_conn():
    import psycopg2

    from tests.conftest import TEST_DATABASE_URL
    return psycopg2.connect(TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))


async def _dep_status(db: AsyncSession, id_deployment: int) -> str:
    db.expire_all()
    return (await db.execute(select(CmsUpdateDeployment.status).where(
        CmsUpdateDeployment.id_deployment == id_deployment))).scalar_one()


async def test_engine_deployment_pauses_at_first_failure_then_only_on_new_ones(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
    monkeypatch,
) -> None:
    from app.workers import ota_worker

    monkeypatch.setattr(ota_worker.notifications, "dispatch_sync", lambda *a, **k: None)
    user, token = super_admin
    players = [_player(org, name=f"RPi{i}") for i in range(3)]
    ver = _engine_version()
    db_session.add_all([*players, ver])
    await db_session.commit()
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    a, b, c = (pl.id_player for pl in players)   # relevés AVANT tout expire_all()
    dep_id = dep.id_deployment

    await ota.report_target(db_session, a, dep_id, "rolled_back", "x")
    await db_session.commit()
    conn = _pg_conn()
    try:
        ota_worker._rollup(conn, dep_id)
    finally:
        conn.close()
    assert await _dep_status(db_session, dep_id) == "paused"
    # Suspendu ⇒ plus de directive pour les écrans restants.
    assert await ota.directive_for_player(db_session, b, track="engine") is None

    # Reprise par l'exploitant : l'échec connu est accepté…
    r = await client.post(f"/api/v1/updates/deployments/{dep_id}/resume",
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert await _dep_status(db_session, dep_id) == "in_progress"
    await ota.report_target(db_session, b, dep_id, "downloading")
    await db_session.commit()
    conn = _pg_conn()
    try:
        ota_worker._rollup(conn, dep_id)
    finally:
        conn.close()
    assert await _dep_status(db_session, dep_id) == "in_progress"

    # …mais un échec NOUVEAU arrête de nouveau.
    await ota.report_target(db_session, b, dep_id, "failed", "y")
    await db_session.commit()
    conn = _pg_conn()
    try:
        ota_worker._rollup(conn, dep_id)
    finally:
        conn.close()
    assert await _dep_status(db_session, dep_id) == "paused"
    assert c  # le 3e écran n'a jamais été touché


async def test_player_track_deployment_does_not_pause_on_failure(
    db_session: AsyncSession, org: CmsOrganization, super_admin, monkeypatch,
) -> None:
    """Non-régression : un déploiement de CODE garde son comportement (pas d'arrêt)."""
    from app.workers import ota_worker

    monkeypatch.setattr(ota_worker.notifications, "dispatch_sync", lambda *a, **k: None)
    user, _ = super_admin
    players = [_player(org, name=f"P{i}") for i in range(2)]
    ver = CmsPlayerUpdateVersion(track="player", version="1.6.23", model_family="rpi",
                                 download_url="/x", sha256_checksum="a" * 64)
    db_session.add_all([*players, ver])
    await db_session.commit()
    dep = await ota.create_deployment(db_session, org.id_org, ver.id_version,
                                      [Target(target_type="org")], "immediate", {}, user.id_user)
    await ota.report_target(db_session, players[0].id_player, dep.id_deployment, "failed", "x")
    await db_session.commit()
    conn = _pg_conn()
    try:
        ota_worker._rollup(conn, dep.id_deployment)
    finally:
        conn.close()
    assert await _dep_status(db_session, dep.id_deployment) == "in_progress"


# ── 4. Gardes : l'exploitant du serveur seul ─────────────────────────────────


async def test_org_admin_cannot_fetch_promote_deploy_or_delete_an_engine(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, org_admin_token: str,
) -> None:
    db_session.add(_player(org))
    eng = _engine_version()
    code = CmsPlayerUpdateVersion(track="player", version="1.6.23", channel="beta",
                                  model_family="rpi", download_url="/x")
    db_session.add_all([eng, code])
    await db_session.commit()
    h = {"Authorization": f"Bearer {org_admin_token}"}

    r = await client.post("/api/v1/updates/engines/fetch", headers=h,
                          json={"version": "42.11.17", "families": ["linux-arm64"]})
    assert r.status_code == 403
    assert (await client.post(f"/api/v1/updates/versions/{eng.id_version}/promote",
                              headers=h)).status_code == 403
    assert (await client.post("/api/v1/updates/deployments", headers=h, json={
        "id_version": eng.id_version, "strategy": "immediate", "strategy_params": {},
        "targets": [{"target_type": "org"}]})).status_code == 403
    assert (await client.delete(f"/api/v1/updates/versions/{eng.id_version}",
                                headers=h)).status_code == 403
    # Non-régression : le même admin promeut toujours une version de CODE.
    assert (await client.post(f"/api/v1/updates/versions/{code.id_version}/promote",
                              headers=h)).status_code == 200


async def test_operator_fetch_enqueues_one_job_per_platform(
    client: AsyncClient, super_admin, monkeypatch,
) -> None:
    from app.workers import engine_worker

    sent = []
    monkeypatch.setattr(engine_worker.fetch_engine, "delay", lambda *a: sent.append(a))
    _, token = super_admin
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/updates/engines/fetch", headers=h, json={
        "version": "42.11.17", "families": ["linux-arm64", "windows", "linux-arm64"]})
    assert r.status_code == 202, r.text
    jobs = r.json()
    assert [j["family"] for j in jobs] == ["linux-arm64", "windows"]   # dédoublonné
    assert [a[1:] for a in sent] == [("42.11.17", "linux-arm64"), ("42.11.17", "windows")]
    st = await client.get(f"/api/v1/updates/engines/jobs/{jobs[0]['job_id']}", headers=h)
    assert st.status_code == 200 and st.json()["state"] == "queued"
    assert (await client.get("/api/v1/updates/engines/jobs/nothex", headers=h)).status_code == 404

    for body in ({"version": "42.11", "families": ["windows"]},
                 {"version": "42.11.17", "families": ["darwin-arm64"]},
                 {"version": "42.11.17", "families": []}):
        assert (await client.post("/api/v1/updates/engines/fetch", headers=h,
                                  json=body)).status_code == 400


# ── 5. Service du moteur aux players ─────────────────────────────────────────


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._b = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._b.read(n)

    def close(self) -> None:
        pass


class _FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 — signature boto
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _FakeBody(self.objects[Key]), "ContentLength": len(self.objects[Key])}


async def test_engine_download_is_scoped_to_the_player_platform(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, monkeypatch,
) -> None:
    from app.services import media

    payload = os.urandom(3 * 1024 * 1024 + 17)   # plusieurs morceaux de 1 Mio
    monkeypatch.setattr(media, "_s3", lambda: _FakeS3(
        {engine_import.storage_key("linux-arm64", "42.11.17"): payload}))
    p = _player(org)
    db_session.add(p)
    await db_session.commit()
    token, _ = create_player_token(p.id_player)
    h = {"Authorization": f"Bearer {token}"}

    ok = await client.get("/api/v1/updates/engines/linux-arm64/42.11.17", headers=h)
    assert ok.status_code == 200 and ok.content == payload
    assert ok.headers["content-length"] == str(len(payload))
    assert (await client.get("/api/v1/updates/engines/windows/42.11.17",
                             headers=h)).status_code == 403
    assert (await client.get("/api/v1/updates/engines/darwin/42.11.17",
                             headers=h)).status_code == 404
    assert (await client.get("/api/v1/updates/engines/linux-arm64/42.11.16",
                             headers=h)).status_code == 404
    # Un jeton UTILISATEUR n'ouvre pas ce chemin.
    assert (await client.get("/api/v1/updates/engines/linux-arm64/42.11.17")).status_code in (
        401, 403)


# ── 6. Récupération : somme, droits, hôtes ───────────────────────────────────


def _electron_zip(version: str = "42.11.17", extra: list[tuple[str, int, bytes]] | None = None,
                  with_version: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        def add(name: str, mode: int, data: bytes) -> None:
            zi = zipfile.ZipInfo(name)
            zi.external_attr = mode << 16
            zf.writestr(zi, data)
        add("electron", 0o100755, b"\x7fELF fake")
        add("resources/", 0o040755, b"")
        add("resources/default_app.asar", 0o100644, b"asar")
        add("libffmpeg.so", 0o100755, b"so")
        if with_version:
            add("version", 0o100644, version.encode())
        for name, mode, data in extra or []:
            add(name, mode, data)
    return buf.getvalue()


def test_parse_shasums() -> None:
    sha = "a" * 64
    txt = f"{'b' * 64} *electron-v1.2.3-win32-x64.zip\n{sha} *electron-v1.2.3-linux-arm64.zip\n"
    assert engine_import.parse_shasums(txt, "electron-v1.2.3-linux-arm64.zip") == sha
    assert engine_import.parse_shasums(f"{sha}  x.zip", "x.zip") == sha
    with pytest.raises(engine_import.EngineImportError):
        engine_import.parse_shasums(txt, "electron-v1.2.3-linux-x64.zip")


def test_zip_to_targz_keeps_exec_bits_under_dist(tmp_path) -> None:
    z = tmp_path / "e.zip"
    z.write_bytes(_electron_zip())
    out = tmp_path / "e.tar.gz"
    engine_import.zip_to_targz(str(z), str(out), "42.11.17")
    with tarfile.open(out) as tar:
        m = {ti.name: ti for ti in tar.getmembers()}
    assert set(m) >= {"dist/electron", "dist/libffmpeg.so", "dist/version",
                      "dist/resources", "dist/resources/default_app.asar"}
    assert m["dist/electron"].mode == 0o755 and m["dist/libffmpeg.so"].mode == 0o755
    assert m["dist/resources/default_app.asar"].mode == 0o644
    assert m["dist/resources"].isdir()


@pytest.mark.parametrize("extra,with_version,version,msg", [
    ([("../evil", 0o100644, b"x")], True, "42.11.17", "Unsafe path"),
    ([("/etc/passwd", 0o100644, b"x")], True, "42.11.17", "Unsafe path"),
    ([("link", 0o120777, b"electron")], True, "42.11.17", "Symbolic link"),
    (None, False, "42.11.17", "no `version`"),
    (None, True, "42.11.16", "does not match"),
])
def test_zip_to_targz_refuses_bad_archives(tmp_path, extra, with_version, version, msg) -> None:
    z = tmp_path / "e.zip"
    z.write_bytes(_electron_zip("42.11.17", extra, with_version))
    with pytest.raises(engine_import.EngineImportError, match=msg):
        engine_import.zip_to_targz(str(z), str(tmp_path / "o.tar.gz"), version)


def _suffix_only(url: str) -> bool:
    """Garde d'hôte SANS résolution DNS (tests hors réseau) : même règle de suffixe."""
    host = httpx.URL(url).host
    return httpx.URL(url).scheme == "https" and any(
        host == s or host.endswith("." + s) for s in engine_import.ALLOWED_HOSTS)


def _github_transport(zip_bytes: bytes, sums_sha: str, redirect_to: str | None = None):
    name = "electron-v42.11.17-linux-arm64.zip"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/SHASUMS256.txt"):
            return httpx.Response(200, text=f"{sums_sha} *{name}\n")
        if url.endswith(f"/{name}") and "github.com/electron" in url:
            return httpx.Response(302, headers={
                "location": redirect_to or f"https://objects.githubusercontent.com/x/{name}"})
        if request.url.host == "objects.githubusercontent.com":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(599)
    return httpx.MockTransport(handler)


def test_fetch_and_package_verifies_and_follows_allowed_redirect(tmp_path) -> None:
    import hashlib

    zb = _electron_zip()
    client = httpx.Client(transport=_github_transport(zb, hashlib.sha256(zb).hexdigest()))
    res = engine_import.fetch_and_package("42.11.17", "linux-arm64", str(tmp_path), client,
                                          url_check=_suffix_only)
    assert res["zip_sha256"] == hashlib.sha256(zb).hexdigest()
    with tarfile.open(res["tar_path"]) as tar:
        assert tar.getmember("dist/electron").mode == 0o755


def test_fetch_and_package_refuses_a_wrong_checksum(tmp_path) -> None:
    client = httpx.Client(transport=_github_transport(_electron_zip(), "0" * 64))
    with pytest.raises(engine_import.EngineImportError, match="Checksum mismatch"):
        engine_import.fetch_and_package("42.11.17", "linux-arm64", str(tmp_path), client,
                                        url_check=_suffix_only)


def test_fetch_and_package_refuses_a_redirect_to_a_foreign_host(tmp_path) -> None:
    import hashlib

    zb = _electron_zip()
    client = httpx.Client(transport=_github_transport(
        zb, hashlib.sha256(zb).hexdigest(), redirect_to="https://evil.example/e.zip"))
    with pytest.raises(engine_import.EngineImportError, match="Refused download host"):
        engine_import.fetch_and_package("42.11.17", "linux-arm64", str(tmp_path), client,
                                        url_check=_suffix_only)


def test_default_url_check_requires_https_and_allowed_hosts() -> None:
    assert not engine_import._check_url("http://github.com/electron/x.zip")
    assert not engine_import._check_url("https://evil.example/x.zip")
    assert not engine_import._check_url("https://github.com.evil.example/x.zip")


def test_validate_rejects_bad_input() -> None:
    for v, f in (("42.11", "windows"), ("42.11.17", "darwin"), ("../1.2.3", "windows")):
        with pytest.raises(engine_import.EngineImportError):
            engine_import.validate(v, f)
    assert engine_import.zip_name("42.11.17", "windows") == "electron-v42.11.17-win32-x64.zip"


async def test_parc_shows_the_electron_version(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    _, token = super_admin
    rpi, bs = _player(org, electron="42.11.18", name="RPi"), _player(org, "brightsign", None, "BS")
    db_session.add_all([rpi, bs])
    await db_session.commit()
    parc = (await client.get("/api/v1/updates/parc",
                             headers={"Authorization": f"Bearer {token}"})).json()
    by = {p["player_name"]: p for p in parc["players"]}
    assert by["RPi"]["electron_version"] == "42.11.18"
    assert by["BS"]["electron_version"] is None


async def test_org_admin_deploys_stable_anywhere_but_beta_only_to_one_player(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, org_admin_token: str,
) -> None:
    """Décision du 14/11 : l'exploitant tient le catalogue, chaque admin d'org déploie sur SES
    écrans — stable partout, bêta sur un seul player (le banc)."""
    p = _player(org)
    beta = _engine_version("42.11.17", channel="beta")
    stable = _engine_version("42.11.16", channel="stable")
    db_session.add_all([p, beta, stable])
    await db_session.commit()
    h = {"Authorization": f"Bearer {org_admin_token}"}

    def dep(ver, targets):
        return client.post("/api/v1/updates/deployments", headers=h, json={
            "id_version": ver.id_version, "strategy": "immediate", "strategy_params": {},
            "targets": targets})

    r = await dep(beta, [{"target_type": "player", "target_id": p.id_player}])
    assert r.status_code == 201, r.text
    for targets in ([{"target_type": "org"}], [{"target_type": "group", "target_id": 1}],
                    [{"target_type": "player", "target_id": p.id_player},
                     {"target_type": "player", "target_id": p.id_player}]):
        r = await dep(beta, targets)
        assert r.status_code == 403 and "single player" in r.json()["detail"], (targets, r.text)
    assert (await dep(stable, [{"target_type": "org"}])).status_code == 201


async def test_operator_deploys_a_beta_engine_to_a_whole_org(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    _, token = super_admin
    beta = _engine_version("42.11.17", channel="beta")
    db_session.add_all([_player(org), beta])
    await db_session.commit()
    r = await client.post("/api/v1/updates/deployments",
                          headers={"Authorization": f"Bearer {token}"}, json={
                              "id_version": beta.id_version, "strategy": "immediate",
                              "strategy_params": {}, "targets": [{"target_type": "org"}]})
    assert r.status_code == 201, r.text


async def test_same_version_cannot_be_deployed_twice_while_in_flight(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, super_admin,
) -> None:
    """Retour Ed 14/11 : le bouton ne montrait rien, 4 clics = 4 déploiements identiques."""
    user, token = super_admin
    p = _player(org)
    v17, v16 = _engine_version("42.11.17"), _engine_version("42.11.16")
    db_session.add_all([p, v17, v16])
    await db_session.commit()
    pid = p.id_player
    h = {"Authorization": f"Bearer {token}"}

    def dep(ver):
        return client.post("/api/v1/updates/deployments", headers=h, json={
            "id_version": ver.id_version, "strategy": "immediate", "strategy_params": {},
            "targets": [{"target_type": "player", "target_id": pid}]})

    first = await dep(v17)
    assert first.status_code == 201, first.text
    second = await dep(v17)
    assert second.status_code == 409 and "RPi" in second.json()["detail"], second.text
    # Une AUTRE version n'est pas un doublon.
    assert (await dep(v16)).status_code == 201
    # Une fois la cible terminée, la même version peut repartir.
    await ota.report_target(db_session, pid, first.json()["id_deployment"], "rolled_back", "x")
    await db_session.commit()
    assert (await dep(v17)).status_code == 201
