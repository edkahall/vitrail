"""
Dépendances FastAPI pour l'authentification et l'autorisation.

Hiérarchie de sessions DB :
  get_db_worker  → BYPASSRLS — auth, lookups cross-tenant
  get_db         → Session app avec RLS positionné sur org de l'utilisateur courant
  get_current_user → valide JWT, retourne CmsUser (via get_db_worker)
  require_permission(module, op) → factory RBAC
"""

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import AsyncSessionLocal, get_db_worker
from app.models.organization import CmsOrganization
from app.models.player import CmsPlayer
from app.models.user import CmsApiKey, CmsUser
from app.services import rbac
from app.services.player_token import IDENTITY_UNKNOWN, token_identity
from app.services.session_revocation import token_predates_revocation

_bearer = HTTPBearer(auto_error=True)
_bearer_optional = HTTPBearer(auto_error=False)

# Clé opérateur de l'installeur terrain (§XXIX) : longue durée, révocable, scopée provisioning.
# Distincte des tokens player par-appareil (sfp_, scope player:register). Permet à l'outil terrain
# de créer des payloads + lire l'état d'un player SANS le JWT org-admin (15 min) à re-copier.
PROVISIONING_OPERATOR_SCOPE = "provisioning:operator"

# Clé BI (§24.7) : longue durée, révocable, scopée lecture des exports reporting. Préfixe sfr_.
# Consommée par Power BI / Tableau / Metabase — header Bearer OU query `?api_key=` (les
# connecteurs BI web ne savent pas toujours poser un header).
REPORTS_EXPORT_SCOPE = "reports:export"


async def _user_from_token(
    token: str, db: AsyncSession, *, accept: tuple[str, ...]
) -> CmsUser:
    """Valide un JWT dont le TYPE est dans `accept`, et retourne l'utilisateur actif.

    Le type est discriminant : c'est lui, et non la seule signature, qui distingue un jeton
    d'accès d'un jeton de rafraîchissement ou d'enrôlement. Utilise get_db_worker (BYPASSRLS)
    pour le lookup par id_user.
    """
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
    except JWTError:
        raise credentials_exc

    if payload.get("type") not in accept:
        raise credentials_exc

    id_user_str: str | None = payload.get("sub")
    if not id_user_str:
        raise credentials_exc
    try:
        id_user = int(id_user_str)
    except (TypeError, ValueError):
        raise credentials_exc

    result = await db.execute(
        select(CmsUser).where(
            CmsUser.id_user == id_user,
            CmsUser.is_active == True,  # noqa: E712
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exc

    # Révocation des sessions (P1.1) : un jeton émis avant la dernière coupure ne vaut
    # plus rien, même s'il est parfaitement signé et non expiré. Le refactor de P0.2 a
    # fait de cette fonction le point unique pour les jetons d'ACCÈS et d'ENRÔLEMENT —
    # mais PAS pour le rafraîchissement, qui décode de son côté : `/auth/refresh` porte
    # la même comparaison, sans quoi la révocation serait décorative.
    if token_predates_revocation(user, payload):
        raise credentials_exc

    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CmsUser:
    """
    Valide le JWT access token et retourne l'utilisateur actif.
    Utilise get_db_worker (BYPASSRLS) pour le lookup par id_user.
    """
    return await _user_from_token(credentials.credentials, db, accept=("access",))


async def get_user_for_totp_enrolment(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CmsUser:
    """La SEULE dépendance qui accepte un jeton d'enrôlement 2FA.

    Deux chemins y mènent, et c'est voulu : l'enrôlement volontaire depuis le profil (jeton
    d'accès ordinaire) et l'administrateur qui ne peut pas se connecter tant qu'il n'est pas
    enrôlé (jeton d'enrôlement délivré par /auth/login).

    ⚠️ Ne poser cette dépendance QUE sur /auth/totp/setup et /auth/totp/verify. L'ajouter
    ailleurs rendrait la 2FA obligatoire contournable par quiconque détient le mot de passe —
    un test (`test_auth_totp_enrolment.py`) mesure qu'elle n'ouvre rien d'autre.
    """
    return await _user_from_token(
        credentials.credentials, db, accept=("access", "totp_enrolment")
    )


async def get_db(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
) -> AsyncSession:
    """
    Session app avec RLS positionné sur l'org de l'utilisateur courant.
    Doit être résolu APRÈS get_current_user pour garantir que org_id est disponible.
    """
    async with AsyncSessionLocal() as session:
        # Le contexte RLS est (re)posé en `SET LOCAL` à CHAQUE début de transaction par le
        # listener `after_begin` (cf. db/session.py) — survit aux commits multi-étapes d'un
        # endpoint ET ne fuit jamais sur la connexion poolée. Ici on range l'org cible et on
        # force l'ouverture de la transaction pour que le GUC soit appliqué avant toute requête.
        session.info["current_org_id"] = current_user.id_org
        await session.connection()
        yield session


def require_permission(module: str, operation: str):
    """
    Factory de dépendance RBAC.

    Usage :
        @router.post("/", dependencies=[Depends(require_permission("media", "create"))])
    """

    async def _check(
        current_user: Annotated[CmsUser, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db_worker)],
    ) -> CmsUser:
        allowed = await rbac.check_permission(db, current_user.id_user, module, operation)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {module}:{operation}",
            )
        return current_user

    return _check


def require_admin_or_permission(module: str, operation: str):
    """RBAC granulaire, avec l'org-admin qui passe TOUJOURS.

    Utilisé sur l'administration (users, groups, org_settings, api_keys), dont
    les permissions étaient seedées mais vérifiées NULLE PART : ces routes
    étaient gatées par le booléen `require_org_admin`, ce qui rendait
    `users:*`/`groups:*` décoratifs et empêchait de déléguer la gestion des
    comptes à un rôle custom (audit rôles 2026-07-20).

    ⚠️ POURQUOI PAS UN SIMPLE `require_permission` : on ne peut pas supposer que
    `CMS_ORG_ADMIN` porte le catalogue complet. `bootstrap.py` crée ce rôle en
    `on_conflict_do_nothing` SANS permissions, et rien ne les lui rattache
    ensuite. Un basculement en RBAC strict enfermerait donc l'admin hors de sa
    propre organisation — sans super-admin pour le dépanner, puisqu'il n'existe
    aucun chemin d'en créer. Défaut attrapé par la suite de tests le 2026-07-20.
    """

    async def _check(
        current_user: Annotated[CmsUser, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db_worker)],
    ) -> CmsUser:
        roles = await rbac.get_user_role_keys(db, current_user.id_user)
        if "CMS_SUPER_ADMIN" in roles or "CMS_ORG_ADMIN" in roles:
            return current_user
        if await rbac.check_permission(db, current_user.id_user, module, operation):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {module}:{operation}",
        )

    return _check


def require_any_permission(*pairs: tuple[str, str]):
    """RBAC granulaire acceptant PLUSIEURS permissions équivalentes (l'une suffit).

    Sert la séparation **ADDITIVE** des modules qui vivaient sous `media:*` — flux,
    pages web, tags, génération IA (audit du menu Média, 2026-07-28). Constat d'Ed :
    « tu ne peux pas confier la gestion des flux à quelqu'un sans lui ouvrir toute la
    médiathèque en écriture, suppression de vidéos comprise. »

    ⚠️ POURQUOI ADDITIF ET NON UNE BASCULE SÈCHE : le produit tourne déjà chez un client,
    et **16 comptes** portent aujourd'hui `media:edit` via 4 rôles — dont des rôles
    PERSONNALISÉS qu'un client a pu créer et que nous ne connaissons pas. Exiger
    `feeds:edit` du jour au lendemain leur retirerait l'accès aux flux sans préavis.
    On accepte donc l'ancienne permission OU la nouvelle : la délégation fine devient
    possible (« gestionnaire de flux » sans la médiathèque) sans rien casser.
    Contrepartie assumée et documentée : `media:edit` reste un passe-partout pour qui
    le détient déjà.

    L'org-admin passe toujours, pour la raison exposée dans `require_admin_or_permission`.
    """
    if not pairs:
        raise ValueError("require_any_permission expects at least one permission")

    async def _check(
        current_user: Annotated[CmsUser, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db_worker)],
    ) -> CmsUser:
        roles = await rbac.get_user_role_keys(db, current_user.id_user)
        if "CMS_SUPER_ADMIN" in roles or "CMS_ORG_ADMIN" in roles:
            return current_user
        for module, operation in pairs:
            if await rbac.check_permission(db, current_user.id_user, module, operation):
                return current_user
        # Le message nomme la permission ATTENDUE en premier (la nouvelle, spécifique) :
        # c'est celle qu'on veut voir accorder, pas le passe-partout hérité.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {pairs[0][0]}:{pairs[0][1]}",
        )

    return _check


async def get_current_player(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CmsPlayer:
    """Valide un token player (type=player) et retourne le CmsPlayer correspondant."""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired player token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Token machine de player = permanent → pas de vérification d'expiration.
        # La révocation est assurée plus bas par la correspondance avec player.player_token.
        payload = decode_token(credentials.credentials, verify_exp=False)
    except JWTError:
        raise credentials_exc

    if payload.get("type") != "player":
        raise credentials_exc

    id_player_str: str | None = payload.get("sub")
    if not id_player_str:
        raise credentials_exc
    try:
        id_player = int(id_player_str)
    except (TypeError, ValueError):
        raise credentials_exc

    result = await db.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == id_player)
    )
    player = result.scalar_one_or_none()
    if player is None:
        raise credentials_exc

    # Révocation (audit 2026-06-24) : le token présenté doit correspondre au token courant
    # stocké sur le player, OU à l'ancien token encore dans la fenêtre de grâce (rotation
    # « Option B », 2026-06-26). Tout autre token (fuité / révoqué) est rejeté. La rotation
    # elle-même est pilotée par le WS + le worker, pas par ce chemin HTTP (dépendance légère).
    identity = token_identity(
        credentials.credentials,
        player.player_token,
        player.player_token_prev,
        player.player_token_prev_until,
        datetime.now(UTC),
    )
    if identity == IDENTITY_UNKNOWN:
        raise credentials_exc

    # Org suspendue → on coupe l'accès du player (cohérent avec le blocage du login utilisateur).
    org_active = (
        await db.execute(
            select(CmsOrganization.is_active).where(CmsOrganization.id_org == player.id_org)
        )
    ).scalar_one_or_none()
    if org_active is False:
        raise credentials_exc

    return player


async def require_super_admin(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CmsUser:
    """Dépendance réservée au CMS_SUPER_ADMIN."""
    role_keys = await rbac.get_user_role_keys(db, current_user.id_user)
    if "CMS_SUPER_ADMIN" not in role_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Restricted to the Super Administrator",
        )
    return current_user


async def require_org_admin(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CmsUser:
    """Dépendance réservée au CMS_ORG_ADMIN (ou SUPER_ADMIN)."""
    role_keys = await rbac.get_user_role_keys(db, current_user.id_user)
    if not {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Restricted to the organization administrator",
        )
    return current_user


async def require_server_operator(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CmsUser:
    """Dépendance réservée à l'EXPLOITANT du serveur (`rbac.user_is_server_operator`) :
    super-admin, OU org-admin d'un serveur sans super-admin actif."""
    if not await rbac.user_is_server_operator(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Restricted to the server operator",
        )
    return current_user


class ProvisioningActor:
    """Acteur autorisé sur les endpoints de provisioning terrain : soit un org-admin (JWT), soit
    une clé opérateur (sfk_). Ne porte que l'org (+ id_user si JWT) — jamais de droits org-admin
    généraux : les endpoints de provisioning sont les seuls à accepter la clé."""

    def __init__(self, id_org: int, id_user: int | None = None, kind: str = "user"):
        self.id_org = id_org
        self.id_user = id_user
        self.kind = kind  # "user" | "key"


def _api_key_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def require_provisioning_actor(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ProvisioningActor:
    """Accepte une **clé opérateur** `sfk_` (scope provisioning, longue durée) OU un **JWT
    org-admin**. Renvoie l'org autorisée. Utilisé par l'installeur terrain (§XXIX)."""
    cred = credentials.credentials
    if cred.startswith("sfk_"):
        exc = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired or revoked provisioning key",
            headers={"WWW-Authenticate": "Bearer"},
        )
        key = (
            await db.execute(
                select(CmsApiKey).where(
                    CmsApiKey.key_hash == _api_key_hash(cred),
                    CmsApiKey.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if key is None or PROVISIONING_OPERATOR_SCOPE not in (key.scopes or []):
            raise exc
        if key.expires_at is not None and key.expires_at < datetime.now(UTC):
            raise exc
        org_active = (
            await db.execute(
                select(CmsOrganization.is_active).where(CmsOrganization.id_org == key.id_org)
            )
        ).scalar_one_or_none()
        if org_active is False:
            raise exc
        key.last_used_at = datetime.now(UTC)
        await db.commit()
        return ProvisioningActor(id_org=key.id_org, id_user=key.id_user, kind="key")

    # Sinon : JWT → doit être org-admin (ou super-admin).
    user = await get_current_user(credentials, db)
    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    if not {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Restricted to the organization administrator or a provisioning key",
        )
    return ProvisioningActor(id_org=user.id_org, id_user=user.id_user, kind="user")


class ReportingActor:
    """Acteur autorisé sur les exports BI (§24.7) : un utilisateur `reports:read` (JWT, hors démo)
    ou une clé BI `sfr_` (scope reports:export). Ne porte que l'org — les endpoints d'export sont
    les seuls à accepter la clé."""

    def __init__(self, id_org: int, id_user: int | None = None, kind: str = "user"):
        self.id_org = id_org
        self.id_user = id_user
        self.kind = kind  # "user" | "key"


async def require_reporting_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_optional)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ReportingActor:
    """Auth des exports BI : Bearer JWT (`reports:read`, comptes démo refusés — même règle que
    les exports Proof of Play) OU clé BI `sfr_` via Bearer ou query `?api_key=`."""
    cred = credentials.credentials if credentials else request.query_params.get("api_key")
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid, expired or revoked BI key or token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not cred:
        raise exc

    if cred.startswith("sfr_"):
        key = (
            await db.execute(
                select(CmsApiKey).where(
                    CmsApiKey.key_hash == _api_key_hash(cred),
                    CmsApiKey.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if key is None or REPORTS_EXPORT_SCOPE not in (key.scopes or []):
            raise exc
        if key.expires_at is not None and key.expires_at < datetime.now(UTC):
            raise exc
        org_active = (
            await db.execute(
                select(CmsOrganization.is_active).where(CmsOrganization.id_org == key.id_org)
            )
        ).scalar_one_or_none()
        if org_active is False:
            raise exc
        key.last_used_at = datetime.now(UTC)
        await db.commit()
        return ReportingActor(id_org=key.id_org, id_user=key.id_user, kind="key")

    if credentials is None:
        raise exc  # une valeur non-sfr_ en query param n'est jamais un JWT accepté

    user = await get_current_user(credentials, db)
    allowed = await rbac.check_permission(db, user.id_user, "reports", "read")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: reports:read",
        )
    if user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="demo_blocked")
    return ReportingActor(id_org=user.id_org, id_user=user.id_user, kind="user")


# Clé d'import de campagne (v2 import JSON — 2026-07-12) : une agence/un annonceur
# pousse ses ordres d'insertion PAR API, sans humain. Préfixe sfi_, scope dédié.
CAMPAIGN_IMPORT_SCOPE = "campaigns:import"


class CampaignImportActor:
    """Acteur autorisé sur l'import de campagne : utilisateur `campaigns:create` (JWT,
    même gate que le drawer) OU clé machine `sfi_` (scope campaigns:import)."""

    def __init__(self, id_org: int, id_user: int | None = None, kind: str = "user"):
        self.id_org = id_org
        self.id_user = id_user
        self.kind = kind  # "user" | "key"


async def require_campaign_import_actor(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> CampaignImportActor:
    cred = credentials.credentials
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid, expired or revoked import key or token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if cred.startswith("sfi_"):
        key = (
            await db.execute(
                select(CmsApiKey).where(
                    CmsApiKey.key_hash == _api_key_hash(cred),
                    CmsApiKey.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if key is None or CAMPAIGN_IMPORT_SCOPE not in (key.scopes or []):
            raise exc
        if key.expires_at is not None and key.expires_at < datetime.now(UTC):
            raise exc
        org_active = (
            await db.execute(
                select(CmsOrganization.is_active).where(CmsOrganization.id_org == key.id_org)
            )
        ).scalar_one_or_none()
        if org_active is False:
            raise exc
        key.last_used_at = datetime.now(UTC)
        await db.commit()
        return CampaignImportActor(id_org=key.id_org, id_user=key.id_user, kind="key")

    user = await get_current_user(credentials, db)
    allowed = await rbac.check_permission(db, user.id_user, "campaigns", "create")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: campaigns:create",
        )
    return CampaignImportActor(id_org=user.id_org, id_user=user.id_user, kind="user")


async def get_db_for_actor(
    actor: Annotated[ProvisioningActor, Depends(require_provisioning_actor)],
) -> AsyncSession:
    """Session app avec RLS positionné sur l'org de l'acteur de provisioning (miroir de get_db)."""
    async with AsyncSessionLocal() as session:
        session.info["current_org_id"] = actor.id_org
        await session.connection()
        yield session


async def deny_demo(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
) -> CmsUser:
    """Org-admin MAIS refusé aux comptes démo (§II) — pour les endpoints de LECTURE sensibles que
    le DemoGuardMiddleware ne couvre pas (GET), ex. export CSV/PDF Proof of Play. Renvoie le code
    `demo_blocked` → le front affiche le toast localisé."""
    if current_user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="demo_blocked")
    return current_user
