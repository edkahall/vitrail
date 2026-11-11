"""
Endpoints d'authentification (§II.1, §2.1.1).

POST /auth/login         — credentials → access + refresh tokens
POST /auth/refresh       — refresh token → nouveau access token
POST /auth/revoke        — invalidation du refresh token (blacklist via Redis)
POST /auth/totp/setup    — génère secret TOTP + QR code + backup codes
POST /auth/totp/verify   — confirme le secret et active le 2FA
"""

import hashlib
from datetime import datetime, timezone
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_user_for_totp_enrolment
from app.core.security import (
    create_access_token,
    create_enrolment_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.db.session import get_db_worker
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.user import CmsUser
from app.schemas.auth import (
    AccessTokenResponse,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    TotpSetupResponse,
    TotpVerifyRequest,
)
from app.services import rbac
from app.services.session_revocation import token_predates_revocation
from app.services.totp import (
    generate_backup_codes,
    generate_totp_secret,
    protect_totp_secret,
    reveal_totp_secret,
    secret_is_encrypted,
    verify_backup_code,
    verify_totp,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Anti-brute-force (audit 2026-06-24) : limite le nombre de tentatives de login par IP et par
# compte sur une fenêtre glissante (compteur Redis). Sans ça, mot de passe ET code TOTP 6 chiffres
# étaient brute-forçables sans entrave. Une connexion réussie réinitialise le compteur du compte.
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW_S = 300

# Hash bcrypt factice (anti-énumération par timing) : on l'évalue quand l'email n'existe pas pour
# que le temps de réponse soit identique à celui d'un mot de passe erroné sur un compte réel.
_DUMMY_PASSWORD_HASH = "$2b$12$98HPvBvrrVs39Df8phXXkuwlWKx75cMQ777CtFiIJ00/4J/QLmtza"


def _redis() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def _client_ip(request: Request) -> str:
    """IP cliente réelle derrière le reverse-proxy Caddy (1ʳᵉ valeur de X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_login_blocked(*keys: str) -> None:
    """Lève 429 si un compteur d'ÉCHECS dépasse le seuil sur la fenêtre (lecture seule —
    une connexion réussie n'incrémente rien, donc pas de blocage d'un usage légitime)."""
    async with _redis() as r:
        for key in keys:
            v = await r.get(key)
            if v is not None and int(v) >= _LOGIN_MAX_ATTEMPTS:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts. Try again in a few minutes.",
                )


async def _record_login_failure(*keys: str) -> None:
    """Incrémente les compteurs d'échecs (TTL posé au 1er échec de la fenêtre)."""
    async with _redis() as r:
        for key in keys:
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, _LOGIN_WINDOW_S)


async def _totp_code_replayed(id_user: int, code: str) -> bool:
    """Anti-rejeu TOTP (audit 2026-06-24) : un code n'est utilisable qu'une fois dans sa
    fenêtre de validité. Renvoie True si le code a DÉJÀ été consommé (rejeu)."""
    h = hashlib.sha256(f"{id_user}:{code}".encode()).hexdigest()
    async with _redis() as r:
        created = await r.set(f"signflow:totp:used:{h}", "1", nx=True, ex=120)
    return not created


def _refresh_blacklist_key(token: str) -> str:
    # SHA-256 du token complet — évite les collisions dues au header JWT commun à tous les tokens HS256
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"signflow:rt:revoked:{digest}"


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> TokenResponse:
    """
    Authentification email/password.
    - Limite anti-brute-force par IP + par compte (Redis)
    - Vérifie la politique 2FA de l'organisation
    - Supporte TOTP et codes de secours
    - Embarque locale, org_id, et role_keys dans le payload JWT
    """
    email_key = f"signflow:login:fail:email:{body.email.lower()}"
    ip_key = f"signflow:login:fail:ip:{_client_ip(request)}"
    fail_keys = (ip_key, email_key)
    await _check_login_blocked(*fail_keys)

    result = await db.execute(
        select(CmsUser).where(CmsUser.email == body.email, CmsUser.is_active == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    _bad_creds = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
    )
    if user is None:
        # Anti-énumération par timing : hash factice pour égaliser le temps de réponse.
        verify_password(body.password, _DUMMY_PASSWORD_HASH)
        await _record_login_failure(*fail_keys)
        raise _bad_creds
    if not verify_password(body.password, user.password_hash):
        await _record_login_failure(*fail_keys)
        raise _bad_creds

    # ── Politique 2FA ──────────────────────────────────────────────────────────
    auth_cfg_result = await db.execute(
        select(CmsOrgAuthSettings).where(CmsOrgAuthSettings.id_org == user.id_org)
    )
    auth_cfg = auth_cfg_result.scalar_one_or_none()
    effective_policy = settings.CMS_2FA_GLOBAL_POLICY
    if auth_cfg:
        effective_policy = auth_cfg.auth_2fa_policy

    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    # MFA obligatoire pour SUPER_ADMIN et ORG_ADMIN
    if {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
        effective_policy = "required"

    # Compte démo (§II) : PAS de 2FA (destiné à des prospects qui n'ont pas de TOTP). Sûr, car le
    # compte est en lecture seule (toute écriture est bloquée par DemoGuardMiddleware).
    if user.is_demo:
        effective_policy = "disabled"

    # Org suspendue → blocage du login (le super-admin, compte plateforme, est exempté)
    if "CMS_SUPER_ADMIN" not in role_keys:
        org_active = (
            await db.execute(
                select(CmsOrganization.is_active).where(CmsOrganization.id_org == user.id_org)
            )
        ).scalar_one_or_none()
        if org_active is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Organization suspended"
            )

    if effective_policy == "required" and not user.totp_enabled:
        # Refusé, mais pas abandonné : l'appelant reçoit un jeton qui n'ouvre que les deux
        # endpoints d'enrôlement (cf. create_enrolment_token). Le mot de passe vient d'être
        # vérifié et le compte n'a pas encore de second facteur — ce jeton n'accorde donc
        # rien que le mot de passe n'accordait déjà, là où sans lui le compte est
        # DÉFINITIVEMENT hors de l'application, sauf depuis un shell sur le serveur.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "2FA required",
                "force_2fa_setup": True,
                "enrolment_token": create_enrolment_token(user.id_user),
            },
            headers={"X-Force-2FA-Setup": "true"},
        )

    if user.totp_enabled and not user.is_demo:
        if body.totp_code:
            if not verify_totp(reveal_totp_secret(user.totp_secret), body.totp_code):
                await _record_login_failure(*fail_keys)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid TOTP code"
                )
            # Single-use : un code déjà consommé dans sa fenêtre est rejeté (anti-rejeu).
            if await _totp_code_replayed(user.id_user, body.totp_code):
                await _record_login_failure(*fail_keys)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="TOTP code already used"
                )
        elif body.backup_code:
            idx = verify_backup_code(body.backup_code, user.totp_backup_codes)
            if idx is None:
                await _record_login_failure(*fail_keys)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid backup code"
                )
            # Consommer le code de secours utilisé
            new_codes = list(user.totp_backup_codes)
            new_codes.pop(idx)
            await db.execute(
                update(CmsUser)
                .where(CmsUser.id_user == user.id_user)
                .values(totp_backup_codes=new_codes)
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="TOTP code required"
            )

    # ── Migration paresseuse : chiffre au repos un secret TOTP encore en clair ──
    if user.totp_enabled and user.totp_secret and not secret_is_encrypted(user.totp_secret):
        enc = protect_totp_secret(user.totp_secret)
        if enc != user.totp_secret:  # le coffre a effectivement chiffré
            await db.execute(
                update(CmsUser).where(CmsUser.id_user == user.id_user).values(totp_secret=enc)
            )

    # ── Mise à jour last_login_at ──────────────────────────────────────────────
    await db.execute(
        update(CmsUser)
        .where(CmsUser.id_user == user.id_user)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await db.commit()

    # Connexion réussie → on efface le compteur d'échecs du compte.
    async with _redis() as r:
        await r.delete(email_key)

    extra = {"org": user.id_org, "roles": role_keys, "locale": user.locale, "is_demo": user.is_demo}
    access = create_access_token(user.id_user, extra=extra)
    refresh = create_refresh_token(user.id_user)

    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        locale=user.locale,
    )


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh_token(
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> AccessTokenResponse:
    """Échange un refresh token valide contre un nouveau access token."""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked refresh token"
    )
    try:
        payload = decode_token(body.refresh_token)
    except JWTError:
        raise credentials_exc

    if payload.get("type") != "refresh":
        raise credentials_exc

    # Vérification blacklist Redis
    async with _redis() as r:
        if await r.exists(_refresh_blacklist_key(body.refresh_token)):
            raise credentials_exc

    id_user = int(payload["sub"])
    result = await db.execute(
        select(CmsUser).where(CmsUser.id_user == id_user, CmsUser.is_active == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exc

    # Révocation des sessions (P1.1) — LE point qui décide si la révocation existe
    # vraiment. Sans lui, couper un accès ne coûterait qu'un aller-retour à l'attaquant :
    # le 401 sur le jeton d'accès déclenche un rafraîchissement automatique côté front,
    # et cette route lui rendrait un jeton neuf, postérieur à la borne. La session
    # survivrait à sa propre révocation, en donnant l'illusion inverse.
    if token_predates_revocation(user, payload):
        raise credentials_exc

    role_keys = await rbac.get_user_role_keys(db, user.id_user)
    extra = {"org": user.id_org, "roles": role_keys, "locale": user.locale, "is_demo": user.is_demo}
    access = create_access_token(user.id_user, extra=extra)

    return AccessTokenResponse(access_token=access)


@router.post("/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(body: RefreshRequest) -> None:
    """Révoque un refresh token (logout)."""
    try:
        payload = decode_token(body.refresh_token)
    except JWTError:
        return  # Token déjà invalide — silencieux

    if payload.get("type") != "refresh":
        return

    exp = payload.get("exp", 0)
    ttl = max(0, exp - int(datetime.now(timezone.utc).timestamp()))
    if ttl > 0:
        async with _redis() as r:
            await r.setex(_refresh_blacklist_key(body.refresh_token), ttl, "1")


@router.post("/totp/setup", response_model=TotpSetupResponse)
async def totp_setup(
    current_user: Annotated[CmsUser, Depends(get_user_for_totp_enrolment)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> TotpSetupResponse:
    """
    Génère un secret TOTP + QR code + 10 codes de secours.
    N'active PAS encore le 2FA — l'utilisateur doit appeler /totp/verify ensuite.
    """
    org_result = await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
    )
    org = org_result.scalar_one()

    totp_data = generate_totp_secret(current_user.email, org.org_name)
    plain_codes, hashed_codes = generate_backup_codes()

    # Stocker le secret CHIFFRÉ AU REPOS (non activé) et les codes hashés
    await db.execute(
        update(CmsUser)
        .where(CmsUser.id_user == current_user.id_user)
        .values(
            totp_secret=protect_totp_secret(totp_data["secret"]),
            totp_backup_codes=hashed_codes,
            totp_enabled=False,
        )
    )
    await db.commit()

    return TotpSetupResponse(
        secret=totp_data["secret"],
        qr_b64=totp_data["qr_b64"],
        backup_codes=plain_codes,
    )


@router.post("/totp/verify", status_code=status.HTTP_204_NO_CONTENT)
async def totp_verify(
    body: TotpVerifyRequest,
    current_user: Annotated[CmsUser, Depends(get_user_for_totp_enrolment)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """
    Vérifie le code TOTP et active le 2FA si valide.
    Le secret doit correspondre à celui généré par /totp/setup.
    """
    # Le secret stocké est chiffré au repos → on le déchiffre pour comparer.
    if reveal_totp_secret(current_user.totp_secret) != body.secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inconsistent TOTP secret — run /totp/setup again",
        )
    if not verify_totp(body.secret, body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid TOTP code"
        )

    await db.execute(
        update(CmsUser)
        .where(CmsUser.id_user == current_user.id_user)
        .values(totp_enabled=True)
    )
    await db.commit()
