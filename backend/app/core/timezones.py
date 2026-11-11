"""Une seule définition de « ce qu'est un fuseau horaire utilisable ».

Trois consommateurs, trois besoins, une seule règle :
  - les **schémas** (site, événement) valident à la SAISIE et refusent en 422 ;
  - les **services** replient une valeur illisible déjà en base, sans casser la diffusion ;
  - le **worker** évalue les fenêtres horaires dans le fuseau demandé.

Vit dans `core` et pas dans `services` pour que les schémas puissent l'importer sans
inverser les couches — un validateur dupliqué finirait par diverger de son service, et
c'est exactement ce qui produit « une surface refuse, l'autre accepte ».
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_valid_tz(tz: str | None) -> bool:
    """`tz` désigne-t-il une vraie zone IANA ?"""
    if not tz:
        return False
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def safe_tz_name(tz: str | None) -> str:
    """Nom de fuseau UTILISABLE : `tz` s'il est valide, sinon `SIGNFLOW_TZ`.

    ⚠️ Repli vers `SIGNFLOW_TZ` et **jamais vers UTC** : une valeur illisible ne doit pas
    téléporter un site à Greenwich pendant que le serveur est à Paris. Le repli est
    journalisé — une donnée fausse en base doit se voir, pas se taire.
    """
    if not tz:
        return settings.SIGNFLOW_TZ
    if not is_valid_tz(tz):
        logger.warning("Fuseau invalide (%r) — repli sur SIGNFLOW_TZ", tz)
        return settings.SIGNFLOW_TZ
    return tz


def safe_zoneinfo(tz: str | None) -> ZoneInfo:
    """`ZoneInfo` correspondant à `safe_tz_name` — ne lève jamais.

    Dernier filet : si `SIGNFLOW_TZ` lui-même est mal configuré, on retombe sur UTC plutôt
    que de faire échouer un worker qui tourne pour tout le parc.
    """
    try:
        return ZoneInfo(safe_tz_name(tz))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def validate_tz_or_raise(v: str | None) -> str | None:
    """Validateur pydantic : refuse un fuseau inconnu **au moment de la saisie**.

    Une chaîne vide vaut « pas de fuseau » (`None`) — c'est ce que rend un champ vidé à
    l'écran, et le traiter comme une valeur invalide interdirait de revenir au défaut.
    """
    if v is None or v == "":
        return None
    if not is_valid_tz(v):
        raise ValueError(
            f"Unknown time zone: {v!r} (expected an IANA name, e.g. 'Europe/Paris')"
        )
    return v
