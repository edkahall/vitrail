"""
Endpoints organisations et paramètres auth org (§II.1, §2.1.1).

CRUD orgs      → /orgs (CMS_SUPER_ADMIN uniquement)
Auth settings  → /orgs/{id}/auth-settings (CMS_ORG_ADMIN+)
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import pyotp

from app.core.crypto import VaultDecryptError, decrypt_secret, vault_available
from app.core.deps import get_current_user, get_db_worker, require_org_admin, require_super_admin
from app.core.security import hash_password
from app.models.audit_log import CmsAuditLog
from app.models.organization import CmsOrgAiCredential, CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import admin_refs, ai_credentials, spend
from app.services.password_policy import validate_password_for_org
from app.services.tags import seed_standard_namespaces
from app.services.totp import protect_totp_secret
from app.services.session_revocation import revocation_bound
from app.schemas.organization import (
    OrgAiCredentialRead,
    OrgAiCredentialUpsert,
    OrgAuthSettingsRead,
    OrgAuthSettingsUpdate,
    OrgCreate,
    OrgPlayerAlertSettings,
    OrgProvisionAdmin,
    OrgProvisionAdminResult,
    OrgRead,
    OrgUpdate,
)

router = APIRouter(prefix="/orgs", tags=["organizations"])


# ── CRUD organisations (SUPER_ADMIN) ─────────────────────────────────────────

@router.get("", response_model=list[OrgRead])
async def list_orgs(
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[OrgRead]:
    """Liste toutes les organisations (sans RLS — SUPER_ADMIN uniquement)."""
    result = await db.execute(select(CmsOrganization).order_by(CmsOrganization.org_name))
    return [OrgRead.model_validate(o) for o in result.scalars()]


@router.post("", response_model=OrgRead, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: OrgCreate,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgRead:
    existing = await db.execute(
        select(CmsOrganization).where(CmsOrganization.org_slug == body.org_slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already in use")
    org = CmsOrganization(**body.model_dump())
    db.add(org)
    await db.commit()
    await db.refresh(org)
    # Créer les paramètres auth par défaut
    db.add(CmsOrgAuthSettings(id_org=org.id_org))
    await db.commit()
    # Seed des namespaces de tags standards (§XVII)
    await seed_standard_namespaces(db, org.id_org)
    await db.commit()
    return OrgRead.model_validate(org)


@router.post(
    "/{id_org}/provision-admin",
    response_model=OrgProvisionAdminResult,
    status_code=status.HTTP_201_CREATED,
)
async def provision_admin(
    id_org: int,
    body: OrgProvisionAdmin,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgProvisionAdminResult:
    """Crée le 1er administrateur (CMS_ORG_ADMIN) d'un tenant. Worker/BYPASSRLS car
    on écrit dans une AUTRE org que celle du super-admin. TOTP enrôlé (2FA forcée
    pour les admins) → l'URI est retournée pour le QR de provisioning."""
    org = (
        await db.execute(select(CmsOrganization).where(CmsOrganization.id_org == id_org))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if (await db.execute(select(CmsUser).where(CmsUser.email == body.email))).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")

    role = (
        await db.execute(select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System role CMS_ORG_ADMIN missing (IAM seed)",
        )

    await validate_password_for_org(db, id_org, body.password)

    totp_secret = pyotp.random_base32()
    user = CmsUser(
        id_org=id_org,
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        is_active=True,
        # Chiffré au repos via le coffre (durcissement du pentest 2026-06-24) : ce
        # chemin stockait encore le secret EN CLAIR. `reveal_totp_secret` tolère les
        # deux formes, la bascule est donc sans effet de bord sur les comptes existants.
        totp_secret=protect_totp_secret(totp_secret),
        totp_enabled=True,
    )
    db.add(user)
    await db.flush()
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()

    totp_uri = pyotp.totp.TOTP(totp_secret).provisioning_uri(
        name=body.email, issuer_name="Vitrail"
    )
    return OrgProvisionAdminResult(
        id_user=user.id_user, email=user.email, totp_secret=totp_secret, totp_uri=totp_uri
    )


@router.post(
    "/{id_org}/reset-admin",
    response_model=OrgProvisionAdminResult,
    status_code=status.HTTP_200_OK,
)
async def reset_org_admin(
    id_org: int,
    body: OrgProvisionAdmin,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgProvisionAdminResult:
    """Réinitialise le mot de passe + la 2FA d'un compte (par email) DANS une org —
    récupération quand un admin d'org perd son accès. Le super-admin doit connaître
    l'email (il ne voit pas la liste des users de l'org). Ne donne aucune visibilité
    sur le contenu/les autres users du tenant."""
    org = (
        await db.execute(select(CmsOrganization).where(CmsOrganization.id_org == id_org))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    user = (
        await db.execute(
            select(CmsUser).where(CmsUser.email == body.email, CmsUser.id_org == id_org)
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No user with this email in this organization",
        )

    await validate_password_for_org(db, id_org, body.password)

    totp_secret = pyotp.random_base32()
    await db.execute(
        update(CmsUser)
        .where(CmsUser.id_user == user.id_user)
        .values(
            password_hash=hash_password(body.password),
            totp_secret=protect_totp_secret(totp_secret),  # cf. provision_admin
            totp_enabled=True,
            # P1.1 — la seule réinitialisation de mot de passe du produit, donc le seul
            # endroit où « couper l'accès » a un sens. Sans cette borne, un compte repris
            # en main gardait ses sessions ouvertes : jeton d'accès valable jusqu'à son
            # expiration, et jeton de rafraîchissement pour les prolonger des jours.
            # `provision-admin`, lui, CRÉE un compte (409 si l'email existe) : rien à
            # révoquer là-bas.
            sessions_valid_from=revocation_bound(),
        )
    )
    await db.commit()

    totp_uri = pyotp.totp.TOTP(totp_secret).provisioning_uri(name=body.email, issuer_name="Vitrail")
    return OrgProvisionAdminResult(
        id_user=user.id_user, email=user.email, totp_secret=totp_secret, totp_uri=totp_uri
    )


@router.get("/{id_org}", response_model=OrgRead)
async def get_org(
    id_org: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgRead:
    """
    ORG_ADMIN peut voir son propre org.
    SUPER_ADMIN peut voir n'importe quel org.
    """
    from app.services import rbac

    role_keys = await rbac.get_user_role_keys(db, current_user.id_user)
    is_super = "CMS_SUPER_ADMIN" in role_keys
    if not is_super and current_user.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    result = await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return OrgRead.model_validate(org)


@router.patch("/{id_org}", response_model=OrgRead)
async def update_org(
    id_org: int,
    body: OrgUpdate,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgRead:
    result = await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    if body.ai_quota_period and body.ai_quota_period not in {"weekly", "monthly"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ai_quota_period must be weekly or monthly",
        )
    if body.ai_quota_unit and body.ai_quota_unit not in {"tokens", "eur"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ai_quota_unit must be tokens or eur",
        )
    if body.ai_alert_threshold_pct is not None and not (0 <= body.ai_alert_threshold_pct <= 100):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ai_alert_threshold_pct must be between 0 and 100",
        )
    if body.ai_byok_mode and body.ai_byok_mode not in {"strict", "platform"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ai_byok_mode must be strict or platform",
        )
    if body.ai_spend_cap_period and body.ai_spend_cap_period not in spend.PERIODS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ai_spend_cap_period must be day, week or month",
        )
    # Un plafond NÉGATIF n'a aucun sens et se lirait « aucun plafond » (`_cap` traite ≤ 0
    # ainsi) : on refuse à la saisie plutôt que de laisser croire à une protection posée.
    for field in ("ai_spend_cap_usd", "ai_user_spend_cap_usd"):
        v = getattr(body, field)
        if v is not None and v < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} cannot be negative (0 = no cap)",
            )
    values = body.model_dump(exclude_none=True)
    if values:
        await db.execute(
            update(CmsOrganization).where(CmsOrganization.id_org == id_org).values(**values)
        )
        await db.commit()
        await db.refresh(org)
    return OrgRead.model_validate(org)


# ── Suppression d'une organisation (SUPER_ADMIN) — P3.3 ──────────────────────
#
# Il n'existait AUCUN endpoint de suppression : un super-admin pouvait créer des
# organisations, jamais en retirer une. On en ajoute un, mais **honnête** : il efface
# franchement, ou il refuse en NOMMANT ce qui le retient.
#
# ⚠️ Ce qu'un opérateur doit voir avant de trancher n'est pas seulement « bloqué » :
# mesuré sur le schéma réel, supprimer une organisation **emporterait 30 tables en
# CASCADE**. Le rapport d'impact expose donc les trois natures de lien séparément
# (retient / serait emporté / serait délié), cf. `services/admin_refs`.


def _org_closed_door(id_org: int, actor: CmsUser) -> str | None:
    """Portes fermées PAR CONSTRUCTION. Renvoie la cause, ou None si la porte est ouverte.

    L'écran s'en sert pour GRISER le bouton et l'expliquer : jamais un bouton qui ne peut
    qu'échouer. Le serveur la revérifie — un écran n'est pas une sécurité.
    """
    if id_org == actor.id_org:
        return "own_org"
    return None


@router.get("/{id_org}/deletion-impact")
async def org_deletion_impact(
    id_org: int,
    actor: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> dict:
    """Ce qui retient, ce qui serait emporté, ce qui serait délié."""
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    impact = (await admin_refs.organization_impact(db, id_org)).as_dict()
    door = _org_closed_door(id_org, actor)
    impact["closed_reason"] = door
    # Une porte fermée prime sur le décompte : inutile de laisser croire qu'un ménage
    # rendrait la suppression possible.
    impact["can_delete"] = impact["can_delete"] and door is None
    # L'alternative EXISTE et elle est réversible (§II.1) : la nommer plutôt que de laisser
    # l'opérateur devant un refus sans issue.
    impact["suspend_available"] = org.is_active
    return impact


@router.delete("/{id_org}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org(
    id_org: int,
    actor: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """Supprime RÉELLEMENT l'organisation, ou refuse en nommant ce qui la retient.

    ⛔ Aucun `?force=true` ici, contrairement aux templates ou aux paniers : forcer
    emporterait une trentaine de tables en cascade — médias, programmes, preuves de
    diffusion — de façon irréversible et sans que personne ait vu la liste. Quand ça
    coince, la réponse est le ménage ou la SUSPENSION, pas un drapeau.
    """
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    door = _org_closed_door(id_org, actor)
    if door == "own_org":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own organization",
        )

    impact = await admin_refs.organization_impact(db, id_org)
    if impact.blocking:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "org_has_references",
                "message": "This organization is still referenced.",
                "blocking": [r.as_dict() for r in impact.blocking],
                "suspend_available": org.is_active,
            },
        )

    name = org.org_name
    await db.execute(CmsOrganization.__table__.delete().where(CmsOrganization.id_org == id_org))
    # Le journal d'audit est écrit dans l'org de l'ACTEUR, pas dans celle qui disparaît :
    # sa ligne serait partie avec elle (`cms_audit_log.id_org` est en SET NULL, mais on
    # perdrait le rattachement qui rend la trace lisible).
    db.add(CmsAuditLog(
        id_org=actor.id_org, id_user=actor.id_user, action="delete",
        resource_type="organization", resource_id=str(id_org),
        detail={"org_name": name}, source="api",
    ))
    await db.commit()


# ── Paramètres auth par organisation (ORG_ADMIN+) ─────────────────────────────

@router.get("/{id_org}/auth-settings", response_model=OrgAuthSettingsRead)
async def get_auth_settings(
    id_org: int,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgAuthSettingsRead:
    from app.services import rbac as _rbac
    role_keys = await _rbac.get_user_role_keys(db, admin.id_user)
    if "CMS_SUPER_ADMIN" not in role_keys and admin.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    result = await db.execute(
        select(CmsOrgAuthSettings).where(CmsOrgAuthSettings.id_org == id_org)
    )
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settings not found")
    return OrgAuthSettingsRead.model_validate(cfg)


@router.put("/{id_org}/auth-settings", response_model=OrgAuthSettingsRead)
async def update_auth_settings(
    id_org: int,
    body: OrgAuthSettingsUpdate,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgAuthSettingsRead:
    from app.services import rbac as _rbac
    role_keys = await _rbac.get_user_role_keys(db, admin.id_user)
    if "CMS_SUPER_ADMIN" not in role_keys and admin.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if body.auth_2fa_policy and body.auth_2fa_policy not in {"disabled", "optional", "required"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth_2fa_policy must be disabled, optional or required",
        )

    values = body.model_dump(exclude_none=True)
    values["updated_at"] = datetime.now(timezone.utc)

    await db.execute(
        update(CmsOrgAuthSettings)
        .where(CmsOrgAuthSettings.id_org == id_org)
        .values(**values)
    )
    await db.commit()

    result = await db.execute(
        select(CmsOrgAuthSettings).where(CmsOrgAuthSettings.id_org == id_org)
    )
    return OrgAuthSettingsRead.model_validate(result.scalar_one())


# ── Player alert settings ──────────────────────────────────────────────────────

_DEFAULT_ALERT_SETTINGS = {
    "offline_critical_min": 10,
    "cpu_warn_pct": 90.0,
    "ram_warn_pct": 90.0,
    "disk_warn_pct": 85.0,
    "token_warn_days": 7,
    "min_player_version": None,
}


@router.get("/{id_org}/player-alert-settings", response_model=OrgPlayerAlertSettings)
async def get_player_alert_settings(
    id_org: int,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgPlayerAlertSettings:
    from app.services import rbac as _rbac
    role_keys = await _rbac.get_user_role_keys(db, admin.id_user)
    if "CMS_SUPER_ADMIN" not in role_keys and admin.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    merged = {**_DEFAULT_ALERT_SETTINGS, **(org.player_alert_settings or {})}
    return OrgPlayerAlertSettings(**merged)


@router.put("/{id_org}/player-alert-settings", response_model=OrgPlayerAlertSettings)
async def update_player_alert_settings(
    id_org: int,
    body: OrgPlayerAlertSettings,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgPlayerAlertSettings:
    from app.services import rbac as _rbac
    role_keys = await _rbac.get_user_role_keys(db, admin.id_user)
    if "CMS_SUPER_ADMIN" not in role_keys and admin.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.execute(
        update(CmsOrganization)
        .where(CmsOrganization.id_org == id_org)
        .values(player_alert_settings=body.model_dump())
    )
    await db.commit()
    return body


# ── Clés API IA par organisation (BYOK §XXVII — ORG_ADMIN+) ──────────────────
# Le secret n'est JAMAIS renvoyé en clair (seul key_last4). Chiffré au repos (Fernet).
# Cf. docs/PLAN_BYOK_AI_KEYS.md.

async def _assert_same_org_or_super(db: AsyncSession, admin: CmsUser, id_org: int) -> None:
    from app.services import rbac as _rbac
    role_keys = await _rbac.get_user_role_keys(db, admin.id_user)
    if "CMS_SUPER_ADMIN" not in role_keys and admin.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")


@router.get("/{id_org}/ai-keys", response_model=list[OrgAiCredentialRead])
async def list_ai_keys(
    id_org: int,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[OrgAiCredentialRead]:
    await _assert_same_org_or_super(db, admin, id_org)
    result = await db.execute(
        select(CmsOrgAiCredential)
        .where(CmsOrgAiCredential.id_org == id_org)
        .order_by(CmsOrgAiCredential.provider)
    )
    return [OrgAiCredentialRead.model_validate(r) for r in result.scalars()]


@router.put("/{id_org}/ai-keys", response_model=OrgAiCredentialRead)
async def upsert_ai_key(
    id_org: int,
    body: OrgAiCredentialUpsert,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgAiCredentialRead:
    await _assert_same_org_or_super(db, admin, id_org)
    if not vault_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault unavailable (CMS_VAULT_KEY not configured)",
        )
    if not body.api_key.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty key")
    row = await ai_credentials.upsert_org_key(
        db, id_org, body.provider, body.api_key.strip(), created_by=admin.id_user
    )
    db.add(CmsAuditLog(
        id_org=id_org,
        id_user=admin.id_user,
        action="ai_key_set",
        resource_type="ai_credential",
        resource_id=body.provider,
        detail={"provider": body.provider, "key_last4": row.key_last4},
        source="cms",
    ))
    await db.commit()
    await db.refresh(row)
    return OrgAiCredentialRead.model_validate(row)


@router.delete("/{id_org}/ai-keys/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_key(
    id_org: int,
    provider: str,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    await _assert_same_org_or_super(db, admin, id_org)
    deleted = await ai_credentials.delete_org_key(db, id_org, provider)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    db.add(CmsAuditLog(
        id_org=id_org,
        id_user=admin.id_user,
        action="ai_key_delete",
        resource_type="ai_credential",
        resource_id=provider,
        detail={"provider": provider},
        source="cms",
    ))
    await db.commit()


class OrgAiPlanRead(BaseModel):
    """Ce que l'offre du provider permet, lu EN DIRECT.

    Jamais stocké : un instantané pris à la saisie de la clé se périme à la première remise
    à zéro mensuelle, et à tout changement d'offre — c'est-à-dire pile au moment où l'on
    voudrait que la plateforme s'en aperçoive.
    """

    provider: str
    supported: bool = False      # False = ce provider n'expose rien de tel
    tier: str | None = None
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    resets_at: datetime | None = None
    detail: str | None = None    # pourquoi il n'y a rien à montrer


@router.get("/{id_org}/ai-keys/{provider}/plan", response_model=OrgAiPlanRead)
async def read_ai_key_plan(
    id_org: int,
    provider: str,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> OrgAiPlanRead:
    """L'abonnement derrière une clé : offre et quota restant.

    Seul ElevenLabs répond à cette question aujourd'hui, et l'endpoint le DIT au lieu
    d'inventer une abstraction sur un seul provider : fal.ai est à l'usage, sans notion
    d'abonnement interrogeable, et Anthropic n'expose aucun quota.
    """
    await _assert_same_org_or_super(db, admin, id_org)
    if provider != "elevenlabs":
        return OrgAiPlanRead(
            provider=provider, supported=False,
            detail="This provider does not expose a queryable subscription.",
        )
    if not vault_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault unavailable (CMS_VAULT_KEY not configured)",
        )
    row = await ai_credentials.get_org_key_row(db, id_org, provider)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    try:
        plaintext = decrypt_secret(row.secret_encrypted)
    except VaultDecryptError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Key unreadable (vault key changed?) — enter the key again",
        ) from None

    from app.services.generation import GenerationError, elevenlabs

    try:
        quota = await run_in_threadpool(elevenlabs.get_quota, api_key=plaintext)
    except GenerationError as exc:
        # Les mots du provider (« clé refusée », « 402 … ») valent mieux qu'un panneau vide.
        return OrgAiPlanRead(provider=provider, supported=True, detail=str(exc))
    except Exception:  # noqa: BLE001
        return OrgAiPlanRead(provider=provider, supported=True,
                             detail="Subscription unreadable for now.")
    if quota is None:
        return OrgAiPlanRead(provider=provider, supported=True,
                             detail="Subscription unreadable for now.")
    return OrgAiPlanRead(
        provider=provider, supported=True, tier=quota.tier, used=quota.used,
        limit=quota.limit, remaining=quota.remaining, resets_at=quota.resets_at,
    )


@router.post("/{id_org}/ai-keys/{provider}/test")
async def test_ai_key(
    id_org: int,
    provider: str,
    admin: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> dict:
    """Teste la clé enregistrée par un appel cheap au provider → met à jour le statut."""
    await _assert_same_org_or_super(db, admin, id_org)
    if not vault_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault unavailable (CMS_VAULT_KEY not configured)",
        )
    row = await ai_credentials.get_org_key_row(db, id_org, provider)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    try:
        plaintext = decrypt_secret(row.secret_encrypted)
    except VaultDecryptError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Key unreadable (vault key changed?) — enter the key again",
        ) from None
    ok, detail = await ai_credentials.validate_provider_key(provider, plaintext)
    if ok is True:
        row.status = "valid"
        row.validated_at = datetime.now(timezone.utc)
    elif ok is False:
        row.status = "invalid"
    # ok is None → statut laissé inchangé (non vérifiable)
    await db.commit()
    return {"ok": ok, "detail": detail, "status": row.status}
