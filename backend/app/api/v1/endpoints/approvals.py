"""Approval Workflow §XXI — endpoints génériques (S7 Phase C1).

GET    /approvals                     — file (reviewer = toute l'org ; sinon SES demandes)
GET    /approvals/dashboard           — compteurs par statut + urgents (due < 48 h)
GET    /approvals/{id}                — détail + timeline des commentaires
POST   /approvals                     — soumettre (approvals:submit)
POST   /approvals/{id}/approve        — approuver (approvals:approve)
POST   /approvals/{id}/reject         — rejeter, motif OBLIGATOIRE (approvals:reject)
POST   /approvals/{id}/request-changes — demander des modifs, commentaire OBLIGATOIRE
POST   /approvals/{id}/resubmit       — resoumettre (soumetteur ou org-admin)
POST   /approvals/{id}/comments       — commentaire libre (participants)

La PUBLICATION n'est pas ici : le module ressource publie (ex. POST /campaigns/{id}/status
publish) et appelle services.approvals.mark_published — un seul chemin d'enforcement.
Notifications §XXII (webhooks org) : approval_requested/approved/rejected/changes_requested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_db_worker, require_permission
from app.models.approval import CmsApprovalComment, CmsApprovalRequest
from app.models.user import CmsUser
from app.schemas.approval import (
    ApprovalActionBody,
    ApprovalCommentRead,
    ApprovalDashboard,
    ApprovalDetail,
    ApprovalRead,
    ApprovalSubmit,
)
from app.services import (
    approval_emails,
    approval_scope,
    approvals_export,
    notifications,
    rbac,
)
from app.services import approvals as approvals_svc

router = APIRouter(prefix="/approvals", tags=["approvals"])

_REVIEW_PERMS = {"approvals:review", "approvals:approve", "approvals:reject"}


async def _actor_caps(db_worker: AsyncSession, user: CmsUser) -> dict:
    """Capacités de l'utilisateur sur la file : reviewer (voit tout) / submitter (voit
    les siennes). Org-admin/super-admin = reviewer d'office (rôle '*')."""
    role_keys = await rbac.get_user_role_keys(db_worker, user.id_user)
    if {"CMS_SUPER_ADMIN", "CMS_ORG_ADMIN"}.intersection(role_keys):
        return {"reviewer": True, "submitter": True, "admin": True}
    perms = await rbac.get_user_permissions(db_worker, user.id_user)
    return {
        "reviewer": bool(_REVIEW_PERMS.intersection(perms)),
        "submitter": "approvals:submit" in perms,
        "admin": False,
    }


async def _get_request_or_404(
    db: AsyncSession, user: CmsUser, id_request: int, caps: dict
) -> CmsApprovalRequest:
    """Demande de l'org (RLS), 404 sinon. Un non-reviewer ne voit que SES demandes
    (cross = 404, jamais 403 — même règle que le multi-tenant)."""
    req = (await db.execute(
        select(CmsApprovalRequest).where(
            CmsApprovalRequest.id_request == id_request,
            CmsApprovalRequest.id_org == user.id_org,
        )
    )).scalar_one_or_none()
    if req is None or (not caps["reviewer"] and req.submitted_by != user.id_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return req


async def _enrich(db: AsyncSession, rows: list[CmsApprovalRequest]) -> list[ApprovalRead]:
    """Peuple resource_name + noms des users (1 requête users, 1 resolve par ressource)."""
    if not rows:
        return []
    uids = {r.submitted_by for r in rows} | {r.reviewed_by for r in rows if r.reviewed_by}
    names = dict((await db.execute(
        select(CmsUser.id_user, func.coalesce(CmsUser.full_name, CmsUser.email))
        .where(CmsUser.id_user.in_(uids))
    )).all()) if uids else {}
    out: list[ApprovalRead] = []
    for r in rows:
        item = ApprovalRead.model_validate(r)
        info = await approvals_svc.resolve_resource(db, r.id_org, r.resource_type, r.resource_id)
        item.resource_name = info["name"] if info else None
        item.submitted_by_name = names.get(r.submitted_by)
        item.reviewed_by_name = names.get(r.reviewed_by) if r.reviewed_by else None
        out.append(item)
    return out


async def _dispatch(db: AsyncSession, id_org: int, event_type: str, ctx: dict) -> None:
    """Best-effort : une alerte ne casse jamais l'action qui la déclenche."""
    try:
        await notifications.dispatch_async(db, id_org, event_type, ctx)
    except Exception:  # noqa: BLE001
        pass


# ── Lecture (routes fixes AVANT /{id_request}) ───────────────────────────────

@router.get("/dashboard", response_model=ApprovalDashboard)
async def approvals_dashboard(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalDashboard:
    caps = await _actor_caps(db_worker, current_user)
    if not (caps["reviewer"] or caps["submitter"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Permission denied: approvals")
    base = (select(CmsApprovalRequest.status, func.count())
            .where(CmsApprovalRequest.id_org == current_user.id_org)
            .group_by(CmsApprovalRequest.status))
    if not caps["reviewer"]:
        base = base.where(CmsApprovalRequest.submitted_by == current_user.id_user)
    counts = dict((await db.execute(base)).all())
    urgent_q = select(func.count()).select_from(CmsApprovalRequest).where(
        CmsApprovalRequest.id_org == current_user.id_org,
        CmsApprovalRequest.status.in_(("pending_review", "changes_requested")),
        CmsApprovalRequest.due_date.is_not(None),
        CmsApprovalRequest.due_date < datetime.now(UTC) + timedelta(hours=48),
    )
    if not caps["reviewer"]:
        urgent_q = urgent_q.where(CmsApprovalRequest.submitted_by == current_user.id_user)
    urgent = (await db.execute(urgent_q)).scalar_one()
    return ApprovalDashboard(
        pending_review=counts.get("pending_review", 0),
        changes_requested=counts.get("changes_requested", 0),
        approved=counts.get("approved", 0),
        rejected=counts.get("rejected", 0),
        published=counts.get("published", 0),
        urgent=urgent,
        is_reviewer=caps["reviewer"],
    )


@router.get("/", response_model=list[ApprovalRead])
async def list_approvals(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    resource_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ApprovalRead]:
    caps = await _actor_caps(db_worker, current_user)
    if not (caps["reviewer"] or caps["submitter"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Permission denied: approvals")
    q = (select(CmsApprovalRequest)
         .where(CmsApprovalRequest.id_org == current_user.id_org)
         .order_by(CmsApprovalRequest.submitted_at.desc()).limit(limit))
    if not caps["reviewer"]:
        q = q.where(CmsApprovalRequest.submitted_by == current_user.id_user)
    if status_filter:
        q = q.where(CmsApprovalRequest.status == status_filter)
    if resource_type:
        q = q.where(CmsApprovalRequest.resource_type == resource_type)
    rows = list((await db.execute(q)).scalars().all())
    rows = await _scoped_rows(db, current_user, rows, caps)
    return await _enrich(db, rows)


async def _scoped_rows(
    db: AsyncSession, user: CmsUser, rows: list[CmsApprovalRequest], caps: dict
) -> list[CmsApprovalRequest]:
    """N5 : la file ne montre au valideur que ce qu'il peut réellement TRANCHER.

    Il garde toujours SES propres soumissions (c'est le correctif N4 : le soumissionnaire
    doit voir revenir un « modifications demandées »). Afficher une demande qu'on ne peut
    pas décider est ce qui a rendu N5 invisible : tout le monde voyait tout, donc quelqu'un
    décidait — y compris sur son propre contenu.
    """
    if not caps["reviewer"] or not rows:
        return rows
    decidable = await approval_scope.decidable_request_ids(db, user, rows)
    return [r for r in rows
            if r.id_request in decidable or r.submitted_by == user.id_user]


# ── Exports CSV/PDF (retour Ed 12/07) — routes fixes AVANT /{id_request} ─────

async def _export_rows(
    db: AsyncSession, db_worker: AsyncSession, user: CmsUser,
    status_filter: str | None,
) -> list[dict]:
    """Lignes d'export (même scope que la liste : reviewer = org, sinon SES demandes)
    + dernier motif de décision par demande."""
    caps = await _actor_caps(db_worker, user)
    if not (caps["reviewer"] or caps["submitter"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Permission denied: approvals")
    q = (select(CmsApprovalRequest)
         .where(CmsApprovalRequest.id_org == user.id_org)
         .order_by(CmsApprovalRequest.submitted_at.desc()).limit(1000))
    if not caps["reviewer"]:
        q = q.where(CmsApprovalRequest.submitted_by == user.id_user)
    if status_filter:
        q = q.where(CmsApprovalRequest.status == status_filter)
    rows = list((await db.execute(q)).scalars().all())
    items = await _enrich(db, rows)
    # Dernier commentaire de DÉCISION par demande (motif reject/modifs/approve)
    ids = [r.id_request for r in rows]
    last_comment: dict[int, str] = {}
    if ids:
        for c in (await db.execute(
            select(CmsApprovalComment)
            .where(CmsApprovalComment.id_request.in_(ids),
                   CmsApprovalComment.action.in_(
                       ("approved", "rejected", "changes_requested")))
            .order_by(CmsApprovalComment.created_at, CmsApprovalComment.id_comment)
        )).scalars().all():
            last_comment[c.id_request] = c.comment_text
    out = []
    for item in items:
        d = item.model_dump()
        d["decision_comment"] = last_comment.get(item.id_request)
        out.append(d)
    return out


@router.get("/export.csv")
async def export_approvals_csv(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> Response:
    rows = await _export_rows(db, db_worker, current_user, status_filter)
    content = approvals_export.approvals_csv(rows, locale=current_user.locale)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    filename = approvals_export.export_filename("csv", stamp, current_user.locale)
    return Response(
        content="﻿" + content,  # BOM : Excel ouvre l'UTF-8 correctement
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export.pdf")
async def export_approvals_pdf(
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> Response:
    from app.models.organization import CmsOrganization

    rows = await _export_rows(db, db_worker, current_user, status_filter)
    org_name = (await db.execute(
        select(CmsOrganization.org_name).where(
            CmsOrganization.id_org == current_user.id_org)
    )).scalar_one_or_none() or "Vitrail"
    now = datetime.now(UTC)
    pdf = approvals_export.build_approvals_pdf(
        org_name=org_name, rows=rows,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        status_filter=status_filter,
        locale=current_user.locale,
    )
    filename = approvals_export.export_filename("pdf", now.strftime("%Y%m%d"),
                                                current_user.locale)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{id_request}", response_model=ApprovalDetail)
async def get_approval(
    id_request: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalDetail:
    caps = await _actor_caps(db_worker, current_user)
    if not (caps["reviewer"] or caps["submitter"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Permission denied: approvals")
    req = await _get_request_or_404(db, current_user, id_request, caps)
    detail = ApprovalDetail.model_validate((await _enrich(db, [req]))[0].model_dump())
    # Le reviewer voit CE qu'il approuve : paramètres commerciaux + cibles nommées +
    # occupation projetée + créatifs avec prévisualisation signée.
    extra = await approvals_svc.resource_details(
        db, req.id_org, req.resource_type, req.resource_id)
    detail.creatives = extra.get("creatives", [])
    detail.campaign = extra.get("campaign")
    detail.playlist = extra.get("playlist")
    comments = list((await db.execute(
        select(CmsApprovalComment).where(CmsApprovalComment.id_request == id_request)
        .order_by(CmsApprovalComment.created_at, CmsApprovalComment.id_comment)
    )).scalars().all())
    uids = {c.id_user for c in comments}
    names = dict((await db.execute(
        select(CmsUser.id_user, func.coalesce(CmsUser.full_name, CmsUser.email))
        .where(CmsUser.id_user.in_(uids))
    )).all()) if uids else {}
    detail.comments = []
    for c in comments:
        cr = ApprovalCommentRead.model_validate(c)
        cr.user_name = names.get(c.id_user)
        detail.comments.append(cr)
    return detail


# ── Mutations ────────────────────────────────────────────────────────────────

@router.post("/", response_model=ApprovalRead, status_code=status.HTTP_201_CREATED)
async def submit_approval(
    body: ApprovalSubmit,
    current_user: Annotated[CmsUser, Depends(require_permission("approvals", "submit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalRead:
    try:
        req, ctx = await approvals_svc.submit(
            db, current_user, body.resource_type, body.resource_id,
            notes=body.notes, due_date=body.due_date,
        )
    except approvals_svc.ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    await db.commit()
    await db.refresh(req)
    await _dispatch(db, current_user.id_org, "approval_requested", ctx)
    return (await _enrich(db, [req]))[0]


async def _do_transition(
    db: AsyncSession, db_worker: AsyncSession, user: CmsUser,
    id_request: int, action: str, comment: str | None,
    notify_advertiser: bool = False,
) -> ApprovalRead:
    caps = await _actor_caps(db_worker, user)
    req = await _get_request_or_404(db, user, id_request, caps)
    if action == "resubmit" and not caps["admin"] and req.submitted_by != user.id_user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the submitter can resubmit")
    # N5 (audit 2026-07-27) : DÉCIDER sur du contenu gouverné exige d'administrer un nœud
    # PARENT, et personne ne valide sa propre soumission. Mesuré en base : demandes 22/23
    # avec submitted_by = reviewed_by = 37.
    if action in ("approve", "reject", "request_changes"):
        refusal = await approval_scope.decision_refusal(db, user, req)
        if refusal:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    try:
        event_type, ctx = await approvals_svc.apply_transition(db, req, user, action, comment)
    except approvals_svc.ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    # Gouvernance §XVIII (R15/C) : approuver une playlist gouvernée PUBLIE son brouillon,
    # dans la MÊME transaction que la décision (un seul chemin d'application, comme la
    # publication directe). La demande passe alors approved → published.
    published_playlist = False
    if action == "approve" and req.resource_type == "playlist":
        from app.models.playlist import CmsPlaylist
        from app.services import playlist_publish
        pl = (await db.execute(select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == req.resource_id,
            CmsPlaylist.id_org == req.id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        ))).scalar_one_or_none()
        if pl is not None and playlist_publish.has_draft(pl):
            try:
                await playlist_publish.apply_draft(db, pl)
            except playlist_publish.DraftApplyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid sub-playlist: {exc}") from exc
            await approvals_svc.mark_published(db, user, "playlist", req.resource_id)
            published_playlist = True

    await db.commit()
    await db.refresh(req)
    if published_playlist:
        from app.services import playlist_publish
        await playlist_publish.push_org_force_sync(db)
    await _dispatch(db, user.id_org, event_type, ctx)
    item = (await _enrich(db, [req]))[0]
    # Retour Ed #9 : email de décision aux comptes portail de l'annonceur — APRÈS le
    # commit et best-effort : un échec SMTP n'annule jamais la décision.
    if notify_advertiser and action in approval_emails.DECISION_ACTIONS:
        try:
            item.email_sent_to = await approval_emails.send_decision_email(
                db, req, user, action, comment)
            await db.commit()  # trace audit `approval_email`
        except Exception as exc:  # noqa: BLE001
            item.email_error = str(exc)
    return item


@router.post("/{id_request}/approve", response_model=ApprovalRead)
async def approve_request(
    id_request: int,
    body: ApprovalActionBody,
    current_user: Annotated[CmsUser, Depends(require_permission("approvals", "approve"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalRead:
    return await _do_transition(db, db_worker, current_user, id_request, "approve",
                                body.comment, notify_advertiser=body.notify_advertiser)


@router.post("/{id_request}/reject", response_model=ApprovalRead)
async def reject_request(
    id_request: int,
    body: ApprovalActionBody,
    current_user: Annotated[CmsUser, Depends(require_permission("approvals", "reject"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalRead:
    return await _do_transition(db, db_worker, current_user, id_request, "reject",
                                body.comment, notify_advertiser=body.notify_advertiser)


@router.post("/{id_request}/request-changes", response_model=ApprovalRead)
async def request_changes(
    id_request: int,
    body: ApprovalActionBody,
    current_user: Annotated[CmsUser, Depends(require_permission("approvals", "review"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalRead:
    return await _do_transition(
        db, db_worker, current_user, id_request, "request_changes", body.comment,
        notify_advertiser=body.notify_advertiser)


@router.post("/{id_request}/resubmit", response_model=ApprovalRead)
async def resubmit_request(
    id_request: int,
    body: ApprovalActionBody,
    current_user: Annotated[CmsUser, Depends(require_permission("approvals", "submit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalRead:
    return await _do_transition(db, db_worker, current_user, id_request, "resubmit", body.comment)


@router.delete("/{id_request}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_approval(
    id_request: int,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> None:
    """Ménage de la file (retour Ed 12/07) : suppression d'une demande TERMINÉE
    (rejetée/publiée/annulée) — reviewer seulement. Une demande ACTIVE ne se supprime
    pas (elle gouverne le publish de la ressource) : rejeter/annuler d'abord. La trace
    subsiste dans cms_audit_log ; les commentaires sont purgés avec la demande."""
    from sqlalchemy import delete as sa_delete

    from app.models.audit_log import CmsAuditLog

    caps = await _actor_caps(db_worker, current_user)
    if not caps["reviewer"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Permission denied: approvals")
    req = await _get_request_or_404(db, current_user, id_request, caps)
    if req.status in approvals_svc.ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Active request — reject or cancel it before deleting")
    await db.execute(sa_delete(CmsApprovalComment).where(
        CmsApprovalComment.id_request == id_request))
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user,
        action="approval_deleted", resource_type=req.resource_type,
        resource_id=str(req.resource_id),
        detail={"id_request": id_request, "status": req.status}))
    await db.delete(req)
    await db.commit()


@router.post("/{id_request}/comments", response_model=ApprovalCommentRead,
             status_code=status.HTTP_201_CREATED)
async def add_comment(
    id_request: int,
    body: ApprovalActionBody,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    db_worker: Annotated[AsyncSession, Depends(get_db_worker)],
) -> ApprovalCommentRead:
    caps = await _actor_caps(db_worker, current_user)
    if not (caps["reviewer"] or caps["submitter"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Permission denied: approvals")
    req = await _get_request_or_404(db, current_user, id_request, caps)
    text_ = (body.comment or "").strip()
    if not text_:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Empty comment")
    comment = CmsApprovalComment(
        id_request=req.id_request, id_org=req.id_org, id_user=current_user.id_user,
        comment_text=text_, action=None,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    out = ApprovalCommentRead.model_validate(comment)
    out.user_name = current_user.full_name or current_user.email
    return out
