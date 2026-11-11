"""
Groupes de SITES (S6 Retail Media — cible « région » pour campagnes ET programmation).

POST   /site-groups                          — create group
GET    /site-groups                          — list groups for org (+ site/player counts)
GET    /site-groups/{id}                     — get group
PUT    /site-groups/{id}                     — update group
DELETE /site-groups/{id}                     — delete group (+ purge des cibles orphelines)
GET    /site-groups/{id}/sites               — list sites in group
POST   /site-groups/{id}/sites               — add site to group (idempotent)
DELETE /site-groups/{id}/sites/{id_site}     — remove site from group
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_admin_or_permission, require_org_admin
from app.models.campaign import CmsCampaignTarget
from app.models.event import CmsEvent
from app.models.organization import CmsSite
from app.models.player import CmsPlayer
from app.models.schedule import CmsScheduleTarget
from app.models.site_group import CmsSiteGroup, CmsSiteGroupMember
from app.models.user import CmsUser
from app.schemas.site import SiteRead
from app.schemas.site_group import SiteGroupCreate, SiteGroupMemberAdd, SiteGroupRead, SiteGroupUpdate
from app.services.targeting import event_target_config_without

router = APIRouter(prefix="/site-groups", tags=["site-groups"])


async def _get_group_or_404(id_site_group: int, db: AsyncSession) -> CmsSiteGroup:
    group = (
        await db.execute(
            select(CmsSiteGroup).where(CmsSiteGroup.id_site_group == id_site_group)
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


async def _with_counts(db: AsyncSession, groups: list[CmsSiteGroup]) -> list[SiteGroupRead]:
    """Peuple site_count + player_count (players des sites membres) par groupe."""
    if not groups:
        return []
    gids = [g.id_site_group for g in groups]
    memberships = (await db.execute(
        select(CmsSiteGroupMember.id_site_group, CmsSiteGroupMember.id_site)
        .where(CmsSiteGroupMember.id_site_group.in_(gids))
    )).all()
    site_ids_by_group: dict[int, list[int]] = {}
    site_counts: dict[int, int] = {}
    for gid, sid in memberships:
        site_ids_by_group.setdefault(gid, []).append(sid)
        site_counts[gid] = site_counts.get(gid, 0) + 1
    player_counts = dict((await db.execute(
        select(CmsSiteGroupMember.id_site_group, func.count(CmsPlayer.id_player))
        .join(CmsPlayer, CmsPlayer.id_site == CmsSiteGroupMember.id_site)
        .where(CmsSiteGroupMember.id_site_group.in_(gids))
        .group_by(CmsSiteGroupMember.id_site_group)
    )).all())
    out: list[SiteGroupRead] = []
    for g in groups:
        item = SiteGroupRead.model_validate(g)
        item.site_count = site_counts.get(g.id_site_group, 0)
        item.player_count = player_counts.get(g.id_site_group, 0)
        item.site_ids = site_ids_by_group.get(g.id_site_group, [])
        out.append(item)
    return out


@router.post("/", response_model=SiteGroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: SiteGroupCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteGroupRead:
    group = CmsSiteGroup(
        id_org=current_user.id_org,
        group_name=body.group_name,
        description=body.description,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return SiteGroupRead.model_validate(group)


@router.get("/", response_model=list[SiteGroupRead])
async def list_groups(
    # Lecture : honore players:read (cible campagnes/programmes) — l'org-admin passe
    # toujours. Les mutations de groupe restent org-admin.
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SiteGroupRead]:
    groups = list((await db.execute(
        select(CmsSiteGroup).order_by(CmsSiteGroup.group_name)
    )).scalars().all())
    return await _with_counts(db, groups)


@router.get("/{id_site_group}", response_model=SiteGroupRead)
async def get_group(
    id_site_group: int,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteGroupRead:
    group = await _get_group_or_404(id_site_group, db)
    return (await _with_counts(db, [group]))[0]


@router.put("/{id_site_group}", response_model=SiteGroupRead)
async def update_group(
    id_site_group: int,
    body: SiteGroupUpdate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SiteGroupRead:
    group = await _get_group_or_404(id_site_group, db)
    if body.group_name is not None:
        group.group_name = body.group_name
    if body.description is not None:
        group.description = body.description
    await db.commit()
    await db.refresh(group)
    return (await _with_counts(db, [group]))[0]


@router.delete("/{id_site_group}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    id_site_group: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    group = await _get_group_or_404(id_site_group, db)
    # Cibles GÉNÉRIQUES sans FK : même purge que la suppression d'un groupe de players
    # (schedules + events + campagnes) — sinon le groupe supprimé reste ciblé à vie.
    await db.execute(
        sa_delete(CmsScheduleTarget).where(
            CmsScheduleTarget.target_type == "site_group",
            CmsScheduleTarget.target_id == id_site_group,
        )
    )
    await db.execute(
        sa_delete(CmsCampaignTarget).where(
            CmsCampaignTarget.target_type == "site_group",
            CmsCampaignTarget.target_id == id_site_group,
        )
    )
    events = (await db.execute(
        select(CmsEvent).where(CmsEvent.id_org == current_user.id_org)
    )).scalars().all()
    for ev in events:
        # Les deux formes de target_config, hétérogène comprise.
        cleaned = event_target_config_without(ev.target_config, "site_group", id_site_group)
        if cleaned is not None:
            ev.target_config = cleaned
    await db.delete(group)
    await db.commit()


@router.get("/{id_site_group}/sites", response_model=list[SiteRead])
async def list_group_sites(
    id_site_group: int,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsSite]:
    await _get_group_or_404(id_site_group, db)
    result = await db.execute(
        select(CmsSite)
        .join(CmsSiteGroupMember, CmsSite.id_site == CmsSiteGroupMember.id_site)
        .where(CmsSiteGroupMember.id_site_group == id_site_group)
        .order_by(CmsSite.site_name)
    )
    return list(result.scalars().all())


@router.post("/{id_site_group}/sites", status_code=status.HTTP_204_NO_CONTENT)
async def add_site_to_group(
    id_site_group: int,
    body: SiteGroupMemberAdd,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_group_or_404(id_site_group, db)
    site = (
        await db.execute(select(CmsSite).where(
            CmsSite.id_site == body.id_site, CmsSite.id_org == current_user.id_org
        ))
    ).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")

    existing = (
        await db.execute(
            select(CmsSiteGroupMember).where(
                CmsSiteGroupMember.id_site_group == id_site_group,
                CmsSiteGroupMember.id_site == body.id_site,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(CmsSiteGroupMember(
            id_site_group=id_site_group, id_site=body.id_site, id_org=current_user.id_org
        ))
        await db.commit()


@router.delete("/{id_site_group}/sites/{id_site}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_site_from_group(
    id_site_group: int,
    id_site: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    member = (
        await db.execute(
            select(CmsSiteGroupMember).where(
                CmsSiteGroupMember.id_site_group == id_site_group,
                CmsSiteGroupMember.id_site == id_site,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    await db.delete(member)
    await db.commit()
