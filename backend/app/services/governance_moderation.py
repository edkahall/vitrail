"""Alerte e-mail au modérateur d'un contenu gouverné (§XVIII R15/E, décision D8).

Quand un responsable local soumet une playlist gouvernée à modération, les MODÉRATEURS
(admins du plus proche nœud ancêtre, repli org-admins — cf. governance_access.
node_moderator_user_ids) sont prévenus par e-mail. Best-effort : un échec SMTP ne bloque
JAMAIS la soumission (l'endpoint capture l'erreur), même philosophie que approval_emails.

Réutilise le SMTP existant (services.email_delivery) — aucune infrastructure nouvelle.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.models.playlist import CmsPlaylist
from app.models.user import CmsUser
from app.services.doc_i18n import norm_locale, tr
from app.services.email_delivery import send_email
from app.services.governance_access import node_moderator_user_ids

# Textes de l'e-mail, dans les 5 langues du produit. Chaque modérateur le reçoit dans la
# langue de SON profil (repli anglais).
DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "subject": "Moderation — playlist “{name}” awaits your approval",
        "body": ("Hello,\n\n{submitter} submitted the playlist “{name}” for your approval.\n"
                 "Open the CMS approval queue to review and approve it.\n{portal}\n\n"
                 "— Vitrail"),
    },
    "fr": {
        "subject": "Modération — la playlist « {name} » attend votre validation",
        "body": ("Bonjour,\n\n{submitter} a soumis la playlist « {name} » à votre validation.\n"
                 "Ouvrez la file d'approbation du CMS pour la relire et l'approuver.\n{portal}\n\n"
                 "— Vitrail"),
    },
    "es": {
        "subject": "Moderación — la playlist «{name}» espera su validación",
        "body": ("Hola:\n\n{submitter} ha enviado la playlist «{name}» para su validación.\n"
                 "Abra la cola de aprobación del CMS para revisarla y aprobarla.\n{portal}\n\n"
                 "— Vitrail"),
    },
    "pt": {
        "subject": "Moderação — a playlist «{name}» aguarda a sua validação",
        "body": ("Olá,\n\n{submitter} submeteu a playlist «{name}» à sua validação.\n"
                 "Abra a fila de aprovação do CMS para a rever e aprovar.\n{portal}\n\n"
                 "— Vitrail"),
    },
    "de": {
        "subject": "Moderation — die Playlist „{name}“ wartet auf Ihre Freigabe",
        "body": ("Guten Tag,\n\n{submitter} hat die Playlist „{name}“ zur Freigabe "
                 "eingereicht.\nÖffnen Sie die Freigabe-Warteschlange im CMS, um sie zu "
                 "prüfen und freizugeben.\n{portal}\n\n— Vitrail"),
    },
}


async def notify_moderators_submitted(
    db: AsyncSession, pl: CmsPlaylist, submitter: CmsUser
) -> None:
    """Prévient par e-mail les modérateurs de la playlist soumise. Best-effort.

    Silencieux si le nœud n'a pas de modérateur joignable, si SMTP n'est pas configuré,
    ou en cas d'erreur d'envoi — la soumission reste valide dans tous les cas.
    """
    if pl.id_governance_node is None:
        return
    user_ids = await node_moderator_user_ids(db, pl.id_governance_node)
    # Ne pas s'auto-notifier (le soumetteur peut être admin d'un ancêtre par ailleurs).
    user_ids = [uid for uid in user_ids if uid != submitter.id_user]
    if not user_ids:
        return
    rows = (await db.execute(
        select(CmsUser.email, CmsUser.locale).where(
            CmsUser.id_user.in_(user_ids),
            CmsUser.is_active.is_(True),
            CmsUser.email.is_not(None),
        )
    )).all()
    # Un envoi par langue : chaque modérateur lit l'e-mail dans la langue de SON profil.
    by_locale: dict[str, list[str]] = {}
    for email, locale in rows:
        if email:
            by_locale.setdefault(norm_locale(locale), []).append(email)
    if not by_locale:
        return

    who = submitter.full_name or submitter.email
    portal = (f"{settings.PUBLIC_WEB_URL.rstrip('/')}/approvals"
              if settings.PUBLIC_WEB_URL else "")
    for loc, emails in by_locale.items():
        try:
            await run_in_threadpool(
                send_email,
                to=emails,
                subject=tr(DOC_TEXTS, loc, "subject", name=pl.playlist_name),
                body=tr(DOC_TEXTS, loc, "body", name=pl.playlist_name, submitter=who,
                        portal=portal),
            )
        except Exception:  # noqa: BLE001 — best-effort, ne bloque jamais la soumission
            continue
