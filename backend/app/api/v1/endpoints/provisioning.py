"""
Provisioning tokens + enregistrement player (§XXIX.2).

POST   /orgs/{id_org}/provisioning-tokens        — créer token (org_admin)
GET    /orgs/{id_org}/provisioning-tokens         — lister (org_admin)
DELETE /orgs/{id_org}/provisioning-tokens/{id}   — révoquer (org_admin)
POST   /players/register                         — enregistrement player (public, org_token)

Les provisioning tokens sont des CmsApiKey avec scopes=['player:register'].
Le token brut est généré côté serveur (sfp_<32 octets urlsafe>), stocké haché (SHA-256).
La valeur brute est retournée une seule fois à la création.

Note Sprint 1.6 : SUPER_ADMIN peut uniquement gérer les tokens de son propre org.
Support cross-org SUPER_ADMIN prévu en Phase 2.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Annotated

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    PROVISIONING_OPERATOR_SCOPE,
    ProvisioningActor,
    get_db,
    get_db_for_actor,
    get_db_worker,
    require_org_admin,
    require_provisioning_actor,
)
from app.core.security import create_player_token
from app.models.audit_log import CmsAuditLog
from app.models.player import CmsPlayer
from app.models.user import CmsApiKey, CmsUser
from app.schemas.provisioning import (
    PlayerRegisterRequest,
    PlayerRegisterResponse,
    ProvisioningBatch,
    ProvisioningBatchRequest,
    ProvisioningKeyCreate,
    ProvisioningKeyRead,
    ProvisioningPayload,
    ProvisioningPayloadRequest,
    ProvisioningPlayerStatus,
    ProvisioningTokenCreate,
    ProvisioningTokenRead,
    ProvisioningWhoami,
)

router = APIRouter(tags=["provisioning"])


_PROV_SCOPE = "player:register"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post(
    "/orgs/{id_org}/provisioning-tokens",
    response_model=ProvisioningTokenRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_provisioning_token(
    id_org: int,
    body: ProvisioningTokenCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvisioningTokenRead:
    if current_user.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization not allowed")

    raw_token = f"sfp_{secrets.token_urlsafe(32)}"
    key = CmsApiKey(
        id_org=id_org,
        id_user=current_user.id_user,
        key_hash=_token_hash(raw_token),
        key_prefix=raw_token[:12],
        label=body.label,
        scopes=[_PROV_SCOPE],
        expires_at=body.expires_at,
        max_uses=body.max_uses,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    result = ProvisioningTokenRead.model_validate(key)
    result.token_value = raw_token
    return result


@router.post(
    "/orgs/{id_org}/provisioning-payload",
    response_model=ProvisioningPayload,
    status_code=status.HTTP_201_CREATED,
)
async def create_provisioning_payload(
    id_org: int,
    body: ProvisioningPayloadRequest,
    request: Request,
    actor: Annotated[ProvisioningActor, Depends(require_provisioning_actor)],
    db: Annotated[AsyncSession, Depends(get_db_for_actor)],
) -> ProvisioningPayload:
    """Installeur terrain (§XXIX) : génère le **payload de provisioning** d'un player (token `sfp_`
    à usage unique + config réseau/options), consommable par n'importe quel canal (CLI ADB/SSH,
    image USB/SD, ZIP BrightSign). `id_player` → ré-attache à un player existant (anti-doublon).
    Auth = JWT org-admin **ou** clé opérateur `sfk_` (§XXIX, longue durée)."""
    if actor.id_org != id_org:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not allowed")

    target_id = body.id_player
    if target_id is not None:
        # Ré-attache : le player cible doit exister dans l'org.
        target = (await db.execute(
            select(CmsPlayer).where(
                CmsPlayer.id_player == target_id, CmsPlayer.id_org == id_org)
        )).scalar_one_or_none()
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Target player not found")
    else:
        # Création : on PRÉ-CRÉE le player (offline) et on lie le token dessus → le device s'y
        # ré-attache toujours (verify online déterministe + zéro doublon, quelle que soit la
        # séquence de reprovisioning).
        player = CmsPlayer(id_org=id_org, player_name=body.ref, player_os=body.os)
        db.add(player)
        await db.flush()
        target_id = player.id_player

    raw_token = f"sfp_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)
    key = CmsApiKey(
        id_org=id_org,
        id_user=actor.id_user,
        key_hash=_token_hash(raw_token),
        key_prefix=raw_token[:12],
        label=f"provision:{body.ref}"[:255],
        scopes=[_PROV_SCOPE],
        expires_at=now + timedelta(days=body.expires_in_days),
        max_uses=1,
        provision_target_player=target_id,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    backend_url = (body.backend_url or str(request.base_url)).rstrip("/")
    return ProvisioningPayload(
        ref=body.ref, os=body.os, backend_url=backend_url, token=raw_token,
        network=body.network, options=body.options,
        id_key=key.id_key, id_player=target_id,
    )


@router.post(
    "/orgs/{id_org}/provisioning-batch",
    response_model=ProvisioningBatch,
    status_code=status.HTTP_201_CREATED,
)
async def create_provisioning_batch(
    id_org: int,
    body: ProvisioningBatchRequest,
    request: Request,
    actor: Annotated[ProvisioningActor, Depends(require_provisioning_actor)],
    db: Annotated[AsyncSession, Depends(get_db_for_actor)],
) -> ProvisioningBatch:
    """Provisioning en LOT (§XXIX.10) : prépare **N players du même OS** via une seule clé USB.
    - `per_machine` : N players **pré-créés nommés** + N tokens `sfp_` `max_uses=1` (chaque machine
      prend une entrée, la marque consommée). Anti-doublon via `provision_target_player`.
    - `shared` : **1 token** `max_uses=count` (players créés à la volée, nommés côté terrain).
    Le frontend aplatit le résultat en `signflow-config.json` v2. Auth = JWT org-admin **ou** clé
    opérateur `sfk_`."""
    if actor.id_org != id_org:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not allowed")

    backend_url = (body.backend_url or str(request.base_url)).rstrip("/")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=body.expires_in_days)

    if body.mode == "per_machine":
        if not body.entries:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "At least one machine (entries) is required in per_machine mode.")
        if len(body.entries) > 500:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Batch too large (max 500 machines).")
        players: list[ProvisioningPayload] = []
        for entry in body.entries:
            # PRÉ-CRÉE le player (offline) + lie le token dessus → ré-attache déterministe, zéro doublon.
            player = CmsPlayer(id_org=id_org, player_name=entry.ref, player_os=body.os)
            db.add(player)
            await db.flush()
            raw_token = f"sfp_{secrets.token_urlsafe(32)}"
            key = CmsApiKey(
                id_org=id_org, id_user=actor.id_user,
                key_hash=_token_hash(raw_token), key_prefix=raw_token[:12],
                label=f"provision:{entry.ref}"[:255], scopes=[_PROV_SCOPE],
                expires_at=expires_at, max_uses=1,
                provision_target_player=player.id_player,
            )
            db.add(key)
            await db.flush()
            players.append(ProvisioningPayload(
                ref=entry.ref, os=body.os, backend_url=backend_url, token=raw_token,
                network=entry.network, options=entry.options,
                id_key=key.id_key, id_player=player.id_player,
            ))
        await db.commit()
        return ProvisioningBatch(
            mode="per_machine", os=body.os, backend_url=backend_url, players=players)

    # mode shared : un seul token à usage multiple, players créés à l'enregistrement.
    if not body.ref or not body.count:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "ref and count are required in shared mode.")
    raw_token = f"sfp_{secrets.token_urlsafe(32)}"
    key = CmsApiKey(
        id_org=id_org, id_user=actor.id_user,
        key_hash=_token_hash(raw_token), key_prefix=raw_token[:12],
        label=f"provision(lot×{body.count}):{body.ref}"[:255], scopes=[_PROV_SCOPE],
        expires_at=expires_at, max_uses=body.count,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return ProvisioningBatch(
        mode="shared", os=body.os, backend_url=backend_url,
        token=raw_token, count=body.count, ref=body.ref, id_key=key.id_key,
        network=body.network, options=body.options)


# ── Endpoints scopés utilisés par l'outil terrain (JWT org-admin OU clé opérateur sfk_) ──

@router.get("/provisioning/whoami", response_model=ProvisioningWhoami)
async def provisioning_whoami(
    actor: Annotated[ProvisioningActor, Depends(require_provisioning_actor)],
) -> ProvisioningWhoami:
    """Renvoie l'org de l'acteur (identifie l'org sans exposer /users/me à la clé opérateur)."""
    return ProvisioningWhoami(id_org=actor.id_org, kind=actor.kind)


@router.get(
    "/provisioning/players/{id_player}/status",
    response_model=ProvisioningPlayerStatus,
)
async def provisioning_player_status(
    id_player: int,
    actor: Annotated[ProvisioningActor, Depends(require_provisioning_actor)],
    db: Annotated[AsyncSession, Depends(get_db_for_actor)],
) -> ProvisioningPlayerStatus:
    """État d'un player, scopé à l'org de l'acteur (l'outil poll le passage online)."""
    player = (
        await db.execute(
            select(CmsPlayer).where(
                CmsPlayer.id_player == id_player, CmsPlayer.id_org == actor.id_org)
        )
    ).scalar_one_or_none()
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Player not found")
    return ProvisioningPlayerStatus(
        id_player=player.id_player, player_name=player.player_name, status=player.status)


# ── Clés opérateur (org-admin JWT uniquement — la génération est sensible) ──

@router.post(
    "/orgs/{id_org}/provisioning-keys",
    response_model=ProvisioningKeyRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_provisioning_key(
    id_org: int,
    body: ProvisioningKeyCreate,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvisioningKeyRead:
    """Génère une **clé opérateur** `sfk_` (longue durée, révocable) pour l'installeur terrain :
    l'outil crée des payloads + lit l'état des players sans JWT org-admin à re-copier (§XXIX)."""
    if current_user.id_org != id_org:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not allowed")
    raw_token = f"sfk_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)
    key = CmsApiKey(
        id_org=id_org,
        id_user=current_user.id_user,
        key_hash=_token_hash(raw_token),
        key_prefix=raw_token[:12],
        label=body.label,
        scopes=[PROVISIONING_OPERATOR_SCOPE],
        expires_at=now + timedelta(days=body.expires_in_days),
        max_uses=None,
    )
    db.add(key)
    db.add(CmsAuditLog(
        id_org=id_org, id_user=current_user.id_user, action="create",
        resource_type="provisioning_key", resource_id=key.key_prefix,
        detail={"label": body.label}))
    await db.commit()
    await db.refresh(key)
    result = ProvisioningKeyRead.model_validate(key)
    result.token_value = raw_token
    return result


@router.get("/orgs/{id_org}/provisioning-keys", response_model=list[ProvisioningKeyRead])
async def list_provisioning_keys(
    id_org: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ProvisioningKeyRead]:
    if current_user.id_org != id_org:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not allowed")
    rows = (
        await db.execute(
            select(CmsApiKey).where(CmsApiKey.id_org == id_org).order_by(CmsApiKey.created_at.desc())
        )
    ).scalars().all()
    return [
        ProvisioningKeyRead.model_validate(r)
        for r in rows if PROVISIONING_OPERATOR_SCOPE in (r.scopes or [])
    ]


@router.post(
    "/orgs/{id_org}/provisioning-keys/{id_key}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_provisioning_key(
    id_org: int,
    id_key: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if current_user.id_org != id_org:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not allowed")
    key = await _get_operator_key_or_404(id_org, id_key, db)
    key.is_active = False
    db.add(CmsAuditLog(
        id_org=id_org, id_user=current_user.id_user, action="revoke",
        resource_type="provisioning_key", resource_id=key.key_prefix,
        detail={"label": key.label}))
    await db.commit()


@router.delete(
    "/orgs/{id_org}/provisioning-keys/{id_key}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_provisioning_key(
    id_org: int,
    id_key: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if current_user.id_org != id_org:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not allowed")
    key = await _get_operator_key_or_404(id_org, id_key, db)
    db.add(CmsAuditLog(
        id_org=id_org, id_user=current_user.id_user, action="delete",
        resource_type="provisioning_key", resource_id=key.key_prefix,
        detail={"label": key.label}))
    await db.delete(key)
    await db.commit()


async def _get_operator_key_or_404(id_org: int, id_key: int, db: AsyncSession) -> CmsApiKey:
    key = (
        await db.execute(
            select(CmsApiKey).where(CmsApiKey.id_key == id_key, CmsApiKey.id_org == id_org)
        )
    ).scalar_one_or_none()
    if key is None or PROVISIONING_OPERATOR_SCOPE not in (key.scopes or []):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    return key


@router.get("/orgs/{id_org}/provisioning-tokens", response_model=list[ProvisioningTokenRead])
async def list_provisioning_tokens(
    id_org: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ProvisioningTokenRead]:
    if current_user.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization not allowed")

    rows = (
        await db.execute(
            select(CmsApiKey)
            .where(CmsApiKey.id_org == id_org)
            .order_by(CmsApiKey.created_at.desc())
        )
    ).scalars().all()

    return [
        ProvisioningTokenRead.model_validate(r)
        for r in rows
        if _PROV_SCOPE in (r.scopes or [])
    ]


async def _get_token_or_404(id_org: int, id_key: int, db: AsyncSession) -> CmsApiKey:
    key = (
        await db.execute(
            select(CmsApiKey).where(
                CmsApiKey.id_key == id_key,
                CmsApiKey.id_org == id_org,
            )
        )
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return key


@router.post(
    "/orgs/{id_org}/provisioning-tokens/{id_key}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_provisioning_token(
    id_org: int,
    id_key: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if current_user.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization not allowed")
    key = await _get_token_or_404(id_org, id_key, db)
    db.add(CmsAuditLog(
        id_org=id_org,
        id_user=current_user.id_user,
        action="revoke",
        resource_type="provisioning_token",
        resource_id=str(id_key),
        detail={"label": key.label, "key_prefix": key.key_prefix},
    ))
    key.is_active = False
    await db.commit()


@router.delete(
    "/orgs/{id_org}/provisioning-tokens/{id_key}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_provisioning_token(
    id_org: int,
    id_key: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if current_user.id_org != id_org:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization not allowed")
    key = await _get_token_or_404(id_org, id_key, db)
    db.add(CmsAuditLog(
        id_org=id_org,
        id_user=current_user.id_user,
        action="delete",
        resource_type="provisioning_token",
        resource_id=str(id_key),
        detail={"label": key.label, "key_prefix": key.key_prefix},
    ))
    await db.delete(key)
    await db.commit()


@router.post(
    "/players/register",
    response_model=PlayerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_player(
    body: PlayerRegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> PlayerRegisterResponse:
    """Endpoint public — le player s'enregistre avec un org_token (provisioning token)."""
    key = (
        await db.execute(
            select(CmsApiKey).where(
                CmsApiKey.key_hash == _token_hash(body.org_token),
                CmsApiKey.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()

    if key is None or _PROV_SCOPE not in (key.scopes or []):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid registration token",
        )

    now = datetime.now(timezone.utc)
    if key.expires_at and key.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Registration token expired",
        )

    # Ré-attache EXPLICITE (installeur terrain §XXIX) : le provisioning token vise un player
    # existant (`provision_target_player`) → le device reprend cette identité (réutilise/rote son
    # token) sans créer de doublon. Décision Ed : « ré-attacher OU créer, au choix » côté payload.
    if key.provision_target_player is not None:
        target = (await db.execute(
            select(CmsPlayer).where(
                CmsPlayer.id_player == key.provision_target_player,
                CmsPlayer.id_org == key.id_org)
        )).scalar_one_or_none()
        if target is not None:
            if (target.player_token and target.token_expires_at
                    and target.token_expires_at > now):
                token, expires_at = target.player_token, target.token_expires_at
            else:
                token, expires_at = create_player_token(target.id_player)
                target.player_token = token
                target.token_expires_at = expires_at
            if body.player_os:
                target.player_os = body.player_os
            if body.player_version:
                target.player_version = body.player_version
            key.last_used_at = now
            key.use_count = (key.use_count or 0) + 1
            await db.commit()
            return PlayerRegisterResponse(
                id_player=target.id_player, player_token=token, expires_at=expires_at)

    # Ré-attache idempotente par n° de série (P0 self-heal) : un device qui a perdu son JWT
    # (ex. BrightSign dont le localStorage est wipé au saut de firmware) re-`register` avec son
    # provisioning token. S'il porte un n° de série DÉJÀ connu dans l'org, on le ré-attache au
    # player existant (son token, sans doublon ni usage consommé) au lieu de 409/duplication. La
    # sécurité reste portée par le provisioning token ; le série ne fait que router vers le player.
    serial = (body.serial or "").strip()
    if serial:
        existing = (
            await db.execute(
                select(CmsPlayer)
                .where(
                    CmsPlayer.id_org == key.id_org,
                    CmsPlayer.capabilities["bs_serial"].astext == serial,
                )
                .order_by(CmsPlayer.id_player)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.player_token
                and existing.token_expires_at
                and existing.token_expires_at > now
            ):
                token, expires_at = existing.player_token, existing.token_expires_at
            else:
                token, expires_at = create_player_token(existing.id_player)
                existing.player_token = token
                existing.token_expires_at = expires_at
            key.last_used_at = now
            await db.commit()
            return PlayerRegisterResponse(
                id_player=existing.id_player, player_token=token, expires_at=expires_at,
            )

    if key.max_uses is not None and (key.use_count or 0) >= key.max_uses:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registration token used up (max_uses reached)",
        )

    player = CmsPlayer(
        id_org=key.id_org,
        player_name=body.player_name,
        player_os=body.player_os,
        player_version=body.player_version,
        security_config=body.os_info,
        # Sème le n° de série dès la création → la ré-attache ci-dessus marche même si le player
        # re-`register` avant son 1er heartbeat de capabilities (qui enrichira ensuite ce dict).
        capabilities={"bs_serial": serial} if serial else None,
    )
    db.add(player)
    await db.flush()

    token, expires_at = create_player_token(player.id_player)
    player.player_token = token
    player.token_expires_at = expires_at

    key.use_count = (key.use_count or 0) + 1
    key.last_used_at = now

    await db.commit()

    return PlayerRegisterResponse(
        id_player=player.id_player,
        player_token=token,
        expires_at=expires_at,
    )
