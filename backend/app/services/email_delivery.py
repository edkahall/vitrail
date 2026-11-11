"""Envoi d'emails (SMTP) — utilisé par les rapports planifiés (§24.3).

smtplib stdlib. Configuration via SMTP_* (core/config). Sans SMTP_HOST → l'envoi lève une
erreur explicite (les rapports le journalisent en `failed`). Pièces jointes supportées (PDF).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.core.config import settings


class EmailError(RuntimeError):
    pass


def smtp_configured() -> bool:
    return bool(settings.SMTP_HOST)


def send_email(
    *,
    to: list[str],
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Envoie un email texte + pièces jointes. `attachments` = [(filename, data, mime_subtype)].
    Lève EmailError si SMTP non configuré / échec."""
    if not settings.SMTP_HOST:
        raise EmailError("SMTP not configured (SMTP_HOST empty).")
    recipients = [a.strip() for a in to if a and a.strip()]
    if not recipients:
        raise EmailError("No recipient.")

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, data, subtype in (attachments or []):
        msg.add_attachment(data, maintype="application", subtype=subtype, filename=filename)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
            if settings.SMTP_TLS:
                s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:  # noqa: BLE001
        raise EmailError(f"SMTP send failed: {e}") from e
