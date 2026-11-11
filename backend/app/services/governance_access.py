"""
Gouvernance §XVIII Phase 2 — contrôle d'accès par couche (immutabilité §18.4).

Détermine si un utilisateur peut éditer un programme (cms_schedule) rattaché à un nœud
de gouvernance. Règle : org_admin contourne ; sinon l'utilisateur doit administrer le nœud
**ou un de ses ancêtres** (administrer un national édite tout en dessous). Un schedule sans
nœud (id_governance_node NULL) n'a aucune restriction de gouvernance (comportement inchangé).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.governance import CmsGovernanceNode, CmsGovernanceNodeAdmin
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import rbac

_BYPASS_ROLES = {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}

# G4 (décision Ed, 2026-07-27) — rôles dont la sémantique EST un périmètre : leur porteur
# n'agit que dans son sous-arbre, et **le contenu non gouverné lui est invisible**. Sans
# cette liste, un « programmeur de gouvernance » garderait la main sur tout le contenu
# d'organisation (défaut N3 de l'audit : playlist d'org éditée, programme créé vers un
# écran quelconque, template partagé modifié).
# ⛔ Ed a écarté l'alternative « tout admin de nœud est restreint » : elle changerait le
# comportement d'utilisateurs existants sans décision. C'est le RÔLE qui porte la règle.
GOVERNANCE_SCOPED_ROLES = {
    "CMS_GOVERNANCE_MANAGER",    # Directeur
    "CMS_SITE_PROGRAMMER",       # Programmeur
    "CMS_GOVERNANCE_EDITOR",     # Éditeur
    "CMS_GOVERNANCE_APPROVER",   # Valideur
}


async def user_is_governance_scoped(db: AsyncSession, user: CmsUser) -> bool:
    """`user` porte-t-il un rôle de gouvernance (G4) sans rôle de contournement ?

    Un org-admin qui détiendrait aussi un rôle de gouvernance reste org-admin : le
    contournement l'emporte, sinon on enfermerait l'administrateur hors de son org.
    """
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if _BYPASS_ROLES.intersection(role_keys):
        return False
    return bool(GOVERNANCE_SCOPED_ROLES.intersection(role_keys))


async def user_can_edit_node(db: AsyncSession, user: CmsUser, id_node: int | None) -> bool:
    """True si `user` peut éditer un schedule rattaché à `id_node`.

    - id_node None → True (contenu non gouverné, aucune restriction)… **sauf** pour un
      porteur de rôle de gouvernance : G4 lui rend le non-gouverné inaccessible. C'est le
      point unique par lequel passent playlists, programmes et médias — le fermer ici les
      ferme tous.
    - org_admin / super_admin → True (édite tout l'org).
    - sinon : True si l'utilisateur administre le nœud ou un de ses ancêtres
      (les composants du `path` matérialisé "1.5.23" sont les id_node ancêtres).
    """
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if _BYPASS_ROLES.intersection(role_keys):
        return True

    if id_node is None:
        return not GOVERNANCE_SCOPED_ROLES.intersection(role_keys)

    node = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node,
            CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if node is None:
        # Nœud introuvable pour cet org (donnée incohérente / SET NULL en cours) :
        # ne pas bloquer sur la gouvernance — la permission schedules:edit suffit.
        return True

    ancestor_ids = [int(p) for p in (node.path or "").split(".") if p] or [id_node]
    row = (await db.execute(
        select(CmsGovernanceNodeAdmin.id_node).where(
            CmsGovernanceNodeAdmin.id_user == user.id_user,
            CmsGovernanceNodeAdmin.id_node.in_(ancestor_ids),
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


async def assert_can_edit_node(
    db: AsyncSession, user: CmsUser, id_node: int | None, subject: str
) -> None:
    """403 si `user` n'administre pas `id_node` — garde d'écriture partagée.

    `subject` nomme l'objet dans le message (« ce template », « cet événement »…) : un 403
    nu sur une page d'édition n'apprend rien à celui qui le reçoit.

    Phase 4 (2026-07-27) : introduite pour templates / events / VOD plutôt qu'une 6ᵉ copie
    du même `if not await user_can_edit_node(...)`. La règle de gouvernance doit avoir une
    seule formulation — c'est la leçon de D-b (le risque du module est la couverture).
    """
    if not await user_can_edit_node(db, user, id_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not administer the governance node of {subject}",
        )


async def assert_can_detach(
    db: AsyncSession, user: CmsUser, id_node: int | None, subject: str
) -> None:
    """403 si `user` ne peut pas SORTIR l'objet de la gouvernance (N2, ancêtre strict)."""
    if not await user_can_detach_from_node(db, user, id_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only an administrator of a parent node can detach {subject} "
                   "from governance",
        )


async def content_visibility(
    db: AsyncSession, user: CmsUser, *, ungoverned_readable: bool = False
):
    """Filtre de lecture du contenu gouverné, en UN appel — (clause, visible, editable).

    `clause` est à passer tel quel à `.where()` (ou None = aucun filtre, org-admin) :
      · lecture normale = contenu d'org (nœud NULL) **∪** nœuds visibles (D6) ;
      · porteur d'un rôle de gouvernance (G4) = **nœuds visibles SEULEMENT** — le contenu
        non gouverné lui est invisible, sinon la restriction de périmètre s'arrêterait à la
        frontière de la gouvernance et N3 resterait ouvert sous un autre nom.

    `ungoverned_readable=True` rend le non-gouverné LISIBLE à ces rôles — **jamais éditable**,
    l'écriture restant fermée par `user_can_edit_node(None)`. Réservé aux MÉDIAS, via
    `media_visibility()` : voir la décision d'Ed du 2026-07-29 documentée là-bas.

    Regroupé ici pour que les trois surfaces (playlists, médias, programmes) ne puissent pas
    diverger : c'est la leçon de D-b — le risque de ce module est la COUVERTURE, pas la règle.
    Le paramètre `column` est fourni par l'appelant car chaque modèle a sa propre colonne.
    """
    visible = await visible_node_ids(db, user)
    editable = await editable_node_ids(db, user)
    scoped = await user_is_governance_scoped(db, user)

    def clause(column):
        if visible is None:          # org-admin : aucun filtre
            return None
        if scoped and not ungoverned_readable:   # G4 : rien hors gouvernance
            return column.in_(visible or {-1})
        return column.is_(None) | column.in_(visible or {-1})

    return clause, visible, editable


async def media_visibility(db: AsyncSession, user: CmsUser):
    """Visibilité des MÉDIAS — la médiathèque non gouvernée est lisible par tous les rôles.

    🔑 Décision d'Ed, 2026-07-29, prise sur mesure. G4 rendait le non-gouverné invisible aux
    rôles de gouvernance ; or **aucun média n'est rattaché à un nœud** (0 sur 89 en prod), si
    bien qu'un « Éditeur de gouvernance » recevait une médiathèque VIDE et ne pouvait
    composer aucune playlist — un module cohérent avec lui-même et inutilisable. L'impasse
    était masquée par un trou de sécurité (l'ajout à une playlist acceptait n'importe quel
    identifiant de média) ; en le fermant, elle est devenue franche.

    Le modèle retenu est celui de l'enseigne : **le siège fournit les visuels, le magasin les
    programme**. La médiathèque commune se LIT ; l'écriture reste gardée par le nœud, donc
    un rôle scopé ne peut ni modifier ni supprimer un média qu'il n'a pas sous sa
    responsabilité (`_enforce_media_node_edit` → `user_can_edit_node(None)` = False).

    ⛔ N'élargit QUE les médias. Playlists, programmes, templates, événements et VOD gardent
    `content_visibility` : leur non-gouverné reste invisible aux rôles scopés (N3).
    """
    return await content_visibility(db, user, ungoverned_readable=True)


async def user_can_detach_from_node(db: AsyncSession, user: CmsUser, id_node: int | None) -> bool:
    """True si `user` peut DÉTACHER un contenu de `id_node` (id_governance_node → NULL).

    Détacher n'est pas éditer : c'est sortir le contenu de la gouvernance (fin de la
    modération D7, fin du cloisonnement — un programme détaché GARDE ses cibles et
    diffuse hors contrôle, défaut N2 mesuré le 2026-07-27). L'admin du nœud lui-même
    ne peut donc pas s'auto-affranchir : réservé à un admin d'un ancêtre STRICT ou à
    l'org-admin. Même logique que le retrait de la modération (`requires_approval`).

    - id_node None → True (contenu déjà détaché : no-op).
    - nœud racine → org-admin seulement (aucun ancêtre strict n'existe).
    """
    if id_node is None:
        return True

    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if _BYPASS_ROLES.intersection(role_keys):
        return True

    node = (await db.execute(
        select(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node == id_node,
            CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if node is None:
        # Même contrat que user_can_edit_node : donnée incohérente → ne pas bloquer.
        return True
    if node.parent_id is None:
        return False
    # user_can_edit_node(parent) = admin du parent ou d'un de SES ancêtres = ancêtre strict.
    return await user_can_edit_node(db, user, node.parent_id)


async def editable_node_ids(db: AsyncSession, user: CmsUser) -> set[int] | None:
    """Ensemble des id_node que l'utilisateur peut éditer (nœuds administrés + sous-arbres).

    Renvoie None = « tous » (org_admin). Sert au front pour griser les couches non éditables.
    """
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if _BYPASS_ROLES.intersection(role_keys):
        return None

    admin_node_ids = set((await db.execute(
        select(CmsGovernanceNodeAdmin.id_node)
        .join(CmsGovernanceNode, CmsGovernanceNode.id_node == CmsGovernanceNodeAdmin.id_node)
        .where(
            CmsGovernanceNodeAdmin.id_user == user.id_user,
            CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalars().all())
    if not admin_node_ids:
        return set()

    # Sous-arbres : tout nœud dont le path contient un nœud administré comme préfixe.
    admin_nodes = (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_node.in_(admin_node_ids))
    )).scalars().all()
    all_nodes = (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_org == user.id_org)
    )).scalars().all()
    prefixes = [n.path for n in admin_nodes]
    editable: set[int] = set()
    for n in all_nodes:
        if any(n.path == p or n.path.startswith(p + ".") for p in prefixes):
            editable.add(n.id_node)
    return editable


async def visible_node_ids(db: AsyncSession, user: CmsUser) -> set[int] | None:
    """Nœuds dont le contenu est VISIBLE (lecture) — décision D6.

    = nœuds éditables (nœuds administrés + sous-arbres) **∪ leurs ancêtres** (visibles en
    lecture seule : « comprendre ce qui passe au-dessus de soi fait partie du métier »).
    Renvoie None = « tous » (org_admin/super_admin) — la lecture n'est alors pas filtrée.

    L'écriture reste gardée séparément par `user_can_edit_node` (les ancêtres ajoutés ici ne
    sont visibles qu'en lecture ; ils ne passeront pas la garde d'édition).
    """
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if _BYPASS_ROLES.intersection(role_keys):
        return None

    admin_node_ids = set((await db.execute(
        select(CmsGovernanceNodeAdmin.id_node)
        .join(CmsGovernanceNode, CmsGovernanceNode.id_node == CmsGovernanceNodeAdmin.id_node)
        .where(
            CmsGovernanceNodeAdmin.id_user == user.id_user,
            CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalars().all())
    if not admin_node_ids:
        return set()

    all_nodes = (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_org == user.id_org)
    )).scalars().all()
    admin_nodes = [n for n in all_nodes if n.id_node in admin_node_ids]
    prefixes = [n.path for n in admin_nodes]

    visible: set[int] = set()
    for n in all_nodes:
        # Éditable : le nœud est dans le sous-arbre d'un nœud administré.
        if any(n.path == p or n.path.startswith(p + ".") for p in prefixes):
            visible.add(n.id_node)
    # Ancêtres des nœuds administrés (composants du path) = lecture seule.
    for n in admin_nodes:
        visible.update(int(p) for p in (n.path or "").split(".") if p)
    return visible


async def node_moderator_user_ids(db: AsyncSession, id_node: int) -> list[int]:
    """Modérateurs d'un contenu rattaché à `id_node` — décision D8.

    = admins du **plus proche ancêtre STRICT** qui en possède (la hiérarchie EST
    l'organigramme de validation : le contenu d'un magasin est validé par sa région).
    Repli **org-admins** si aucun ancêtre n'a d'admin (même repli que les préavis de licence).

    On exclut le nœud lui-même : ses admins sont les auteurs locaux qui SOUMETTENT ; ils ne
    peuvent pas se modérer eux-mêmes.
    """
    node = (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_node == id_node)
    )).scalar_one_or_none()
    org_id = node.id_org if node is not None else None

    # Ancêtres stricts, du plus proche (parent) au plus lointain (racine).
    ancestor_ids: list[int] = []
    if node is not None:
        parts = [int(p) for p in (node.path or "").split(".") if p]
        ancestor_ids = [pid for pid in parts if pid != id_node][::-1]

    for anc in ancestor_ids:
        admins = (await db.execute(
            select(CmsGovernanceNodeAdmin.id_user).where(CmsGovernanceNodeAdmin.id_node == anc)
        )).scalars().all()
        if admins:
            return list(dict.fromkeys(admins))

    # Repli : org-admins actifs de l'org.
    q = (
        select(CmsUserRoleAssignment.id_user)
        .join(CmsRole, CmsRole.id_role == CmsUserRoleAssignment.id_role)
        .join(CmsUser, CmsUser.id_user == CmsUserRoleAssignment.id_user)
        .where(CmsRole.role_key == "CMS_ORG_ADMIN", CmsUser.is_active.is_(True))
    )
    if org_id is not None:
        q = q.where(CmsUser.id_org == org_id)
    admins = (await db.execute(q)).scalars().all()
    return list(dict.fromkeys(admins))

async def node_requires_approval(db: AsyncSession, id_node: int | None) -> bool:
    """Le contenu rattaché à ce nœud passe-t-il par un modérateur ? (D7, étendu 2026-07-26)

    **Monotone vers le bas**, symétrique de la délégation des règles : si un ANCÊTRE exige la
    modération, le nœud l'exige aussi, quelle que soit sa propre case. Sans ça, il suffisait
    de décocher plus bas pour publier sans validation — la modération du national n'aurait
    tenu que si personne en dessous ne la retirait (décision Ed 2026-07-26).

    `id_node` NULL = contenu d'organisation, non gouverné → libre.
    """
    if id_node is None:
        return False
    node = (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_node == id_node)
    )).scalar_one_or_none()
    if node is None:
        return False
    if node.requires_approval:
        return True
    ancestors = [int(p) for p in (node.path or "").split(".") if p and int(p) != node.id_node]
    if not ancestors:
        return False
    return bool((await db.execute(
        select(func.count()).select_from(CmsGovernanceNode).where(
            CmsGovernanceNode.id_node.in_(ancestors),
            CmsGovernanceNode.requires_approval == True,  # noqa: E712
        )
    )).scalar_one())


async def effective_max_item_sec(db: AsyncSession, id_node: int | None) -> int | None:
    """Durée maximale d'UN contenu qui s'applique réellement à ce nœud (secondes).

    Le plus petit plafond de la chaîne (nœud + ancêtres, colonnes posées par la migr 0110) :
    un nœud subit le plafond de ses ancêtres même s'il n'en a posé aucun, et ne peut pas se
    donner plus large — sinon il rouvrirait la porte que `max_item_sec` ferme.

    Sert deux appelants qui n'ont pas le même but, d'où la mise en commun ici :
      · la compilation, qui **écarte** le contenu trop long (et le nomme) ;
      · l'éditeur de playlist, qui **avertit au chargement** — décision Ed 2026-07-26 : voir
        le problème seulement à l'aperçu du programme compilé arrivait trop tard.
    ⚠️ L'avertissement de l'éditeur ne remplace pas l'exclusion : seule la compilation connaît
    le contexte réel (quel nœud diffuse la playlist, à quel instant).
    """
    if id_node is None:
        return None
    node = (await db.execute(
        select(CmsGovernanceNode).where(CmsGovernanceNode.id_node == id_node)
    )).scalar_one_or_none()
    if node is None:
        return None
    chain = [int(p) for p in (node.path or "").split(".") if p]
    if node.id_node not in chain:
        chain.append(node.id_node)
    caps = [v for (v,) in (await db.execute(
        select(CmsGovernanceNode.max_item_sec).where(CmsGovernanceNode.id_node.in_(chain))
    )).all() if v is not None]
    return min(caps) if caps else None
