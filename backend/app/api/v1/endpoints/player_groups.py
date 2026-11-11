"""
Player Groups (Sprint 1.7).

POST   /player-groups                         — create group
GET    /player-groups                         — list groups for org
GET    /player-groups/{id}                    — get group
PUT    /player-groups/{id}                    — update group
DELETE /player-groups/{id}                    — delete group
GET    /player-groups/{id}/players            — list players in group
POST   /player-groups/{id}/players            — add player to group
DELETE /player-groups/{id}/players/{id_player} — remove player from group
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_admin_or_permission, require_org_admin
from app.models.event import CmsEvent
from app.models.player import CmsPlayer
from app.models.player_group import CmsPlayerGroup, CmsPlayerGroupMember
from app.models.schedule import CmsScheduleTarget
from app.models.user import CmsUser
from app.schemas.player import PlayerRead
from app.schemas.player_group import PlayerGroupCreate, PlayerGroupMemberAdd, PlayerGroupRead, PlayerGroupUpdate
from app.services.targeting import event_target_config_without

router = APIRouter(prefix="/player-groups", tags=["player-groups"])


async def _get_group_or_404(id_group: int, db: AsyncSession) -> CmsPlayerGroup:
    group = (
        await db.execute(select(CmsPlayerGroup).where(CmsPlayerGroup.id_group == id_group))
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


@router.post("/", response_model=PlayerGroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: PlayerGroupCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerGroup:
    group = CmsPlayerGroup(
        id_org=current_user.id_org,
        group_name=body.group_name,
        group_type=body.group_type,
        description=body.description,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


@router.get("/", response_model=list[PlayerGroupRead])
async def list_groups(
    # Lecture : honore players:read (cible campagnes/programmes) — l'org-admin passe
    # toujours. Les mutations de groupe restent org-admin.
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlayerGroupRead]:
    from app.services.governance_scope import user_player_scope

    result = await db.execute(
        select(CmsPlayerGroup).order_by(CmsPlayerGroup.group_name)
    )
    groups = list(result.scalars().all())
    ids_by_group: dict[int, list[int]] = {}
    if groups:
        memberships = (await db.execute(
            select(CmsPlayerGroupMember.id_group, CmsPlayerGroupMember.id_player)
            .where(CmsPlayerGroupMember.id_group.in_([g.id_group for g in groups]))
        )).all()
        for gid, pid in memberships:
            ids_by_group.setdefault(gid, []).append(pid)
    # N8/G1 : pour un admin de nœud, ne montrer que les groupes qui touchent son périmètre,
    # et n'exposer que leurs membres DANS ce périmètre (les autres ids fuiraient le parc).
    scope = await user_player_scope(db, current_user)
    out: list[PlayerGroupRead] = []
    for g in groups:
        member_ids = ids_by_group.get(g.id_group, [])
        if scope is not None:
            member_ids = [pid for pid in member_ids if pid in scope]
            if not member_ids:
                continue
        item = PlayerGroupRead.model_validate(g)
        item.player_ids = member_ids
        out.append(item)
    return out


@router.get("/{id_group}", response_model=PlayerGroupRead)
async def get_group(
    id_group: int,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerGroup:
    return await _get_group_or_404(id_group, db)


@router.put("/{id_group}", response_model=PlayerGroupRead)
async def update_group(
    id_group: int,
    body: PlayerGroupUpdate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPlayerGroup:
    group = await _get_group_or_404(id_group, db)
    if body.group_name is not None:
        group.group_name = body.group_name
    if body.group_type is not None:
        group.group_type = body.group_type
    if body.description is not None:
        group.description = body.description
    await db.commit()
    await db.refresh(group)
    return group


@router.delete("/{id_group}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    id_group: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    group = await _get_group_or_404(id_group, db)
    # Cibles GÉNÉRIQUES sans FK (target_type/target_id) : sans nettoyage, un groupe supprimé
    # reste ciblé à vie par des schedules/events — même fuite que la suppression de player
    # (fix f5ac283 du 2026-07-07). "player_group" = alias accepté à l'entrée (targeting.py).
    await db.execute(
        sa_delete(CmsScheduleTarget).where(
            CmsScheduleTarget.target_type.in_(("group", "player_group")),
            CmsScheduleTarget.target_id == id_group,
        )
    )
    # Events : target_config JSONB → retirer l'id (les deux formes, hétérogène comprise ;
    # l'alias historique `player_group` est normalisé par le helper).
    events = (await db.execute(
        select(CmsEvent).where(CmsEvent.id_org == current_user.id_org)
    )).scalars().all()
    for ev in events:
        cleaned = event_target_config_without(ev.target_config, "group", id_group)
        if cleaned is not None:
            ev.target_config = cleaned
    await db.delete(group)
    await db.commit()


@router.get("/{id_group}/players", response_model=list[PlayerRead])
async def list_group_players(
    id_group: int,
    current_user: Annotated[CmsUser, Depends(require_admin_or_permission("players", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsPlayer]:
    from app.services.governance_scope import user_player_scope

    await _get_group_or_404(id_group, db)
    stmt = (
        select(CmsPlayer)
        .join(CmsPlayerGroupMember, CmsPlayer.id_player == CmsPlayerGroupMember.id_player)
        .where(CmsPlayerGroupMember.id_group == id_group)
        .order_by(CmsPlayer.player_name)
    )
    # N8/G1 : membres restreints au périmètre de gouvernance de l'utilisateur.
    scope = await user_player_scope(db, current_user)
    if scope is not None:
        stmt = stmt.where(CmsPlayer.id_player.in_(scope or {-1}))
    return list((await db.execute(stmt)).scalars().all())


@router.post("/{id_group}/players", status_code=status.HTTP_204_NO_CONTENT)
async def add_player_to_group(
    id_group: int,
    body: PlayerGroupMemberAdd,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_group_or_404(id_group, db)
    # Verify player exists and belongs to same org
    player = (
        await db.execute(select(CmsPlayer).where(CmsPlayer.id_player == body.id_player))
    ).scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    # Idempotent — ignore if already member
    existing = (
        await db.execute(
            select(CmsPlayerGroupMember).where(
                CmsPlayerGroupMember.id_group == id_group,
                CmsPlayerGroupMember.id_player == body.id_player,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(CmsPlayerGroupMember(id_group=id_group, id_player=body.id_player))
        await db.commit()


@router.delete("/{id_group}/players/{id_player}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_player_from_group(
    id_group: int,
    id_player: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    member = (
        await db.execute(
            select(CmsPlayerGroupMember).where(
                CmsPlayerGroupMember.id_group == id_group,
                CmsPlayerGroupMember.id_player == id_player,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    await db.delete(member)
    await db.commit()
