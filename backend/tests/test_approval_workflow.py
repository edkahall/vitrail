"""Tests Approval Workflow §XXI générique (S7 Phase C1, migr 0081).

Cycle complet sur le 1er resource type `campaign` :
- submit (RBAC approvals:submit, dedup 1 demande active/ressource, 404/422/409) ;
- approve / reject (motif OBLIGATOIRE) / request-changes (commentaire OBLIGATOIRE) /
  resubmit (soumetteur seul) ;
- publish campagne : 409 tant que pending_review, OK après approve → demande `published` ;
- cancel/delete campagne → demande `cancelled` ;
- visibilité : reviewer = toute l'org, soumetteur = SES demandes, sans permission = 403,
  cross-org = 404 ;
- notifications §XXII (webhook approval_requested) best-effort.
"""

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.approval import CmsApprovalRequest
from app.models.campaign import CmsAdvertiser, CmsCampaign, CmsCampaignCreative, CmsCampaignTarget
from app.models.media import CmsMedia
from app.models.notification import CmsAlertRule, CmsNotificationChannel, CmsNotificationLog
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import CmsPermission, CmsRole, CmsRolePermission, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import notifications


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="AWF", org_slug=f"awf-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


async def _grant(db: AsyncSession, role: CmsRole, module: str, op: str) -> None:
    await db.execute(
        pg_insert(CmsPermission)
        .values(module=module, operation=op, label=f"{module}.{op}")
        .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation"))
    await db.flush()
    perm = (await db.execute(select(CmsPermission).where(
        CmsPermission.module == module, CmsPermission.operation == op))).scalar_one()
    await db.execute(
        pg_insert(CmsRolePermission)
        .values(id_role=role.id_role, id_permission=perm.id_permission)
        .on_conflict_do_nothing())


async def _user(db: AsyncSession, org: CmsOrganization, role_key: str,
                perms: list[tuple[str, str]], *, is_system: bool = True) -> CmsUser:
    user = CmsUser(id_org=org.id_org, email=f"u-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True,
                   full_name=f"User {role_key[:12]}")
    db.add(user)
    await db.flush()
    role = (await db.execute(select(CmsRole).where(
        CmsRole.role_key == role_key))).scalars().first()
    if role is None:
        role = CmsRole(id_org=None if is_system else org.id_org, role_key=role_key,
                       label=role_key, is_system=is_system)
        db.add(role)
        await db.flush()
    for module, op in perms:
        await _grant(db, role, module, op)
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    return user


def _headers(user: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    return await _user(db_session, org, "CMS_ORG_ADMIN",
                       [("campaigns", op) for op in ("read", "create", "edit", "publish",
                                                     "delete")]
                       + [("approvals", op) for op in ("submit", "approve", "reject", "review")])


@pytest_asyncio.fixture
async def submitter(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    """Rôle custom : soumet mais ne review pas."""
    return await _user(db_session, org, f"SUBM-{uuid.uuid4().hex[:6]}",
                       [("approvals", "submit"), ("campaigns", "read")], is_system=False)


@pytest_asyncio.fixture
async def approver(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    """Rôle custom façon CMS_APPROVER : review/approve/reject, ne soumet pas."""
    return await _user(db_session, org, f"APPR-{uuid.uuid4().hex[:6]}",
                       [("approvals", "review"), ("approvals", "approve"),
                        ("approvals", "reject")], is_system=False)


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession, org: CmsOrganization) -> CmsUser:
    """Aucune permission approvals → la file répond 403."""
    return await _user(db_session, org, f"OUT-{uuid.uuid4().hex[:6]}",
                       [("campaigns", "read")], is_system=False)


async def _campaign(db: AsyncSession, org: CmsOrganization, *,
                    with_creative: bool = False,
                    creative_status: str = "approved") -> CmsCampaign:
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name=f"Acme-{uuid.uuid4().hex[:6]}")
    db.add(adv)
    await db.flush()
    camp = CmsCampaign(
        id_org=org.id_org, id_advertiser=adv.id_advertiser, campaign_name="Rentrée",
        status="draft", starts_on=date.today() - timedelta(days=1),
        ends_on=date.today() + timedelta(days=7), sov_percent=10.0,
    )
    db.add(camp)
    await db.flush()
    db.add(CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                             target_type="org", target_id=None))
    if with_creative:
        m = CmsMedia(id_org=org.id_org, media_name="spot", media_type="video",
                     storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                     transcode_status="done", duration_ms=15_000)
        db.add(m)
        await db.flush()
        db.add(CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                                   id_media=m.id_media, duration_ms=15_000,
                                   approval_status=creative_status))
    await db.commit()
    return camp


async def _submit(client: AsyncClient, user: CmsUser, camp: CmsCampaign,
                  notes: str | None = None) -> dict:
    r = await client.post("/api/v1/approvals/", headers=_headers(user),
                          json={"resource_type": "campaign",
                                "resource_id": camp.id_campaign, "notes": notes})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_submit_dedup_and_validation(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser, outsider: CmsUser):
    camp = await _campaign(db_session, org)
    req = await _submit(client, submitter, camp, notes="À valider avant lundi")
    assert req["status"] == "pending_review"
    assert req["resource_name"] == "Rentrée"
    assert req["submitted_by_name"] == submitter.full_name

    # dedup : une seule demande active par ressource
    r = await client.post("/api/v1/approvals/", headers=_headers(submitter),
                          json={"resource_type": "campaign", "resource_id": camp.id_campaign})
    assert r.status_code == 409

    # type inconnu / ressource inexistante / sans permission
    r = await client.post("/api/v1/approvals/", headers=_headers(submitter),
                          json={"resource_type": "schedule", "resource_id": 1})
    assert r.status_code == 422
    r = await client.post("/api/v1/approvals/", headers=_headers(submitter),
                          json={"resource_type": "campaign", "resource_id": 999_999})
    assert r.status_code == 404
    r = await client.post("/api/v1/approvals/", headers=_headers(outsider),
                          json={"resource_type": "campaign", "resource_id": camp.id_campaign})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_submit_requires_draft(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser):
    camp = await _campaign(db_session, org)
    camp.status = "approved"
    await db_session.commit()
    r = await client.post("/api/v1/approvals/", headers=_headers(submitter),
                          json={"resource_type": "campaign", "resource_id": camp.id_campaign})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_full_cycle_approve_then_campaign_publish(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser, submitter: CmsUser, approver: CmsUser):
    camp = await _campaign(db_session, org, with_creative=True)
    req = await _submit(client, submitter, camp)

    # la campagne expose la demande active
    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}", headers=_headers(admin))
    assert r.status_code == 200
    assert r.json()["approval_request"] == {
        "id_request": req["id_request"], "status": "pending_review"}

    # publish bloqué tant que la demande est en revue
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 409 and "approval" in r.json()["detail"]

    # approve (approver custom, pas org-admin) → publish passe
    r = await client.post(f"/api/v1/approvals/{req['id_request']}/approve",
                          headers=_headers(approver), json={"comment": "OK pour moi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["reviewed_by_name"] == approver.full_name

    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 200, r.text

    # la demande est marquée published, plus de demande active sur la campagne
    r = await client.get(f"/api/v1/approvals/{req['id_request']}", headers=_headers(approver))
    assert r.status_code == 200
    detail = r.json()
    assert detail["status"] == "published" and detail["published_at"] is not None
    actions = [c["action"] for c in detail["comments"]]
    assert actions == ["submitted", "approved", "published"]
    r = await client.get(f"/api/v1/campaigns/{camp.id_campaign}", headers=_headers(admin))
    assert r.json()["approval_request"] is None


@pytest.mark.asyncio
async def test_approve_also_approves_pending_creatives(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser, submitter: CmsUser, approver: CmsUser):
    """Décision Ed 2026-07-11 : approuver la demande de campagne approuve aussi ses
    créatifs EN ATTENTE (le reviewer valide la campagne comme un tout) — sinon publish
    422 « aucun créatif approuvé » sans aucune alerte. Le détail montre les créatifs
    (avec URL signée de prévisualisation) pour que le reviewer voie ce qu'il approuve."""
    org.campaign_creative_approval = True
    await db_session.commit()
    camp = await _campaign(db_session, org, with_creative=True, creative_status="pending")
    req = await _submit(client, submitter, camp)

    # Détail reviewer : créatif visible, statut pending, prévisualisation signée
    r = await client.get(f"/api/v1/approvals/{req['id_request']}", headers=_headers(approver))
    creatives = r.json()["creatives"]
    assert len(creatives) == 1
    assert creatives[0]["approval_status"] == "pending"
    assert creatives[0]["media_name"] == "spot"
    assert creatives[0]["preview_url"]
    # ... et les paramètres commerciaux + cibles + occupation projetée (pas de
    # validation à l'aveugle — retour Ed 11/07)
    info = r.json()["campaign"]
    assert info["sov_percent"] == 10.0
    assert info["pricing_model"] == "sov"
    assert info["advertiser_name"].startswith("Acme-")
    assert info["targets"] == [{"target_type": "org", "name": None}]
    assert info["occupancy"] is not None
    assert info["occupancy"]["blocked"] is False

    # Approve → le créatif passe approved dans la même transaction
    r = await client.post(f"/api/v1/approvals/{req['id_request']}/approve",
                          headers=_headers(approver), json={})
    assert r.status_code == 200
    cr = (await db_session.execute(select(CmsCampaignCreative).where(
        CmsCampaignCreative.id_campaign == camp.id_campaign))).scalar_one()
    await db_session.refresh(cr)
    assert cr.approval_status == "approved"
    assert cr.reviewed_by == approver.id_user

    # publish passe (aurait été 422 « aucun créatif approuvé » sans l'auto-approve)
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "publish"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_reject_requires_comment_then_resubmittable(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser, approver: CmsUser):
    camp = await _campaign(db_session, org)
    req = await _submit(client, submitter, camp)

    r = await client.post(f"/api/v1/approvals/{req['id_request']}/reject",
                          headers=_headers(approver), json={})
    assert r.status_code == 422

    r = await client.post(f"/api/v1/approvals/{req['id_request']}/reject",
                          headers=_headers(approver), json={"comment": "Visuel hors charte"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    # rejected = terminal → une NOUVELLE soumission redevient possible
    req2 = await _submit(client, submitter, camp)
    assert req2["id_request"] != req["id_request"]


@pytest.mark.asyncio
async def test_request_changes_and_resubmit_rights(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser, submitter: CmsUser, approver: CmsUser):
    camp = await _campaign(db_session, org)
    req = await _submit(client, submitter, camp)
    rid = req["id_request"]

    r = await client.post(f"/api/v1/approvals/{rid}/request-changes",
                          headers=_headers(approver), json={})
    assert r.status_code == 422  # commentaire obligatoire

    r = await client.post(f"/api/v1/approvals/{rid}/request-changes",
                          headers=_headers(approver), json={"comment": "Ajouter le prix"})
    assert r.status_code == 200 and r.json()["status"] == "changes_requested"

    # resubmit : réservé au soumetteur (l'approver a approvals:submit ? non → 403 RBAC ;
    # un AUTRE submitter → 403 métier)
    other = await _user(db_session, org, f"SUBM-{uuid.uuid4().hex[:6]}",
                        [("approvals", "submit")], is_system=False)
    r = await client.post(f"/api/v1/approvals/{rid}/resubmit",
                          headers=_headers(other), json={})
    assert r.status_code in (403, 404)  # non-reviewer ne voit pas la demande d'un autre

    r = await client.post(f"/api/v1/approvals/{rid}/resubmit",
                          headers=_headers(submitter), json={"comment": "Prix ajouté"})
    assert r.status_code == 200 and r.json()["status"] == "pending_review"

    # approve depuis pending_review re-fonctionne ; commentaire libre possible
    r = await client.post(f"/api/v1/approvals/{rid}/comments",
                          headers=_headers(approver), json={"comment": "Je regarde demain"})
    assert r.status_code == 201 and r.json()["user_name"] == approver.full_name
    r = await client.get(f"/api/v1/approvals/{rid}", headers=_headers(submitter))
    actions = [c["action"] for c in r.json()["comments"]]
    assert actions == ["submitted", "changes_requested", "resubmitted", None]


@pytest.mark.asyncio
async def test_visibility_scopes_and_dashboard(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser, submitter: CmsUser, approver: CmsUser, outsider: CmsUser):
    camp1 = await _campaign(db_session, org)
    camp2 = await _campaign(db_session, org)
    req1 = await _submit(client, submitter, camp1)
    await _submit(client, admin, camp2)

    # approver (reviewer) voit tout ; submitter ne voit que la sienne ; outsider 403
    r = await client.get("/api/v1/approvals/", headers=_headers(approver))
    assert r.status_code == 200 and len(r.json()) == 2
    r = await client.get("/api/v1/approvals/", headers=_headers(submitter))
    assert [x["id_request"] for x in r.json()] == [req1["id_request"]]
    r = await client.get("/api/v1/approvals/", headers=_headers(outsider))
    assert r.status_code == 403

    # filtre statut + dashboard
    r = await client.get("/api/v1/approvals/?status=pending_review",
                         headers=_headers(approver))
    assert len(r.json()) == 2
    r = await client.get("/api/v1/approvals/dashboard", headers=_headers(approver))
    assert r.status_code == 200 and r.json()["pending_review"] == 2
    r = await client.get("/api/v1/approvals/dashboard", headers=_headers(submitter))
    assert r.json()["pending_review"] == 1

    # détail : le submitter ne voit pas la demande d'un autre (404, jamais 403)
    r = await client.get(f"/api/v1/approvals/{req1['id_request']}",
                         headers=_headers(submitter))
    assert r.status_code == 200
    other_req = (await client.get("/api/v1/approvals/", headers=_headers(approver))).json()
    other_id = next(x["id_request"] for x in other_req
                    if x["id_request"] != req1["id_request"])
    r = await client.get(f"/api/v1/approvals/{other_id}", headers=_headers(submitter))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cross_org_404(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser):
    camp = await _campaign(db_session, org)
    req = await _submit(client, submitter, camp)

    org_b = CmsOrganization(org_name="B", org_slug=f"b-{uuid.uuid4().hex[:8]}")
    db_session.add(org_b)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=org_b.id_org))
    await db_session.commit()
    admin_b = await _user(db_session, org_b, "CMS_ORG_ADMIN",
                          [("approvals", "approve")])
    r = await client.get(f"/api/v1/approvals/{req['id_request']}", headers=_headers(admin_b))
    assert r.status_code == 404
    r = await client.post(f"/api/v1/approvals/{req['id_request']}/approve",
                          headers=_headers(admin_b), json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_campaign_cancel_and_delete_cancel_request(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        admin: CmsUser, submitter: CmsUser):
    # cancel de la campagne → demande cancelled
    camp = await _campaign(db_session, org)
    req = await _submit(client, submitter, camp)
    r = await client.post(f"/api/v1/campaigns/{camp.id_campaign}/status",
                          headers=_headers(admin), json={"action": "cancel"})
    assert r.status_code == 200
    r = await client.get(f"/api/v1/approvals/{req['id_request']}", headers=_headers(admin))
    assert r.json()["status"] == "cancelled"

    # delete de la campagne → demandes PURGÉES (retour Ed 11/07 : plus de « #7 »
    # fantôme dans la file ; la trace reste dans l'audit log)
    camp2 = await _campaign(db_session, org)
    req2 = await _submit(client, submitter, camp2)
    r = await client.delete(f"/api/v1/campaigns/{camp2.id_campaign}", headers=_headers(admin))
    assert r.status_code == 204
    row = (await db_session.execute(select(CmsApprovalRequest).where(
        CmsApprovalRequest.id_request == req2["id_request"]))).scalar_one_or_none()
    assert row is None
    r = await client.get(f"/api/v1/approvals/{req2['id_request']}", headers=_headers(admin))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_terminal_request_and_exports(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser, approver: CmsUser):
    """Retour Ed 12/07 : ménage de la file (DELETE terminaux seulement, reviewer) +
    exports CSV/PDF (scope de la liste, motif de décision inclus)."""
    from app.models.approval import CmsApprovalComment

    camp = await _campaign(db_session, org)
    req = await _submit(client, submitter, camp)
    rid = req["id_request"]

    # demande ACTIVE : suppression refusée (elle gouverne le publish)
    r = await client.delete(f"/api/v1/approvals/{rid}", headers=_headers(approver))
    assert r.status_code == 409

    r = await client.post(f"/api/v1/approvals/{rid}/reject",
                          headers=_headers(approver), json={"comment": "Non conforme"})
    assert r.status_code == 200

    # un soumetteur non-reviewer ne supprime pas
    r = await client.delete(f"/api/v1/approvals/{rid}", headers=_headers(submitter))
    assert r.status_code == 403

    # exports : CSV (motif + nom de ressource) + PDF valide
    r = await client.get("/api/v1/approvals/export.csv", headers=_headers(approver))
    assert r.status_code == 200
    assert "Rentrée" in r.text and "Non conforme" in r.text and "Rejected" in r.text
    assert "approvals_" in r.headers["content-disposition"]
    # langue du profil de l'exportateur : français si fr (témoin : l'anglais disparaît)
    approver.locale = "fr"
    await db_session.commit()
    r = await client.get("/api/v1/approvals/export.csv", headers=_headers(approver))
    assert "Rejetée" in r.text and "Rejected" not in r.text
    assert "approbations_" in r.headers["content-disposition"]
    r = await client.get("/api/v1/approvals/export.csv?status=published",
                         headers=_headers(approver))
    assert "Rentrée" not in r.text  # filtre statut respecté
    r = await client.get("/api/v1/approvals/export.pdf", headers=_headers(approver))
    assert r.status_code == 200 and r.content[:4] == b"%PDF"

    # suppression d'une demande terminée : 204, commentaires purgés, 404 ensuite
    r = await client.delete(f"/api/v1/approvals/{rid}", headers=_headers(approver))
    assert r.status_code == 204
    left = (await db_session.execute(select(CmsApprovalComment).where(
        CmsApprovalComment.id_request == rid))).scalars().all()
    assert left == []
    r = await client.get(f"/api/v1/approvals/{rid}", headers=_headers(approver))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_decision_email_to_advertiser_portal_accounts(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser, approver: CmsUser, monkeypatch):
    """Retour Ed #9 : notify_advertiser=True sur une décision → email aux comptes
    portail ACTIFS de l'annonceur (hors démo/inactifs), dans la langue du profil du
    destinataire, motif inclus. Sans la case → aucun envoi (email_sent_to = None)."""
    from app.services import approval_emails

    camp = await _campaign(db_session, org)
    adv_id = camp.id_advertiser

    def portal_user(email: str, *, active: bool = True, demo: bool = False,
                    locale: str = "fr") -> CmsUser:
        return CmsUser(id_org=org.id_org, email=email,
                       password_hash=hash_password("Password1!"), is_active=active,
                       is_demo=demo, locale=locale, id_advertiser=adv_id)

    active = portal_user(f"adv-{uuid.uuid4().hex[:8]}@t.com")
    db_session.add_all([
        active,
        portal_user(f"inact-{uuid.uuid4().hex[:8]}@t.com", active=False),
        portal_user(f"demo-{uuid.uuid4().hex[:8]}@t.com", demo=True),
    ])
    await db_session.flush()
    active_email = active.email  # capturer AVANT expiration (piège MissingGreenlet)
    await db_session.commit()

    sent: list[dict] = []

    def fake_send(*, to, subject, body, attachments=None):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(approval_emails, "send_email", fake_send)

    req = await _submit(client, submitter, camp)
    # décision SANS la case → aucun email
    r = await client.post(f"/api/v1/approvals/{req['id_request']}/request-changes",
                          headers=_headers(approver), json={"comment": "Prix ?"})
    assert r.status_code == 200
    assert r.json()["email_sent_to"] is None and not sent

    r = await client.post(f"/api/v1/approvals/{req['id_request']}/reject",
                          headers=_headers(approver),
                          json={"comment": "Visuel hors charte",
                                "notify_advertiser": True})
    assert r.status_code == 200, r.text
    assert r.json()["email_sent_to"] == 1
    assert len(sent) == 1
    assert sent[0]["to"] == [active_email]  # actif seul (inactif + démo exclus)
    assert "Rentrée" in sent[0]["subject"]
    assert "rejetée" in sent[0]["subject"]          # locale fr du destinataire
    assert "Visuel hors charte" in sent[0]["body"]  # motif inclus


@pytest.mark.asyncio
async def test_decision_email_failure_never_blocks_decision(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser, approver: CmsUser, monkeypatch):
    """Best-effort : un échec SMTP ne bloque JAMAIS la décision — l'endpoint renvoie
    la demande approuvée + email_error informatif."""
    from app.services import approval_emails
    from app.services.email_delivery import EmailError

    camp = await _campaign(db_session, org)
    db_session.add(CmsUser(id_org=org.id_org, email=f"adv-{uuid.uuid4().hex[:8]}@t.com",
                           password_hash=hash_password("Password1!"), is_active=True,
                           id_advertiser=camp.id_advertiser))
    await db_session.commit()

    def boom(**kwargs):
        raise EmailError("SMTP down")

    monkeypatch.setattr(approval_emails, "send_email", boom)
    req = await _submit(client, submitter, camp)
    r = await client.post(f"/api/v1/approvals/{req['id_request']}/approve",
                          headers=_headers(approver), json={"notify_advertiser": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"  # la décision passe malgré l'échec SMTP
    assert body["email_sent_to"] is None
    assert "SMTP down" in body["email_error"]


@pytest.mark.asyncio
async def test_notification_webhook_on_submit(
        client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
        submitter: CmsUser, monkeypatch):
    channel = CmsNotificationChannel(id_org=org.id_org, name="hook",
                                     channel_type="webhook",
                                     config={"url": "http://example.test/hook"})
    db_session.add(channel)
    await db_session.flush()
    db_session.add(CmsAlertRule(id_org=org.id_org, event_type="approval_requested",
                                id_channel=channel.id_channel))
    await db_session.commit()

    calls: list[tuple[str, dict]] = []

    # ⚠️ Contrat de `webhook_post` depuis P1.7 : il reçoit aussi le secret de signature
    # et le type d'événement, et rend un TRIPLET (ok, détail, réessayable).
    def fake_post(url: str, payload: dict, secret: str = "", event_type: str = "",
                  attempt: int = 1):
        calls.append((url, payload))
        return True, "HTTP 200", False

    monkeypatch.setattr(notifications, "webhook_post", fake_post)

    camp = await _campaign(db_session, org)
    await _submit(client, submitter, camp, notes="Merci de valider")

    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://example.test/hook"
    assert payload["event"] == "approval_requested"
    assert "Rentrée" in payload["subject"]
    log = (await db_session.execute(select(CmsNotificationLog).where(
        CmsNotificationLog.id_org == org.id_org,
        CmsNotificationLog.event_type == "approval_requested"))).scalars().all()
    assert len(log) == 1 and log[0].status == "sent"
