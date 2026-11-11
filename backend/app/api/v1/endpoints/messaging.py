"""« Jeanine » — messagerie conversationnelle (POC Telegram). Cf. services/messaging_gateway.

- `POST /messaging/telegram/webhook` : updates Telegram (public, authentifié par le
  `secret_token` de setWebhook — en-tête X-Telegram-Bot-Api-Secret-Token). Répond 200
  immédiatement et traite en tâche de fond (Telegram retente au-delà de ~60 s).
- `POST /messaging/link-code` : code de liaison opt-in (l'utilisateur courant l'envoie
  au bot : `/link CODE`).
- `GET /messaging/status` : état du canal pour l'utilisateur courant (configuré, username
  du bot, ses propres identités liées, PIN défini) — alimente la carte « Messagerie ».
- `POST/DELETE /messaging/pin` : PIN 6 chiffres des actions sensibles (migr 0090) —
  défini depuis la session CMS (auth forte), exigé par Jeanine sur les actions sensibles.
- `DELETE /messaging/identities/me/{id}` : déliaison par l'utilisateur lui-même.
- `GET/DELETE /messaging/identities` : gestion de l'allowlist par l'org-admin.
"""

import logging
import re
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, get_db, require_org_admin
from app.core.security import hash_password
from app.models.messaging import CmsMessagingIdentity
from app.models.user import CmsUser
from app.services.messaging_gateway import (
    LINK_CODE_TTL_S,
    create_link_code,
    get_bot_username,
    process_telegram_update,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messaging", tags=["messaging"])


@router.post("/telegram/webhook")
async def telegram_webhook(
    update: dict,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Réception des updates du bot. Le secret est REQUIS (pas de bot sans lui)."""
    if not settings.CMS_TELEGRAM_BOT_TOKEN or not settings.CMS_TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Telegram channel not configured.")
    if x_telegram_bot_api_secret_token != settings.CMS_TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret.")
    background.add_task(process_telegram_update, update)
    return {"ok": True}


@router.post("/link-code")
async def new_link_code(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
) -> dict:
    """Code de liaison opt-in (TTL 10 min, usage unique) → à envoyer au bot : /link CODE."""
    if not settings.CMS_TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Telegram channel not configured.")
    code = await create_link_code(current_user.id_user, current_user.id_org)
    return {"code": code, "expires_in_s": LINK_CODE_TTL_S, "usage": f"/link {code}"}


def _identity_dict(ident: CmsMessagingIdentity) -> dict:
    return {
        "id_identity": ident.id_identity,
        "id_user": ident.id_user,
        "channel": ident.channel,
        "display_name": ident.display_name,
        "is_active": ident.is_active,
        "last_seen_at": ident.last_seen_at,
        "created_at": ident.created_at,
    }


@router.get("/status")
async def messaging_status(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """État du canal pour l'utilisateur courant — carte « Messagerie » du profil."""
    configured = bool(settings.CMS_TELEGRAM_BOT_TOKEN)
    rows = (await db.execute(
        select(CmsMessagingIdentity)
        .where(CmsMessagingIdentity.id_user == current_user.id_user)
        .order_by(CmsMessagingIdentity.created_at.desc())
    )).scalars().all()
    return {
        "configured": configured,
        "bot_username": await get_bot_username() if configured else None,
        "identities": [_identity_dict(i) for i in rows],
        "pin_set": current_user.assistant_pin_hash is not None,
    }


class PinBody(BaseModel):
    pin: str


@router.post("/pin")
async def set_pin(
    body: PinBody,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Définit/remplace le PIN des actions sensibles (6 chiffres, bcrypt).

    Posé depuis la session CMS (auth forte) — c'est le 2e facteur du « OUI »
    quand une action sensible est demandée par messagerie."""
    if not re.fullmatch(r"\d{6}", body.pin):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="The PIN must be exactly 6 digits.")
    user = (await db.execute(
        select(CmsUser).where(CmsUser.id_user == current_user.id_user)
    )).scalar_one()
    user.assistant_pin_hash = hash_password(body.pin)
    await db.commit()
    return {"ok": True}


@router.delete("/pin")
async def delete_pin(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Supprime le PIN — les actions sensibles par messagerie seront refusées."""
    user = (await db.execute(
        select(CmsUser).where(CmsUser.id_user == current_user.id_user)
    )).scalar_one()
    user.assistant_pin_hash = None
    await db.commit()
    return {"ok": True}


@router.delete("/identities/me/{id_identity}")
async def delete_own_identity(
    id_identity: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Déliaison par l'utilisateur lui-même (son identité uniquement — 404 sinon)."""
    ident = (await db.execute(
        select(CmsMessagingIdentity).where(
            CmsMessagingIdentity.id_identity == id_identity,
            CmsMessagingIdentity.id_user == current_user.id_user,
        )
    )).scalar_one_or_none()
    if ident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Identity not found.")
    await db.delete(ident)
    await db.commit()
    return {"ok": True}


@router.get("/identities")
async def list_identities(
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """Allowlist de l'org : qui est lié à quel compte de messagerie (org-admin)."""
    rows = (await db.execute(
        select(CmsMessagingIdentity, CmsUser.email)
        .join(CmsUser, CmsUser.id_user == CmsMessagingIdentity.id_user)
        .order_by(CmsMessagingIdentity.created_at.desc())
    )).all()
    return [
        {
            "id_identity": ident.id_identity,
            "id_user": ident.id_user,
            "email": email,
            "channel": ident.channel,
            "display_name": ident.display_name,
            "is_active": ident.is_active,
            "last_seen_at": ident.last_seen_at,
            "created_at": ident.created_at,
        }
        for ident, email in rows
    ]


@router.delete("/identities/{id_identity}")
async def delete_identity(
    id_identity: int,
    _: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Déliaison (org-admin) — le compte de messagerie ne peut plus rien faire."""
    ident = (await db.execute(
        select(CmsMessagingIdentity)
        .where(CmsMessagingIdentity.id_identity == id_identity)
    )).scalar_one_or_none()
    if ident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Identity not found.")
    await db.delete(ident)
    await db.commit()
    return {"ok": True}
