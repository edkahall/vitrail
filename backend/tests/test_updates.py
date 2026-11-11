"""Tests OTA (§XXX) — catalogue de versions, inventaire du parc, déploiement (slice 1)."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services.ota import semver_key


def test_semver_key_orders() -> None:
    assert semver_key("1.8.3") == (1, 8, 3)
    assert semver_key("1.10.0") > semver_key("1.9.9")
    assert semver_key(None) == (0, 0, 0)
    assert semver_key("nope") == (0, 0, 0)


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="OtaOrg", org_slug=f"ota-{uuid.uuid4().hex[:8]}")
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


@pytest_asyncio.fixture
async def player(db_session: AsyncSession, org: CmsOrganization) -> CmsPlayer:
    p = CmsPlayer(id_org=org.id_org, player_name="NUC", player_os="linux",
                  player_version="1.7.0", status="online")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture(autouse=True)
async def _clean_ota_globals(db_session: AsyncSession):
    """`cms_player_update_versions` est GLOBALE (pas d'`id_org` → pas isolée par l'org fraîche de
    chaque test) et `create_deployment` COMMIT → versions/déploiements fuient entre tests d'une même
    session pytest (le rollback de teardown de `db_session` ne défait pas un commit). On purge ces 3
    tables globales après chaque test → restaure l'isolation (sinon versions dupliquées)."""
    from sqlalchemy import text
    yield
    for tbl in ("cms_update_deployment_targets", "cms_update_deployments",
                "cms_player_update_versions"):
        await db_session.execute(text(f"DELETE FROM {tbl}"))
    await db_session.commit()


async def test_ota_slice2_directive_report_success(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Slice 2 : directive au player, report d'apply, détection de succès observable."""
    from datetime import UTC, datetime, timedelta

    from app.models.player_version import CmsPlayerUpdateVersion, CmsUpdateDeploymentTarget
    from app.services import ota
    from app.services.targeting import Target

    u = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(u)
    p = CmsPlayer(id_org=org.id_org, player_name="NUC", player_os="linux",
                  player_version="1.7.0", status="online")
    db_session.add(p)
    ver = CmsPlayerUpdateVersion(track="player", version="1.8.0",
                                 download_url="https://rel/x.zip", sha256_checksum="abc")
    db_session.add(ver)
    await db_session.flush()

    dep = await ota.create_deployment(
        db_session, org.id_org, ver.id_version,
        [Target(target_type="org")], "immediate", {}, u.id_user)

    # 1) Directive présente pour le player (cible pending)
    d = await ota.directive_for_player(db_session, p.id_player)
    assert d and d["version"] == "1.8.0" and d["sha256"] == "abc"
    assert d["id_deployment"] == dep.id_deployment

    # 2) Report `downloading` → la cible reste EN VOL (Slice 2c : le player pilote sa machine à
    #    états download→staged→install depuis la directive) → directive émise (non terminale).
    assert await ota.report_target(
        db_session, p.id_player, dep.id_deployment, "downloading") is True
    assert await ota.directive_for_player(db_session, p.id_player) is not None

    # 2b) Report `installing` → started_at posé (base du timeout), en vol → directive émise.
    assert await ota.report_target(
        db_session, p.id_player, dep.id_deployment, "installing") is True
    assert await ota.directive_for_player(db_session, p.id_player) is not None

    # 3) Succès observable : le player remonte la version cible → cible `success` (TERMINALE) →
    #    la directive disparaît.
    changed = await ota.mark_success_if_updated(db_session, p.id_player, "1.8.0")
    assert dep.id_deployment in changed
    tgt = (await db_session.execute(
        select(CmsUpdateDeploymentTarget)
        .where(CmsUpdateDeploymentTarget.id_deployment == dep.id_deployment)
    )).scalar_one()
    assert tgt.status == "success"
    assert await ota.directive_for_player(db_session, p.id_player) is None

    # 4) Un report sur une cible déjà terminale est ignoré (idempotent / anti-rejeu)
    assert await ota.report_target(
        db_session, p.id_player, dep.id_deployment, "failed", "x") is False

    # 5) Stratégie `scheduled` avec start_at futur → pas de directive
    ver2 = CmsPlayerUpdateVersion(track="player", version="1.9.0")
    db_session.add(ver2)
    await db_session.flush()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await ota.create_deployment(
        db_session, org.id_org, ver2.id_version, [Target(target_type="org")],
        "scheduled", {"start_at": future}, u.id_user)
    assert await ota.directive_for_player(db_session, p.id_player) is None


async def test_ota_version_parc_deploy_flow(
    client: AsyncClient, admin_token: str, player: CmsPlayer
) -> None:
    h = {"Authorization": f"Bearer {admin_token}"}

    # Enregistre une version player
    rv = await client.post("/api/v1/updates/versions", headers=h, json={
        "track": "player", "version": "1.8.0", "download_url": "https://rel/player-1.8.0.zip",
        "release_notes": "notes"})
    assert rv.status_code == 201
    id_version = rv.json()["id_version"]

    # Version invalide rejetée
    bad = await client.post("/api/v1/updates/versions", headers=h,
                            json={"track": "player", "version": "pas-semver"})
    assert bad.status_code == 400

    # Inventaire du parc : le player (1.7.0) est EN RETARD sur 1.8.0
    parc = (await client.get("/api/v1/updates/parc", headers=h)).json()
    row = next(p for p in parc["players"] if p["id_player"] == player.id_player)
    assert row["current_version"] == "1.7.0" and row["latest_version"] == "1.8.0"
    assert row["up_to_date"] is False and parc["outdated"] >= 1

    # Crée un déploiement vers toute l'org → 1 target (le player), previous_version capturée
    dep = await client.post("/api/v1/updates/deployments", headers=h, json={
        "id_version": id_version, "strategy": "immediate",
        "targets": [{"target_type": "org"}]})
    assert dep.status_code == 201
    id_dep = dep.json()["id_deployment"]
    assert dep.json()["total_targets"] == 1

    detail = (await client.get(f"/api/v1/updates/deployments/{id_dep}", headers=h)).json()
    assert len(detail["targets"]) == 1
    assert detail["targets"][0]["id_player"] == player.id_player
    assert detail["targets"][0]["previous_version"] == "1.7.0"
    assert detail["targets"][0]["status"] == "pending"

    # La version est utilisée → suppression refusée (409)
    rd = await client.delete(f"/api/v1/updates/versions/{id_version}", headers=h)
    assert rd.status_code == 409

    # Annulation du déploiement
    cancel = await client.post(f"/api/v1/updates/deployments/{id_dep}/cancel", headers=h)
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"
    d2 = (await client.get(f"/api/v1/updates/deployments/{id_dep}", headers=h)).json()
    assert d2["targets"][0]["status"] == "cancelled"


async def test_ota_brightsign_platform_scoping(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Un player BrightSign scope sous la plateforme « brightsign » (= player_os, PAS bs_family) :
    une version model_family=« brightsign » le marque en retard sans toucher un player linux, et une
    version linux ne fait pas apparaître le BrightSign en retard. Prouve que la granularité par SoC
    (malibu/pagani) n'est PLUS utilisée pour le track player (une version « brightsign » couvre tout
    le parc BS, cohérent avec le sélecteur model_family de l'UI = les player_os du parc)."""
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import ota

    bs = CmsPlayer(id_org=org.id_org, player_name="XT1144", player_os="brightsign",
                   player_version="1.1.0", status="online",
                   capabilities={"bs_family": "malibu"})   # bs_family présent → ignoré (fix)
    lx = CmsPlayer(id_org=org.id_org, player_name="NUC", player_os="linux",
                   player_version="1.7.0", status="online")
    db_session.add_all([bs, lx])
    db_session.add(CmsPlayerUpdateVersion(
        track="player", version="2.0.0", model_family="brightsign"))
    db_session.add(CmsPlayerUpdateVersion(
        track="player", version="1.8.0", model_family="linux"))
    await db_session.commit()

    inv = {p["player_name"]: p for p in await ota.parc_inventory(db_session, org.id_org)}
    # BrightSign : cible la version « brightsign » (pas la linux) → en retard sur 2.0.0
    assert inv["XT1144"]["latest_version"] == "2.0.0"
    assert inv["XT1144"]["up_to_date"] is False
    # Linux : cible la version « linux » (pas la brightsign 2.0.0) → en retard sur 1.8.0
    assert inv["NUC"]["latest_version"] == "1.8.0"
    assert inv["NUC"]["up_to_date"] is False


async def test_ota_deployment_filters_by_model_family(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Garde plateforme (incident 2026-07-06) : un déploiement org-wide d'une version à
    `model_family` défini ne cible QUE les players de cette plateforme — un bundle windows ne doit
    JAMAIS partir sur un player linux/rpi (il s'installerait par-dessus son code). Une version
    générique (model_family NULL) continue de cibler tout l'org."""
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import ota
    from app.services.targeting import Target

    u = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    win = CmsPlayer(id_org=org.id_org, player_name="AOpen", player_os="windows",
                    player_version="1.6.1", status="online")
    lx = CmsPlayer(id_org=org.id_org, player_name="NUC", player_os="linux-x64",
                   player_version="1.5.4", status="online")
    rpi = CmsPlayer(id_org=org.id_org, player_name="RPi", player_os="linux-arm64",
                    player_version="1.6.1", status="online")
    db_session.add_all([u, win, lx, rpi])
    ver_win = CmsPlayerUpdateVersion(track="player", version="1.6.5", model_family="windows",
                                     download_url="/api/v1/updates/bundles/windows/1.6.5")
    ver_any = CmsPlayerUpdateVersion(track="player", version="9.9.9")
    db_session.add_all([ver_win, ver_any])
    await db_session.flush()

    # Version windows, cible org → SEUL le player windows est ciblé.
    dep = await ota.create_deployment(
        db_session, org.id_org, ver_win.id_version,
        [Target(target_type="org")], "immediate", {}, u.id_user)
    targets = await ota.deployment_targets(db_session, dep.id_deployment)
    assert dep.total_targets == 1
    assert [t.id_player for t in targets] == [win.id_player]

    # Version générique (model_family NULL), cible org → les 3 players.
    dep2 = await ota.create_deployment(
        db_session, org.id_org, ver_any.id_version,
        [Target(target_type="org")], "immediate", {}, u.id_user)
    assert dep2.total_targets == 3


async def test_ota_deployment_pause_resume_delete(
    client: AsyncClient, admin_token: str, player: CmsPlayer, db_session: AsyncSession
) -> None:
    """Cycle suspendre → (directive coupée) → reprendre → supprimer. La suppression n'est permise
    que sur un déploiement annulé/suspendu/terminé — jamais actif (409)."""
    from app.services import ota

    h = {"Authorization": f"Bearer {admin_token}"}
    rv = await client.post("/api/v1/updates/versions", headers=h, json={
        "track": "player", "version": "1.9.0",
        "download_url": "/api/v1/updates/bundles/linux/1.9.0"})
    id_version = rv.json()["id_version"]
    dep = await client.post("/api/v1/updates/deployments", headers=h, json={
        "id_version": id_version, "strategy": "immediate", "targets": [{"target_type": "org"}]})
    id_dep = dep.json()["id_deployment"]

    base = f"/api/v1/updates/deployments/{id_dep}"

    # Actif (pending) : suppression refusée, directive servie au player.
    assert (await client.delete(base, headers=h)).status_code == 409
    assert await ota.directive_for_player(db_session, player.id_player) is not None

    # Pause : statut paused, la directive n'est PLUS servie ; re-pause = 409.
    rp = await client.post(f"{base}/pause", headers=h)
    assert rp.status_code == 200 and rp.json()["status"] == "paused"
    assert await ota.directive_for_player(db_session, player.id_player) is None
    assert (await client.post(f"{base}/pause", headers=h)).status_code == 409

    # Un retardataire qui monte de version PENDANT la pause est quand même constaté success.
    changed = await ota.mark_success_if_updated(db_session, player.id_player, "1.9.0")
    assert changed == [id_dep]
    await db_session.commit()

    # Reprise : recalcul depuis les cibles → tout terminal → completed ;
    # resume d'un non-paused = 409.
    rr = await client.post(f"{base}/resume", headers=h)
    assert rr.status_code == 200 and rr.json()["status"] == "completed"
    assert rr.json()["success_count"] == 1
    assert (await client.post(f"{base}/resume", headers=h)).status_code == 409

    # Terminé → suppression OK (cibles purgées), puis disparu de la liste.
    assert (await client.delete(base, headers=h)).status_code == 204
    deps = (await client.get("/api/v1/updates/deployments", headers=h)).json()
    assert all(d["id_deployment"] != id_dep for d in deps)

    # Suspendu → suppression OK aussi.
    dep2 = await client.post("/api/v1/updates/deployments", headers=h, json={
        "id_version": id_version, "strategy": "immediate", "targets": [{"target_type": "org"}]})
    base2 = f"/api/v1/updates/deployments/{dep2.json()['id_deployment']}"
    await client.post(f"{base2}/pause", headers=h)
    assert (await client.delete(base2, headers=h)).status_code == 204


async def test_ota_version_promote_and_targeted_deploy(
    client: AsyncClient, admin_token: str, player: CmsPlayer
) -> None:
    """Workflow canary : version BETA (invisible du « latest » du parc) → déploiement CIBLÉ sur un
    player → promotion beta→stable (la version devient la référence du parc). Canal invalide
    = 400."""
    h = {"Authorization": f"Bearer {admin_token}"}

    bad = await client.post("/api/v1/updates/versions", headers=h, json={
        "track": "player", "version": "2.1.0", "channel": "nightly"})
    assert bad.status_code == 400

    rv = await client.post("/api/v1/updates/versions", headers=h, json={
        "track": "player", "version": "2.1.0", "channel": "beta"})
    assert rv.status_code == 201
    id_version = rv.json()["id_version"]

    # Beta → PAS la référence du parc : le player (1.7.0) n'est pas « en retard ».
    # ⚠️ Cette assertion FIGEAIT un défaut : elle exigeait `up_to_date is True` alors qu'aucune
    # version stable n'existe pour cette plateforme — donc qu'on ne compare à rien. L'état correct
    # est « inconnu », et il est compté à part par l'endpoint.
    parc = (await client.get("/api/v1/updates/parc", headers=h)).json()
    row = next(p for p in parc["players"] if p["id_player"] == player.id_player)
    assert row["latest_version"] is None
    assert row["update_state"] == "unknown" and row["up_to_date"] is False
    assert parc["unknown"] >= 1 and parc["outdated"] == 0

    # Déploiement CIBLÉ sur un player précis (pas l'org) → 1 seule cible.
    dep = await client.post("/api/v1/updates/deployments", headers=h, json={
        "id_version": id_version, "strategy": "immediate",
        "targets": [{"target_type": "player", "target_id": player.id_player}]})
    assert dep.status_code == 201 and dep.json()["total_targets"] == 1

    # Promotion beta → stable ; re-promotion = 409 ; le parc voit désormais la 2.1.0.
    pr = await client.post(f"/api/v1/updates/versions/{id_version}/promote", headers=h)
    assert pr.status_code == 200 and pr.json()["channel"] == "stable"
    assert (await client.post(
        f"/api/v1/updates/versions/{id_version}/promote", headers=h)).status_code == 409
    parc2 = (await client.get("/api/v1/updates/parc", headers=h)).json()
    row2 = next(p for p in parc2["players"] if p["id_player"] == player.id_player)
    assert row2["latest_version"] == "2.1.0" and row2["up_to_date"] is False
    assert row2["update_state"] == "outdated" and parc2["outdated"] >= 1


# ── OTA Android par bundle serveur (étape 4, 2026-07-19) ──────────────────────
#
# Android passait par une URL MinIO PRÉ-SIGNÉE absolue : ça marchait (5 déploiements réussis)
# mais l'URL expire à 7 jours et embarque des identifiants MinIO en base. On l'aligne sur le
# chemin durable des Electron : artefact servi par le serveur, URL relative, token machine.


def test_android_mapped_to_bundle_platform() -> None:
    """Toutes les ABI Android partagent le même APK universel → une seule plateforme."""
    from app.api.v1.endpoints.updates import _BUNDLE_ARTIFACT, _BUNDLE_PLATFORMS, _OS_TO_BUNDLE

    assert "android" in _BUNDLE_PLATFORMS
    for abi in ("android", "android-armeabi-v7a", "android-arm64-v8a", "android-x86_64"):
        assert _OS_TO_BUNDLE[abi] == "android", abi
    # L'artefact Android est un APK (installé par PackageInstaller), pas une archive à extraire.
    assert _BUNDLE_ARTIFACT["android"] == (".apk", "application/vnd.android.package-archive")
    for plat in ("linux", "rpi", "windows"):
        assert _BUNDLE_ARTIFACT[plat] == (".tar.gz", "application/gzip"), plat


def test_electron_bundles_unchanged_by_android_support() -> None:
    """Garde-fou : l'ajout d'Android ne doit PAS élargir le mapping des Electron — un player
    linux qui téléchargerait un bundle windows l'installerait par-dessus son code (incident
    du 2026-07-06, à l'origine de cette garde)."""
    from app.api.v1.endpoints.updates import _OS_TO_BUNDLE

    assert _OS_TO_BUNDLE["linux-x64"] == "linux"
    assert _OS_TO_BUNDLE["linux-arm64"] == "rpi"
    assert _OS_TO_BUNDLE["windows"] == "windows"
    # Un OS inconnu ne mappe sur rien → l'endpoint répond 403 plutôt que de servir au hasard.
    assert _OS_TO_BUNDLE.get("brightsign") is None
    assert _OS_TO_BUNDLE.get("chromeos") is None


# ── LOT 2 : import d'un bundle player (linux/rpi/windows/android) ──────────────


def _make_player_targz(version: str) -> bytes:
    """Archive tar.gz minimale d'un player Electron : package.json (version) + src/ à la racine,
    comme celle que produit `tar -czf … -C player-<os> package.json src`."""
    import io
    import json as _json
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        pkg = _json.dumps({"name": "signflow-player", "version": version}).encode()
        ti = tarfile.TarInfo("package.json")
        ti.size = len(pkg)
        tf.addfile(ti, io.BytesIO(pkg))
        src = b"// main"
        ts = tarfile.TarInfo("src/main.js")
        ts.size = len(src)
        tf.addfile(ts, io.BytesIO(src))
    return buf.getvalue()


async def test_bundle_import_targz_detect_store_dedup(
    client: AsyncClient, admin_token: str
) -> None:
    """Import tar.gz : détection plateforme (nom) + version (package.json), stockage + ligne catalogue
    à download_url RELATIF, puis ré-import idempotent. dry_run = aperçu sans rien écrire."""
    from app.services import media

    h = {"Authorization": f"Bearer {admin_token}"}
    ver = "8.8.8"
    data = _make_player_targz(ver)
    fname = f"signflow-player-linux-{ver}.tar.gz"
    up = {"file": (fname, data, "application/gzip")}

    dry = await client.post("/api/v1/updates/versions/import", headers=h, files=up,
                            data={"dry_run": "true"})
    assert dry.status_code == 200
    b = dry.json()
    assert b["platform"] == "linux" and b["version"] == ver
    assert b["dry_run"] is True and b["already_registered"] is False
    assert b["download_url"] == f"/api/v1/updates/bundles/linux/{ver}"

    real = await client.post("/api/v1/updates/versions/import", headers=h, files=up, data={})
    assert real.status_code == 200
    rb = real.json()
    assert rb["already_registered"] is False and rb["artifact_present"] is True
    assert rb["id_version"] is not None

    versions = (await client.get("/api/v1/updates/versions?track=player", headers=h)).json()
    row = next(v for v in versions if v["version"] == ver and v["model_family"] == "linux")
    assert row["download_url"] == f"/api/v1/updates/bundles/linux/{ver}"

    again = await client.post("/api/v1/updates/versions/import", headers=h, files=up,
                              data={"dry_run": "true"})
    assert again.json()["already_registered"] is True
    assert again.json()["id_version"] == rb["id_version"]

    await media.delete_file(f"player-bundles/linux/{ver}.tar.gz")


async def test_bundle_import_rejects_bad_magic_and_unknown_platform(
    client: AsyncClient, admin_token: str
) -> None:
    """Un .tar.gz non-gzip (mauvaise magie) → 422 ; un nom sans plateforme et sans param → 422."""
    h = {"Authorization": f"Bearer {admin_token}"}
    bad = await client.post("/api/v1/updates/versions/import", headers=h, data={},
        files={"file": ("signflow-player-linux-1.0.0.tar.gz", b"not gzip", "application/gzip")})
    assert bad.status_code == 422
    nofmt = await client.post("/api/v1/updates/versions/import", headers=h, data={},
        files={"file": ("random.tar.gz", _make_player_targz("1.0.0"), "application/gzip")})
    assert nofmt.status_code == 422


async def test_bundle_import_url_ssrf_blocked(
    client: AsyncClient, admin_token: str
) -> None:
    """Une URL vers une IP interne (MinIO/loopback) est refusée par la garde anti-SSRF → 400."""
    h = {"Authorization": f"Bearer {admin_token}"}
    r = await client.post("/api/v1/updates/versions/import", headers=h,
        data={"url": "http://127.0.0.1:9000/signflow-player-linux-1.0.0.tar.gz"})
    assert r.status_code == 400


# ── Une version IMPORTÉE doit être déployable (bug trouvé le 2026-07-26) ──────
#
# L'import de bundle écrit `model_family` = PLATEFORME (« rpi », « linux »), alors que le
# garde de plateforme de `create_deployment` comparait ce champ au `player_os` du parc
# (« linux-arm64 », « linux-x64 »). Aucune égalité possible ⇒ le déploiement était créé avec
# **0 cible**, sans erreur : l'opérateur voyait un déploiement « créé » qui ne mettait jamais
# rien à jour. Trouvé en préparant un vrai OTA, pas en relecture.

async def test_imported_bundle_version_reaches_its_players(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    from app.models.player_version import CmsPlayerUpdateVersion, CmsUpdateDeploymentTarget
    from app.services import ota
    from app.services.targeting import Target

    # Parc réaliste : le RPi s'annonce « linux-arm64 » et le NUC « linux-x64 ».
    rpi = CmsPlayer(id_org=org.id_org, player_name="RPi", player_os="linux-arm64",
                    player_version="1.6.10", status="online")
    nuc = CmsPlayer(id_org=org.id_org, player_name="NUC", player_os="linux-x64",
                    player_version="1.5.14", status="online")
    db_session.add_all([rpi, nuc])
    await db_session.flush()

    user = CmsUser(id_org=org.id_org, email=f"ota-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    # Version telle que l'écrit l'import : model_family = plateforme de bundle.
    version = CmsPlayerUpdateVersion(
        track="player", version="1.6.11", channel="stable", model_family="rpi",
        download_url="/api/v1/updates/bundles/rpi/1.6.11", sha256_checksum="a" * 64,
    )
    db_session.add(version)
    await db_session.commit()

    dep = await ota.create_deployment(
        db_session, org.id_org, version.id_version,
        [Target(target_type="org", target_id=None, tag_expr={})],
        "immediate", {}, user.id_user,
    )
    targets = list((await db_session.execute(
        select(CmsUpdateDeploymentTarget.id_player)
        .where(CmsUpdateDeploymentTarget.id_deployment == dep.id_deployment)
    )).scalars().all())

    # Le bundle « rpi » atteint le player linux-arm64…
    assert targets == [rpi.id_player], targets
    # …et surtout PAS le NUC : la garde anti-mismatch du 2026-07-06 reste entière (un bundle
    # posé sur le mauvais OS s'installe par-dessus le player et casse son identité d'app).
    assert nuc.id_player not in targets
    assert dep.total_targets == 1


def test_bundle_platform_covers_every_declared_os() -> None:
    """`player_os_for_bundle` est l'inverse exact de la table OS → bundle (pas de trou)."""
    from app.services import ota

    for platform in ota.BUNDLE_PLATFORMS:
        oses = ota.player_os_for_bundle(platform)
        assert oses, platform
        assert all(ota.OS_TO_BUNDLE[o] == platform for o in oses), platform
    assert "linux-arm64" in ota.player_os_for_bundle("rpi")
    assert "linux-x64" in ota.player_os_for_bundle("linux")
    assert "linux-x64" not in ota.player_os_for_bundle("rpi")


# ── Amorçage automatique des bundles (#4, 2026-07-27) ────────────────────────
#
# Sur une install neuve, le catalogue de versions players est VIDE : le client ne peut mettre à
# jour aucun player. Les bundles voyagent désormais dans une image OCI du registre, déversée par
# `scripts/seed_player_bundles.py` à l'install et à chaque MAJ. Ce chemin n'est PAS le chemin HTTP
# (aucun utilisateur, aucune requête) : il appelle directement `bundle_import.import_bundle`.
# Ces tests fixent l'invariant qui compte — l'amorçage produit EXACTEMENT ce que produit l'import
# manuel de l'opérateur (même clé de stockage, même download_url relatif) et reste idempotent, car
# il est rejoué à chaque mise à jour.

async def test_seeded_bundle_is_identical_to_operator_import(db_session: AsyncSession) -> None:
    """Amorçage automatique = même clé MinIO, même download_url relatif, même ligne catalogue."""
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import bundle_import, media

    ver = "7.7.7"
    data = _make_player_targz(ver)
    fname = f"signflow-player-rpi-{ver}.tar.gz"

    res = await bundle_import.import_bundle(db_session, data=data, filename=fname)
    assert res.platform == "rpi" and res.version == ver
    # L'invariant du #4 : RELATIF, donc le player le résout contre SON serveur.
    assert res.download_url == f"/api/v1/updates/bundles/rpi/{ver}"
    assert res.artifact_present is True and res.id_version is not None
    # La clé doit être exactement celle que relit `download_bundle` (sinon 404 côté player).
    assert await media.object_exists(f"player-bundles/rpi/{ver}.tar.gz")

    row = (await db_session.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.track == "player",
            CmsPlayerUpdateVersion.version == ver,
            CmsPlayerUpdateVersion.model_family == "rpi",
        )
    )).scalar_one()
    assert row.download_url == res.download_url and row.sha256_checksum == res.sha256

    # Rejoué à CHAQUE mise à jour : ne doit ni dupliquer la ligne ni re-téléverser.
    again = await bundle_import.import_bundle(db_session, data=data, filename=fname)
    assert again.already_registered is True and again.id_version == res.id_version
    rows = (await db_session.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.track == "player",
            CmsPlayerUpdateVersion.version == ver,
            CmsPlayerUpdateVersion.model_family == "rpi",
        )
    )).scalars().all()
    assert len(rows) == 1

    await media.delete_file(f"player-bundles/rpi/{ver}.tar.gz")


async def test_seeded_bundle_refuses_what_the_endpoint_refuses(db_session: AsyncSession) -> None:
    """Les mêmes refus qu'en HTTP, mais en exception : l'amorçage saute le fichier et continue.

    Un bundle exotique dans l'image ne doit jamais faire échouer une mise à jour serveur.
    """
    from app.services import bundle_import

    with pytest.raises(bundle_import.BundleImportError):  # magie gzip absente
        await bundle_import.import_bundle(
            db_session, data=b"not gzip", filename="signflow-player-linux-1.0.0.tar.gz"
        )
    with pytest.raises(bundle_import.BundleImportError):  # plateforme indétectable
        await bundle_import.import_bundle(
            db_session, data=_make_player_targz("1.0.0"), filename="random.tar.gz"
        )
    with pytest.raises(bundle_import.BundleImportError):  # contenu vide
        await bundle_import.import_bundle(
            db_session, data=b"", filename="signflow-player-linux-1.0.0.tar.gz"
        )


# ── Décision 2 « canal unique » (2026-07-28) : firmware minimum d'un renderer BS ──────────


def test_firmware_at_least_handles_unknown() -> None:
    """Inconnu = SOUS le minimum (fail-safe : on ne sert pas un renderer sans preuve du BOS)."""
    from app.services.firmware import firmware_at_least

    assert firmware_at_least("9.1.140", "9.1.132") is True
    assert firmware_at_least("9.1.132", "9.1.132") is True
    assert firmware_at_least("9.0.200", "9.1.132") is False
    assert firmware_at_least(None, "9.1.132") is False
    assert firmware_at_least(None, None) is True          # aucune exigence
    assert firmware_at_least("8.5.70", "") is True


async def test_renderer_resolution_holds_until_min_firmware(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """La garde de `resolve_renderer_version` : un renderer BS exigeant un BOS minimum n'est
    JAMAIS servi à un device en dessous — il garde son renderer courant (fail-safe), puis
    reçoit la version épinglée dès que le BOS remonté satisfait l'exigence. C'est CETTE garde
    qui rend l'ordre firmware→renderer automatique, quel que soit le timing des déploiements."""
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import ota
    from app.services.targeting import Target

    u = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    bs = CmsPlayer(id_org=org.id_org, player_name="XT-old-bos", player_os="brightsign",
                   player_version="2.0.27", status="online",
                   capabilities={"bs_family": "malibu", "bs_bos": "9.0.100"})
    db_session.add_all([u, bs])
    # Firmware épinglé adéquat → l'orchestration de create_deployment passe (pas un refus).
    pinned = CmsPlayerUpdateVersion(track="firmware", version="9.1.140", model_family="malibu",
                                    is_pinned=True, storage_key="firmware/malibu/9.1.140.bsfw")
    ver = CmsPlayerUpdateVersion(track="player", version="2.1.0", model_family="brightsign",
                                 min_firmware_version="9.1.132")
    db_session.add_all([pinned, ver])
    await db_session.flush()

    await ota.create_deployment(
        db_session, org.id_org, ver.id_version,
        [Target(target_type="player", target_id=bs.id_player)], "immediate", {}, u.id_user)

    # BOS 9.0.100 < 9.1.132 → la résolution RETIENT 2.1.0 et rend le renderer courant.
    assert await ota.resolve_renderer_version(db_session, bs) == "2.0.27"
    # Le device applique le firmware orchestré et remonte 9.1.140 → 2.1.0 est enfin servie.
    bs.capabilities = {"bs_family": "malibu", "bs_bos": "9.1.140"}
    await db_session.commit()
    assert await ota.resolve_renderer_version(db_session, bs) == "2.1.0"


async def test_min_firmware_orchestration_stages_pinned_push(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Créer un déploiement renderer BS vers un player sous le minimum pousse AUSSI le firmware
    épinglé de sa famille (déploiement track=firmware + cible queued), dans la même transaction."""
    from app.models.player_version import (
        CmsPlayerUpdateVersion,
        CmsUpdateDeployment,
        CmsUpdateDeploymentTarget,
    )
    from app.services import ota
    from app.services.targeting import Target

    u = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    bs = CmsPlayer(id_org=org.id_org, player_name="XT-fw-push", player_os="brightsign",
                   player_version="2.0.27", status="online",
                   capabilities={"bs_family": "malibu", "bs_bos": "9.0.100"})
    db_session.add_all([u, bs])
    pinned = CmsPlayerUpdateVersion(track="firmware", version="9.1.140", model_family="malibu",
                                    is_pinned=True, storage_key="firmware/malibu/9.1.140.bsfw")
    ver = CmsPlayerUpdateVersion(track="player", version="2.1.0", model_family="brightsign",
                                 min_firmware_version="9.1.132")
    db_session.add_all([pinned, ver])
    await db_session.flush()

    dep = await ota.create_deployment(
        db_session, org.id_org, ver.id_version,
        [Target(target_type="player", target_id=bs.id_player)], "immediate", {}, u.id_user)
    assert dep.total_targets == 1

    fw_targets = (await db_session.execute(
        select(CmsUpdateDeploymentTarget)
        .join(CmsUpdateDeployment,
              CmsUpdateDeployment.id_deployment == CmsUpdateDeploymentTarget.id_deployment)
        .where(CmsUpdateDeployment.id_version == pinned.id_version,
               CmsUpdateDeploymentTarget.id_player == bs.id_player)
    )).scalars().all()
    assert len(fw_targets) == 1 and fw_targets[0].status == "queued"
    assert fw_targets[0].previous_version == "9.0.100"


async def test_min_firmware_without_pinned_names_the_gap(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Aucun firmware épinglé ne répond à l'exigence → refus NOMMANT famille, players et version
    requise (le geste réparateur — importer le .bsfw — est dans le message), jamais un déploiement
    créé qui ne s'appliquerait jamais en silence."""
    import pytest

    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import ota
    from app.services.firmware import FirmwareRequired
    from app.services.targeting import Target

    u = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    bs = CmsPlayer(id_org=org.id_org, player_name="XT-no-fw", player_os="brightsign",
                   player_version="2.0.27", status="online",
                   capabilities={"bs_family": "pagani", "bs_bos": "9.0.100"})
    db_session.add_all([u, bs])
    ver = CmsPlayerUpdateVersion(track="player", version="2.1.0", model_family="brightsign",
                                 min_firmware_version="9.1.132")
    db_session.add(ver)
    await db_session.flush()

    with pytest.raises(FirmwareRequired) as exc:
        await ota.create_deployment(
            db_session, org.id_org, ver.id_version,
            [Target(target_type="player", target_id=bs.id_player)], "immediate", {}, u.id_user)
    msg = str(exc.value)
    assert "9.1.132" in msg and "pagani" in msg and "XT-no-fw" in msg


async def test_min_firmware_patch_endpoint(
    client: AsyncClient, admin_token: str, db_session: AsyncSession
) -> None:
    """PATCH /updates/versions/{id} : pose le minimum sur une ligne BS existante (le flux réel la
    crée par served-versions/register, SANS minimum), refuse hors BrightSign et le format libre."""
    from app.models.player_version import CmsPlayerUpdateVersion

    bs_ver = CmsPlayerUpdateVersion(track="player", version="2.1.1", model_family="brightsign")
    lx_ver = CmsPlayerUpdateVersion(track="player", version="1.9.9", model_family="linux")
    db_session.add_all([bs_ver, lx_ver])
    await db_session.commit()
    h = {"Authorization": f"Bearer {admin_token}"}

    r = await client.patch(f"/api/v1/updates/versions/{bs_ver.id_version}",
                           json={"min_firmware_version": "9.1.132"}, headers=h)
    assert r.status_code == 200 and r.json()["min_firmware_version"] == "9.1.132"

    r = await client.patch(f"/api/v1/updates/versions/{lx_ver.id_version}",
                           json={"min_firmware_version": "9.1.132"}, headers=h)
    assert r.status_code == 400

    r = await client.patch(f"/api/v1/updates/versions/{bs_ver.id_version}",
                           json={"min_firmware_version": "v9-beta"}, headers=h)
    assert r.status_code == 400

    r = await client.patch(f"/api/v1/updates/versions/{bs_ver.id_version}",
                           json={"min_firmware_version": None}, headers=h)
    assert r.status_code == 200 and r.json()["min_firmware_version"] is None


# ── Décision 3 « canal unique » (2026-07-28) : import de renderers BS/ChromeOS en zip ─────


def _make_renderer_zip(platform: str, version: str, *, manifest: bool = True,
                       extra: dict[str, bytes] | None = None) -> bytes:
    """Zip de renderer comme le produira le script de publication : `renderer.json` à la racine
    + fichiers consommés un à un par le bootstrap (player.js, style.css, …)."""
    import io
    import json as _json
    import zipfile as _zipfile

    buf = io.BytesIO()
    with _zipfile.ZipFile(buf, "w") as zf:
        if manifest:
            zf.writestr("renderer.json", _json.dumps({"platform": platform, "version": version}))
        zf.writestr("player.js", b"// renderer main")
        zf.writestr("vendor/ed25519.js", b"// crypto")
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


async def test_renderer_zip_import_extracts_and_serves(
    client: AsyncClient, admin_token: str
) -> None:
    """Un zip brightsign s'importe par le MÊME canal que les bundles : ligne catalogue
    (model_family='brightsign' = déployable/épinglable tel quel par resolve_renderer_version),
    fichiers EXTRAITS dans MinIO et servis un à un par `/updates/renderers/…` (ce que le
    bootstrap consommera), en cache immutable. La route est publique — même posture que les
    statiques `public/player-chromeos/` d'aujourd'hui."""
    from app.services import media

    h = {"Authorization": f"Bearer {admin_token}"}
    ver = "7.7.7"
    data = _make_renderer_zip("brightsign", ver)
    up = {"file": (f"signflow-player-brightsign-{ver}.zip", data, "application/zip")}

    dry = await client.post("/api/v1/updates/versions/import", headers=h, files=up,
                            data={"dry_run": "true"})
    assert dry.status_code == 200
    b = dry.json()
    assert b["platform"] == "brightsign" and b["version"] == ver
    assert b["files_extracted"] == 2
    assert b["download_url"] == f"/api/v1/updates/renderers/brightsign/{ver}"

    real = await client.post("/api/v1/updates/versions/import", headers=h, files=up, data={})
    assert real.status_code == 200 and real.json()["id_version"] is not None

    # Fichiers servis (sans auth : le PWA charge par <script>, sans en-tête possible).
    # HEAD d'abord : c'est la SONDE de l'amorce ChromeOS — @router.get seul répondait 405
    # et l'amorce retombait à plat en silence (mesuré au canary du 2026-07-28).
    r = await client.head(f"/api/v1/updates/renderers/brightsign/{ver}/player.js")
    assert r.status_code == 200
    r = await client.get(f"/api/v1/updates/renderers/brightsign/{ver}/player.js")
    assert r.status_code == 200 and r.content == b"// renderer main"
    assert r.headers["content-type"].startswith("text/javascript")
    assert "immutable" in r.headers.get("cache-control", "")
    r = await client.get(f"/api/v1/updates/renderers/brightsign/{ver}/vendor/ed25519.js")
    assert r.status_code == 200 and r.content == b"// crypto"
    # Le manifeste décrit le colis : il n'est PAS servi.
    r = await client.get(f"/api/v1/updates/renderers/brightsign/{ver}/renderer.json")
    assert r.status_code == 404
    r = await client.get(f"/api/v1/updates/renderers/brightsign/{ver}/absent.js")
    assert r.status_code == 404
    r = await client.get(f"/api/v1/updates/renderers/linux/{ver}/player.js")
    assert r.status_code == 404       # plateforme hors liste fermée

    # Ré-import : idempotent (ligne + artefact dédupliqués).
    again = await client.post("/api/v1/updates/versions/import", headers=h, files=up,
                              data={"dry_run": "true"})
    assert again.json()["already_registered"] is True

    for k in (f"player-bundles/brightsign/{ver}.zip",
              f"player-renderers/brightsign/{ver}/player.js",
              f"player-renderers/brightsign/{ver}/vendor/ed25519.js"):
        await media.delete_file(k)


async def test_renderer_zip_detected_by_manifest_alone(
    client: AsyncClient, admin_token: str
) -> None:
    """Sans nom conventionnel ni param, `renderer.json` suffit à détecter plateforme + version
    (même logique de repli que le package.json des tar.gz Electron)."""
    from app.services import media

    h = {"Authorization": f"Bearer {admin_token}"}
    ver = "7.7.8"
    data = _make_renderer_zip("chromeos", ver)
    up = {"file": ("mystery.zip", data, "application/zip")}
    r = await client.post("/api/v1/updates/versions/import", headers=h, files=up,
                          data={"dry_run": "true"})
    assert r.status_code == 200
    assert r.json()["platform"] == "chromeos" and r.json()["version"] == ver
    # dry_run : rien d'écrit, rien à nettoyer — sonde par acquit.
    assert not await media.object_exists(f"player-bundles/chromeos/{ver}.zip")


async def test_renderer_zip_rejects_hostile_paths(
    client: AsyncClient, admin_token: str
) -> None:
    """Un membre à chemin hostile (traversée, absolu) rejette le bundle ENTIER — dès le dry_run,
    pas à l'écriture. Et l'URL de service refuse la traversée même si un objet existait."""
    import io
    import zipfile as _zipfile

    h = {"Authorization": f"Bearer {admin_token}"}
    buf = io.BytesIO()
    with _zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("renderer.json", '{"platform": "brightsign", "version": "7.7.9"}')
        zf.writestr("../evil.js", b"// escape")
    up = {"file": ("signflow-player-brightsign-7.7.9.zip", buf.getvalue(), "application/zip")}
    r = await client.post("/api/v1/updates/versions/import", headers=h, files=up,
                          data={"dry_run": "true"})
    assert r.status_code == 422
    assert "path rejected" in r.json()["detail"]

    r = await client.get("/api/v1/updates/renderers/brightsign/7.7.9/..%2Fevil.js")
    assert r.status_code == 404


def test_publish_script_uses_the_artifact_table_not_targz() -> None:
    """Le script de publication d'un bundle doit dériver l'extension de la PLATEFORME.

    🔴 Il codait `.tar.gz` en dur pour tout le monde (corrigé le 2026-07-30). Or l'endpoint
    `download_bundle` RELIT la clé via `BUNDLE_ARTIFACT` : publier un APK Android écrivait
    `player-bundles/android/<ver>.tar.gz` là où le player irait chercher `<ver>.apk` — un
    **404 à la mise à jour**, sans que rien n'échoue à la publication. Défaut jamais rencontré
    parce que les APK avaient toujours été publiés par l'autre chemin (`bundle_import`).

    Ce test lit le SOURCE du script : il n'a pas de point d'entrée testable (il écrit dans
    MinIO au premier appel), mais la garde qui compte est qu'il ne réintroduise pas
    d'extension en dur ni sa propre table.
    """
    from pathlib import Path

    from app.services.bundle_import import BUNDLE_ARTIFACT

    src = (Path(__file__).resolve().parents[1] / "scripts" / "publish_player_bundle.py").read_text(
        encoding="utf-8"
    )
    body = src.split('"""', 2)[-1]        # hors docstring de tête (elle cite des exemples .tgz)
    assert "BUNDLE_ARTIFACT[platform]" in body, "l'extension doit venir de la table partagée"
    assert '{version}.tar.gz"' not in body, "extension .tar.gz codée en dur — Android reçoit un 404"
    assert '"application/gzip"' not in body, "type MIME codé en dur — idem pour .apk / .zip"
    # La table elle-même reste la seule définition, et couvre bien les artefacts non-tar.
    assert BUNDLE_ARTIFACT["android"][0] == ".apk"
    assert BUNDLE_ARTIFACT["chromeos"][0] == ".zip"


async def test_latest_version_sees_imported_bundle_platforms(
    db_session: AsyncSession, org: CmsOrganization
) -> None:
    """Une version IMPORTÉE doit compter comme « dernière disponible » pour les players qu'elle sert.

    🔴 Défaut constaté par Ed le 2026-07-30 : un bundle importé porte une PLATEFORME
    (`android`, `rpi`, `linux`) dans `model_family`, alors que le player porte un `player_os`
    (`android-armeabi-v7a`, `linux-arm64`, `linux-x64`). La comparaison par égalité seule rendait
    ces versions INVISIBLES : la prod annonçait android 2.5.15 alors que 2.5.17 était publiée, et
    une install dont TOUTES les versions viennent de l'import n'affichait AUCUNE version
    disponible. 3 plateformes sur 6 touchées ; les 3 autres (windows/chromeos/brightsign) passaient
    inaperçues, leur nom de bundle coïncidant avec le player_os.

    Miroir exact de la garde de `create_deployment` (incident 2026-07-26), dans l'autre sens.
    """
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import ota

    android = CmsPlayer(id_org=org.id_org, player_name="Stick", player_os="android-armeabi-v7a",
                        player_version="2.5.15", status="online")
    rpi = CmsPlayer(id_org=org.id_org, player_name="RPi", player_os="linux-arm64",
                    player_version="1.6.9", status="online")
    nuc = CmsPlayer(id_org=org.id_org, player_name="NUC64", player_os="linux-x64",
                    player_version="1.5.9", status="online")
    db_session.add_all([android, rpi, nuc])
    # Écriture HÉRITÉE (player_os) — doit rester visible : on n'a rien cassé de l'existant.
    db_session.add(CmsPlayerUpdateVersion(
        track="player", version="2.5.15", model_family="android-armeabi-v7a"))
    # Écriture d'IMPORT (plateforme de bundle) — c'est elle qui était ignorée.
    db_session.add_all([
        CmsPlayerUpdateVersion(track="player", version="2.5.17", model_family="android"),
        CmsPlayerUpdateVersion(track="player", version="1.6.12", model_family="rpi"),
        CmsPlayerUpdateVersion(track="player", version="1.5.16", model_family="linux"),
    ])
    await db_session.commit()

    inv = {p["player_name"]: p for p in await ota.parc_inventory(db_session, org.id_org)}
    assert inv["Stick"]["latest_version"] == "2.5.17", "la version importée doit primer sur 2.5.15"
    assert inv["Stick"]["up_to_date"] is False
    assert inv["RPi"]["latest_version"] == "1.6.12"
    assert inv["NUC64"]["latest_version"] == "1.5.16"

    # ⛔ Le cloisonnement par plateforme reste entier : un bundle android ne rend pas un RPi
    # « en retard » sur 2.5.17, et réciproquement.
    assert await ota.latest_player_version(db_session, "windows") is None


# ── Colis de bundles : une plateforme sautee doit se NOMMER (defaut du 2026-08-09) ─────────


def _deploy_script(name: str) -> str:
    """Source d'un script de `deploy/registry/`, ou SAUT explicite s'il est hors de portée.

    ⚠️ Le conteneur backend ne monte que `backend/` : `deploy/` n'y existe pas, ces tests s'y
    sautent donc. Ils tournent pour de vrai en CI, qui exécute pytest sur l'hôte avec le dépôt
    complet — c'est là qu'ils font foi. Le saut est NOMMÉ plutôt que silencieux : un test qui
    disparaît sans le dire est indiscernable d'un test qui passe.
    """
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "deploy" / "registry" / name
    if not path.exists():
        pytest.skip(f"{name} hors de portée (deploy/ non monté dans le conteneur) — vérifié en CI")
    return path.read_text(encoding="utf-8")


class _FakeVersionRow:
    """Ligne de catalogue minimale — `plan_package` ne lit que ces deux attributs."""

    def __init__(self, model_family: str, version: str) -> None:
        self.model_family = model_family
        self.version = version


def test_package_plan_takes_the_latest_stable_per_platform() -> None:
    """Le colis embarque la DERNIÈRE version de chaque plateforme, comparée en SemVer.

    ⚠️ Le piège est la comparaison de chaînes : « 2.0.9 » > « 2.0.15 » en lexicographique, et le
    client recevrait un renderer plus ancien que celui que son serveur sert — exactement le genre
    d'écart qui ne se voit qu'à l'usage.
    """
    from app.services import bundle_package

    planned = bundle_package.plan_package([
        _FakeVersionRow("chromeos", "2.0.9"),
        _FakeVersionRow("chromeos", "2.0.15"),
        _FakeVersionRow("chromeos", "2.0.13"),
        _FakeVersionRow("android", "2.5.26"),
        _FakeVersionRow("server", "1.0.32"),        # hors BUNDLE_ARTIFACT : ignorée sans bruit
    ])
    by_platform = {b.platform: b for b in planned}
    assert set(by_platform) == {"chromeos", "android"}
    assert by_platform["chromeos"].version == "2.0.15"
    assert by_platform["chromeos"].storage_key == "player-bundles/chromeos/2.0.15.zip"
    # L'extension vient de la table partagée, elle n'est pas devinée par plateforme.
    assert by_platform["android"].storage_key == "player-bundles/android/2.5.26.apk"


def test_package_shortfall_names_the_skipped_platform() -> None:
    """🔴 LE DÉFAUT DU 2026-08-09, figé.

    Le colis de la 1.0.32 ne contenait que 4 plateformes sur 6 : les renderers chromeos/brightsign
    déclarés au catalogue SANS artefact étaient sautés par un `continue`, dans une étape appelée en
    best-effort — donc le script réussissait et la release partait. Chez le client, plus
    d'épinglage ni de canary sur ces plateformes, et **rien ne distinguait « sautée » de « non
    gérée »**. Ce que ce test exige : une plateforme prévue et absente est NOMMÉE, avec sa version.
    """
    from app.services import bundle_package

    planned = bundle_package.plan_package([
        _FakeVersionRow("chromeos", "2.0.15"),
        _FakeVersionRow("brightsign", "2.0.34"),
        _FakeVersionRow("android", "2.5.26"),
        _FakeVersionRow("linux", "1.5.21"),
    ])
    missing = bundle_package.package_shortfall(planned, ["android", "linux"])
    assert {(b.platform, b.version) for b in missing} == {
        ("chromeos", "2.0.15"), ("brightsign", "2.0.34"),
    }
    # Témoin : colis complet ⇒ aucun manque. Sans lui, une fonction qui renverrait TOUJOURS
    # quelque chose passerait l'assertion ci-dessus et bloquerait toutes les releases.
    assert bundle_package.package_shortfall(
        planned, ["android", "linux", "chromeos", "brightsign"]) == []
    # ⛔ Aucun repli sur une version plus ancienne qui aurait, elle, son artefact : le colis
    # paraîtrait complet en servant un renderer périmé.
    assert [b.version for b in missing if b.platform == "chromeos"] == ["2.0.15"]


def test_bundles_image_script_refuses_an_incomplete_package() -> None:
    """Le script de colis doit ARRÊTER sur une plateforme manquante, pas la taire.

    Lecture du source (le script n'a pas de point d'entrée testable : il parle à Docker et au
    registre). La garde qui compte est triple — il délègue la décision au service partagé, il
    NOMME ce qui manque, et il sort en erreur au lieu de publier un colis amputé.
    """
    src = _deploy_script("publish-player-bundles-image.sh")
    assert "bundle_package.plan_package" in src, "la sélection doit venir du service testé"
    assert "bundle_package.package_shortfall" in src, "le manque doit être calculé, pas devine"
    assert "MANQUANT" in src, "une plateforme sautée doit être nommée"
    assert "SystemExit(2)" in src, "un colis incomplet doit arrêter la publication"
    assert "ALLOW_PARTIAL_BUNDLES" in src, "l'échappatoire doit être explicite et tracée"
    # Un echec laisse des artefacts derriere lui, et `seed_player_bundles` PARCOURT le repertoire
    # (il ne lit pas le manifeste) : sans vidage prealable, un reliquat partirait chez le client
    # sans figurer au manifeste. Devenu probable depuis que ce script refuse un colis incomplet.
    assert "shutil.rmtree(OUT" in src, "la sortie doit être vidée AVANT de remplir le colis"


def test_renderer_publish_script_uses_the_single_definition() -> None:
    """L'outil de publication d'un renderer doit passer par `bundle_import.import_bundle`.

    🔴 C'est l'ABSENCE de cet outil qui a produit le défaut : l'étape était rejouée de mémoire
    (trois conventions de nom en trois séances, lisibles dans le catalogue de la prod) et a fini
    par être oubliée deux fois le même jour. Réécrire ici l'écriture MinIO + la ligne catalogue
    ferait diverger un deuxième chemin de `import_bundle`, qui est la définition unique.
    """
    from pathlib import Path

    py = (Path(__file__).resolve().parents[1] / "scripts"
          / "publish_renderer.py").read_text(encoding="utf-8")
    body = py.split('"""', 2)[-1]
    assert "bundle_import.import_bundle" in body
    assert "put_object" not in body, "l'écriture MinIO doit rester dans le service"
    assert "CmsPlayerUpdateVersion(" not in body, "la ligne catalogue doit rester dans le service"

    sh = _deploy_script("publish-renderer.sh")
    # Le dernier maillon est vérifié par la route publique, pas par le compte rendu de l'import.
    assert "/api/v1/updates/renderers/" in sh
    assert "sha256sum" in sh, "les octets servis doivent être comparés à l'instantané"
    assert "absent.js" in sh, "un témoin négatif doit accompagner la vérification"


async def test_renderer_import_inherits_and_repairs_the_bos_guard(
    db_session: AsyncSession,
) -> None:
    """Un renderer BrightSign importé reprend le BOS minimum, et RÉPARE une ligne incomplète.

    🔴 DEUX défauts réels, la même colonne. `min_firmware_version` n'est pas dans le zip : un
    renderer importé naissait donc à NULL — et NULL ne retient RIEN (ni `resolve_renderer_version`,
    ni l'orchestration firmware→renderer). La garde disparaissait à chaque publication et il
    fallait la reposer à la main ; noté comme piège les 06/08 ET 07/08. Second défaut, mesuré sur
    la prod le 09/08 : les versions déclarées au catalogue sans artefact (`download_url` NUL) sont
    épinglables en apparence et servent des 404 ; ré-importer doit les réparer, pas les dupliquer.
    """
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import bundle_import, media

    # Historique : une version BS antérieure porte la garde BOS.
    db_session.add(CmsPlayerUpdateVersion(
        track="player", version="8.0.1", channel="stable", model_family="brightsign",
        download_url="/api/v1/updates/renderers/brightsign/8.0.1", min_firmware_version="9.1.140",
    ))
    # ... et une plus ANCIENNE avec une exigence plus basse : c'est la plus récente qui fait foi.
    db_session.add(CmsPlayerUpdateVersion(
        track="player", version="8.0.0", channel="stable", model_family="brightsign",
        download_url="/api/v1/updates/renderers/brightsign/8.0.0", min_firmware_version="9.0.100",
    ))
    # Le cas de réparation : ligne déclarée SANS artefact (le geste du 07/08).
    orphan = CmsPlayerUpdateVersion(
        track="player", version="8.0.2", channel="stable", model_family="brightsign",
        download_url=None,
    )
    db_session.add(orphan)
    await db_session.commit()
    await db_session.refresh(orphan)
    assert orphan.download_url is None and orphan.min_firmware_version is None

    data = _make_renderer_zip("brightsign", "8.0.2")
    res = await bundle_import.import_bundle(
        db_session, data=data, filename="signflow-player-brightsign-8.0.2.zip")

    # Réparation en place : pas de doublon, et la ligne devient réellement servable.
    assert res.already_registered is True and res.id_version == orphan.id_version
    await db_session.refresh(orphan)
    assert orphan.download_url == "/api/v1/updates/renderers/brightsign/8.0.2"
    assert orphan.sha256_checksum and orphan.file_size == len(data)
    # La garde BOS est reprise de la version la PLUS RÉCENTE qui en portait une.
    assert orphan.min_firmware_version == "9.1.140"
    assert res.min_firmware_version == "9.1.140"      # annoncée, jamais héritée en silence

    # Une version neuve l'hérite aussi (chemin de création).
    fresh = await bundle_import.import_bundle(
        db_session, data=_make_renderer_zip("brightsign", "8.0.3"),
        filename="signflow-player-brightsign-8.0.3.zip")
    row = (await db_session.execute(
        select(CmsPlayerUpdateVersion).where(
            CmsPlayerUpdateVersion.id_version == fresh.id_version))).scalar_one()
    assert row.min_firmware_version == "9.1.140"

    # ⛔ TÉMOIN : ChromeOS n'a pas de notion de BOS — il ne doit RIEN hériter, sans quoi la valeur
    # d'une plateforme contaminerait l'autre (les deux partagent le même catalogue).
    cos = await bundle_import.import_bundle(
        db_session, data=_make_renderer_zip("chromeos", "8.0.4"),
        filename="signflow-player-chromeos-8.0.4.zip")
    assert cos.min_firmware_version is None

    for ver, plat in (("8.0.2", "brightsign"), ("8.0.3", "brightsign"), ("8.0.4", "chromeos")):
        await media.delete_file(f"player-bundles/{plat}/{ver}.zip")
        await media.delete_file(f"player-renderers/{plat}/{ver}/player.js")
        await media.delete_file(f"player-renderers/{plat}/{ver}/vendor/ed25519.js")


async def test_renderer_import_never_overwrites_a_hand_set_bos(
    db_session: AsyncSession,
) -> None:
    """Une exigence BOS posée par l'opérateur survit au ré-import.

    L'héritage est un défaut sûr pour une valeur ABSENTE ; écraser une valeur choisie (un BOS
    durci à la main sur un renderer précis) transformerait la réparation en régression silencieuse.
    """
    from app.models.player_version import CmsPlayerUpdateVersion
    from app.services import bundle_import, media

    db_session.add(CmsPlayerUpdateVersion(
        track="player", version="8.1.0", channel="stable", model_family="brightsign",
        download_url="/api/v1/updates/renderers/brightsign/8.1.0", min_firmware_version="9.1.140",
    ))
    hardened = CmsPlayerUpdateVersion(
        track="player", version="8.1.1", channel="stable", model_family="brightsign",
        download_url=None, min_firmware_version="9.2.200",
    )
    db_session.add(hardened)
    await db_session.commit()

    await bundle_import.import_bundle(
        db_session, data=_make_renderer_zip("brightsign", "8.1.1"),
        filename="signflow-player-brightsign-8.1.1.zip")
    await db_session.refresh(hardened)
    assert hardened.min_firmware_version == "9.2.200"      # pas 9.1.140
    assert hardened.download_url == "/api/v1/updates/renderers/brightsign/8.1.1"   # réparé quand même

    await media.delete_file("player-bundles/brightsign/8.1.1.zip")
    await media.delete_file("player-renderers/brightsign/8.1.1/player.js")
    await media.delete_file("player-renderers/brightsign/8.1.1/vendor/ed25519.js")


# ── Trois états d'inventaire : à jour / en retard / INCONNU (2026-08-10) ──────
#
# Le défaut corrigé ici est une SÉMANTIQUE, pas un calcul : sans version au catalogue pour la
# plateforme d'un écran, `parc_inventory` repliait le cas sur « à jour » (`(latest_ver is None) or
# …`) et l'endpoint comptait `not up_to_date` ⇒ une installation neuve, dont le catalogue est vide,
# affichait « 0 en retard » sur TOUT son parc. Le mode de panne est le pire : l'écran n'annonce pas
# une erreur, il annonce un succès — et personne ne met jamais rien à jour.
async def test_parc_inventory_three_states(
    client: AsyncClient, admin_token: str, org: CmsOrganization, db_session: AsyncSession
) -> None:
    h = {"Authorization": f"Bearer {admin_token}"}
    # 3 écrans sur 3 plateformes : linux (référence 2.0.0), windows (référence 1.0.0), android
    # (AUCUNE version publiée → aucune référence).
    for name, os_, ver in (("Lnx", "linux", "1.0.0"), ("Win", "windows", "1.0.0"),
                           ("Dro", "android-armeabi-v7a", "2.5.0")):
        db_session.add(CmsPlayer(id_org=org.id_org, player_name=name, player_os=os_,
                                 player_version=ver, status="online"))
    await db_session.commit()
    for v, fam in (("2.0.0", "linux"), ("1.0.0", "windows")):
        assert (await client.post("/api/v1/updates/versions", headers=h, json={
            "track": "player", "version": v, "model_family": fam,
            "download_url": f"https://rel/{fam}-{v}.zip"})).status_code == 201

    parc = (await client.get("/api/v1/updates/parc", headers=h)).json()
    by = {p["player_name"]: p for p in parc["players"]}
    assert by["Lnx"]["update_state"] == "outdated"          # 1.0.0 < 2.0.0
    assert by["Win"]["update_state"] == "up_to_date"        # 1.0.0 == 1.0.0
    # 🔑 L'assertion qui porte le correctif : pas de référence ⇒ ni « à jour », ni « en retard ».
    assert by["Dro"]["latest_version"] is None
    assert by["Dro"]["update_state"] == "unknown"
    assert by["Dro"]["up_to_date"] is False

    # Les compteurs d'en-tête distinguent les trois : un « inconnu » ne gonfle pas les « en retard »
    # (on crierait au loup sur une install neuve) et ne disparaît pas dans les « à jour ».
    assert parc["outdated"] == 1
    assert parc["unknown"] == 1
    assert sum(1 for p in parc["players"] if p["update_state"] == "up_to_date") == 1


async def test_un_renderer_degrade_ne_se_cache_pas_derriere_un_simple_retard(
    client: AsyncClient, admin_token: str, org: CmsOrganization, db_session: AsyncSession
) -> None:
    """Un RECUL de renderer est distinct d'un RETARD, et l'écran doit le dire.

    Défaut mesuré le 2026-08-12 : un ChromeOS a passé trois semaines en 2.0.8 pendant que le
    serveur épinglait 2.0.17, en repassant deux fois par un déploiement qui le remontait. La seule
    trace était un numéro de version ancien — qu'on lit spontanément comme « pas encore déployé ».
    Le player remonte désormais, dans ses capabilities, la version qu'il n'a PAS pu charger : un
    retard attend un déploiement, un recul est arrivé tout seul.
    """
    h = {"Authorization": f"Bearer {admin_token}"}
    sain = CmsPlayer(id_org=org.id_org, player_name="Sain", player_os="chromeos",
                     player_version="2.0.18", status="online",
                     capabilities={"platform": "chromeos"})
    recule = CmsPlayer(id_org=org.id_org, player_name="Recule", player_os="chromeos",
                       player_version="2.0.8", status="online",
                       capabilities={"platform": "chromeos", "renderer_degraded": "2.0.18"})
    db_session.add_all([sain, recule])
    await db_session.commit()
    assert (await client.post("/api/v1/updates/versions", headers=h, json={
        "track": "player", "version": "2.0.18", "model_family": "chromeos",
        "download_url": "https://rel/chromeos-2.0.18.zip"})).status_code == 201

    by = {p["player_name"]: p
          for p in (await client.get("/api/v1/updates/parc", headers=h)).json()["players"]}
    assert by["Recule"]["renderer_degraded"] == "2.0.18", (
        "sans ce champ, le recul est indiscernable d'un retard"
    )
    # ⚠️ Témoin : un player sain ne doit PAS porter le drapeau, sinon il ne distinguerait rien.
    assert by["Sain"]["renderer_degraded"] is None
    # Le drapeau ne remplace pas l'état : ce player est aussi « en retard », les deux sont vrais.
    assert by["Recule"]["update_state"] == "outdated"
