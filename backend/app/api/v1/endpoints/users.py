"""
Endpoints gestion utilisateurs, groupes et rôles (§II.1).

CRUD users          → /users
CRUD groups         → /groups
Role assignments    → /users/{id}/roles
Custom roles        → /roles
/users/me           → profil courant
"""

from typing import Annotated

import pyotp
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_current_user,
    get_db,
    get_db_worker,
    require_admin_or_permission,
)
from app.core.security import hash_password
from app.services.rbac import get_user_permissions, get_user_role_keys
from app.models.audit_log import CmsAuditLog
from app.models.organization import CmsOrganization
from app.models.role import (
    CmsGroupMember,
    CmsGroupRole,
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserGroup,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser
from app.schemas.user import (
    AssignedRoleRead,
    GroupCreate,
    GroupRead,
    PermissionRead,
    RoleCreate,
    RolePermissionsUpdate,
    RoleRead,
    TotpResetResponse,
    UserCreate,
    UserRead,
    UserRoleAssign,
    UserUpdate,
)
from app.services import admin_refs, rbac
from app.services.password_policy import validate_password_for_org
from app.services.totp import protect_totp_secret

router = APIRouter(tags=["users"])


# ── /users/me ─────────────────────────────────────────────────────────────────

@router.get("/users/me", response_model=UserRead)
async def get_me(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> UserRead:
    org = (
        await db.execute(
            select(CmsOrganization.org_name).where(CmsOrganization.id_org == current_user.id_org)
        )
    ).scalar_one_or_none()
    result = UserRead.model_validate(current_user)
    result.org_name = org
    # Droits effectifs de l'utilisateur — l'UI s'en sert pour masquer les entrées
    # de menu et les boutons d'action inutilisables. C'est un CONFORT D'AFFICHAGE,
    # jamais une sécurité : l'autorisation reste entièrement côté serveur
    # (`require_permission` / `require_org_admin`). Un front modifié ne gagne rien.
    result.roles = await get_user_role_keys(db, current_user.id_user)
    result.permissions = sorted(await get_user_permissions(db, current_user.id_user))
    return result


@router.patch("/users/me", response_model=UserRead)
async def update_me(
    body: UserUpdate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> UserRead:
    """Mise à jour profil : full_name, avatar_url, locale/theme (§XXVIII)."""
    values = body.model_dump(exclude_none=True)
    # Sécu : le self-service ne peut PAS modifier son propre statut démo (sinon un compte démo
    # s'échapperait du mode lecture seule). Seul l'admin (update_user) pose is_demo.
    values.pop("is_demo", None)
    if values:
        await db.execute(
            update(CmsUser).where(CmsUser.id_user == current_user.id_user).values(**values)
        )
        await db.commit()
        result = await db.execute(
            select(CmsUser).where(CmsUser.id_user == current_user.id_user)
        )
        current_user = result.scalar_one()
    return UserRead.model_validate(current_user)


# ── CRUD utilisateurs ─────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserRead])
async def list_users(
    current: Annotated[CmsUser, Depends(require_admin_or_permission("users", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[UserRead]:
    """Liste les utilisateurs de l'organisation (RLS appliqué). Un compte démo ne voit que
    lui-même (§II) — évite qu'un prospect récupère les logins des autres comptes."""
    q = select(CmsUser).where(CmsUser.is_active == True).order_by(CmsUser.created_at)  # noqa: E712
    if current.is_demo:
        q = q.where(CmsUser.id_user == current.id_user)
    result = await db.execute(q)
    return [UserRead.model_validate(u) for u in result.scalars()]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    existing = await db.execute(select(CmsUser).where(CmsUser.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")
    await validate_password_for_org(db, admin.id_org, body.password)
    user = CmsUser(
        id_org=admin.id_org,
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        locale=body.locale,
        is_demo=body.is_demo,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")
    await db.refresh(user)
    db.add(_iam_audit(admin, "create", "user", str(user.id_user), {"email": user.email}))
    await db.commit()
    return UserRead.model_validate(user)


async def _org_user_or_404(db: AsyncSession, id_org: int, id_user: int) -> CmsUser:
    """Compte de MON organisation — 404 sinon (audit menu ⑫, 2026-07-31).

    🔴 `GET`, `PATCH` et `DELETE /users/{id}` chargeaient le compte **sans filtre
    d'organisation** : l'isolation reposait uniquement sur la RLS. Or on a mesuré le 30/07
    qu'une install client tournait en SUPERUSER avec toute la RLS inerte — le module des
    COMPTES est le dernier endroit où la seule barrière doit être celle qu'on a vue tomber.
    Mesuré avant correctif : désactiver le compte d'une AUTRE organisation répondait **204**.

    🔑 Le même fichier filtre correctement dans 4 autres routes (rôles, TOTP) : c'était une
    omission, pas un choix.
    """
    user = (await db.execute(
        select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.id_org == id_org)
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="User not found")
    return user


def _iam_audit(
    actor: CmsUser, action: str, resource_type: str, resource_id: str,
    detail: dict | None = None,
) -> CmsAuditLog:
    """Trace d'un geste IAM (audit menu ⑫).

    🔴 Mesuré en production : **0 ligne** pour `user`/`role`/`group` sur plus de 800, alors
    que tous les autres modules en écrivent (399 pour les écrans, 115 pour les playlists).
    C'est pourtant le module qui décide QUI PEUT QUOI : « qui a donné les droits
    d'administration à cette personne, et quand ? » n'avait aucune réponse — et une escalade
    réussie (celle du 20/07 était réelle) ne laissait rien derrière elle.

    ⛔ Jamais de mot de passe ni de secret TOTP dans le détail.
    """
    return CmsAuditLog(
        id_org=actor.id_org, id_user=actor.id_user, action=action,
        resource_type=resource_type, resource_id=resource_id, source="cms",
        detail=detail or {},
    )


@router.get("/users/{id_user}", response_model=UserRead)
async def get_user(
    id_user: int,
    current: Annotated[CmsUser, Depends(require_admin_or_permission("users", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    user = await _org_user_or_404(db, current.id_org, id_user)
    return UserRead.model_validate(user)


@router.patch("/users/{id_user}", response_model=UserRead)
async def update_user(
    id_user: int,
    body: UserUpdate,
    current: Annotated[CmsUser, Depends(require_admin_or_permission("users", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    user = await _org_user_or_404(db, current.id_org, id_user)
    values = body.model_dump(exclude_none=True)
    if values:
        await db.execute(update(CmsUser).where(CmsUser.id_user == id_user).values(**values))
        db.add(_iam_audit(current, "update", "user", str(id_user),
                          {"email": user.email, "changes": sorted(values)}))
        await db.commit()
        await db.refresh(user)
    return UserRead.model_validate(user)


async def _assert_not_last_admin(db: AsyncSession, admin: CmsUser, id_user: int) -> None:
    """Refuse de retirer le DERNIER administrateur actif de l'organisation (P3.3).

    🔴 Trou mesuré : `DELETE /users/{id}` n'avait qu'une garde, « pas son propre compte ».
    Or l'endpoint accepte `users:delete` **ou** l'org-admin — donc un porteur d'un rôle
    personnalisé qui n'est PAS administrateur pouvait désactiver tous les administrateurs
    et laisser l'organisation sans personne pour la rouvrir. La garde « pas soi-même » ne
    l'attrape pas : il ne se supprime pas lui-même, il supprime les autres.

    ✅ Vérifié plutôt que supposé : `UserUpdate` n'expose PAS `is_active`, la désactivation
    ne passe donc que par ici — il n'y a pas de seconde porte à garder (le motif « garde à
    la création mais pas à la mise à jour » ne s'applique pas).
    """
    admins = await rbac.org_admin_user_ids(db, admin.id_org)
    if id_user in admins and len(admins) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "last_org_admin",
                "message": "This is the last active administrator of the organization: "
                           "no one could reopen it.",
            },
        )


@router.get("/users/{id_user}/deletion-impact")
async def user_deletion_impact(
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "read"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> dict:
    """Ce qui retient ce compte, ce qui serait emporté, ce qui serait délié (P3.3).

    Sert à GRISER et EXPLIQUER côté écran plutôt qu'à laisser cliquer un bouton qui ne peut
    qu'échouer. Lecture seule — `get_db_worker` parce que les rôles système portent
    `id_org = NULL` et ne passeraient pas la RLS d'organisation.
    """
    target = (await db.execute(
        select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.id_org == admin.id_org)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    impact = (await admin_refs.user_impact(db, id_user)).as_dict()
    admins = await rbac.org_admin_user_ids(db, admin.id_org)
    reason = None
    if id_user == admin.id_user:
        reason = "self"
    elif id_user in admins and len(admins) <= 1:
        reason = "last_org_admin"
    impact["closed_reason"] = reason
    impact["can_delete"] = impact["can_delete"] and reason is None
    # La désactivation reste possible même quand la suppression ne l'est pas : c'est
    # l'alternative à proposer, et elle est réversible.
    impact["deactivate_available"] = target.is_active and reason is None
    return impact


@router.delete("/users/{id_user}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    purge: bool = False,
) -> None:
    """`purge=false` (défaut) DÉSACTIVE ; `purge=true` supprime RÉELLEMENT (P3.3).

    Les deux gestes sont distincts et l'écran les nomme distinctement — c'était le vrai
    défaut : le bouton s'appelait « Supprimer » et désactivait. La désactivation reste le
    geste NORMAL (l'email reste pris, une ré-invitation rattache le compte) ; la purge est
    là pour le compte créé par erreur, et elle **refuse en nommant** ce qui la retient.

    ⛔ Pas de `?force=true` : la plupart des liens d'un compte sont en `NO ACTION` sur du
    contenu (médias, playlists, programmes créés par lui). Forcer voudrait dire détruire ce
    contenu ou l'orpheliner — jamais ce que quelqu'un veut en « supprimant un compte ».
    """
    if id_user == admin.id_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )
    target = await _org_user_or_404(db, admin.id_org, id_user)
    await _assert_not_last_admin(db, admin, id_user)

    if purge:
        impact = await admin_refs.user_impact(db, id_user)
        if impact.blocking:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "user_has_references",
                    "message": "This account is still referenced.",
                    "blocking": [r.as_dict() for r in impact.blocking],
                    "deactivate_available": target.is_active,
                },
            )
        email = target.email
        # Trace écrite AVANT la suppression : `cms_audit_log.id_user` est en SET NULL, la
        # ligne survit donc au compte — mais son auteur devient anonyme, d'où l'email dans
        # le détail. C'est ce qui permet encore de dire QUI a été supprimé.
        db.add(_iam_audit(admin, "purge", "user", str(id_user), {"email": email}))
        await db.execute(CmsUser.__table__.delete().where(CmsUser.id_user == id_user))
        await db.commit()
        return

    # Désactivation logique. On DÉLIE aussi l'annonceur éventuel (retour Ed 11/07) :
    # « supprimer » le compte doit le faire disparaître de la liste « Accès portail »
    # du drawer annonceur — sinon il y reste affiché « révoqué » à vie. L'email reste
    # pris (audit) ; une ré-invitation le rattache/réactive (get-or-link).
    await db.execute(update(CmsUser).where(CmsUser.id_user == id_user)
                     .values(is_active=False, id_advertiser=None))
    # L'email reste pris : le journal garde donc DE QUI il s'agissait.
    db.add(_iam_audit(admin, "delete", "user", str(id_user), {"email": target.email}))
    await db.commit()


# ── Assignation de rôles à un utilisateur ─────────────────────────────────────

@router.get("/users/{id_user}/roles", response_model=list[AssignedRoleRead])
async def list_user_roles(
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "read"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[AssignedRoleRead]:
    """Rôles assignés à un utilisateur (worker : les rôles système ont id_org=NULL
    et ne passeraient pas la RLS d'org). Borné à l'org de l'admin courant."""
    target = (
        await db.execute(
            select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.id_org == admin.id_org)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    rows = await db.execute(
        select(CmsRole.id_role, CmsRole.role_key, CmsRole.label, CmsUserRoleAssignment.is_deny)
        .join(CmsUserRoleAssignment, CmsUserRoleAssignment.id_role == CmsRole.id_role)
        .where(CmsUserRoleAssignment.id_user == id_user)
        .order_by(CmsRole.is_system.desc(), CmsRole.role_key)
    )
    return [
        AssignedRoleRead(id_role=r.id_role, role_key=r.role_key, label=r.label, is_deny=r.is_deny)
        for r in rows
    ]


# Rôles à privilège PLATEFORME — jamais assignables/retirables par un org-admin.
# Les assigner depuis un org-admin = escalade verticale (org-admin → super-admin →
# contrôle cross-tenant de toutes les organisations). Cf. audit sécurité 2026-06-24.
_PLATFORM_ROLE_KEYS = {"CMS_SUPER_ADMIN"}


async def _assignable_role_or_403(db: AsyncSession, id_role: int, admin: CmsUser) -> CmsRole:
    """Retourne le rôle si un org-admin a le droit de l'(dé)assigner, sinon 403.

    Règle : rôle système NON-plateforme, ou custom role de SA propre org.
    Bloque l'escalade vers CMS_SUPER_ADMIN et l'usage d'un custom role d'une autre org.
    (Worker/BYPASSRLS requis : les rôles système ont id_org=NULL → invisibles sous RLS.)
    """
    role = (
        await db.execute(select(CmsRole).where(CmsRole.id_role == id_role))
    ).scalar_one_or_none()
    if (
        role is None
        or role.role_key in _PLATFORM_ROLE_KEYS
        or not (role.is_system or role.id_org == admin.id_org)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Role not assignable")
    return role


async def _group_in_org_or_404(db: AsyncSession, id_group: int, admin: CmsUser) -> CmsUserGroup:
    """Le groupe doit appartenir à l'org de l'admin.

    Les endpoints de groupe (rôles, membres) prenaient `id_group` au mot et
    inséraient directement : un id d'une autre org était accepté. Cross-tenant =
    404, jamais 403 (règle produit : ne pas révéler l'existence de la ressource).
    """
    group = (
        await db.execute(
            select(CmsUserGroup).where(
                CmsUserGroup.id_group == id_group, CmsUserGroup.id_org == admin.id_org
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


async def _target_user_in_org_or_404(db: AsyncSession, id_user: int, admin: CmsUser) -> CmsUser:
    target = (
        await db.execute(
            select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.id_org == admin.id_org)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return target


@router.post("/users/{id_user}/roles", status_code=status.HTTP_204_NO_CONTENT)
async def assign_role_to_user(
    id_user: int,
    body: UserRoleAssign,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    await _target_user_in_org_or_404(db, id_user, admin)
    await _assignable_role_or_403(db, body.id_role, admin)
    await db.execute(
        insert(CmsUserRoleAssignment)
        .values(id_user=id_user, id_role=body.id_role, is_deny=body.is_deny)
        .on_conflict_do_update(
            index_elements=["id_user", "id_role"],
            set_={"is_deny": body.is_deny},
        )
    )
    # 🔑 Le geste le plus sensible du produit : donner un rôle, c'est donner des droits —
    # et c'est par là qu'est passée l'escalade réelle du 20/07. Sans trace, une escalade
    # réussie ne laisse rien derrière elle.
    db.add(_iam_audit(admin, "assign", "user_role", str(id_user),
                      {"id_role": body.id_role, "is_deny": body.is_deny}))
    await db.commit()


@router.delete("/users/{id_user}/roles/{id_role}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_role_from_user(
    id_user: int,
    id_role: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    await _target_user_in_org_or_404(db, id_user, admin)
    await _assignable_role_or_403(db, id_role, admin)
    await db.execute(
        delete(CmsUserRoleAssignment).where(
            CmsUserRoleAssignment.id_user == id_user,
            CmsUserRoleAssignment.id_role == id_role,
        )
    )
    db.add(_iam_audit(admin, "revoke", "user_role", str(id_user), {"id_role": id_role}))
    await db.commit()


# ── 2FA / TOTP d'un utilisateur (enrôlement par l'admin) ──────────────────────

@router.post("/users/{id_user}/totp/reset", response_model=TotpResetResponse)
async def reset_user_totp(
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> TotpResetResponse:
    """(Ré)enrôle la 2FA d'un utilisateur de l'org : nouveau secret, 2FA activée,
    et renvoie l'URI otpauth (le front en fait un QR pour Google Authenticator).
    Débloque un admin à 2FA forcée qui n'a pas encore enrôlé."""
    target = (
        await db.execute(
            select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.id_org == admin.id_org)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    org_name = (
        await db.execute(
            select(CmsOrganization.org_name).where(CmsOrganization.id_org == admin.id_org)
        )
    ).scalar_one_or_none() or "Vitrail"
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=target.email, issuer_name=org_name)
    # Secret CHIFFRÉ AU REPOS (audit 2026-06-24) ; l'URI/QR renvoyé contient le secret en clair.
    await db.execute(
        update(CmsUser)
        .where(CmsUser.id_user == id_user)
        .values(totp_secret=protect_totp_secret(secret), totp_enabled=True)
    )
    # ⛔ Le SECRET n'entre jamais dans le journal — seulement le fait qu'un ré-enrôlement
    # a eu lieu, et sur quel compte.
    db.add(_iam_audit(admin, "totp_reset", "user", str(id_user), {"email": target.email}))
    await db.commit()
    return TotpResetResponse(email=target.email, secret=secret, otpauth_uri=uri)


@router.post("/users/{id_user}/totp/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_user_totp(
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """Désactive la 2FA d'un utilisateur de l'org (efface secret + codes de secours). L'admin peut
    la ré-enrôler ensuite via /totp/reset. Note : la 2FA reste obligatoire pour ORG/SUPER_ADMIN au
    login → un tel compte re-forcera l'enrôlement à la prochaine connexion."""
    target = (
        await db.execute(
            select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.id_org == admin.id_org)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await db.execute(
        update(CmsUser)
        .where(CmsUser.id_user == id_user)
        .values(totp_enabled=False, totp_secret=None, totp_backup_codes=[])
    )
    # Abaisser le niveau de sécurité d'un compte est un geste légitime en dépannage —
    # mais qui doit se voir.
    db.add(_iam_audit(admin, "totp_disable", "user", str(id_user), {"email": target.email}))
    await db.commit()


# ── CRUD groupes ──────────────────────────────────────────────────────────────

@router.get("/groups", response_model=list[GroupRead])
async def list_groups(
    _: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GroupRead]:
    result = await db.execute(
        select(CmsUserGroup).where(CmsUserGroup.is_active == True).order_by(CmsUserGroup.group_name)  # noqa: E712
    )
    return [GroupRead.model_validate(g) for g in result.scalars()]


@router.post("/groups", response_model=GroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupCreate,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GroupRead:
    group = CmsUserGroup(id_org=admin.id_org, **body.model_dump())
    db.add(group)
    await db.flush()
    db.add(_iam_audit(admin, "create", "group", str(group.id_group),
                      {"group_name": group.group_name}))
    await db.commit()
    await db.refresh(group)
    return GroupRead.model_validate(group)


@router.delete("/groups/{id_group}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    id_group: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Supprime (soft-delete) un groupe de l'org — il disparaît de la liste ; ses membres/rôles
    ne sont plus appliqués."""
    group = (
        await db.execute(
            select(CmsUserGroup).where(
                CmsUserGroup.id_group == id_group, CmsUserGroup.id_org == admin.id_org
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    await db.execute(
        update(CmsUserGroup).where(CmsUserGroup.id_group == id_group).values(is_active=False)
    )
    # Le groupe disparaît de la liste et ses rôles cessent de s'appliquer à TOUS ses
    # membres d'un coup : le journal garde son nom, sinon plus rien ne dit pourquoi des
    # gens ont perdu leurs droits.
    db.add(_iam_audit(admin, "delete", "group", str(id_group),
                      {"group_name": group.group_name}))
    await db.commit()


@router.post("/groups/{id_group}/members/{id_user}", status_code=status.HTTP_204_NO_CONTENT)
async def add_group_member(
    id_group: int,
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    # Groupe ET utilisateur doivent être de l'org : sans ces contrôles, on pouvait
    # placer un utilisateur d'une autre org dans un groupe (et lui en donner les rôles).
    await _group_in_org_or_404(db, id_group, admin)
    await _target_user_in_org_or_404(db, id_user, admin)
    await db.execute(
        insert(CmsGroupMember)
        .values(id_group=id_group, id_user=id_user, added_by=admin.id_user)
        .on_conflict_do_nothing()
    )
    # Entrer dans un groupe, c'est hériter de ses rôles : même portée qu'une attribution
    # directe (`rbac.get_user_role_keys` unionne les deux).
    db.add(_iam_audit(admin, "member_add", "group", str(id_group), {"id_user": id_user}))
    await db.commit()


@router.delete("/groups/{id_group}/members/{id_user}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member(
    id_group: int,
    id_user: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _group_in_org_or_404(db, id_group, admin)
    await db.execute(
        delete(CmsGroupMember).where(
            CmsGroupMember.id_group == id_group,
            CmsGroupMember.id_user == id_user,
        )
    )
    db.add(_iam_audit(admin, "member_remove", "group", str(id_group), {"id_user": id_user}))
    await db.commit()


@router.post("/groups/{id_group}/roles/{id_role}", status_code=status.HTTP_204_NO_CONTENT)
async def assign_role_to_group(
    id_group: int,
    id_role: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """⚠️ MÊME GARDE-FOU QUE L'ASSIGNATION DIRECTE — c'est la 2e porte de l'escalade.

    `rbac.get_user_role_keys` unionne rôles DIRECTS et rôles de GROUPE : sans ce
    contrôle, un org-admin créait un groupe, y attachait CMS_SUPER_ADMIN, s'y
    ajoutait, et obtenait le bypass RBAC/RLS/gouvernance + le cross-tenant. Le
    correctif du 2026-06-24 n'avait fermé que `POST /users/{id}/roles`.
    (Worker/BYPASSRLS requis : les rôles système ont id_org=NULL, donc invisibles
    sous RLS — la vérification échouerait sur une session scopée.)
    """
    await _group_in_org_or_404(db, id_group, admin)
    await _assignable_role_or_403(db, id_role, admin)
    await db.execute(
        insert(CmsGroupRole)
        .values(id_group=id_group, id_role=id_role)
        .on_conflict_do_nothing()
    )
    # ⚠️ C'est la 2ᵉ porte de l'escalade réelle du 20/07 (groupe → rôle → s'y ajouter).
    # La porte est fermée depuis ; la trace fait qu'une tentative ne passe plus inaperçue.
    db.add(_iam_audit(admin, "role_assign", "group", str(id_group), {"id_role": id_role}))
    await db.commit()


@router.delete("/groups/{id_group}/roles/{id_role}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_role_from_group(
    id_group: int,
    id_role: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("groups", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    await _group_in_org_or_404(db, id_group, admin)
    await _assignable_role_or_403(db, id_role, admin)
    await db.execute(
        delete(CmsGroupRole).where(
            CmsGroupRole.id_group == id_group,
            CmsGroupRole.id_role == id_role,
        )
    )
    db.add(_iam_audit(admin, "role_revoke", "group", str(id_group), {"id_role": id_role}))
    await db.commit()


# ── Catalogue de permissions ──────────────────────────────────────────────────

#: Modules dont les permissions sont seedées mais qu'AUCUN endpoint n'implémente
#: encore. `spf` = moteur de présentations interactives §XIV (graphe d'états,
#: éditeur node graph, runtime player) : spécifié, pas développé. On conserve les
#: permissions pour ne pas avoir à les recréer, mais l'éditeur doit les montrer
#: comme indisponibles — cocher un droit qui ne protège rien induit en erreur.
_UNAVAILABLE_MODULES = {"spf"}


@router.get("/permissions", response_model=list[PermissionRead])
async def list_permissions(
    _: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[PermissionRead]:
    """Catalogue des permissions atomiques, pour l'éditeur de rôles.

    Il n'existait AUCUN moyen de connaître ce catalogue depuis l'API : c'est
    l'une des deux raisons pour lesquelles les rôles custom étaient inertes
    (audit rôles 2026-07-20).
    """
    rows = (
        await db.execute(
            select(CmsPermission).order_by(CmsPermission.module, CmsPermission.operation)
        )
    ).scalars()
    return [
        PermissionRead(
            id_permission=p.id_permission,
            module=p.module,
            operation=p.operation,
            description=p.label,
            available=p.module not in _UNAVAILABLE_MODULES,
        )
        for p in rows
    ]


@router.put("/roles/{id_role}/permissions", response_model=list[str])
async def set_role_permissions(
    id_role: int,
    body: RolePermissionsUpdate,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[str]:
    """Remplace le jeu de permissions d'un rôle CUSTOM de l'organisation.

    L'endpoint qui manquait : `POST /roles` fabriquait un rôle vide qu'aucun
    appel ne pouvait ensuite garnir, donc définitivement sans effet. Seul
    `POST /roles/{id}/copy` produisait un rôle utile, au jeu de droits figé.

    Garde-fous :
      • rôles SYSTÈME immuables — les modifier changerait le sens d'un rôle pour
        toutes les organisations (les rôles système ont `id_org = NULL`) ;
      • un org-admin ne peut accorder QUE des droits qu'il détient lui-même,
        sinon un rôle custom deviendrait un chemin d'escalade parallèle à celui
        fermé sur les groupes le 2026-07-20 ;
      • les permissions de modules non développés (`spf:*`) sont refusées.
    """
    role = (
        await db.execute(select(CmsRole).where(CmsRole.id_role == id_role))
    ).scalar_one_or_none()
    if role is None or role.id_org != admin.id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="System role cannot be modified"
        )

    wanted = set(body.permissions)
    catalog = {
        f"{p.module}:{p.operation}": p
        for p in (await db.execute(select(CmsPermission))).scalars()
    }
    unknown = wanted - catalog.keys()
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown permissions: {', '.join(sorted(unknown))}",
        )
    unavailable = {p for p in wanted if catalog[p].module in _UNAVAILABLE_MODULES}
    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Feature not available: {', '.join(sorted(unavailable))}",
        )
    # Anti-escalade : un délégué ne peut accorder QUE des droits qu'il détient —
    # sinon un rôle custom rouvrirait un chemin d'escalade parallèle à celui fermé
    # sur les groupes le 2026-07-20.
    #
    # ⚠️ Les ADMINS sont exemptés explicitement, et non « parce qu'ils ont tout » :
    # `bootstrap.py` crée `CMS_ORG_ADMIN` sans lui rattacher la moindre permission.
    # Se fier au calcul aurait empêché l'admin d'une installation neuve de
    # configurer le moindre rôle. Même piège que sur `require_admin_or_permission`.
    roles = await get_user_role_keys(db, admin.id_user)
    if not {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"} & set(roles):
        own = await get_user_permissions(db, admin.id_user)
        escalating = wanted - own
        if escalating:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permissions you do not hold: {', '.join(sorted(escalating))}",
            )

    await db.execute(delete(CmsRolePermission).where(CmsRolePermission.id_role == id_role))
    for perm in sorted(wanted):
        db.add(
            CmsRolePermission(id_role=id_role, id_permission=catalog[perm].id_permission)
        )
    # Changer les permissions d'un rôle change ce que peuvent faire TOUS ses porteurs, d'un
    # coup et sans les toucher : la mutation la plus large du produit. On garde la liste
    # complète — c'est elle, et non un compteur, qui permet de refaire l'histoire.
    db.add(_iam_audit(admin, "permissions", "role", str(id_role),
                      {"role_key": role.role_key, "permissions": sorted(wanted)}))
    await db.commit()
    return sorted(wanted)


@router.get("/roles/{id_role}/permissions", response_model=list[str])
async def get_role_permissions(
    id_role: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[str]:
    """Permissions d'un rôle (système ou custom de l'org) — alimente l'éditeur."""
    role = (
        await db.execute(select(CmsRole).where(CmsRole.id_role == id_role))
    ).scalar_one_or_none()
    if role is None or (role.id_org is not None and role.id_org != admin.id_org):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    rows = (
        await db.execute(
            select(CmsPermission.module, CmsPermission.operation)
            .join(CmsRolePermission, CmsRolePermission.id_permission == CmsPermission.id_permission)
            .where(CmsRolePermission.id_role == id_role)
            .order_by(CmsPermission.module, CmsPermission.operation)
        )
    ).all()
    return [f"{m}:{o}" for m, o in rows]


# ── Custom roles ──────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleRead])
async def list_roles(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[RoleRead]:
    """Liste les rôles système (id_org=NULL) + custom roles de l'org courante."""
    result = await db.execute(
        select(CmsRole).where(
            (CmsRole.id_org == None) | (CmsRole.id_org == current_user.id_org)  # noqa: E711
        ).order_by(CmsRole.is_system.desc(), CmsRole.role_key)
    )
    return [RoleRead.model_validate(r) for r in result.scalars()]


@router.post("/roles", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def create_custom_role(
    body: RoleCreate,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> RoleRead:
    """Création d'un custom role pour l'organisation (CMS_ORG_ADMIN uniquement)."""
    role = CmsRole(
        id_org=admin.id_org,
        role_key=body.role_key,
        label=body.label,
        description=body.description,
        is_system=False,
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return RoleRead.model_validate(role)


@router.delete("/roles/{id_role}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_role(
    id_role: int,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """Supprime un CUSTOM role de l'org (les rôles système sont protégés). Retire d'abord ses
    permissions et ses affectations (utilisateurs/groupes)."""
    from app.models.role import CmsRolePermission
    role = (
        await db.execute(select(CmsRole).where(CmsRole.id_role == id_role))
    ).scalar_one_or_none()
    # Hors org (y compris rôles système, id_org=NULL) → 404, jamais 403 (règle : ne pas
    # révéler l'existence cross-tenant). Le garde is_system reste en ceinture-bretelles.
    if role is None or role.id_org != admin.id_org or role.is_system:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    await db.execute(delete(CmsRolePermission).where(CmsRolePermission.id_role == id_role))
    await db.execute(delete(CmsUserRoleAssignment).where(CmsUserRoleAssignment.id_role == id_role))
    await db.execute(delete(CmsGroupRole).where(CmsGroupRole.id_role == id_role))
    await db.execute(delete(CmsRole).where(CmsRole.id_role == id_role))
    await db.commit()


@router.post("/roles/{id_role}/copy", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def copy_role(
    id_role: int,
    body: RoleCreate,
    admin: Annotated[CmsUser, Depends(require_admin_or_permission("users", "assign_roles"))],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> RoleRead:
    """Copie un rôle existant (système ou custom de l'org) en nouveau custom role."""
    result = await db.execute(
        select(CmsRole).where(
            CmsRole.id_role == id_role,
            (CmsRole.id_org == None) | (CmsRole.id_org == admin.id_org),  # noqa: E711
        )
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source role not found")
    # 3e porte de l'escalade : la copie recopie les PERMISSIONS. Cloner
    # CMS_SUPER_ADMIN produirait un custom role portant les 71 permissions du
    # catalogue — et un custom role, lui, s'assigne sans restriction.
    if source.role_key in _PLATFORM_ROLE_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Platform role cannot be copied"
        )

    new_role = CmsRole(
        id_org=admin.id_org,
        role_key=body.role_key,
        label=body.label,
        description=body.description,
        is_system=False,
    )
    db.add(new_role)
    await db.flush()

    # Copier les permissions
    from app.models.role import CmsRolePermission
    perms_result = await db.execute(
        select(CmsRolePermission).where(CmsRolePermission.id_role == id_role)
    )
    for rp in perms_result.scalars():
        db.add(CmsRolePermission(id_role=new_role.id_role, id_permission=rp.id_permission))

    await db.commit()
    await db.refresh(new_role)
    return RoleRead.model_validate(new_role)
