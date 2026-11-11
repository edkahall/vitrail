"""Retours utilisateurs (feedback) — endpoints (§ retours UI).

- POST   /feedback                 : tout utilisateur authentifié signale un retour (son org) ;
                                      triage IA enqueué (best-effort).
- GET    /admin/feedback           : liste cross-org (super-admin, BYPASSRLS) + filtres.
- PATCH  /admin/feedback/{id}      : statut + note de tri (super-admin).
- DELETE /admin/feedback/{id}      : suppression (super-admin).
"""

import logging
import uuid
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_db_worker, require_super_admin
from app.models.feedback import CmsFeedback
from app.models.organization import CmsOrganization
from app.models.user import CmsUser
from app.schemas.feedback import FeedbackCreate, FeedbackRead, FeedbackUpdate
from app.services.media import delete_file, get_signed_url, upload_file

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])

# Captures jointes : images uniquement, taille bornée (capture d'écran de page → quelques Mo).
_SCREENSHOT_MAX_BYTES = 5 * 1024 * 1024
_SCREENSHOT_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
}


def _feedback_prefix(id_org: int) -> str:
    return f"feedback/{id_org}/"


@router.post("/feedback/screenshot")
async def upload_feedback_screenshot(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
) -> dict[str, str]:
    """Upload manuel d'une capture (PNG/JPEG/WebP/GIF) → MinIO ; renvoie la clé à joindre
    au retour. Clé namespacée par org (`feedback/{id_org}/…`) → l'org ne peut référencer
    que ses propres captures (vérifié à la création du retour)."""
    ext = _SCREENSHOT_EXT.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Image attendue (PNG/JPEG/WebP/GIF)"
        )
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")
    if len(content) > _SCREENSHOT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Screenshot too large (max 5 MB)",
        )
    key = f"{_feedback_prefix(current_user.id_org)}{uuid.uuid4().hex}.{ext}"
    await upload_file(content, key, file.content_type or "application/octet-stream")
    return {"key": key}


@router.post("/feedback", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    body: FeedbackCreate,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FeedbackRead:
    """Enregistre un retour pour l'org de l'utilisateur + enqueue le triage IA (best-effort)."""
    # On ne garde que les captures appartenant à l'org (anti-référence d'une clé arbitraire).
    prefix = _feedback_prefix(current_user.id_org)
    screenshot_keys = [k for k in (body.screenshot_keys or []) if k.startswith(prefix)]
    fb = CmsFeedback(
        id_org=current_user.id_org,
        id_user=current_user.id_user,
        feedback_type=body.feedback_type,
        description=body.description.strip(),
        page_url=body.page_url,
        route=body.route,
        user_agent=body.user_agent,
        app_version=body.app_version,
        console_errors=body.console_errors or [],
        screenshot_keys=screenshot_keys,
        severity=body.severity if body.feedback_type == "bug" else None,
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    try:
        from app.workers.feedback_worker import triage_feedback
        triage_feedback.delay(fb.id_feedback)
    except Exception:  # noqa: BLE001 — best-effort, le triage n'est pas bloquant
        logger.info("feedback %s : triage non enqueué", fb.id_feedback)
    return FeedbackRead.model_validate(fb)


@router.get("/admin/feedback", response_model=list[FeedbackRead])
async def list_feedback(
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
    status_filter: str | None = None,
    feedback_type: str | None = None,
    limit: int = 200,
) -> list[FeedbackRead]:
    """Liste cross-org des retours (super-admin) + nom d'org + email auteur."""
    q = (
        select(CmsFeedback, CmsOrganization.org_name, CmsUser.email)
        .join(CmsOrganization, CmsOrganization.id_org == CmsFeedback.id_org)
        .join(CmsUser, CmsUser.id_user == CmsFeedback.id_user)
        .order_by(CmsFeedback.created_at.desc())
        .limit(max(1, min(limit, 1000)))
    )
    if status_filter:
        q = q.where(CmsFeedback.status == status_filter)
    if feedback_type:
        q = q.where(CmsFeedback.feedback_type == feedback_type)

    out: list[FeedbackRead] = []
    for fb, org_name, email in (await db.execute(q)).all():
        item = FeedbackRead.model_validate(fb)
        item.org_name = org_name
        item.author_email = email
        item.screenshot_urls = [get_signed_url(k) for k in (fb.screenshot_keys or [])]
        out.append(item)
    return out


@router.patch("/admin/feedback/{id_feedback}", response_model=FeedbackRead)
async def update_feedback(
    id_feedback: int,
    body: FeedbackUpdate,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> FeedbackRead:
    """Met à jour le statut et/ou la note de tri d'un retour (super-admin)."""
    fb = (await db.execute(
        select(CmsFeedback).where(CmsFeedback.id_feedback == id_feedback)
    )).scalar_one_or_none()
    if fb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    values = body.model_dump(exclude_none=True)
    if values:
        from datetime import datetime
        values["updated_at"] = datetime.now(UTC)
        await db.execute(
            update(CmsFeedback).where(CmsFeedback.id_feedback == id_feedback).values(**values)
        )
        await db.commit()
        await db.refresh(fb)
    return FeedbackRead.model_validate(fb)


@router.delete("/admin/feedback/{id_feedback}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    id_feedback: int,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """Supprime un retour (super-admin) + ses captures MinIO (best-effort)."""
    fb = (await db.execute(
        select(CmsFeedback).where(CmsFeedback.id_feedback == id_feedback)
    )).scalar_one_or_none()
    keys = list(fb.screenshot_keys or []) if fb else []
    await db.execute(delete(CmsFeedback).where(CmsFeedback.id_feedback == id_feedback))
    await db.commit()
    for k in keys:
        try:
            await delete_file(k)
        except Exception:  # noqa: BLE001 — nettoyage best-effort, ne bloque pas la suppression
            logger.info("feedback screenshot %s non supprimée de MinIO", k)
