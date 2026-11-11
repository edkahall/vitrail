"""Périmètre de la DÉCISION d'approbation §XXI × gouvernance §XVIII — défaut N5.

Constat de l'audit du 2026-07-27, mesuré dans les données réelles : les demandes 22 et 23
portent `submitted_by = reviewed_by = 37`. Le même utilisateur a soumis **et** validé son
propre contenu. Deux causes cumulées :

  · le reviewer était déterminé par le **RBAC seul** — `approvals:approve` est une
    permission d'ORGANISATION, elle ne dit rien du nœud concerné ;
  · D8 ne calcule que **qui est PRÉVENU** (les admins du plus proche ancêtre strict), jamais
    qui a le **DROIT** de décider.

Règle posée ici, symétrique de D8 pour que le modèle reste lisible :
  1. **contenu gouverné** → décider exige d'administrer un ancêtre STRICT du nœud (ou d'être
     org-admin). L'admin du nœud lui-même produit et soumet ; il ne se valide pas.
  2. **jamais sa propre soumission** — sauf org-admin, cf. ci-dessous.

⚠️ **L'org-admin est exempté de la règle 2, et ce n'est pas une facilité** : la publication
directe d'une playlist gouvernée sous modération est refusée (409) **à tout le monde,
org-admin compris** (`playlists.py`, garde D7). L'interdire de valider sa propre soumission
l'enfermerait donc sans issue sur une installation à UN SEUL administrateur — le cas normal
d'un client. Mesuré sur la prod avant de trancher : l'org-admin y est l'auteur ET le
validateur de 13 demandes, dont des playlists gouvernées. Le défaut N5 mesuré portait sur un
admin de NŒUD (demandes 22/23, utilisateur 37), et c'est lui que cette règle ferme.

⛔ **Hors gouvernance, rien ne change** : une demande sans nœud (campagne Retail Media,
créatif) garde exactement le comportement validé en production depuis le 2026-07-10. Le
défaut mesuré portait sur le contenu gouverné ; élargir l'interdiction d'auto-validation aux
campagnes bloquerait un client à un seul administrateur, qui crée et approuve ses campagnes.
À rouvrir si Ed veut la règle des quatre yeux partout.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import CmsApprovalRequest
from app.models.media import CmsMedia
from app.models.playlist import CmsPlaylist
from app.models.schedule import CmsSchedule
from app.models.user import CmsUser
from app.services import rbac
from app.services.governance_access import user_can_detach_from_node

# resource_type → (modèle, colonne d'identité). `campaign`/`creative` sont absents
# volontairement : ils ne portent pas de gouvernance (cf. docstring).
_GOVERNED_RESOURCES = {
    "playlist": (CmsPlaylist, CmsPlaylist.id_playlist),
    "media": (CmsMedia, CmsMedia.id_media),
    "schedule": (CmsSchedule, CmsSchedule.id_schedule),
}


async def resource_node(
    db: AsyncSession, id_org: int, resource_type: str, resource_id: int
) -> int | None:
    """Nœud de gouvernance de la ressource visée, ou None (ressource non gouvernée)."""
    entry = _GOVERNED_RESOURCES.get(resource_type)
    if entry is None:
        return None
    model, id_col = entry
    return (await db.execute(
        select(model.id_governance_node).where(id_col == resource_id, model.id_org == id_org)
    )).scalar_one_or_none()


async def decision_refusal(
    db: AsyncSession, user: CmsUser, req: CmsApprovalRequest
) -> str | None:
    """Motif de refus de DÉCIDER sur cette demande, ou None si la décision est permise.

    Renvoie un message destiné à l'utilisateur : un « 403 » nu sur une file d'attente
    n'apprend rien à celui qui la regarde.
    """
    id_node = await resource_node(db, req.id_org, req.resource_type, req.resource_id)
    if id_node is None:
        return None  # hors gouvernance : comportement inchangé

    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    is_admin = bool({"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys))
    if req.submitted_by == user.id_user and not is_admin:
        return "You cannot approve your own submission."
    # « Ancêtre STRICT ou org-admin » = exactement la règle de détachement (N2) et de
    # `node_moderator_user_ids` (D8) : une seule définition du « niveau au-dessus ».
    if not await user_can_detach_from_node(db, user, id_node):
        return (
            "This request belongs to a governance node you do not administer: "
            "only an administrator of a parent node can decide on it."
        )
    return None


async def decidable_request_ids(
    db: AsyncSession, user: CmsUser, requests: list[CmsApprovalRequest]
) -> set[int]:
    """Sous-ensemble des demandes que `user` peut décider (pour filtrer la file).

    La file ne doit pas afficher au valideur des demandes qu'il ne peut pas trancher :
    c'est exactement ce qui a rendu N5 invisible — tout le monde voyait tout, donc
    quelqu'un décidait.
    """
    out: set[int] = set()
    for req in requests:
        if await decision_refusal(db, user, req) is None:
            out.add(req.id_request)
    return out
