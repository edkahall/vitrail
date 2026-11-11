"""Génération de contenu IA (§ génération) — adaptateurs providers derrière le coffre BYOK.

Image/vidéo/audio via fal.ai (`fal.py`) ; audio (musique/voix) aussi via ElevenLabs
(`elevenlabs.py`). Strict BYOK : les clés viennent du coffre par org (jamais de clé plateforme).
"""

from __future__ import annotations

from typing import Any

import httpx


class GenerationError(RuntimeError):
    """Échec de génération (clé refusée, réseau, réponse inattendue, modèle inconnu)."""


# Où les providers mettent la phrase qu'un humain doit lire. Essayés dans l'ordre ; la
# première entrée qui donne une chaîne gagne.
_MESSAGE_PATHS: tuple[tuple[str, ...], ...] = (
    ("detail", "message"),   # ElevenLabs
    ("detail",),
    ("error", "message"),
    ("error",),
    ("message",),
)


def _provider_message(response: httpx.Response) -> str:
    """Les mots du provider, ou un extrait court de ce qu'il a envoyé.

    Les providers s'expliquent correctement — « Free users cannot use library voices via
    the API » dit exactement quoi faire ensuite. Remplacer ça par une phrase générique de
    notre cru jetterait la seule partie de la réponse qui valait d'être lue.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        return " ".join(response.text.split())[:300]

    for path in _MESSAGE_PATHS:
        node: Any = payload
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node.strip():
            return node.strip()[:300]
    return " ".join(response.text.split())[:300]


def raise_for_provider(provider: str, response: httpx.Response) -> None:
    """Transforme le refus d'un provider en erreur qui DIT ce qui s'est passé.

    Remplace `resp.raise_for_status()`, qui lève un `HTTPStatusError` dont il ne restait
    que le nom de la classe — et comme `httpx.HTTPStatusError` hérite de `httpx.HTTPError`,
    il retombait dans le `except` réseau. C'est ainsi qu'un « 402 : Free users cannot use
    library voices via the API » s'affichait « erreur réseau ElevenLabs : HTTPStatusError » :
    la mauvaise cause, et zéro explication. Le code HTTP et la phrase du provider sont ce
    qui permet à quelqu'un d'agir.

    ⚠️ Ne PAS rappeler `raise_for_status()` dans un adaptateur qui utilise ce helper.
    """
    if response.is_success:
        return
    message = _provider_message(response)
    if response.status_code in (401, 403):
        raise GenerationError(
            f"{provider} key rejected ({response.status_code})"
            + (f": {message}" if message else "")
        )
    raise GenerationError(
        f"{provider} responded {response.status_code}" + (f": {message}" if message else "")
    )


def network_error(provider: str, exc: Exception) -> GenerationError:
    """Un échec de TRANSPORT — et rien d'autre.

    `httpx.RequestError` couvre ce que « réseau » veut dire : DNS, TCP, TLS, délai. Un
    `HTTPStatusError` n'y a pas sa place : le serveur a répondu, il a répondu non.
    """
    return GenerationError(f"{provider} network error: {exc.__class__.__name__}")
