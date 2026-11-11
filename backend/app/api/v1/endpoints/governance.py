"""
Gouvernance hiérarchique §XVIII — fondation (CRUD).

Arbre de gouvernance, assignations (player|group|site) et règles de délégation.
Le moteur d'application (compiled-schedule, budgets, préemption §18.4) = Phase 2.

GET    /governance/nodes                  — arbre de l'org (tous les nœuds)
POST   /governance/nodes                  — créer un nœud
GET    /governance/nodes/{id}             — détail + enfants + règles + assignations
PUT    /governance/nodes/{id}             — renommer / changer le type
DELETE /governance/nodes/{id}             — supprimer (bloqué si enfants ou schedules)
GET    /governance/nodes/{id}/subtree     — sous-arbre complet
GET    /governance/nodes/{id}/players     — players ciblés (directs + hérités)
POST   /governance/nodes/{id}/assignments — rattacher player|group|site
DELETE /governance/nodes/{id}/assignments — détacher
GET    /governance/nodes/{id}/rules       — règles du nœud
POST   /governance/nodes/{id}/rules       — créer une règle
PUT    /governance/rules/{id}             — modifier (params, soft_cut, is_active)
DELETE /governance/rules/{id}             — supprimer
"""

import json
from datetime import datetime
from typing import Annotated, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_permission
from app.models.governance import (
    CmsDelegationRule,
    CmsGovernanceAssignment,
    CmsGovernanceNode,
    CmsGovernanceNodeAdmin,
)
from app.models.media import CmsMedia
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroup, CmsPlayerGroupMember
from app.models.organization import CmsSite
from app.models.site_group import CmsSiteGroup, CmsSiteGroupMember
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.schedule import CmsSchedule, CmsScheduleRule
from app.models.user import CmsUser
from app.services.governance_engine import (
    CompiledItem,
    DelegationConstraints,
    LayerInput,
    node_budget_status,
    parse_constraints,
)
from app.services import rbac
from app.services.governance_access import (
    editable_node_ids,
    effective_max_item_sec,
    user_can_edit_node,
    visible_node_ids,
)
from app.services.governance_scope import (
    node_coverage,
    node_perimeter_players,
    orphan_players,
    scoped_schedule_ids,
    user_admin_nodes,
)
from app.services.playlist_rotation import rotation_seed, unroll_slots
from app.services.scheduling import local_now, schedule_active_at, time_rule_from_schedule
from app.services.targeting import Target, players_for_targets
from app.schemas.governance import (
    AssignmentCreate,
    AssignmentRead,
    BudgetStatusRead,
    NodeAdminCreate,
    NodeAdminRead,
    NodeCreate,
    NodeDetail,
    NodeRead,
    NodeUpdate,
    RuleCreate,
    RuleRead,
    RuleUpdate,
)

router = APIRouter(prefix="/governance", tags=["governance"])

_VALID_RULE_TYPES = {
    "time_budget", "item_count", "time_window", "preemptive",
}  # frequency_cap / position_lock / priority_band retirés (D3, 2026-07-24)
# Les 4 familles de restriction (migr 0110) sont exactement les 4 types de règle : une case
# par famille gouverne le droit d'écrire la règle correspondante. Tuple ordonné = ordre
# d'affichage stable à l'écran.
_RULE_FAMILIES = ("item_count", "time_budget", "time_window", "preemptive")
# `site_group` ajouté le 2026-07-26 (P1 de la revue) : sans lui, la sélection de périmètre
# sur carte ne pouvait produire qu'une liste de sites à plat — un « groupe de sites »
# créé depuis le panier n'aurait été assignable nulle part.
_VALID_TARGET_TYPES = {"player", "group", "site", "site_group"}


async def _get_node(db: AsyncSession, id_org: int, id_node: int) -> CmsGovernanceNode:
    node = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node,
            CmsGovernanceNode.id_org == id_org,
        )
    )).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return node


async def _get_visible_node(
    db: AsyncSession, user: CmsUser, id_node: int
) -> CmsGovernanceNode:
    """Nœud de l'org **visible** par l'utilisateur — 404 sinon (N7).

    Le détail d'un nœud d'une branche étrangère répondait 200 : on y lisait son nom, ses
    règles, ses affectations et **ses administrateurs**. 404 et non 403, comme partout
    ailleurs : ce qui est hors périmètre n'existe pas.
    """
    node = await _get_node(db, user.id_org, id_node)
    visible = await visible_node_ids(db, user)
    if visible is not None and id_node not in visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return node


async def _child_counts(db: AsyncSession, id_org: int) -> dict[int, int]:
    rows = (await db.execute(
        select(CmsGovernanceNode.parent_id, func.count())
        .where(CmsGovernanceNode.id_org == id_org, CmsGovernanceNode.parent_id.is_not(None))
        .group_by(CmsGovernanceNode.parent_id)
    )).all()
    return {pid: cnt for pid, cnt in rows}


def _to_read(
    node: CmsGovernanceNode, child_count: int = 0, editable: set[int] | None = None
) -> NodeRead:
    """`editable` = None ⇒ tous les nœuds sont éditables (org-admin), cf. editable_node_ids."""
    r = NodeRead.model_validate(node)
    r.child_count = child_count
    r.can_edit = editable is None or node.id_node in editable
    return r


async def _can_disable_moderation(
    db: AsyncSession, user: CmsUser, node: CmsGovernanceNode
) -> bool:
    """D7 : l'utilisateur peut-il RETIRER la modération de `node` ?

    Réservé à un admin d'un ancêtre STRICT (ou org-admin) — un nœud ne se dispense pas
    lui-même de modération. À la racine (aucun ancêtre), seul l'org-admin le peut.
    """
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
        return True
    if node.parent_id is None:
        return False  # nœud racine : pas d'ancêtre → org-admin seulement (déjà écarté)
    # user_can_edit_node(parent) = admin du parent ou d'un de SES ancêtres = ancêtre strict du nœud.
    return await user_can_edit_node(db, user, node.parent_id)


async def _enforce_structure_edit(
    db: AsyncSession, user: CmsUser, id_node: int | None
) -> None:
    """N1 (audit 2026-07-27) — les routes de STRUCTURE se cloisonnent comme le contenu.

    La permission RBAC dit ce qu'on peut FAIRE, l'assignation d'admin de nœud dit OÙ.
    Sans cette garde, un porteur de `governance:delegate` se nommait admin de la RACINE
    (201) et passait de 403 à 200 sur le contenu national — alors que le contenu et les
    règles, eux, étaient cloisonnés. `id_node` None = créer un nœud RACINE : réservé à
    l'org-admin (aucun sous-arbre n'existe pour porter ce droit).
    """
    if id_node is None:
        role_keys = await rbac.get_user_role_keys(db, user.id_user)
        if not {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an organization administrator can create a root node",
            )
        return
    if not await user_can_edit_node(db, user, id_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not administer this governance node",
        )


# ── Nœuds ─────────────────────────────────────────────────────────────────────

@router.get("/nodes", response_model=list[NodeRead])
async def list_nodes(
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    parent_id: Optional[int] = Query(None),
) -> list[NodeRead]:
    """Liste les nœuds de l'org (tout l'arbre, ou seulement les enfants directs de parent_id).

    N7 (audit 2026-07-27) : l'arbre rendait **les 11 nœuds à tout le monde** — D6 protégeait
    le CONTENU, jamais la STRUCTURE. Il est désormais filtré par `visible_node_ids`, c'est-à-
    dire le sous-arbre administré **plus ses ancêtres en lecture** : comprendre ce qui passe
    au-dessus de soi fait partie du métier, voir la branche du voisin non.
    """
    stmt = select(CmsGovernanceNode).where(CmsGovernanceNode.id_org == current_user.id_org)
    if parent_id is not None:
        stmt = stmt.where(CmsGovernanceNode.parent_id == parent_id)
    visible = await visible_node_ids(db, current_user)
    if visible is not None:
        stmt = stmt.where(CmsGovernanceNode.id_node.in_(visible or {-1}))
    stmt = stmt.order_by(CmsGovernanceNode.path)
    nodes = (await db.execute(stmt)).scalars().all()
    counts = await _child_counts(db, current_user.id_org)
    # can_edit : le sélecteur de nœud (playlists/médias, contenu gouverné D6/D9) n'offre
    # que ce que l'utilisateur peut réellement écrire — sinon l'assignation part en 403.
    editable = await editable_node_ids(db, current_user)
    return [_to_read(n, counts.get(n.id_node, 0), editable) for n in nodes]


@router.post("/nodes", response_model=NodeRead, status_code=status.HTTP_201_CREATED)
async def create_node(
    body: NodeCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NodeRead:
    parent: CmsGovernanceNode | None = None
    if body.parent_id is not None:
        parent = await _get_node(db, current_user.id_org, body.parent_id)
    # N1 : on crée un nœud SOUS un nœud qu'on administre ; une racine = org-admin seul.
    await _enforce_structure_edit(db, current_user, body.parent_id)

    # D7 : requires_approval hérité du parent (true à la racine), surchargeable par le body.
    if body.requires_approval is not None:
        requires_approval = body.requires_approval
    else:
        requires_approval = parent.requires_approval if parent else True

    node = CmsGovernanceNode(
        id_org=current_user.id_org,
        parent_id=parent.id_node if parent else None,
        node_name=body.node_name,
        node_level=(parent.node_level + 1) if parent else 0,
        node_type=body.node_type,
        requires_approval=requires_approval,
        path="",
    )
    db.add(node)
    await db.flush()  # obtient id_node
    node.path = f"{parent.path}.{node.id_node}" if parent else str(node.id_node)
    await db.commit()
    await db.refresh(node)
    return _to_read(node, 0)


@router.get("/nodes/{id_node}", response_model=NodeDetail)
async def get_node(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NodeDetail:
    node = await _get_visible_node(db, current_user, id_node)

    children = (await db.execute(
        select(CmsGovernanceNode)
        .where(CmsGovernanceNode.parent_id == id_node)
        .order_by(CmsGovernanceNode.node_name)
    )).scalars().all()
    counts = await _child_counts(db, current_user.id_org)

    rules = (await db.execute(
        select(CmsDelegationRule)
        .where(CmsDelegationRule.id_node == id_node)
        .order_by(CmsDelegationRule.id_rule)
    )).scalars().all()

    assignments = await _resolve_assignment_names(db, current_user.id_org, id_node)
    admins = await _resolve_node_admins(db, id_node)

    editable = await editable_node_ids(db, current_user)
    detail = NodeDetail.model_validate(node)
    detail.child_count = counts.get(id_node, 0)
    detail.can_edit = editable is None or id_node in editable
    detail.can_disable_moderation = await _can_disable_moderation(db, current_user, node)
    detail.children = [_to_read(c, counts.get(c.id_node, 0), editable) for c in children]
    detail.rules = [RuleRead.model_validate(r) for r in rules]
    detail.assignments = assignments
    detail.admins = admins
    # Restrictions des règles : le front doit pouvoir GRISER plutôt que laisser tomber en 403,
    # et borner les champs aux plafonds hérités plutôt que d'encaisser un 422 à l'envoi.
    above = await _locked_above(db, node)
    detail.rules_locked_above = above
    detail.rules_locked_effective = {
        f: above[f] or _locked_here(node, f) for f in _RULE_FAMILIES
    }
    ancestor_admin = await _is_strict_ancestor_admin(db, current_user, node)
    detail.can_set_rules_delegation = ancestor_admin
    own_editor = await user_can_edit_node(db, current_user, id_node)
    detail.can_edit_rule_types = {
        f: ancestor_admin or (own_editor and not detail.rules_locked_effective[f])
        for f in _RULE_FAMILIES
    }
    caps = await _inherited_caps(db, node)
    detail.inherited_max_per_turn = caps["max_for_children"]
    detail.inherited_max_item_sec = caps["max_item_sec"]
    return detail


@router.put("/nodes/{id_node}", response_model=NodeRead)
async def update_node(
    id_node: int,
    body: NodeUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NodeRead:
    node = await _get_node(db, current_user.id_org, id_node)
    # N1 : renommer/reconfigurer un nœud = dans son sous-arbre seulement. Les gardes plus
    # fines ci-dessous (décocher la modération, lever une restriction = ancêtre strict)
    # restent des SUR-restrictions de celle-ci.
    await _enforce_structure_edit(db, current_user, id_node)
    moved_players: set[int] = set()
    if "parent_id" in body.model_fields_set and body.parent_id != node.parent_id:
        # P3 (revue du 2026-07-23) : la forme de l'arbre se décidait à la CRÉATION — se
        # corriger imposait de détacher les programmes et de tout recréer.
        moved_players = await _reparent_node(db, current_user, node, body.parent_id)
    if body.node_name is not None:
        node.node_name = body.node_name
    if "node_type" in body.model_fields_set:
        node.node_type = body.node_type
    if "requires_approval" in body.model_fields_set and body.requires_approval is not None \
            and body.requires_approval != node.requires_approval:
        # D7 : DÉCOCHER (retirer la modération) est réservé à un admin d'ANCÊTRE (ou org-admin) —
        # sinon un nœud se dispenserait lui-même de modération et elle ne vaudrait rien. RE-COCHER
        # (durcir) est permis à tout éditeur du nœud.
        if body.requires_approval is False:
            allowed = await _can_disable_moderation(db, current_user, node)
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only an administrator of a parent node can remove moderation",
                )
        node.requires_approval = body.requires_approval
    # Recompilation à pousser (R12) : les restrictions et les plafonds changent ce que le
    # nœud ET son sous-arbre ont le droit de diffuser, donc le programme compilé.
    touched_governance = False
    if "rules_locked" in body.model_fields_set and body.rules_locked is not None:
        current = dict(node.rules_locked or {})
        for family, locked in body.rules_locked.items():
            if family not in _RULE_FAMILIES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown rule family: {family}",
                )
            if bool(locked) == _locked_here(node, family):
                continue
            # LEVER une restriction ne se fait QUE depuis un nœud parent (ou l'org-admin) :
            # un nœud qui s'auto-délègue viderait le modèle de son sens — c'est précisément
            # ce que la délégation doit empêcher (décision Ed 2026-07-26). La REPOSER
            # (durcir) reste ouverte à tout éditeur du nœud, comme pour `requires_approval`.
            if not locked and not await _is_strict_ancestor_admin(db, current_user, node):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only an administrator of a parent node can lift a "
                           "restriction on rules",
                )
            current[family] = bool(locked)
        # JSONB : réaffecter un NOUVEAU dict, sinon SQLAlchemy ne voit pas la mutation.
        node.rules_locked = current
        touched_governance = True
    # Plafonds imposés aux descendants. Ils décrivent le droit des AUTRES : les régler
    # demande d'être éditeur de ce nœud, et jamais de dépasser ce qu'on a soi-même reçu —
    # sinon un département rouvrirait à ses sites la porte que le national lui a fermée.
    caps_wanted = {
        k: getattr(body, k) for k in ("max_for_children", "max_item_sec")
        if k in body.model_fields_set
    }
    if caps_wanted:
        if not await user_can_edit_node(db, current_user, id_node):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not administer this node",
            )
        inherited = await _inherited_caps(db, node)
        labels = {
            "max_for_children": "The level above limits this node to {cap} item(s) per loop.",
            "max_item_sec": "The level above limits an item's duration to {cap} s.",
        }
        for key, value in caps_wanted.items():
            cap = inherited[key]
            if value is not None and cap is not None and value > cap:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=labels[key].format(cap=cap),
                )
            if value is not None and value < 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A cap must be at least 1 (leave empty = no cap).",
                )
            setattr(node, key, value)
        touched_governance = True
    await db.commit()
    await db.refresh(node)
    if touched_governance or moved_players:
        await _force_sync_node(db, current_user.id_org, id_node)
    # Déplacement : les écrans du sous-arbre changent d'ancêtres, donc de couches héritées.
    # Ceux capturés AVANT le déplacement doivent recompiler eux aussi (R12).
    await _push_force_sync(moved_players)
    counts = await _child_counts(db, current_user.id_org)
    return _to_read(node, counts.get(id_node, 0))


async def _reparent_node(
    db: AsyncSession, user: CmsUser, node: CmsGovernanceNode, new_parent_id: int | None
) -> set[int]:
    """P3 — déplace un nœud (et tout son sous-arbre) sous un nouveau parent.

    Recalcule `path` et `node_level` de proche en proche : ce sont eux qui portent la
    hiérarchie (l'ordre national→local de la compilation, l'héritage des restrictions, la
    modération D7, la garde d'édition). Les laisser périmés ferait mentir tout le module.

    Gardes :
      · le nœud cible doit être administré (`_enforce_structure_edit`), et **le nouveau
        parent aussi** — on ne greffe pas une branche dans un périmètre qu'on n'administre
        pas ; en faire une RACINE reste réservé à l'org-admin, comme la création ;
      · **jamais dans son propre sous-arbre** (ni sur lui-même) : ça détacherait la branche
        de l'arbre en silence, sans erreur, et la rendrait irrécupérable.

    Renvoie les écrans du sous-arbre **avant** déplacement (à faire recompiler).
    """
    if new_parent_id == node.id_node:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A node cannot be its own parent.",
        )
    await _enforce_structure_edit(db, user, new_parent_id)

    subtree = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_org == node.id_org,
            CmsGovernanceNode.path.like(f"{node.path}%"),
        )
    )).scalars().all()
    affected = await _node_subtree_players(db, node.id_org, node)

    new_parent: CmsGovernanceNode | None = None
    if new_parent_id is not None:
        new_parent = await _get_node(db, node.id_org, new_parent_id)
        if new_parent.id_node in {n.id_node for n in subtree}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A node cannot be moved under one of its own descendants.",
            )

    old_prefix = node.path
    new_path = f"{new_parent.path}.{node.id_node}" if new_parent else str(node.id_node)
    new_level = (new_parent.node_level + 1) if new_parent else 0
    delta = new_level - node.node_level

    for n in subtree:
        # `path` est un préfixe matérialisé : on remplace la partie qui a bougé et on
        # décale les niveaux du même écart pour tout le sous-arbre.
        n.path = new_path + n.path[len(old_prefix):]
        n.node_level = n.node_level + delta
    node.parent_id = new_parent_id
    return affected


@router.delete("/nodes/{id_node}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    node = await _get_node(db, current_user.id_org, id_node)
    await _enforce_structure_edit(db, current_user, id_node)  # N1

    has_child = (await db.execute(
        select(CmsGovernanceNode.id_node).where(CmsGovernanceNode.parent_id == id_node).limit(1)
    )).scalar_one_or_none()
    if has_child is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Node is not empty: delete its child nodes first",
        )

    has_sched = (await db.execute(
        select(CmsSchedule.id_schedule)
        .where(CmsSchedule.id_governance_node == id_node, CmsSchedule.is_deleted == False)  # noqa: E712
        .limit(1)
    )).scalar_one_or_none()
    if has_sched is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Schedules are attached to this node",
        )

    # R12 : capturer les players ancrés AVANT la suppression (cascade) — supprimer le nœud
    # peut retirer leur ancrage et changer ce qu'ils voient des couches ancêtres.
    affected = await _node_subtree_players(db, current_user.id_org, node)
    # assignations + règles supprimées par ON DELETE CASCADE ; baskets id_node → NULL
    await db.delete(node)
    await db.commit()
    await _push_force_sync(affected)


@router.get("/nodes/{id_node}/subtree", response_model=list[NodeRead])
async def node_subtree(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[NodeRead]:
    node = await _get_visible_node(db, current_user, id_node)
    nodes = (await db.execute(
        select(CmsGovernanceNode)
        .where(
            CmsGovernanceNode.id_org == current_user.id_org,
            CmsGovernanceNode.path.like(f"{node.path}%"),
        )
        .order_by(CmsGovernanceNode.path)
    )).scalars().all()
    counts = await _child_counts(db, current_user.id_org)
    editable = await editable_node_ids(db, current_user)
    return [_to_read(n, counts.get(n.id_node, 0), editable) for n in nodes]


@router.get("/nodes/{id_node}/players", response_model=list[int])
async def node_players(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[int]:
    """IDs des players ciblés par le nœud et son sous-arbre (directs + via groupes + via sites)."""
    node = await _get_visible_node(db, current_user, id_node)
    return sorted(await _node_subtree_players(db, current_user.id_org, node))


async def _node_subtree_players(
    db: AsyncSession, id_org: int, node: CmsGovernanceNode
) -> set[int]:
    """Players ciblés par le nœud et son sous-arbre (directs + via groupes + via sites).

    Délègue à `governance_scope` : le périmètre d'un nœud sert désormais aussi à filtrer les
    cibles d'un programme (D-b) et à mesurer la couverture — une seule définition, sinon
    l'aperçu et la diffusion finissent par diverger.
    """
    return await node_perimeter_players(db, id_org, node)


@router.get("/nodes/{id_node}/coverage")
async def node_coverage_endpoint(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Couverture du nœud par SES programmes (D-b) : écrans retenus par programme + écrans
    du périmètre qu'aucune couche locale ne vise.

    Ce dernier compteur est la contrepartie assumée de D1 (« les écrans d'entrée » = un
    groupe de players) : un écran oublié dans son groupe ne reçoit que l'héritage, et rien
    d'autre ne le signalerait.
    """
    node = await _get_visible_node(db, current_user, id_node)
    return await node_coverage(db, current_user.id_org, node)


async def _push_force_sync(player_ids: set[int]) -> None:
    """Publie player:force_sync sur le canal Redis de chaque player (recompilation immédiate)."""
    if not player_ids:
        return
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        payload = json.dumps({"type": "player:force_sync", "preemptive": False})
        for pid in player_ids:
            await r.publish(f"signflow:player:{pid}:cmds", payload)
    finally:
        await r.aclose()


async def _force_sync_node(db: AsyncSession, id_org: int, id_node: int) -> None:
    """R12 (règle d'or) : toute mutation de gouvernance doit faire recompiler les écrans
    concernés = ceux du nœud ET de son sous-arbre (une règle/assignation change leur
    programme compilé). Sans ça, les écrans n'appliquaient le changement qu'au sync suivant.
    """
    node = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == id_org,
        )
    )).scalar_one_or_none()
    if node is None:
        return
    await _push_force_sync(await _node_subtree_players(db, id_org, node))


# ── Assignations ─────────────────────────────────────────────────────────────

async def _resolve_assignment_names(
    db: AsyncSession, id_org: int, id_node: int
) -> list[AssignmentRead]:
    assigns = (await db.execute(
        select(CmsGovernanceAssignment).where(CmsGovernanceAssignment.id_node == id_node)
    )).scalars().all()
    if not assigns:
        return []

    p_ids = [a.target_id for a in assigns if a.target_type == "player"]
    g_ids = [a.target_id for a in assigns if a.target_type == "group"]
    s_ids = [a.target_id for a in assigns if a.target_type == "site"]
    sg_ids = [a.target_id for a in assigns if a.target_type == "site_group"]
    names: dict[tuple[str, int], str] = {}
    if p_ids:
        for pid, name in (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name).where(CmsPlayer.id_player.in_(p_ids))
        )).all():
            names[("player", pid)] = name
    if g_ids:
        for gid, name in (await db.execute(
            select(CmsPlayerGroup.id_group, CmsPlayerGroup.group_name).where(CmsPlayerGroup.id_group.in_(g_ids))
        )).all():
            names[("group", gid)] = name
    if s_ids:
        for sid, name in (await db.execute(
            select(CmsSite.id_site, CmsSite.site_name).where(CmsSite.id_site.in_(s_ids))
        )).all():
            names[("site", sid)] = name
    if sg_ids:
        for sgid, name in (await db.execute(
            select(CmsSiteGroup.id_site_group, CmsSiteGroup.group_name)
            .where(CmsSiteGroup.id_site_group.in_(sg_ids))
        )).all():
            names[("site_group", sgid)] = name

    return [
        AssignmentRead(
            target_type=a.target_type,
            target_id=a.target_id,
            target_name=names.get((a.target_type, a.target_id)),
        )
        for a in assigns
    ]


@router.post("/nodes/{id_node}/assignments", response_model=AssignmentRead, status_code=status.HTTP_201_CREATED)
async def add_assignment(
    id_node: int,
    body: AssignmentCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssignmentRead:
    await _get_node(db, current_user.id_org, id_node)
    # N1 : une assignation change le PÉRIMÈTRE du nœud (donc la diffusion) — sous-arbre only.
    await _enforce_structure_edit(db, current_user, id_node)
    if body.target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_type must be player | group | site | site_group",
        )
    # Vérifie que la cible appartient à l'org (cross-tenant = 404)
    await _check_target_org(db, current_user.id_org, body.target_type, body.target_id)
    # G1/G2 : rattacher ÉLARGIT un périmètre — la cible doit rester dans la portée de
    # l'auteur : ses sous-arbres, plus les écrans ORPHELINS (porte de rattachement G2,
    # ouverte à tout admin de nœud depuis la révision de G3 par Ed le 2026-07-27 : un
    # écran neuf se rattache sur place, sans passer par le siège). Un écran gouverné
    # AILLEURS reste refusé EN LE NOMMANT. L'org-admin n'est pas contraint : c'est lui
    # qui recompose les périmètres.
    await _enforce_target_in_scope(db, current_user, body.target_type, body.target_id)

    exists = (await db.execute(
        select(CmsGovernanceAssignment).where(
            CmsGovernanceAssignment.id_node == id_node,
            CmsGovernanceAssignment.target_type == body.target_type,
            CmsGovernanceAssignment.target_id == body.target_id,
        )
    )).scalar_one_or_none()
    if exists is None:
        db.add(CmsGovernanceAssignment(
            id_node=id_node, target_type=body.target_type, target_id=body.target_id
        ))
        await db.commit()
        await _force_sync_node(db, current_user.id_org, id_node)

    results = await _resolve_assignment_names(db, current_user.id_org, id_node)
    return next(
        (a for a in results if a.target_type == body.target_type and a.target_id == body.target_id),
        AssignmentRead(target_type=body.target_type, target_id=body.target_id),
    )


@router.post("/nodes/{id_node}/assignments/batch", response_model=list[AssignmentRead],
             status_code=status.HTTP_201_CREATED)
async def add_assignments_batch(
    id_node: int,
    body: list[AssignmentCreate],
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AssignmentRead]:
    """Ajoute PLUSIEURS cibles en une fois — R4 (« 40 écrans = 40 allers-retours »).

    **Tout ou rien** : les gardes sont évaluées sur l'ensemble AVANT d'écrire quoi que ce
    soit. Une sélection à moitié appliquée serait pire que refusée — on ne saurait pas ce
    que le nœud diffuse. Un seul `force_sync` à la fin, pas un par cible.
    """
    await _get_node(db, current_user.id_org, id_node)
    await _enforce_structure_edit(db, current_user, id_node)
    if not body:
        return await _resolve_assignment_names(db, current_user.id_org, id_node)
    if len(body) > 200:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="At most 200 targets per request.")
    for item in body:
        if item.target_type not in _VALID_TARGET_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="target_type must be player | group | site | site_group",
            )
        await _check_target_org(db, current_user.id_org, item.target_type, item.target_id)
        await _enforce_target_in_scope(db, current_user, item.target_type, item.target_id)

    existing = {(a.target_type, a.target_id) for a in (await db.execute(
        select(CmsGovernanceAssignment).where(CmsGovernanceAssignment.id_node == id_node)
    )).scalars().all()}
    added = 0
    for item in body:
        if (item.target_type, item.target_id) in existing:
            continue  # idempotent : re-sélectionner une cible déjà posée n'est pas une erreur
        db.add(CmsGovernanceAssignment(
            id_node=id_node, target_type=item.target_type, target_id=item.target_id))
        existing.add((item.target_type, item.target_id))
        added += 1
    if added:
        await db.commit()
        await _force_sync_node(db, current_user.id_org, id_node)
    return await _resolve_assignment_names(db, current_user.id_org, id_node)


@router.delete("/nodes/{id_node}/assignments", status_code=status.HTTP_204_NO_CONTENT)
async def remove_assignment(
    id_node: int,
    body: AssignmentCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_node(db, current_user.id_org, id_node)
    await _enforce_structure_edit(db, current_user, id_node)  # N1
    # Capturer les players du target AVANT le delete : après, ils ne seront plus résolus
    # par le sous-arbre → ils doivent pourtant recompiler pour LÂCHER la couche.
    removed = await players_for_targets(
        db, current_user.id_org, [Target(body.target_type, body.target_id)],
    )
    await db.execute(
        delete(CmsGovernanceAssignment).where(
            CmsGovernanceAssignment.id_node == id_node,
            CmsGovernanceAssignment.target_type == body.target_type,
            CmsGovernanceAssignment.target_id == body.target_id,
        )
    )
    await db.commit()
    await _push_force_sync(removed)
    await _force_sync_node(db, current_user.id_org, id_node)


async def _check_target_org(db: AsyncSession, id_org: int, target_type: str, target_id: int) -> None:
    if target_type == "player":
        ok = (await db.execute(
            select(CmsPlayer.id_player).where(CmsPlayer.id_player == target_id, CmsPlayer.id_org == id_org)
        )).scalar_one_or_none()
    elif target_type == "group":
        ok = (await db.execute(
            select(CmsPlayerGroup.id_group).where(CmsPlayerGroup.id_group == target_id, CmsPlayerGroup.id_org == id_org)
        )).scalar_one_or_none()
    elif target_type == "site_group":
        ok = (await db.execute(
            select(CmsSiteGroup.id_site_group).where(
                CmsSiteGroup.id_site_group == target_id, CmsSiteGroup.id_org == id_org)
        )).scalar_one_or_none()
    else:  # site
        ok = (await db.execute(
            select(CmsSite.id_site).where(CmsSite.id_site == target_id, CmsSite.id_org == id_org)
        )).scalar_one_or_none()
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")


# ── Administrateurs de nœud (immutabilité §XVIII Phase 2) ────────────────────

async def _resolve_node_admins(db: AsyncSession, id_node: int) -> list[NodeAdminRead]:
    rows = (await db.execute(
        select(CmsUser.id_user, CmsUser.email, CmsUser.full_name)
        .join(CmsGovernanceNodeAdmin, CmsGovernanceNodeAdmin.id_user == CmsUser.id_user)
        .where(CmsGovernanceNodeAdmin.id_node == id_node)
        .order_by(CmsUser.email)
    )).all()
    return [NodeAdminRead(id_user=uid, email=email, full_name=name) for uid, email, name in rows]


@router.get("/nodes/{id_node}/admins", response_model=list[NodeAdminRead])
async def list_node_admins(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[NodeAdminRead]:
    await _get_visible_node(db, current_user, id_node)
    return await _resolve_node_admins(db, id_node)


async def _assignable_players(db: AsyncSession, user: CmsUser) -> set[int] | None:
    """Écrans que `user` peut faire ENTRER dans un nœud (None = aucune limite, org-admin).

    = périmètre de ses sous-arbres **∪ écrans orphelins** (porte de rattachement G2, ouverte
    à tout admin de nœud depuis la révision de G3). Défini une fois ici parce que trois
    surfaces doivent en dépendre : la validation d'écriture, le sélecteur de rattachement et
    la recherche de cibles — trois définitions divergentes = une cible offerte puis refusée.
    """
    admin_nodes = await user_admin_nodes(db, user)
    if admin_nodes is None:
        return None
    allowed = await orphan_players(db, user.id_org)
    for n in admin_nodes:
        allowed |= await node_perimeter_players(db, user.id_org, n)
    return allowed


async def _enforce_target_in_scope(
    db: AsyncSession, user: CmsUser, target_type: str, target_id: int
) -> None:
    """G1/G2 : rattacher ÉLARGIT un périmètre — la cible doit rester dans la portée de
    l'auteur (son sous-arbre + les écrans orphelins). Un écran gouverné AILLEURS est refusé
    **en le nommant** : un 422 nu sur un id n'apprend rien."""
    allowed = await _assignable_players(db, user)
    if allowed is None:
        return
    hit = await players_for_targets(db, user.id_org, [Target(target_type, target_id)])
    outside = hit - allowed
    if not outside:
        return
    names = (await db.execute(
        select(CmsPlayer.player_name)
        .where(CmsPlayer.id_player.in_(outside))
        .order_by(CmsPlayer.player_name)
    )).scalars().all()
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            "Target outside your governance scope: " + ", ".join(names)
            + ". These players are already attached to a node you do not administer — "
              "only its administrator (or the organization administrator) can move them."
        ),
    )


@router.get("/targets/search")
async def search_targets(
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query(max_length=100)] = "",
    types: Annotated[str, Query()] = "player,group,site,site_group",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[dict]:
    """Recherche de cibles assignables — R4 de la revue du 2026-07-23.

    Les trois listes déroulantes de la carte « Cibles » chargeaient **tout** le parc à chaque
    ouverture d'un nœud : à 2000 écrans, 2000 `<option>` natives, et une cible ajoutée à la
    fois. Même classe de défaut que le drawer « Ajouter un média » corrigé le 2026-07-18 —
    la sélection se fait **côté serveur**, pas dans une liste pré-chargée.

    Forme uniforme `{target_type, target_id, name, subtitle, player_count}` pour que l'écran
    n'ait pas à connaître quatre modèles, et **`player_count`** parce que dès qu'on mêle
    groupes, sites et groupes de sites on ne sait plus ce qu'on vient de sélectionner.

    ⚠️ Filtré par le **même** périmètre que l'écriture (`_assignable_players`) : une cible
    proposée ici doit être acceptée par `POST /assignments`, sinon on offre un 422.
    """
    wanted = {t.strip() for t in types.split(",") if t.strip()} & _VALID_TARGET_TYPES
    like = f"%{q.strip()}%"
    allowed = await _assignable_players(db, current_user)
    org = current_user.id_org
    # G2 : un écran encore rattaché à AUCUN nœud est proposé **et étiqueté** — c'est ce qui
    # rend l'oubli visible ET réparable (décision Ed). Sans l'étiquette, on ne saurait pas
    # qu'on est en train de faire entrer un écran dans la gouvernance.
    orphans = await orphan_players(db, org)
    out: list[dict] = []

    async def _keep(target_type: str, target_id: int) -> int | None:
        """Nombre d'écrans RETENUS pour cette cible ; None si elle n'en apporte aucun."""
        hit = await players_for_targets(db, org, [Target(target_type, target_id)])
        if allowed is not None:
            hit &= allowed
        return len(hit) if hit else None

    if "player" in wanted:
        rows = (await db.execute(
            select(CmsPlayer.id_player, CmsPlayer.player_name, CmsSite.site_name)
            .outerjoin(CmsSite, CmsSite.id_site == CmsPlayer.id_site)
            .where(CmsPlayer.id_org == org, CmsPlayer.player_name.ilike(like))
            .order_by(CmsPlayer.player_name).limit(limit * 3)
        )).all()
        for pid, name, site_name in rows:
            if allowed is not None and pid not in allowed:
                continue
            out.append({"target_type": "player", "target_id": pid, "name": name,
                        "subtitle": site_name, "player_count": 1,
                        "orphan": pid in orphans})
            if len(out) >= limit * 4:
                break

    if "group" in wanted:
        for gid, name in (await db.execute(
            select(CmsPlayerGroup.id_group, CmsPlayerGroup.group_name)
            .where(CmsPlayerGroup.id_org == org, CmsPlayerGroup.group_name.ilike(like))
            .order_by(CmsPlayerGroup.group_name).limit(limit)
        )).all():
            count = await _keep("group", gid)
            if count:
                out.append({"orphan": False, "target_type": "group", "target_id": gid, "name": name,
                            "subtitle": None, "player_count": count})

    if "site" in wanted:
        for sid, name, number in (await db.execute(
            select(CmsSite.id_site, CmsSite.site_name, CmsSite.site_number)
            .where(CmsSite.id_org == org, CmsSite.site_name.ilike(like))
            .order_by(CmsSite.site_name).limit(limit)
        )).all():
            count = await _keep("site", sid)
            if count:
                out.append({"orphan": False, "target_type": "site", "target_id": sid, "name": name,
                            "subtitle": number, "player_count": count})

    if "site_group" in wanted:
        for sgid, name in (await db.execute(
            select(CmsSiteGroup.id_site_group, CmsSiteGroup.group_name)
            .where(CmsSiteGroup.id_org == org, CmsSiteGroup.group_name.ilike(like))
            .order_by(CmsSiteGroup.group_name).limit(limit)
        )).all():
            count = await _keep("site_group", sgid)
            if count:
                out.append({"orphan": False, "target_type": "site_group", "target_id": sgid, "name": name,
                            "subtitle": None, "player_count": count})

    return out


@router.get("/admin-candidates")
async def admin_candidates(
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """Utilisateurs nommables administrateurs de nœud, **avec le verdict de leur rôle** (R8/R9).

    🔑 « Admin de nœud » n'accorde AUCUN droit : c'est une seconde grille qui **restreint**
    par-dessus le RBAC (`require_permission` s'exécute en dépendance, `_enforce_node_edit`
    seulement ensuite). Deux effets de bord silencieux que cet endpoint rend visibles :

    - nommer quelqu'un **sans `schedules:edit`** (un approbateur, p. ex.) produit un admin de
      nœud qui ne pourra strictement rien faire — et rien ne le signalait ;
    - nommer un **org-admin est un no-op** : il contourne déjà `user_can_edit_node`, donc on
      ne testera jamais l'immutabilité avec lui (piège vécu par Ed le 2026-07-23).

    Les rôles et permissions sont résolus **en lot** (2 requêtes au total) : la résolution
    unitaire de `rbac` aurait fait 2 requêtes PAR utilisateur.

    **N6 (audit 2026-07-27) — fuite d'annuaire fermée** : cet endpoint rendait les 7 comptes
    de l'org (e-mails, rôles, `bypasses_governance`) à un administrateur de magasin, alors
    que `/users` lui répondait 403.

    Pour un admin de nœud, il ne rend plus que :
      · les gens **déjà dans son sous-arbre** (les retirer, compléter une équipe) ;
      · les comptes **encore rattachés à aucun nœud** — le pendant humain de la porte de
        rattachement G2 pour les écrans (Ed, 2026-07-27) : sans eux, un responsable ne
        pourrait **nommer personne de nouveau** et chaque arrivée deviendrait une demande au
        siège, ce qui viderait la délégation de son sens ;
      · lui-même.
    Ce qui reste fermé est le seul point qui comptait : **le personnel des AUTRES branches**,
    avec ses e-mails et ses rôles. L'org-admin voit tout l'annuaire.
    """
    stmt = select(CmsUser).where(
        CmsUser.id_org == current_user.id_org,
        CmsUser.is_active == True,  # noqa: E712
        CmsUser.id_advertiser.is_(None),  # comptes du portail annonceur : hors sujet
    ).order_by(CmsUser.full_name, CmsUser.email)
    admin_nodes = await user_admin_nodes(db, current_user)
    if admin_nodes is not None:
        subtree = [n.id_node for n in (await db.execute(
            select(CmsGovernanceNode).where(
                CmsGovernanceNode.id_org == current_user.id_org,
                or_(*[CmsGovernanceNode.path.like(f"{n.path}%") for n in admin_nodes]),
            )
        )).scalars().all()] if admin_nodes else []
        mine = set((await db.execute(
            select(CmsGovernanceNodeAdmin.id_user)
            .where(CmsGovernanceNodeAdmin.id_node.in_(subtree or [-1]))
        )).scalars().all())
        placed = set((await db.execute(
            select(CmsGovernanceNodeAdmin.id_user)
            .join(CmsGovernanceNode,
                  CmsGovernanceNode.id_node == CmsGovernanceNodeAdmin.id_node)
            .where(CmsGovernanceNode.id_org == current_user.id_org)
        )).scalars().all())
        allowed = mine | {current_user.id_user}
        stmt = stmt.where(
            or_(CmsUser.id_user.in_(allowed), CmsUser.id_user.not_in(placed or {-1}))
        )
    users = (await db.execute(stmt)).scalars().all()
    if not users:
        return []
    ids = [u.id_user for u in users]

    roles_by_user: dict[int, list[str]] = {}
    for id_user, role_key in (await db.execute(text("""
        SELECT u.id_user, r.role_key
        FROM cms_users u
        LEFT JOIN cms_user_role_assignments ura
               ON ura.id_user = u.id_user AND ura.is_deny = false
        LEFT JOIN cms_group_members gm ON gm.id_user = u.id_user
        LEFT JOIN cms_group_roles gr ON gr.id_group = gm.id_group
        JOIN cms_roles r ON r.id_role = COALESCE(ura.id_role, gr.id_role)
        WHERE u.id_user = ANY(:ids)
    """), {"ids": ids})).all():
        roles_by_user.setdefault(id_user, []).append(role_key)

    editors: set[int] = {r[0] for r in (await db.execute(text("""
        SELECT DISTINCT u.id_user
        FROM cms_users u
        LEFT JOIN cms_user_role_assignments ura
               ON ura.id_user = u.id_user AND ura.is_deny = false
        LEFT JOIN cms_group_members gm ON gm.id_user = u.id_user
        LEFT JOIN cms_group_roles gr ON gr.id_group = gm.id_group
        JOIN cms_role_permissions rp ON rp.id_role = COALESCE(ura.id_role, gr.id_role)
        JOIN cms_permissions p ON p.id_permission = rp.id_permission
        WHERE u.id_user = ANY(:ids) AND p.module = 'schedules' AND p.operation = 'edit'
    """), {"ids": ids})).all()}

    out: list[dict] = []
    for u in users:
        keys = sorted(set(roles_by_user.get(u.id_user, [])))
        bypasses = bool({"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"} & set(keys))
        out.append({
            "id_user": u.id_user,
            "email": u.email,
            "full_name": u.full_name,
            "roles": keys,
            # A-t-il de quoi éditer un programme ? (sinon : admin de nœud sans effet)
            "can_program": bypasses or u.id_user in editors,
            # Contourne déjà la gouvernance : le nommer ne change rien.
            "bypasses_governance": bypasses,
        })
    return out


@router.post("/nodes/{id_node}/admins", response_model=NodeAdminRead, status_code=status.HTTP_201_CREATED)
async def add_node_admin(
    id_node: int,
    body: NodeAdminCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "delegate"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NodeAdminRead:
    """Affecte un utilisateur comme administrateur du nœud (édite ce nœud + son sous-arbre)."""
    await _get_node(db, current_user.id_org, id_node)
    # N1 : nommer un admin = déléguer un pouvoir qu'on détient — sur son sous-arbre, jamais
    # sur un ancêtre ni un nœud frère (c'était LE chemin d'escalade mesuré le 2026-07-27).
    await _enforce_structure_edit(db, current_user, id_node)
    user = (await db.execute(
        select(CmsUser).where(
            CmsUser.id_user == body.id_user, CmsUser.id_org == current_user.id_org
        )
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    exists = (await db.execute(
        select(CmsGovernanceNodeAdmin).where(
            CmsGovernanceNodeAdmin.id_node == id_node,
            CmsGovernanceNodeAdmin.id_user == body.id_user,
        )
    )).scalar_one_or_none()
    if exists is None:
        db.add(CmsGovernanceNodeAdmin(id_node=id_node, id_user=body.id_user))
        await db.commit()
    return NodeAdminRead(id_user=user.id_user, email=user.email, full_name=user.full_name)


@router.delete("/nodes/{id_node}/admins", status_code=status.HTTP_204_NO_CONTENT)
async def remove_node_admin(
    id_node: int,
    body: NodeAdminCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "delegate"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_node(db, current_user.id_org, id_node)
    await _enforce_structure_edit(db, current_user, id_node)  # N1
    await db.execute(
        delete(CmsGovernanceNodeAdmin).where(
            CmsGovernanceNodeAdmin.id_node == id_node,
            CmsGovernanceNodeAdmin.id_user == body.id_user,
        )
    )
    await db.commit()


# ── Règles de délégation ─────────────────────────────────────────────────────

@router.get("/nodes/{id_node}/rules", response_model=list[RuleRead])
async def list_rules(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RuleRead]:
    await _get_visible_node(db, current_user, id_node)
    rules = (await db.execute(
        select(CmsDelegationRule).where(CmsDelegationRule.id_node == id_node).order_by(CmsDelegationRule.id_rule)
    )).scalars().all()
    return [RuleRead.model_validate(r) for r in rules]


async def _inherited_caps(db: AsyncSession, node: CmsGovernanceNode) -> dict[str, int | None]:
    """Plafonds imposés par les ANCÊTRES du nœud (décisions Ed 2026-07-26) :

    · `max_for_children` — items par tour (« le département peut monter jusqu'à 4 ») ;
    · `max_item_sec` — **durée d'un seul contenu**. Sans lui, le « N par tour » compte des
      items et pas du temps : il suffit de livrer une compilation de 3 h en UN fichier pour
      accaparer la boucle (attaque décrite par Ed). ⚠️ `time_budget` seul ne protège pas :
      son mode par défaut `soft_cut` termine l'item qui déborde, donc les 3 h passent.

    Portés par les colonnes du nœud PARENT (migr 0110) — jamais par le nœud contraint, qui
    pourrait sinon les relever. Plusieurs ancêtres peuvent en poser : le plus petit gagne.
    """
    ancestors = _ancestor_ids(node)
    if not ancestors:
        return {"max_for_children": None, "max_item_sec": None}
    rows = (await db.execute(
        select(
            CmsGovernanceNode.max_for_children,
            CmsGovernanceNode.max_item_sec,
        ).where(CmsGovernanceNode.id_node.in_(ancestors))
    )).all()
    per_turn = [v for v, _ in rows if v is not None]
    per_item = [v for _, v in rows if v is not None]
    return {
        "max_for_children": min(per_turn) if per_turn else None,
        "max_item_sec": min(per_item) if per_item else None,
    }


def _ancestor_ids(node: CmsGovernanceNode) -> list[int]:
    """Ancêtres STRICTS du nœud, lus depuis son chemin matérialisé."""
    return [int(p) for p in (node.path or "").split(".") if p and int(p) != node.id_node]


def _locked_here(node: CmsGovernanceNode, family: str) -> bool:
    """La famille est-elle restreinte SUR CE NŒUD ? **Clé absente = restreinte** (migr 0110)."""
    return bool((node.rules_locked or {}).get(family, True))


async def _locked_above(db: AsyncSession, node: CmsGovernanceNode) -> dict[str, bool]:
    """Restrictions posées par les ANCÊTRES du nœud, par famille.

    Une restriction se propage vers le bas et ne se lève que depuis un niveau supérieur
    (décision Ed 2026-07-26) : « si la case est cochée à un niveau, tous les nœuds inférieurs
    ne peuvent plus la rouvrir ». C'est cette monotonie qui donne son sens à la délégation —
    sans elle, le nœud contraint lèverait sa propre contrainte.
    """
    ancestors = _ancestor_ids(node)
    if not ancestors:
        return {f: False for f in _RULE_FAMILIES}
    rows = (await db.execute(
        select(CmsGovernanceNode.rules_locked).where(CmsGovernanceNode.id_node.in_(ancestors))
    )).scalars().all()
    return {
        f: any(bool((r or {}).get(f, True)) for r in rows)
        for f in _RULE_FAMILIES
    }


async def _locked_effective(db: AsyncSession, node: CmsGovernanceNode) -> dict[str, bool]:
    """Restriction EFFECTIVE par famille = OU logique sur le nœud et tous ses ancêtres."""
    above = await _locked_above(db, node)
    return {f: above[f] or _locked_here(node, f) for f in _RULE_FAMILIES}


async def _is_strict_ancestor_admin(
    db: AsyncSession, user: CmsUser, node: CmsGovernanceNode
) -> bool:
    """L'utilisateur administre-t-il un ancêtre STRICT du nœud (ou est-il org-admin) ?

    `user_can_edit_node(parent)` couvre déjà « admin du parent ou d'un de ses ancêtres », et
    renvoie vrai pour l'org-admin. À la racine (aucun parent), seul l'org-admin qualifie.
    """
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
        return True
    if node.parent_id is None:
        return False
    return await user_can_edit_node(db, user, node.parent_id)


async def _enforce_rule_write(
    db: AsyncSession, user: CmsUser, node: CmsGovernanceNode, rule_type: str, params: dict | None
) -> None:
    """Gardes d'écriture d'une règle de délégation (décisions Ed 2026-07-26).

    Qui peut écrire les règles d'un nœud :
      · l'org-admin, toujours ;
      · l'admin d'un **ancêtre strict** — c'est le cas « le national configure tout », qui est
        la norme du modèle voulu par Ed ;
      · l'admin du nœud **lui-même**, uniquement si la **famille de la règle n'est pas
        restreinte** (case décochée sur le nœud ET sur tous ses ancêtres, migr 0110).
    Par défaut tout est restreint : le responsable local ne touche qu'au CONTENU.

    ⛔ Ne pas remplacer par `user_can_edit_node(node)` : ça rouvrirait au nœud le droit de
    régler ses propres paramètres, donc de contourner le plafond que son parent lui impose.

    **Plafond hérité** : refus franc (422) plutôt qu'avertissement — un plafond qu'on peut
    dépasser n'est pas un plafond.
    """
    allowed = await _is_strict_ancestor_admin(db, user, node)
    if not allowed and await user_can_edit_node(db, user, node.id_node):
        allowed = not (await _locked_effective(db, node)).get(rule_type, True)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This setting is defined by the level above (delegation not granted).",
        )
    if rule_type != "item_count":
        return
    caps = await _inherited_caps(db, node)
    p = params or {}

    def _as_int(key):
        try:
            v = p.get(key)
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None  # params illisibles : le moteur les ignore déjà, inutile de bloquer

    wanted, cap = _as_int("max_per_cycle"), caps["max_for_children"]
    if wanted is not None and cap is not None and wanted > cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The level above limits this node to {cap} item(s) per loop.",
        )


@router.post("/nodes/{id_node}/rules", response_model=RuleRead, status_code=status.HTTP_201_CREATED)
async def create_rule(
    id_node: int,
    body: RuleCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "delegate"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuleRead:
    node = await _get_node(db, current_user.id_org, id_node)
    if body.rule_type not in _VALID_RULE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid rule_type ({', '.join(sorted(_VALID_RULE_TYPES))})",
        )
    await _enforce_rule_write(db, current_user, node, body.rule_type, body.params)
    # D5 : une seule règle par type et par nœud (garde explicite → 409 lisible avant la
    # contrainte d'unicité DB). Modifier l'existante ou la supprimer d'abord.
    existing = (await db.execute(
        select(CmsDelegationRule).where(
            CmsDelegationRule.id_node == id_node,
            CmsDelegationRule.rule_type == body.rule_type,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A rule of this type already exists on this node (one per type).",
        )
    rule = CmsDelegationRule(
        id_node=id_node, rule_type=body.rule_type, params=body.params or {}, soft_cut=body.soft_cut
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    await _force_sync_node(db, current_user.id_org, id_node)
    return RuleRead.model_validate(rule)


async def _get_rule(db: AsyncSession, id_org: int, id_rule: int) -> CmsDelegationRule:
    rule = (await db.execute(
        select(CmsDelegationRule)
        .join(CmsGovernanceNode, CmsGovernanceNode.id_node == CmsDelegationRule.id_node)
        .where(CmsDelegationRule.id_rule == id_rule, CmsGovernanceNode.id_org == id_org)
    )).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    return rule


@router.put("/rules/{id_rule}", response_model=RuleRead)
async def update_rule(
    id_rule: int,
    body: RuleUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "delegate"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuleRead:
    rule = await _get_rule(db, current_user.id_org, id_rule)
    node = await _get_node(db, current_user.id_org, rule.id_node)
    await _enforce_rule_write(
        db, current_user, node, rule.rule_type,
        body.params if body.params is not None else rule.params,
    )
    if body.params is not None:
        rule.params = body.params
    if body.soft_cut is not None:
        rule.soft_cut = body.soft_cut
    if body.is_active is not None:
        rule.is_active = body.is_active
    id_node = rule.id_node
    await db.commit()
    await db.refresh(rule)
    await _force_sync_node(db, current_user.id_org, id_node)
    return RuleRead.model_validate(rule)


@router.delete("/rules/{id_rule}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    id_rule: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "delegate"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    rule = await _get_rule(db, current_user.id_org, id_rule)
    node = await _get_node(db, current_user.id_org, rule.id_node)
    # Supprimer une règle = la même écriture qu'en poser une (on ne retire pas le plafond
    # d'un autre nœud, et on n'échappe pas au sien en effaçant la ligne).
    await _enforce_rule_write(db, current_user, node, rule.rule_type, rule.params)
    id_node = rule.id_node
    await db.delete(rule)
    await db.commit()
    await _force_sync_node(db, current_user.id_org, id_node)


# ── Compilation §XVIII Phase 2 ───────────────────────────────────────────────
# La gouvernance EST le moteur de couches. Ces helpers collectent les couches
# (schedules gouvernés actifs) en base puis délèguent l'arbitrage au service pur
# `governance_engine`. Lecture seule — n'altère pas le sync live (_resolve_sync_payload).


async def _anchor_nodes_for_player(db: AsyncSession, id_org: int, id_player: int) -> set[int]:
    """Nœuds où le player est assigné (player direct + via ses groupes + via son site)."""
    player = (await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player, CmsPlayer.id_org == id_org)
    )).scalar_one_or_none()
    if player is None:
        return set()

    group_ids = set((await db.execute(
        select(CmsPlayerGroupMember.id_group).where(CmsPlayerGroupMember.id_player == id_player)
    )).scalars().all())

    conds = [
        (CmsGovernanceAssignment.target_type == "player")
        & (CmsGovernanceAssignment.target_id == id_player)
    ]
    if group_ids:
        conds.append(
            (CmsGovernanceAssignment.target_type == "group")
            & (CmsGovernanceAssignment.target_id.in_(group_ids))
        )
    if player.id_site:
        conds.append(
            (CmsGovernanceAssignment.target_type == "site")
            & (CmsGovernanceAssignment.target_id == player.id_site)
        )
        # ⚠️ Sens INVERSE de `site_group`, à ne pas oublier : une cible acceptée à l'écriture
        # mais absente d'ici serait enregistrée, comptée dans l'aperçu… et jamais diffusée
        # (exactement le défaut d'audit du 2026-07-18 sur la cible `site`).
        site_group_ids = set((await db.execute(
            select(CmsSiteGroupMember.id_site_group)
            .where(CmsSiteGroupMember.id_site == player.id_site)
        )).scalars().all())
        if site_group_ids:
            conds.append(
                (CmsGovernanceAssignment.target_type == "site_group")
                & (CmsGovernanceAssignment.target_id.in_(site_group_ids))
            )

    rows = (await db.execute(
        select(CmsGovernanceAssignment.id_node)
        .join(CmsGovernanceNode, CmsGovernanceNode.id_node == CmsGovernanceAssignment.id_node)
        .where(CmsGovernanceNode.id_org == id_org, or_(*conds))
    )).scalars().all()
    return set(rows)


async def _relevant_node_ids(db: AsyncSession, id_org: int, anchor_ids: set[int]) -> set[int]:
    """Ancres + tous leurs ancêtres (les composants du `path` matérialisé SONT des id_node)."""
    if not anchor_ids:
        return set()
    nodes = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_org == id_org, CmsGovernanceNode.id_node.in_(anchor_ids)
        )
    )).scalars().all()
    relevant: set[int] = set()
    for n in nodes:
        for part in (n.path or "").split("."):
            if part:
                relevant.add(int(part))
    return relevant


async def _layer_items(
    db: AsyncSession, id_playlist: int, id_node: int, id_schedule: int
) -> list[CompiledItem]:
    """Items d'une playlist en représentation légère (sans URL signée — endpoint de lecture).

    Compile en SLOTS puis déroule le cycle, **exactement comme le chemin classique**
    (`ws.py`), au lieu de lire `cms_playlist_items` brut. Deux défauts en découlaient
    (retour Ed 2026-07-25 ②) :
      · **aucune rotation** — « 3 vidéos en aléatoire, 1 par tour » ne pouvait pas
        fonctionner sous gouvernance, la couche servait toujours les mêmes items ;
      · **tout item `sub_playlist` DISPARAISSAIT en silence** — son `id_media` est NULL,
        donc la recherche de média ne renvoyait rien et l'item était simplement sauté :
        une playlist gouvernée contenant une sous-playlist perdait ces contenus sans le
        moindre message.

    ⚠️ Rappel de modèle : la rotation se règle sur la playlist **enfant** (`rotation_mode`
    / `items_per_turn`), et ne s'applique que lorsqu'elle est imbriquée dans une autre.
    Un seul niveau d'imbrication (garde `item_type == 'media'` sur les enfants).
    """
    rows = list((await db.execute(
        select(CmsPlaylistItem)
        .where(CmsPlaylistItem.id_playlist == id_playlist)
        .order_by(CmsPlaylistItem.sort_order)
    )).scalars().all())
    if not rows:
        return []

    # Sous-playlists référencées + leurs items (en lot : cette fonction est appelée pour
    # CHAQUE couche de CHAQUE player à chaque synchro).
    child_ids = {r.id_child_playlist for r in rows
                 if r.item_type == "sub_playlist" and r.id_child_playlist}
    children: dict[int, CmsPlaylist] = {}
    child_rows: dict[int, list[CmsPlaylistItem]] = {}
    if child_ids:
        children = {c.id_playlist: c for c in (await db.execute(
            select(CmsPlaylist).where(
                CmsPlaylist.id_playlist.in_(child_ids),
                CmsPlaylist.is_deleted == False,  # noqa: E712
            )
        )).scalars().all()}
        if children:
            for cr in (await db.execute(
                select(CmsPlaylistItem)
                .where(
                    CmsPlaylistItem.id_playlist.in_(children.keys()),
                    CmsPlaylistItem.item_type == "media",  # garde : 1 seul niveau
                )
                .order_by(CmsPlaylistItem.sort_order)
            )).scalars().all():
                child_rows.setdefault(cr.id_playlist, []).append(cr)

    media_ids = {r.id_media for r in rows if r.id_media}
    media_ids |= {cr.id_media for lst in child_rows.values() for cr in lst if cr.id_media}
    media: dict[int, CmsMedia] = {}
    if media_ids:
        media = {m.id_media: m for m in (await db.execute(
            select(CmsMedia).where(
                CmsMedia.id_media.in_(media_ids),
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.is_blocked == False,  # noqa: E712 — quarantaine (§ Q5)
            )
        )).scalars().all()}

    def _compiled(item: CmsPlaylistItem) -> CompiledItem | None:
        m = media.get(item.id_media) if item.id_media else None
        if m is None:
            return None
        return CompiledItem(
            id_media=m.id_media, media_name=m.media_name, media_type=m.media_type,
            duration_ms=item.duration_ms or m.duration_ms or 10000,
            id_node=id_node, id_schedule=id_schedule,
        )

    base_slots: list[dict] = []
    for it in rows:
        if it.item_type == "sub_playlist":
            child = children.get(it.id_child_playlist) if it.id_child_playlist else None
            if child is None:
                continue  # enfant supprimé → slot ignoré (pas d'écran noir)
            units: list[CompiledItem] = []
            kept_ids: list[int] = []
            for cr in child_rows.get(child.id_playlist, []):
                ci = _compiled(cr)
                if ci is not None:
                    units.append(ci)
                    kept_ids.append(cr.id_item)
            if not units:
                continue
            protect = bool(child.no_ads_inside)
            if child.rotation_mode in ("one_sequential", "one_random") and len(units) > 1:
                base_slots.append({
                    "kind": "rotation", "items": units, "mode": child.rotation_mode,
                    "k": max(1, min(child.items_per_turn or 1, len(units))),
                    # Seed = contenu PUBLIÉ de l'enfant ⇒ tous les players d'un groupe LAN
                    # déroulent le MÊME cycle (indispensable à la synchro).
                    "seed": rotation_seed(child.id_playlist, kept_ids),
                    "protect": protect,
                })
            else:
                base_slots.append({"kind": "inline", "items": units, "protect": protect})
        else:
            ci = _compiled(it)
            if ci is not None:
                base_slots.append({"kind": "inline", "items": [ci]})

    return unroll_slots(base_slots)


async def diagnose_no_layers(
    db: AsyncSession, id_org: int, id_player: int, now: datetime
) -> dict[str, str | None]:
    """Pourquoi ce player n'a-t-il AUCUNE couche de gouvernance à cet instant ? (R10)

    « Aucune couche active » recouvrait cinq situations très différentes et n'en nommait
    aucune — Ed y a perdu du temps le 2026-07-25 alors qu'il manquait seulement le
    rattachement du PROGRAMME au nœud. On rejoue ici la chaîne de `gather_layers`, de
    l'amont vers l'aval, et on renvoie le premier maillon qui casse.

    Quand plusieurs programmes coexistent, on rapporte le cas le PLUS AVANCÉ (celui qui
    est le plus près de diffuser) : c'est celui sur lequel l'utilisateur doit agir.
    `detail` = nom du programme concerné, quand il y en a un.
    """
    anchors = await _anchor_nodes_for_player(db, id_org, id_player)
    relevant = await _relevant_node_ids(db, id_org, anchors)
    if not relevant:
        return {"reason": "no_node", "detail": None}

    rows = (await db.execute(
        select(CmsSchedule).where(
            CmsSchedule.id_governance_node.in_(relevant),
            CmsSchedule.schedule_kind == "content",
            CmsSchedule.is_deleted == False,  # noqa: E712
        ).order_by(CmsSchedule.priority.desc())
    )).scalars().all()
    if not rows:
        return {"reason": "no_schedule", "detail": None}

    # D-b : les programmes ciblés qui ne visent pas CET écran sortent ici. Sans ce maillon,
    # un écran non ciblé se verrait répondre « hors fenêtre » — la mauvaise cause, celle qui
    # envoie chercher au mauvais endroit (exactement le défaut que R10 corrigeait).
    in_scope = await scoped_schedule_ids(db, id_player, {s.id_schedule for s in rows})
    if not in_scope:
        return {"reason": "outside_targets", "detail": rows[0].schedule_name}
    rows = [s for s in rows if s.id_schedule in in_scope]

    enabled = [s for s in rows if s.is_active]
    if not enabled:
        return {"reason": "schedule_inactive", "detail": rows[0].schedule_name}

    with_playlist = [s for s in enabled if s.id_playlist is not None]
    if not with_playlist:
        return {"reason": "no_playlist", "detail": enabled[0].schedule_name}

    # Un programme SANS règle n'est jamais actif (contrainte du moteur, pas un oubli d'UI).
    with_rules: list[CmsSchedule] = []
    for s in with_playlist:
        has_rule = (await db.execute(
            select(CmsScheduleRule.id_rule)
            .where(CmsScheduleRule.id_schedule == s.id_schedule).limit(1)
        )).scalar_one_or_none()
        if has_rule is not None:
            with_rules.append(s)
    if not with_rules:
        return {"reason": "no_rule", "detail": with_playlist[0].schedule_name}

    # Tout est en place : il ne reste que la fenêtre de diffusion (jour/heure/validité).
    return {"reason": "outside_window", "detail": with_rules[0].schedule_name}


async def _active_governed_schedules(
    db: AsyncSession, node_ids: set[int], now: datetime, id_player: int | None = None
) -> list[CmsSchedule]:
    """Schedules content gouvernés (rattachés à node_ids), actifs MAINTENANT (règles+validité).

    `id_player` fourni → applique **D-b** : les cibles du programme filtrent le périmètre du
    nœud (sans cible = tout le nœud). Sans player (usages nœud-centriques), aucun filtrage.
    """
    if not node_ids:
        return []
    schedules = (await db.execute(
        select(CmsSchedule)
        .where(
            CmsSchedule.id_governance_node.in_(node_ids),
            CmsSchedule.schedule_kind == "content",
            CmsSchedule.is_active == True,  # noqa: E712
            CmsSchedule.is_deleted == False,  # noqa: E712
        )
        .order_by(CmsSchedule.priority.desc())
    )).scalars().all()

    if id_player is not None and schedules:
        keep = await scoped_schedule_ids(db, id_player, {s.id_schedule for s in schedules})
        schedules = [s for s in schedules if s.id_schedule in keep]

    active: list[CmsSchedule] = []
    for s in schedules:
        if s.id_playlist is None:
            continue
        rules = (await db.execute(
            select(CmsScheduleRule).where(CmsScheduleRule.id_schedule == s.id_schedule)
        )).scalars().all()
        if not rules:
            continue
        if any(
            schedule_active_at(time_rule_from_schedule(r), s.valid_from, s.valid_until, now)
            for r in rules
        ):
            active.append(s)
    return active


async def _node_constraints(
    db: AsyncSession, id_node: int, node: CmsGovernanceNode | None = None
) -> DelegationConstraints:
    rules = (await db.execute(
        select(CmsDelegationRule).where(CmsDelegationRule.id_node == id_node)
    )).scalars().all()
    c = parse_constraints(rules)
    # Le plafond de durée par contenu vit sur le NŒUD (migr 0110) et s'HÉRITE : le nœud subit
    # le plus petit plafond de la chaîne, même s'il n'a lui-même rien posé. Résolu par un
    # helper partagé avec l'éditeur de playlist (qui avertit au chargement) — deux buts, une
    # seule définition. Le moteur, lui, reste pur.
    if node is not None:
        c.max_item_sec = await effective_max_item_sec(db, node.id_node)
    return c


async def gather_layers(
    db: AsyncSession, id_org: int, id_player: int, now: datetime
) -> list[LayerInput]:
    """Collecte les couches candidates d'un player (schedules gouvernés actifs des nœuds
    pertinents = ancres du player + ancêtres). Réutilisé par l'endpoint compiled-schedule.
    """
    anchors = await _anchor_nodes_for_player(db, id_org, id_player)
    relevant = await _relevant_node_ids(db, id_org, anchors)
    if not relevant:
        return []

    nodes = {n.id_node: n for n in (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_node.in_(relevant))
    )).scalars().all()}

    # D-b : le filtrage par cibles est PLAYER-centrique — c'est ce chemin qui décide de la
    # diffusion réelle (sync + aperçu compilé).
    schedules = await _active_governed_schedules(db, relevant, now, id_player)

    constraints_cache: dict[int, DelegationConstraints] = {}
    layers: list[LayerInput] = []
    for s in schedules:
        node = nodes.get(s.id_governance_node)
        if node is None:
            continue
        if s.id_governance_node not in constraints_cache:
            constraints_cache[s.id_governance_node] = await _node_constraints(
                db, s.id_governance_node, node)
        items = await _layer_items(db, s.id_playlist, node.id_node, s.id_schedule)
        if not items:
            continue
        layers.append(LayerInput(
            id_node=node.id_node, node_name=node.node_name, node_level=node.node_level,
            id_schedule=s.id_schedule, priority=s.priority,
            constraints=constraints_cache[s.id_governance_node], items=items,
        ))
    return layers


def _parse_at(at: Optional[str], tz_name: str | None = None) -> datetime:
    """`at` ISO (heure locale) → datetime naïf ; défaut = maintenant dans `tz_name`.

    `tz_name` = fuseau de l'écran quand la question porte sur UN écran (P2.2) ; laissé à
    None pour une question qui porte sur un NŒUD, dont les écrans peuvent s'étaler sur
    plusieurs fuseaux — là, SIGNFLOW_TZ reste le seul repère défendable.
    """
    if at:
        try:
            return datetime.fromisoformat(at).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid 'at' parameter (ISO 8601 expected)",
            )
    return local_now(tz_name)


@router.get("/nodes/{id_node}/budget-status", response_model=BudgetStatusRead)
async def node_budget(
    id_node: int,
    current_user: Annotated[CmsUser, Depends(require_permission("governance", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    at: Optional[str] = Query(None, description="Local ISO timestamp (default: now)"),
) -> BudgetStatusRead:
    """Consommation **planifiée** du budget d'un nœud (contenu propre du nœud, actif à `at`).

    Indépendant du player (les schedules du nœud sont les mêmes pour tous ses players).
    """
    await _get_visible_node(db, current_user, id_node)
    now = _parse_at(at)

    schedules = await _active_governed_schedules(db, {id_node}, now)
    items: list[CompiledItem] = []
    for s in schedules:
        items.extend(await _layer_items(db, s.id_playlist, id_node, s.id_schedule))

    constraints = await _node_constraints(db, id_node)
    status_dict = node_budget_status(constraints, items)
    status_dict["id_node"] = id_node
    return BudgetStatusRead(**status_dict)
