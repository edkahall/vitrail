"""
Endpoints Templates (§XIII).

POST   /templates          — créer un template
GET    /templates          — liste paginée
GET    /templates/{id}     — détail
PUT    /templates/{id}     — mise à jour
DELETE /templates/{id}     — suppression logique
"""

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, require_permission
from app.models.audit_log import CmsAuditLog
from app.models.governance import CmsGovernanceNode
from app.models.playlist import CmsPlaylist
from app.models.template import CmsTemplate
from app.models.user import CmsUser
from app.schemas.template import TemplateCreate, TemplateRead, TemplateUpdate
from app.services.governance_access import (
    assert_can_detach,
    assert_can_edit_node,
    content_visibility,
    user_can_edit_node,
    user_is_governance_scoped,
)
from app.services.playlist_publish import push_org_force_sync

router = APIRouter(prefix="/templates", tags=["templates"])


def _render_fingerprint(tmpl: CmsTemplate) -> str:
    """Ce qui, dans un template, ATTEINT REELLEMENT l'ecran.

    Le payload de synchro ne transporte du template que sa geometrie (`layout` : canvas,
    zones, labels) et son mode de placement des spots (`campaign_placement`). Ni le nom ni
    la description ne voyagent — les inclure ferait resynchroniser tout le parc pour un
    simple renommage.
    """
    return json.dumps(
        {"layout": tmpl.layout or {}, "campaign_placement": tmpl.campaign_placement},
        sort_keys=True, default=str,
    )


async def _validate_node(db: AsyncSession, user: CmsUser, id_node: int | None) -> None:
    """Le nœud visé doit exister DANS l'org (cross-tenant = 404, jamais 403)."""
    if id_node is None:
        return
    ok = (await db.execute(
        select(CmsGovernanceNode.id_node).where(
            CmsGovernanceNode.id_node == id_node, CmsGovernanceNode.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Governance node not found")


def _with_readonly(tmpl: CmsTemplate, editable: bool) -> TemplateRead:
    """Sérialise en indiquant si l'utilisateur peut RÉELLEMENT modifier ce template."""
    out = TemplateRead.model_validate(tmpl)
    out.governed_readonly = not editable
    return out


@router.post(
    "",
    response_model=TemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("templates", "create"))],
)
async def create_template(
    body: TemplateCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsTemplate:
    # Gouvernance §XVIII phase 4 : on ne crée que dans un nœud qu'on administre, et un
    # porteur de rôle de gouvernance ne crée pas de template d'organisation (G4).
    await _validate_node(db, current_user, body.id_governance_node)
    await assert_can_edit_node(db, current_user, body.id_governance_node, "this template")
    tmpl = CmsTemplate(
        id_org=current_user.id_org,
        template_name=body.template_name,
        description=body.description,
        layout=body.layout.model_dump(),
        id_governance_node=body.id_governance_node,
        # P3.6 : où s'affiche un spot de campagne sur un écran multi-zones.
        campaign_placement=body.campaign_placement,
        created_by=current_user.id_user,
    )
    db.add(tmpl)
    await db.flush()
    # Aucune opération sur les templates n'était tracée — mesuré : 0 ligne d'audit, toutes
    # organisations confondues. C'est ce qui rendait impossible de répondre à « qui a
    # supprimé ce template ? » (constat d'Ed, 2026-07-28).
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="create",
        resource_type="template", resource_id=str(tmpl.id_template), source="cms",
        detail={"template_name": tmpl.template_name,
                "zones": len((tmpl.layout or {}).get("zones") or [])},
    ))
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@router.get(
    "",
    response_model=list[TemplateRead],
    dependencies=[Depends(require_permission("templates", "read"))],
)
async def list_templates(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> list[TemplateRead]:
    stmt = (
        select(CmsTemplate)
        .where(CmsTemplate.is_deleted == False)  # noqa: E712
        .order_by(CmsTemplate.created_at.desc())
    )
    # D6 : nœuds visibles + templates d'org ; G4 : ces derniers disparaissent pour un
    # porteur de rôle de gouvernance.
    gov_clause, _visible, _editable = await content_visibility(db, current_user)
    clause = gov_clause(CmsTemplate.id_governance_node)
    if clause is not None:
        stmt = stmt.where(clause)
    result = await db.execute(stmt.offset(skip).limit(limit))
    rows = list(result.scalars().all())
    return [_with_readonly(r, await user_can_edit_node(db, current_user, r.id_governance_node))
            for r in rows]


@router.get(
    "/{id_template}",
    response_model=TemplateRead,
    dependencies=[Depends(require_permission("templates", "read"))],
)
async def get_template(
    id_template: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TemplateRead:
    tmpl = await _get_visible_template(db, current_user, id_template)
    return _with_readonly(tmpl, await user_can_edit_node(db, current_user, tmpl.id_governance_node))


async def _get_visible_template(
    db: AsyncSession, user: CmsUser, id_template: int
) -> CmsTemplate:
    """Template de l'org, filtré par la visibilité de gouvernance — 404 sinon.

    404 et non 403 : un template d'un nœud qu'on ne voit pas n'existe pas pour nous, même
    règle que le cross-tenant et que les playlists gouvernées (D6).
    """
    tmpl = (await db.execute(
        select(CmsTemplate).where(
            CmsTemplate.id_template == id_template,
            CmsTemplate.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    _gov_clause, visible, _editable = await content_visibility(db, user)
    node = tmpl.id_governance_node
    hidden = (
        (node is not None and visible is not None and node not in visible)
        or (node is None and await user_is_governance_scoped(db, user))
    )
    if hidden:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    return tmpl


@router.put(
    "/{id_template}",
    response_model=TemplateRead,
    dependencies=[Depends(require_permission("templates", "edit"))],
)
async def update_template(
    id_template: int,
    body: TemplateUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsTemplate:
    tmpl = await _get_visible_template(db, current_user, id_template)
    # N3 : un responsable de magasin ne modifie plus une maquette nationale.
    await assert_can_edit_node(db, current_user, tmpl.id_governance_node, "this template")

    before_render = _render_fingerprint(tmpl)

    if "id_governance_node" in body.model_fields_set:
        await _validate_node(db, current_user, body.id_governance_node)
        await assert_can_edit_node(db, current_user, body.id_governance_node,
                                   "the destination node")
        if body.id_governance_node is None:
            await assert_can_detach(db, current_user, tmpl.id_governance_node, "this template")
        tmpl.id_governance_node = body.id_governance_node
    if body.campaign_placement is not None:
        tmpl.campaign_placement = body.campaign_placement
    if body.template_name is not None:
        tmpl.template_name = body.template_name
    if body.description is not None:
        tmpl.description = body.description
    if body.layout is not None:
        tmpl.layout = body.layout.model_dump()

    # `updated_at` ne bougeait JAMAIS : ni `onupdate` au modèle, ni écriture ici, ni trigger.
    # Mesuré : 0 template sur 8 et 0 playlist sur 42 avaient une date de modification
    # postérieure à leur création, alors que le champ est servi par l'API.
    tmpl.updated_at = datetime.now(UTC)
    render_changed = _render_fingerprint(tmpl) != before_render
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="update",
        resource_type="template", resource_id=str(id_template), source="cms",
        detail={"template_name": tmpl.template_name,
                "fields": sorted(body.model_fields_set),
                "render_changed": render_changed},
    ))
    await db.commit()
    await db.refresh(tmpl)
    # Regle d'or : une modification qui doit atteindre l'ecran pousse `force_sync`. Elle
    # manquait ici — constate par Ed le 2026-08-06 en basculant `campaign_placement` de
    # « plein ecran » a « zone principale » : les ecrans ont continue de diffuser l'ancien
    # mode, et il a fallu re-publier la PLAYLIST (qui, elle, pousse) pour que ca prenne.
    # Or le template est ce qui decide de la geometrie ET du recouvrement par un spot
    # FACTURE : sans poussee, on facture au SOV une exposition qui n'est pas celle servie.
    # ⛔ On ne pousse PAS sur un simple renommage (cf. _render_fingerprint).
    if render_changed:
        await push_org_force_sync(db)
    return tmpl


async def _playlists_using(db: AsyncSession, id_template: int) -> list[CmsPlaylist]:
    """Playlists (vivantes) bâties sur ce template."""
    return list((await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_template == id_template,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        ).order_by(CmsPlaylist.playlist_name)
    )).scalars().all())


@router.get(
    "/{id_template}/deletion-impact",
    dependencies=[Depends(require_permission("templates", "read"))],
)
async def template_deletion_impact(
    id_template: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Ce que la suppression emporterait — à afficher AVANT de confirmer.

    Les templates n'avaient rien de tel, contrairement aux médias : on supprimait à
    l'aveugle une maquette dont des playlists dépendaient (audit du menu Templates,
    2026-07-28).
    """
    tmpl = await _get_visible_template(db, current_user, id_template)
    playlists = await _playlists_using(db, id_template)
    zones = len((tmpl.layout or {}).get("zones") or [])
    return {
        "template": {"id_template": tmpl.id_template, "template_name": tmpl.template_name,
                     "zones": zones},
        "playlists": [{"id_playlist": p.id_playlist, "playlist_name": p.playlist_name}
                      for p in playlists],
        "summary": {"playlists": len(playlists), "zones": zones},
    }


@router.delete(
    "/{id_template}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("templates", "delete"))],
)
async def delete_template(
    id_template: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    force: bool = Query(False, description="Detach the affected playlists, then delete"),
) -> None:
    """Supprime RÉELLEMENT le template (décision d'Ed, 2026-07-28 : « quand on efface, on efface »).

    Avant, la suppression n'était qu'un `is_deleted = True` : la ligne restait en base sans
    être ni visible, ni listée, ni restaurable — le pire des deux mondes, ni corbeille
    utilisable ni suppression franche. Mesuré sur la production : 6 templates dans cet état,
    et **aucune trace d'audit** pour dire qui les avait supprimés ni quand.

    Deux garde-fous, parce qu'effacer pour de bon ne doit pas se faire à l'aveugle :
      · une playlist utilise ce template → **409 en la nommant**, sauf `?force=true` ;
      · `force=true` DÉTACHE d'abord les playlists (`id_template = NULL`). Sans ça la
        suppression violerait la clé étrangère ; et surtout, une playlist qui pointe vers
        un template disparu bascule **silencieusement** du multi-zones au plein écran
        mono-zone à la diffusion (`ws.py` ne trouve plus la ligne et retombe sur
        `is_multizone = False`). On préfère un refus explicite à cette dégradation muette.

    La suppression est TRACÉE (`cms_audit_log`) : la ligne du template disparaît, le journal
    reste — c'est précisément ce qui manquait pour répondre à « qui a supprimé ça ? ».
    """
    tmpl = await _get_visible_template(db, current_user, id_template)
    await assert_can_edit_node(db, current_user, tmpl.id_governance_node, "this template")

    playlists = await _playlists_using(db, id_template)
    if playlists and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"\"{tmpl.template_name}\" is used by "
                + ", ".join(f"\"{p.playlist_name}\"" for p in playlists[:5])
                + (f" and {len(playlists) - 5} other(s)" if len(playlists) > 5 else "")
                + " — these playlists would switch to full-screen single-zone."
            ),
        )

    name, zones = tmpl.template_name, len((tmpl.layout or {}).get("zones") or [])
    for p in playlists:
        p.id_template = None
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="delete",
        resource_type="template", resource_id=str(id_template), source="cms",
        detail={"template_name": name, "zones": zones,
                "detached_playlists": [p.playlist_name for p in playlists]},
    ))
    await db.delete(tmpl)
    await db.commit()
    # Les playlists detachees basculent du multi-zones au plein ecran mono-zone : c'est le
    # changement de rendu le plus brutal du produit, il doit atteindre les ecrans tout de
    # suite. Aucune playlist concernee ⇒ rien ne change a l'antenne ⇒ on ne pousse pas.
    if playlists:
        await push_org_force_sync(db)
