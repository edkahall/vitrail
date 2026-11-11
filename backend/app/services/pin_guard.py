"""Garde PIN des actions CMS à fort rayon d'action (migr 0097).

**Pourquoi un PIN alors que la session est déjà authentifiée.** Ce n'est PAS un facteur
d'authentification ici (contrairement à Jeanine, où le PIN compense la faiblesse d'un
compte Telegram — cf. `PIN_REQUIRED_ACTIONS` / `messaging_gateway`). Dans le CMS il
confirme l'**intention** sur une action dont le rayon d'action est le parc entier :
« redémarrer les 40 écrans du réseau » ne doit pas partir d'un clic distrait. Un attaquant
qui détient déjà la session peut de toute façon agir player par player sans PIN — le PIN
ne prétend pas l'en empêcher, il empêche l'accident à grande échelle.

**Périmètre (décision Ed 2026-07-19) : les commandes DESTRUCTIVES seulement.** Un PIN à
chaque capture d'écran de parc serait vite contourné (post-it sur l'écran) — une gêne
permanente sur une action inoffensive dégrade la sécurité au lieu de l'améliorer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.models.user import CmsUser

# Mêmes valeurs que la voie messagerie (cohérence d'expérience).
PIN_MAX_ATTEMPTS = 3
PIN_LOCK_MINUTES = 15

# Commandes qui, en MASSE, exigent le PIN : tout ce qui peut couper/perdre un écran,
# détruire de la configuration ou décaler les horaires. Miroir d'esprit de
# `assistant_actions.PIN_REQUIRED_ACTIONS`, exprimé dans le vocabulaire
# `PlayerCommandRequest.command`.
PIN_REQUIRED_BULK_COMMANDS: frozenset[str] = frozenset({
    "reboot_os",        # redémarre la machine
    "restart",          # redémarre l'app player
    "reset_config",     # DÉTRUIT la config locale (ré-appairage)
    "quit_app",         # laisse l'écran sur le bureau
    "maintenance",      # suspend les alertes du parc (angle mort de supervision)
    "hdmi_input",       # bascule la source affichée
    "set_timezone",     # décale toute la programmation
    "display_command",  # écran on/off (RS232/IP/CEC)
})

# Explicitement SANS PIN : inoffensives et fréquentes en masse.
#   screenshot · detect_capabilities · toggle_overlay · play_now


def bulk_command_requires_pin(command: str) -> bool:
    return command in PIN_REQUIRED_BULK_COMMANDS


async def verify_pin_or_raise(db: AsyncSession, user: CmsUser, pin: str | None) -> None:
    """Valide le PIN de l'utilisateur, ou lève une HTTPException explicite.

    423 LOCKED           — verrou actif (n essais faux) ;
    412 PRECONDITION     — aucun PIN défini : à poser au profil avant d'agir en masse ;
    403 FORBIDDEN        — PIN absent de la requête ou faux (avec essais restants).

    Le compteur est remis à zéro au succès. Codes distincts : le frontend doit pouvoir
    dire « définissez un PIN » plutôt que « PIN incorrect » à quelqu'un qui n'en a pas.
    """
    now = datetime.now(timezone.utc)

    locked = user.assistant_pin_locked_until
    if locked is not None:
        if locked.tzinfo is None:  # colonne lue naïve selon le driver
            locked = locked.replace(tzinfo=timezone.utc)
        if locked > now:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=(f"Too many attempts: PIN locked until "
                        f"{locked.strftime('%H:%M')} UTC."),
            )
        # Verrou expiré : on repart d'une ardoise propre.
        user.assistant_pin_locked_until = None
        user.assistant_pin_attempts = 0

    if not user.assistant_pin_hash:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=("This bulk action requires a PIN. Set it in your profile "
                    "(\"Messaging\" card)."),
        )

    if pin and verify_password(pin, user.assistant_pin_hash):
        if user.assistant_pin_attempts:
            user.assistant_pin_attempts = 0
            await db.commit()
        return

    # Un PIN absent compte comme un essai : sinon on offre un oracle gratuit pour
    # sonder l'état du verrou sans jamais le déclencher.
    user.assistant_pin_attempts = (user.assistant_pin_attempts or 0) + 1
    if user.assistant_pin_attempts >= PIN_MAX_ATTEMPTS:
        user.assistant_pin_attempts = 0
        user.assistant_pin_locked_until = now + timedelta(minutes=PIN_LOCK_MINUTES)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Too many attempts: PIN locked for {PIN_LOCK_MINUTES} minutes.",
        )
    remaining = PIN_MAX_ATTEMPTS - user.assistant_pin_attempts
    await db.commit()
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Incorrect PIN — {remaining} attempt(s) left.",
    )
