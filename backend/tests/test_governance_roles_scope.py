"""Gouvernance §XVIII — phase 3 de l'audit : rôles scopés (G4), approbation scopée (N5), N4.

  · **G4** — le RÔLE porte la sémantique de périmètre : un porteur d'un rôle de gouvernance
    n'agit que dans son sous-arbre ET **le contenu non gouverné lui est invisible**. Sans
    cela, le défaut N3 (« le non-gouverné est ouvert à tous ») serait renommé, pas corrigé :
    un « programmeur de gouvernance » éditerait encore les playlists et programmes d'org.
  · **N5** — décider sur du contenu gouverné exige d'administrer un nœud PARENT, et personne
    ne valide sa propre soumission (mesuré en base : demandes 22/23, submitted_by =
    reviewed_by = 37). Hors gouvernance (campagnes), rien ne change.
  · **N4** — le Programmeur porte `approvals:submit` : il voyait 403 sur `/approvals` après
    avoir soumis, donc ne recevait jamais un « modifications demandées ».
"""

import ast
import importlib.util
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.governance import CmsGovernanceNode, CmsGovernanceNodeAdmin
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.playlist import CmsPlaylist
from app.models.role import (
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser
from app.services.governance_access import user_can_edit_node, user_is_governance_scoped


def _migration_perms() -> dict[str, list[tuple[str, str]]]:
    """Permissions des rôles, LUES DANS LA MIGRATION 0111 — garde anti-dérive.

    ⚠️ La base de test est bâtie par `Base.metadata.create_all` : **les migrations n'y
    tournent pas**, donc aucun rôle système n'y est seedé. Recopier la liste ici ferait
    passer le test alors que la migration dirait autre chose (le pire des deux mondes).
    On lit donc la source de vérité, et le test échoue si elle change sans qu'on le veuille.
    """
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / (
        "0111_governance_scoped_roles.py")
    spec = importlib.util.spec_from_file_location("migr_0111", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        role: [tuple(pair) for pair in ast.literal_eval(f"[{sql}]")]
        for role, sql in module._PERMS.items()
    }


MIGRATION_PERMS = _migration_perms()

# Reprend le seed réel (migr 0002) pour les rôles NON couverts par la 0111 : l'org-admin a
# tout (`*`), le Planificateur programme. Sans eux, le test opposerait un rôle scopé à un
# rôle sans aucune permission — la comparaison ne dirait rien.
_EXTRA_PERMS = {
    "CMS_ORG_ADMIN": [
        ("playlists", "create"), ("playlists", "read"), ("playlists", "edit"),
        ("playlists", "delete"),
        ("media", "read"), ("media", "edit"), ("media", "delete"),
        ("schedules", "create"), ("schedules", "read"), ("schedules", "edit"),
        ("schedules", "delete"),
        ("governance", "read"), ("governance", "edit"), ("governance", "delegate"),
        ("approvals", "submit"), ("approvals", "review"),
        ("approvals", "approve"), ("approvals", "reject"),
    ],
    "CMS_SCHEDULER": [
        ("schedules", "create"), ("schedules", "read"), ("schedules", "edit"),
        ("schedules", "delete"), ("playlists", "read"), ("media", "read"),
    ],
}


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="GovR", org_slug=f"govr-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


async def _role(db: AsyncSession, org: CmsOrganization, role_key: str) -> CmsRole:
    """Rôle système get-or-create, avec les permissions **de la migration 0111**.

    Deux exceptions fidèles au seed réel (migr 0002) : `CMS_ORG_ADMIN` reçoit `*` (toutes
    les permissions du catalogue), et `CMS_SCHEDULER` ce qu'il faut pour programmer — sans
    quoi le test comparerait un rôle scopé à un rôle vide, ce qui ne prouverait rien.
    """
    await db.execute(
        pg_insert(CmsRole)
        .values(id_org=org.id_org, role_key=role_key, label=role_key, is_system=True)
        .on_conflict_do_nothing(index_elements=["role_key"])
    )
    await db.flush()
    role = (await db.execute(select(CmsRole).where(CmsRole.role_key == role_key))).scalar_one()
    for module, operation in MIGRATION_PERMS.get(role_key, _EXTRA_PERMS.get(role_key, [])):
        await db.execute(
            pg_insert(CmsPermission)
            .values(module=module, operation=operation, label=f"{module}.{operation}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        )
        await db.flush()
        perm = (await db.execute(select(CmsPermission).where(
            CmsPermission.module == module, CmsPermission.operation == operation
        ))).scalar_one()
        await db.execute(
            pg_insert(CmsRolePermission)
            .values(id_role=role.id_role, id_permission=perm.id_permission)
            .on_conflict_do_nothing()
        )
    await db.flush()
    return role


async def _user_with_roles(db: AsyncSession, org: CmsOrganization, *role_keys: str) -> CmsUser:
    """Utilisateur portant des rôles SYSTÈME réels (contenu lu dans la migration 0111)."""
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org, email=f"r-{uid}@test.com",
        password_hash=hash_password("Password1!"), is_active=True,
    )
    db.add(user)
    await db.flush()
    for key in role_keys:
        role = await _role(db, org, key)
        db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    await db.refresh(user)
    return user


async def _mk_node(db: AsyncSession, org, name, parent=None, requires_approval=True):
    n = CmsGovernanceNode(
        id_org=org.id_org, parent_id=parent.id_node if parent else None,
        node_name=name, node_level=(parent.node_level + 1) if parent else 0,
        node_type="store" if parent else "national",
        requires_approval=requires_approval, path="",
    )
    db.add(n)
    await db.flush()
    n.path = f"{parent.path}.{n.id_node}" if parent else str(n.id_node)
    await db.flush()
    return n


def _h(user: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


@pytest_asyncio.fixture
async def scene(db_session: AsyncSession, org):
    national = await _mk_node(db_session, org, "National")
    store = await _mk_node(db_session, org, "Store", parent=national)

    programmer = await _user_with_roles(db_session, org, "CMS_SITE_PROGRAMMER")
    editor = await _user_with_roles(db_session, org, "CMS_GOVERNANCE_EDITOR")
    approver = await _user_with_roles(db_session, org, "CMS_GOVERNANCE_APPROVER")
    classic = await _user_with_roles(db_session, org, "CMS_SCHEDULER")  # rôle NON gouvernance
    org_admin = await _user_with_roles(db_session, org, "CMS_ORG_ADMIN")

    for u in (programmer, editor):
        db_session.add(CmsGovernanceNodeAdmin(id_node=store.id_node, id_user=u.id_user))
    # Le valideur administre le nœud PARENT : c'est la règle D8/N5 (on valide d'un cran
    # au-dessus, jamais son propre nœud).
    db_session.add(CmsGovernanceNodeAdmin(id_node=national.id_node, id_user=approver.id_user))

    # Contenu : une playlist du magasin, une playlist d'organisation (non gouvernée).
    gov_pl = CmsPlaylist(id_org=org.id_org, playlist_name="Store PL",
                         id_governance_node=store.id_node)
    # Enfant imbriquable : sert à créer un BROUILLON sans avoir besoin d'un média.
    child_pl = CmsPlaylist(id_org=org.id_org, playlist_name="Child PL",
                           id_governance_node=store.id_node)
    free_pl = CmsPlaylist(id_org=org.id_org, playlist_name="Org PL")
    db_session.add_all([gov_pl, child_pl, free_pl])
    uid = uuid.uuid4().hex[:6]
    free_media = CmsMedia(id_org=org.id_org, media_name=f"m-{uid}", media_type="image",
                          storage_key=f"k/{uid}", checksum_sha256=uid * 4)
    db_session.add(free_media)
    await db_session.commit()
    for obj in (gov_pl, child_pl, free_pl, free_media):
        await db_session.refresh(obj)

    return {
        "org": org, "national": national, "store": store,
        "programmer": programmer, "editor": editor, "approver": approver,
        "classic": classic, "org_admin": org_admin,
        "gov_pl": gov_pl, "child_pl": child_pl, "free_pl": free_pl,
        "free_media": free_media,
    }


# ── G4 : la sémantique de périmètre portée par le rôle ───────────────────────

@pytest.mark.asyncio
async def test_g4_scoped_roles_lose_access_to_ungoverned(
    db_session: AsyncSession, scene
) -> None:
    for key in ("programmer", "editor", "approver"):
        assert await user_is_governance_scoped(db_session, scene[key]) is True
        # Le contenu SANS nœud n'est plus « libre » pour eux (c'était `True` avant).
        assert await user_can_edit_node(db_session, scene[key], None) is False
    # Rôle classique et org-admin : inchangés.
    assert await user_is_governance_scoped(db_session, scene["classic"]) is False
    assert await user_can_edit_node(db_session, scene["classic"], None) is True
    assert await user_is_governance_scoped(db_session, scene["org_admin"]) is False
    assert await user_can_edit_node(db_session, scene["org_admin"], None) is True


@pytest.mark.asyncio
async def test_g4_ungoverned_content_is_invisible_to_scoped_roles(
    client: AsyncClient, scene
) -> None:
    prog = scene["programmer"]

    names = [p["playlist_name"] for p in
             (await client.get("/api/v1/playlists/", headers=_h(prog))).json()]
    assert "Store PL" in names and "Org PL" not in names

    # Deep-link : une playlist non gouvernée n'existe pas pour lui (404, pas 403).
    r = await client.get(f"/api/v1/playlists/{scene['free_pl'].id_playlist}", headers=_h(prog))
    assert r.status_code == 404, r.text
    # …et il ne peut pas l'éditer non plus (N3 fermé).
    r = await client.put(f"/api/v1/playlists/{scene['free_pl'].id_playlist}",
                         json={"description": "hack"}, headers=_h(prog))
    assert r.status_code in (403, 404), r.text

    # ⚠️ EXCEPTION MÉDIAS (décision d'Ed, 2026-07-29) : la médiathèque non gouvernée reste
    # LISIBLE pour ces rôles — sinon ils ne peuvent composer aucune playlist (mesuré : 0
    # média gouverné sur 89 en prod, donc médiathèque VIDE et module inutilisable). Elle
    # n'est pas éditable pour autant : c'est `test_media_ungoverned_readable_not_editable`
    # qui tient l'autre moitié de la règle.
    r = await client.get("/api/v1/media/", headers=_h(scene["editor"]))
    assert r.status_code == 200, r.text
    assert scene["free_media"].media_name in [m["media_name"] for m in r.json()]

    # L'org-admin, lui, voit tout — la restriction ne déborde pas.
    names = [p["playlist_name"] for p in
             (await client.get("/api/v1/playlists/", headers=_h(scene["org_admin"]))).json()]
    assert {"Store PL", "Org PL"} <= set(names)


@pytest.mark.asyncio
async def test_media_ungoverned_readable_not_editable(
    client: AsyncClient, db_session: AsyncSession, scene
) -> None:
    """Décision d'Ed du 2026-07-29 : « le siège fournit les visuels, le magasin les programme ».

    La médiathèque non gouvernée se LIT (liste, détail, recherche) et se PROGRAMME (ajout à
    une playlist du nœud), mais ne se MODIFIE pas. Les deux moitiés comptent autant l'une que
    l'autre : lisible sans être éditable, c'est le contrat ; l'une sans l'autre serait soit
    l'impasse d'avant (rien de composable), soit la réouverture de N3 (tout modifiable).
    """
    editor, media = scene["editor"], scene["free_media"]
    mid = media.id_media

    # ── LISIBLE ──────────────────────────────────────────────────────────────
    r = await client.get(f"/api/v1/media/{mid}", headers=_h(editor))
    assert r.status_code == 200, r.text
    # …et ANNONCÉ en lecture seule : la liste ne doit pas promettre une édition qui finira
    # en 403 (c'est précisément le mensonge que `governed_readonly` existe pour éviter).
    listed = next(m for m in (await client.get("/api/v1/media/", headers=_h(editor))).json()
                  if m["id_media"] == mid)
    assert listed["governed_readonly"] is True
    assert r.json()["governed_readonly"] is True

    # La recherche globale dit la même chose que la liste qu'elle indexe.
    groups = (await client.get(f"/api/v1/search?q={media.media_name}",
                               headers=_h(editor))).json()["groups"]
    found = [i["id"] for g in groups if g["kind"] == "media" for i in g["items"]]
    assert mid in found

    # ── PROGRAMMABLE ─────────────────────────────────────────────────────────
    r = await client.post(f"/api/v1/playlists/{scene['gov_pl'].id_playlist}/items",
                          json={"id_media": mid, "sort_order": 0}, headers=_h(editor))
    assert r.status_code == 201, f"le média commun doit pouvoir être programmé : {r.text[:200]}"

    # ── PAS ÉDITABLE ─────────────────────────────────────────────────────────
    r = await client.patch(f"/api/v1/media/{mid}/rename",
                           json={"media_name": "renommé de force"}, headers=_h(editor))
    assert r.status_code == 403, r.text
    r = await client.patch(f"/api/v1/media/{mid}/governance-node",
                           json={"id_governance_node": scene["store"].id_node},
                           headers=_h(editor))
    assert r.status_code == 403, "s'attribuer un média commun = le retirer aux autres"
    r = await client.delete(f"/api/v1/media/{mid}", headers=_h(editor))
    assert r.status_code == 403, r.text

    await db_session.refresh(media)
    assert media.media_name != "renommé de force"
    assert media.is_deleted is False

    # ⛔ L'élargissement ne vaut QUE pour les médias : une playlist non gouvernée reste
    # invisible (sinon on aurait rouvert N3 par la bande).
    r = await client.get(f"/api/v1/playlists/{scene['free_pl'].id_playlist}", headers=_h(editor))
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_g4_scoped_programmer_cannot_create_ungoverned_schedule(
    client: AsyncClient, scene
) -> None:
    """Un programme SANS nœud diffuserait hors de tout contrôle : c'est exactement le
    geste que l'audit a mesuré (programme créé vers le BrightSign, diffusion réelle)."""
    body = {
        "schedule_name": "Libre", "schedule_kind": "content",
        "id_playlist": scene["gov_pl"].id_playlist,
        "rules": [{"rule_type": "always"}], "targets": [],
    }
    r = await client.post("/api/v1/schedules", json=body, headers=_h(scene["programmer"]))
    assert r.status_code == 403, r.text

    # Avec son nœud, le même geste passe.
    r = await client.post("/api/v1/schedules",
                          json={**body, "id_governance_node": scene["store"].id_node},
                          headers=_h(scene["programmer"]))
    assert r.status_code == 201, r.text

    # Le rôle classique (CMS_SCHEDULER) garde le droit de créer un programme d'org —
    # avec une playlist LIBRE : `gov_pl` lui est invisible (D6, 404 au détail), et depuis
    # l'audit menu ⑧ programmer une playlist qu'on reçoit en 404 est refusé (le défaut
    # documenté le 27/07 : « pointer son programme vers une playlist invisible »).
    r = await client.post(
        "/api/v1/schedules",
        json={**body, "id_playlist": scene["free_pl"].id_playlist},
        headers=_h(scene["classic"]),
    )
    assert r.status_code == 201, r.text
    # Et la playlist gouvernée, invisible pour lui, reste non programmable : 404.
    r = await client.post("/api/v1/schedules", json=body, headers=_h(scene["classic"]))
    assert r.status_code == 404, r.text


# ── N4 : le soumissionnaire voit sa demande ──────────────────────────────────

@pytest.mark.asyncio
async def test_n4_programmer_can_submit_and_see_its_request(
    client: AsyncClient, db_session: AsyncSession, scene
) -> None:
    prog, pl = scene["programmer"], scene["gov_pl"]
    # Un brouillon à soumettre. ⚠️ Renommer/décrire ne crée PAS de brouillon : seuls les
    # ITEMS (ou les réglages de rotation) passent par `draft_items` — d'où le 409
    # « brouillon requis » si on se contente d'un PUT sur la description.
    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/items",
                          json={"id_child_playlist": scene["child_pl"].id_playlist,
                                "sort_order": 0},
                          headers=_h(prog))
    assert r.status_code == 201, r.text

    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/submit-approval",
                          headers=_h(prog))
    assert r.status_code in (200, 201), r.text

    # N4 : il voit SA demande (avant : 403, il ne voyait jamais le retour du valideur).
    r = await client.get("/api/v1/approvals/", headers=_h(prog))
    assert r.status_code == 200, r.text
    assert [x["resource_id"] for x in r.json()] == [pl.id_playlist]


# ── N5 : la décision est scopée, et jamais sur sa propre soumission ──────────

@pytest.mark.asyncio
async def test_n5_no_self_approval_and_parent_scope(
    client: AsyncClient, db_session: AsyncSession, scene
) -> None:
    prog, approver, pl = scene["programmer"], scene["approver"], scene["gov_pl"]

    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/items",
                          json={"id_child_playlist": scene["child_pl"].id_playlist,
                                "sort_order": 0},
                          headers=_h(prog))
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/submit-approval",
                          headers=_h(prog))
    assert r.status_code in (200, 201), r.text
    req_id = (await client.get("/api/v1/approvals/", headers=_h(approver))).json()[0]["id_request"]

    # L'auteur, même s'il détenait le droit d'approuver, ne valide pas sa soumission.
    await _grant(db_session, scene["org"], prog, "CMS_GOVERNANCE_APPROVER")
    r = await client.post(f"/api/v1/approvals/{req_id}/approve", json={}, headers=_h(prog))
    assert r.status_code == 403, r.text
    assert "own submission" in r.json()["detail"]

    # Un valideur d'un nœud PARENT décide.
    r = await client.post(f"/api/v1/approvals/{req_id}/approve", json={}, headers=_h(approver))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_n5_org_admin_can_still_decide_its_own_submission(
    client: AsyncClient, scene
) -> None:
    """Anti-blocage : l'org-admin doit pouvoir trancher SA propre soumission.

    La publication directe d'une playlist gouvernée sous modération est refusée à TOUT LE
    MONDE (409, garde D7) : interdire à l'org-admin de valider sa soumission enfermerait
    sans issue une installation à un seul administrateur — le cas normal chez un client.
    Le défaut N5 mesuré visait un admin de NŒUD, pas lui.
    """
    admin, pl = scene["org_admin"], scene["gov_pl"]
    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/items",
                          json={"id_child_playlist": scene["child_pl"].id_playlist,
                                "sort_order": 0},
                          headers=_h(admin))
    assert r.status_code == 201, r.text
    # La publication DIRECTE lui est bien refusée (c'est ce qui crée le risque de blocage).
    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/publish", headers=_h(admin))
    assert r.status_code == 409, r.text

    r = await client.post(f"/api/v1/playlists/{pl.id_playlist}/submit-approval",
                          headers=_h(admin))
    assert r.status_code in (200, 201), r.text
    req_id = (await client.get("/api/v1/approvals/", headers=_h(admin))).json()[0]["id_request"]
    r = await client.post(f"/api/v1/approvals/{req_id}/approve", json={}, headers=_h(admin))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_n5_sibling_branch_cannot_decide(
    client: AsyncClient, db_session: AsyncSession, scene, org
) -> None:
    """Un valideur d'une AUTRE branche ne voit ni ne décide la demande."""
    other_root = await _mk_node(db_session, org, "Autre enseigne")
    outsider = await _user_with_roles(db_session, org, "CMS_GOVERNANCE_APPROVER")
    db_session.add(CmsGovernanceNodeAdmin(id_node=other_root.id_node, id_user=outsider.id_user))
    await db_session.commit()

    prog, pl = scene["programmer"], scene["gov_pl"]
    await client.post(f"/api/v1/playlists/{pl.id_playlist}/items",
                      json={"id_child_playlist": scene["child_pl"].id_playlist,
                            "sort_order": 0},
                      headers=_h(prog))
    await client.post(f"/api/v1/playlists/{pl.id_playlist}/submit-approval", headers=_h(prog))
    req_id = (await client.get(
        "/api/v1/approvals/", headers=_h(scene["approver"]))).json()[0]["id_request"]

    # La file de l'étranger est vide : on ne montre pas ce qu'on ne peut pas trancher.
    assert (await client.get("/api/v1/approvals/", headers=_h(outsider))).json() == []
    r = await client.post(f"/api/v1/approvals/{req_id}/approve", json={}, headers=_h(outsider))
    assert r.status_code in (403, 404), r.text


async def _grant(db: AsyncSession, org, user: CmsUser, role_key: str) -> None:
    role = await _role(db, org, role_key)
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
