"""
Dossiers médias (§ retours clients) — arborescence de classement.

Path matérialisé (pattern Gouvernance §XVIII). Complémentaire aux Tags §XVII.

GET    /media/folders            — arbre de l'org (+ child_count, media_count)
POST   /media/folders            — créer un dossier
PUT    /media/folders/{id}       — renommer et/ou déplacer (parent_id, None = racine)
DELETE /media/folders/{id}       — supprimer (bloqué si sous-dossiers ; médias → racine)
POST   /media/folders/move-media — déplacer des médias vers un dossier
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_permission
from app.models.media import CmsMedia
from app.models.media_folder import CmsMediaFolder
from app.services.media_folders import folder_full_paths
from app.models.user import CmsUser
from app.services.governance_access import media_visibility, user_can_edit_node
from app.schemas.media import FolderCreate, FolderRead, FolderUpdate, MediaMoveRequest

router = APIRouter(prefix="/media/folders", tags=["media-folders"])


async def _get_folder(db: AsyncSession, id_org: int, id_folder: int) -> CmsMediaFolder:
    f = (await db.execute(
        select(CmsMediaFolder).where(
            CmsMediaFolder.id_folder == id_folder, CmsMediaFolder.id_org == id_org
        )
    )).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
    return f


async def _counts(
    db: AsyncSession, id_org: int, user: CmsUser
) -> tuple[dict[int, int], dict[int, int]]:
    """(sous-dossiers directs par parent_id, médias directs par id_folder).

    `media_count` porte le MÊME filtre de visibilité que `GET /media/` : sans lui, un
    dossier s'annonçait « 12 médias » à quelqu'un dont la liste n'en renvoyait que 3 —
    un compteur qui décrit du contenu invisible est à la fois faux à l'écran et
    indiscret (audit du menu Média, 2026-07-28).
    """
    child_rows = (await db.execute(
        select(CmsMediaFolder.parent_id, func.count())
        .where(CmsMediaFolder.id_org == id_org, CmsMediaFolder.parent_id.is_not(None))
        .group_by(CmsMediaFolder.parent_id)
    )).all()
    gov_clause, _visible, _editable = await media_visibility(db, user)
    media_stmt = (
        select(CmsMedia.id_folder, func.count())
        .where(CmsMedia.id_org == id_org, CmsMedia.id_folder.is_not(None),
               CmsMedia.is_deleted == False)  # noqa: E712
    )
    clause = gov_clause(CmsMedia.id_governance_node)
    if clause is not None:
        media_stmt = media_stmt.where(clause)
    media_rows = (await db.execute(media_stmt.group_by(CmsMedia.id_folder))).all()
    return ({pid: c for pid, c in child_rows}, {fid: c for fid, c in media_rows})


def _to_read(f: CmsMediaFolder, child_count: int, media_count: int) -> FolderRead:
    r = FolderRead.model_validate(f)
    r.child_count = child_count
    r.media_count = media_count
    return r


def _hierarchical_order(folders: list[CmsMediaFolder]) -> list[CmsMediaFolder]:
    """Parcours en profondeur : chaque dossier immédiatement suivi de ses descendants,
    frères triés par NOM. Retourne aussi de quoi construire le chemin lisible.

    ⚠️ NE PAS revenir à un `ORDER BY path` SQL. `path` est une chaîne d'identifiants
    séparés par des points, et la base est en collation `en_US.utf8` — qui **ignore la
    ponctuation** en comparaison primaire. « 11 » et « 1.13 » s'y comparent donc comme
    « 11 » et « 113 », d'où « 11 » AVANT « 1.13 » : mesuré en production, le dossier
    racine « Document » et son enfant « Samsung » s'intercalaient entre « Video » et ses
    8 enfants. Dans le tiroir de classement — une liste à plat indentée par `depth` —
    « Video » paraissait donc VIDE et ses 8 enfants paraissaient appartenir à
    « Document ». Un `COLLATE "C"` corrigerait le symptôme, mais laisserait le tri des
    frères dépendre d'identifiants (1.13 avant 1.5) : on ordonne donc ici, sur ce qui
    est réellement affiché — le nom.
    """
    children: dict[int | None, list[CmsMediaFolder]] = {}
    for f in folders:
        children.setdefault(f.parent_id, []).append(f)
    for siblings in children.values():
        siblings.sort(key=lambda f: (f.folder_name.casefold(), f.id_folder))

    out: list[CmsMediaFolder] = []
    seen: set[int] = set()  # garde anti-boucle : un cycle de parenté ne doit pas figer l'API

    def walk(parent_id: int | None) -> None:
        for f in children.get(parent_id, []):
            if f.id_folder in seen:
                continue
            seen.add(f.id_folder)
            out.append(f)
            walk(f.id_folder)

    walk(None)
    # Un dossier dont le parent a disparu (donnée incohérente) resterait invisible :
    # on le raccroche à la fin plutôt que de le faire disparaître en silence.
    out.extend(f for f in folders if f.id_folder not in seen)
    return out


# `_full_paths` vit desormais dans `services/media_folders.py` : la recherche globale a
# besoin exactement du meme chemin lisible (P2.5), et deux implementations finiraient
# par diverger.
_full_paths = folder_full_paths


@router.get("", response_model=list[FolderRead])
async def list_folders(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FolderRead]:
    """Tous les dossiers de l'org, en ordre hiérarchique (parent puis ses descendants).

    Chaque entrée porte `full_path` (« Video / Divers ») : deux dossiers homonymes sous
    deux parents différents doivent rester distinguables partout où on les liste à plat.
    """
    folders = list((await db.execute(
        select(CmsMediaFolder).where(CmsMediaFolder.id_org == current_user.id_org)
    )).scalars().all())
    child_counts, media_counts = await _counts(db, current_user.id_org, current_user)
    paths = _full_paths(folders)
    out = []
    for f in _hierarchical_order(folders):
        item = _to_read(f, child_counts.get(f.id_folder, 0), media_counts.get(f.id_folder, 0))
        item.full_path = paths.get(f.id_folder, f.folder_name)
        out.append(item)
    return out


@router.post("", response_model=FolderRead, status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: FolderCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FolderRead:
    parent: CmsMediaFolder | None = None
    if body.parent_id is not None:
        parent = await _get_folder(db, current_user.id_org, body.parent_id)

    folder = CmsMediaFolder(
        id_org=current_user.id_org,
        parent_id=parent.id_folder if parent else None,
        folder_name=body.folder_name,
        depth=(parent.depth + 1) if parent else 0,
        path="",
    )
    db.add(folder)
    await db.flush()
    folder.path = f"{parent.path}.{folder.id_folder}" if parent else str(folder.id_folder)
    await db.commit()
    await db.refresh(folder)
    return _to_read(folder, 0, 0)


@router.put("/{id_folder}", response_model=FolderRead)
async def update_folder(
    id_folder: int,
    body: FolderUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FolderRead:
    folder = await _get_folder(db, current_user.id_org, id_folder)

    if folder.system_key is not None and (
        (body.folder_name is not None and body.folder_name != folder.folder_name)
        or ("parent_id" in body.model_fields_set and body.parent_id != folder.parent_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="System folder: it cannot be renamed or moved",
        )

    if body.folder_name is not None:
        folder.folder_name = body.folder_name

    # Déplacement (changement de parent) — recalcul du path/depth du nœud + descendants.
    if "parent_id" in body.model_fields_set and body.parent_id != folder.parent_id:
        new_parent: CmsMediaFolder | None = None
        if body.parent_id is not None:
            new_parent = await _get_folder(db, current_user.id_org, body.parent_id)
            # Anti-cycle : on ne peut pas déplacer un dossier dans lui-même ou un descendant.
            if new_parent.id_folder == folder.id_folder or new_parent.path.startswith(f"{folder.path}."):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid move (folder into itself or one of its descendants)",
                )

        old_path = folder.path
        old_depth = folder.depth
        new_path = f"{new_parent.path}.{folder.id_folder}" if new_parent else str(folder.id_folder)
        new_depth = (new_parent.depth + 1) if new_parent else 0
        delta = new_depth - old_depth

        # Descendants : remplacer le préfixe de path + ajuster depth.
        descendants = (await db.execute(
            select(CmsMediaFolder).where(
                CmsMediaFolder.id_org == current_user.id_org,
                CmsMediaFolder.path.like(f"{old_path}.%"),
            )
        )).scalars().all()
        for d in descendants:
            d.path = new_path + d.path[len(old_path):]
            d.depth = d.depth + delta

        folder.parent_id = new_parent.id_folder if new_parent else None
        folder.path = new_path
        folder.depth = new_depth

    await db.commit()
    await db.refresh(folder)
    child_counts, media_counts = await _counts(db, current_user.id_org, current_user)
    return _to_read(folder, child_counts.get(id_folder, 0), media_counts.get(id_folder, 0))


@router.delete("/{id_folder}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    id_folder: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    folder = await _get_folder(db, current_user.id_org, id_folder)
    child_count = (await db.execute(
        select(func.count()).select_from(CmsMediaFolder).where(
            CmsMediaFolder.parent_id == id_folder
        )
    )).scalar_one()
    if child_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder not empty (subfolders). Move or delete them first.",
        )
    # Les médias du dossier remontent à la racine (FK ondelete=SET NULL).
    await db.delete(folder)
    await db.commit()


@router.post("/move-media", status_code=status.HTTP_204_NO_CONTENT)
async def move_media(
    body: MediaMoveRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Déplace des médias vers un dossier (id_folder None = racine).

    Garde de nœud (D9) : le classement est une MUTATION du média. Cette route faisait un
    `UPDATE` en masse sur les seuls identifiants reçus — un responsable local pouvait donc
    reclasser des médias nationaux qu'il n'a pas le droit de renommer, et que la liste ne
    lui montre même pas (audit du menu Média, 2026-07-28). On vérifie chaque média, et on
    refuse **tout le lot** en nommant le premier refusé : un déplacement partiellement
    appliqué serait pire qu'un refus — l'utilisateur croirait avoir tout classé.
    """
    if body.id_folder is not None:
        await _get_folder(db, current_user.id_org, body.id_folder)  # valide l'appartenance org
    if not body.id_media:
        return

    rows = list((await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media.in_(body.id_media),
            CmsMedia.id_org == current_user.id_org,
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalars().all())
    for media in rows:
        if not await user_can_edit_node(db, current_user, media.id_governance_node):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"\"{media.media_name}\" belongs to a governance node you do not "
                    "administer — no media was moved"
                ),
            )
    for media in rows:
        media.id_folder = body.id_folder
    await db.commit()
