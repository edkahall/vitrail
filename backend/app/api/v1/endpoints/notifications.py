"""Notifications & alertes (§XXII) — canaux webhook + règles + journal + test (org-admin, RLS)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.deps import get_db, require_org_admin
from app.core.ssrf import url_is_safe
from app.models.audit_log import CmsAuditLog
from app.models.notification import (
    CmsAlertRule,
    CmsNotificationChannel,
    CmsNotificationLog,
)
from app.models.user import CmsUser
from app.schemas.notifications import (
    ChannelCreate,
    ChannelRead,
    ChannelUpdate,
    NotificationLogRead,
    RuleCreate,
    RuleRead,
    RuleUpdate,
)
from app.services import notifications
from app.services.doc_i18n import norm_locale
from app.services.webhook_signature import new_secret

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/event-types")
async def event_types(u: Annotated[CmsUser, Depends(require_org_admin)]) -> dict:
    """Types d'événements alertables (whitelist), libellés dans la langue du demandeur."""
    return {"event_types": [{"key": k, "label": notifications.event_type_label(k, u.locale)}
                            for k in notifications.EVENT_TYPES if k != "test"]}


# ── Canaux ─────────────────────────────────────────────────────────────────────────
@router.get("/channels", response_model=list[ChannelRead])
async def channels_list(
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsNotificationChannel]:
    return list((await db.execute(
        select(CmsNotificationChannel).where(CmsNotificationChannel.id_org == u.id_org)
        .order_by(CmsNotificationChannel.id_channel)
    )).scalars().all())


def _validated_config(
    channel_type: str, config: dict | None, previous: dict | None = None
) -> dict:
    """Config nettoyée selon le type (webhook = url + secret ; email = recipients).

    ⚠️ `previous` n'est pas un confort : cette fonction RECONSTRUIT le dictionnaire de
    zéro (elle ne retourne que les clés qu'elle connaît), et elle est appelée aussi bien
    à la création qu'à la mise à jour. Sans lui, changer l'URL d'un canal régénérerait
    son secret **en silence** — le destinataire continuerait de vérifier avec l'ancien et
    rejetterait toutes les alertes suivantes comme des faux. Le secret ne change donc que
    si on le demande explicitement (`rotate_secret`).
    """
    config = config or {}
    previous = previous or {}
    if channel_type == "webhook":
        url = config.get("url", "")
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Invalid webhook URL.")
        # 🔴 SSRF (audit menu ⑬, 2026-07-31) : seul le SCHÉMA était vérifié. Le serveur
        # acceptait donc de POSTer vers la boucle locale, le réseau privé du client ou
        # l'adresse de métadonnées d'un cloud — et le **code HTTP est renvoyé à
        # l'appelant** (« HTTP 200 » contre une erreur de connexion), ce qui suffit à
        # cartographier un réseau interne. Le CORPS de la réponse n'est jamais exposé :
        # SSRF aveugle, pas exfiltration.
        # 🔑 `url_is_safe` existait déjà et sert à 5 autres endroits (import de campagne,
        # médias, promo, import de bundle) — ce canal était simplement l'oublié.
        if not url_is_safe(url, schemes=("http", "https")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook URL rejected: internal, private or unresolvable address.")
        # Secret de signature (P1.7) : conservé s'il existe, créé sinon — les canaux
        # antérieurs en reçoivent donc un à leur première modification, sans geste. On ne
        # le laisse jamais choisir par l'appelant : un secret fourni depuis l'extérieur
        # serait faible, réutilisé, ou déjà passé par un ticket.
        secret = previous.get("secret") or new_secret()
        if config.get("rotate_secret") is True:
            secret = new_secret()
        return {"url": url, "secret": secret}
    if channel_type == "email":
        raw = config.get("recipients", [])
        recipients = [r.strip() for r in raw if isinstance(r, str) and r.strip()]
        if not recipients or any("@" not in r or " " in r for r in recipients):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Invalid email recipients.")
        if len(recipients) > 20:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="20 recipients maximum per channel.")
        return {"recipients": recipients}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Supported channel types: webhook, email.")


def _with_locale(clean: dict, sent: dict | None, fallback: str | None) -> dict:
    """Pose `locale` (langue des messages du canal) dans la config nettoyée.

    `_validated_config` reconstruit le dictionnaire sans cette clé : on la remet ici.
    Langue envoyée explicitement par le client → elle ; sinon `fallback` (création = langue
    du profil du créateur ; mise à jour = langue déjà portée par le canal, si elle existe —
    un canal antérieur sans langue reste en anglais). Ce n'est pas un secret : elle est
    rendue telle quelle par l'API.
    """
    sent = sent or {}
    if sent.get("locale"):
        clean["locale"] = norm_locale(sent["locale"])
    elif fallback:
        clean["locale"] = norm_locale(fallback)
    return clean


def _notif_audit(
    user: CmsUser, action: str, resource_type: str, resource_id: str,
    detail: dict | None = None,
) -> CmsAuditLog:
    """Trace d'un geste sur les alertes (audit menu ⑬) — 0 ligne mesurée avant.

    Créer un canal e-mail, c'est décider OÙ PARTENT les alertes du réseau ; désactiver une
    règle, c'est éteindre une alerte. Le silence qui suit doit avoir une explication.
    """
    return CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action=action,
        resource_type=resource_type, resource_id=resource_id, source="cms",
        detail=detail or {},
    )


@router.post("/channels", response_model=ChannelRead, status_code=status.HTTP_201_CREATED)
async def channel_create(
    body: ChannelCreate,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsNotificationChannel:
    # Langue des messages du canal = celle du profil de son créateur (sauf choix explicite).
    config = _with_locale(_validated_config(body.channel_type, body.config), body.config,
                          u.locale)
    row = CmsNotificationChannel(
        id_org=u.id_org,
        name=(body.name or ("Email" if body.channel_type == "email" else "Webhook"))[:80],
        channel_type=body.channel_type, config=config, enabled=body.enabled)
    db.add(row)
    await db.flush()
    db.add(_notif_audit(u, "create", "notification_channel", str(row.id_channel),
                        {"name": row.name, "type": row.channel_type}))
    await db.commit()
    await db.refresh(row)
    return row


async def _channel_or_404(db: AsyncSession, id_channel: int, id_org: int) -> CmsNotificationChannel:
    row = (await db.execute(
        select(CmsNotificationChannel).where(CmsNotificationChannel.id_channel == id_channel,
                                             CmsNotificationChannel.id_org == id_org)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found.")
    return row


@router.put("/channels/{id_channel}", response_model=ChannelRead)
async def channel_update(
    id_channel: int, body: ChannelUpdate,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsNotificationChannel:
    row = await _channel_or_404(db, id_channel, u.id_org)
    if body.name is not None:
        row.name = body.name[:80] or row.name
    rotated = False
    if body.config is not None:
        previous = row.config or {}
        row.config = _with_locale(_validated_config(row.channel_type, body.config, previous),
                                  body.config, previous.get("locale"))
        rotated = bool(row.config.get("secret")) and row.config.get("secret") != previous.get(
            "secret")
    if body.enabled is not None:
        row.enabled = body.enabled
    # Une rotation de secret casse la vérification chez le destinataire jusqu'à ce qu'il
    # ait la nouvelle valeur : c'est une panne volontaire, elle doit se retrouver dans le
    # journal quand on cherchera pourquoi les alertes ne passent plus. ⛔ Le secret
    # lui-même n'y figure évidemment jamais.
    db.add(_notif_audit(u, "update", "notification_channel", str(id_channel),
                        {"name": row.name, "changes": sorted(body.model_fields_set),
                         **({"secret_rotated": True} if rotated else {})}))
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/channels/{id_channel}", status_code=status.HTTP_204_NO_CONTENT)
async def channel_delete(
    id_channel: int,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    row = await _channel_or_404(db, id_channel, u.id_org)
    # ⚠️ La suppression emporte les règles liées (cascade) : le journal garde le NOM du
    # canal, sinon plus rien ne dit pourquoi les alertes se sont tues.
    db.add(_notif_audit(u, "delete", "notification_channel", str(id_channel),
                        {"name": row.name, "type": row.channel_type}))
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/channels/{id_channel}/test")
async def channel_test(
    id_channel: int,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Envoie un message de test au canal (webhook ou email) + journalise le résultat."""
    row = await _channel_or_404(db, id_channel, u.id_org)
    subject, body = notifications.build_message(
        "test", {}, locale=notifications.channel_locale(row.config))
    ok, detail = await run_in_threadpool(
        notifications.deliver_to_channel, row.channel_type, row.config, "test",
        subject, body, {})
    db.add(CmsNotificationLog(id_org=u.id_org, event_type="test", subject=subject, body=body,
                              status="sent" if ok else "failed", detail=detail))
    await db.commit()
    return {"ok": ok, "detail": detail}


# ── Règles ─────────────────────────────────────────────────────────────────────────
@router.get("/rules", response_model=list[RuleRead])
async def rules_list(
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsAlertRule]:
    return list((await db.execute(
        select(CmsAlertRule).where(CmsAlertRule.id_org == u.id_org).order_by(CmsAlertRule.id_rule)
    )).scalars().all())


@router.post("/rules", response_model=RuleRead, status_code=status.HTTP_201_CREATED)
async def rule_create(
    body: RuleCreate,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsAlertRule:
    if body.event_type not in notifications.EVENT_TYPES or body.event_type == "test":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid event type.")
    await _channel_or_404(db, body.id_channel, u.id_org)   # garde org
    row = CmsAlertRule(id_org=u.id_org, event_type=body.event_type, id_channel=body.id_channel,
                       params=body.params or {}, enabled=body.enabled)
    db.add(row)
    await db.flush()
    db.add(_notif_audit(u, "create", "alert_rule", str(row.id_rule),
                        {"event_type": row.event_type, "id_channel": row.id_channel}))
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/rules/{id_rule}", response_model=RuleRead)
async def rule_update(
    id_rule: int, body: RuleUpdate,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsAlertRule:
    row = (await db.execute(
        select(CmsAlertRule).where(CmsAlertRule.id_rule == id_rule, CmsAlertRule.id_org == u.id_org)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found.")
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.id_channel is not None:
        await _channel_or_404(db, body.id_channel, u.id_org)
        row.id_channel = body.id_channel
    db.add(_notif_audit(u, "update", "alert_rule", str(id_rule),
                        {"event_type": row.event_type,
                         "changes": sorted(body.model_fields_set)}))
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/rules/{id_rule}", status_code=status.HTTP_204_NO_CONTENT)
async def rule_delete(
    id_rule: int,
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    row = (await db.execute(
        select(CmsAlertRule).where(CmsAlertRule.id_rule == id_rule, CmsAlertRule.id_org == u.id_org)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found.")
    db.add(_notif_audit(u, "delete", "alert_rule", str(id_rule),
                        {"event_type": row.event_type}))
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/log", response_model=list[NotificationLogRead])
async def notification_log(
    u: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsNotificationLog]:
    return list((await db.execute(
        select(CmsNotificationLog).where(CmsNotificationLog.id_org == u.id_org)
        .order_by(CmsNotificationLog.created_at.desc()).limit(50)
    )).scalars().all())
