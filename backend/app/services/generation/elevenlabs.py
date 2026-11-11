"""Génération audio via ElevenLabs (§ génération — voix/musique, clients).

Voix (TTS) : POST /v1/text-to-speech/{voice_id} (header `xi-api-key`) → bytes audio/mpeg.
Musique : POST /v1/music (best-effort) → bytes. Strict BYOK : la clé vient du coffre org.
httpx direct (0 dépendance). Toute erreur → GenerationError.

Ce qui dépend du COMPTE se lit sur le compte : les voix (`list_voices`) et le quota
(`get_quota`) appartiennent à celui dont la clé paie, et **rien n'en est stocké** — un
instantané se périme à la première remise à zéro mensuelle, et à tout changement d'offre,
c'est-à-dire pile au moment où l'on voudrait que la plateforme s'en aperçoive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.services.generation import GenerationError, network_error, raise_for_provider

API_BASE = "https://api.elevenlabs.io/v1"

# Voix publique par défaut + modèle multilingue (réglables via params).
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"   # « Rachel » (voix publique standard)
DEFAULT_TTS_MODEL = "eleven_multilingual_v2"

# Prix indicatifs (ESTIMÉS) pour le suivi de coût.
_VOICE_USD_PER_1K_CHARS = 0.10
_MUSIC_USD_PER_MIN = 0.20


def resolve_default_voice(api_key: str) -> str:
    """La voix par défaut appartient au COMPTE, pas au code.

    Mesuré sur la production le 2026-08-04 : `DEFAULT_VOICE_ID` (« Rachel ») **n'est pas
    dans les 21 voix du compte**, et l'offre y est `free` — donc tout appel sans voix
    explicite partait sur une voix de bibliothèque que le plan refuse (402). C'est
    exactement ce qui mettait **la réponse vocale de Jeanine en panne silencieuse** :
    `messaging_gateway` appelle `generate_voice` sans `voice_id`, l'échec est best-effort,
    et le message disait « erreur réseau ».

    Le repli codé en dur reste, pour le cas où la liste est illisible : mieux vaut tenter
    une voix qui peut être refusée que ne pas parler du tout.
    """
    try:
        voices = list_voices(api_key=api_key)
    except Exception:  # noqa: BLE001 — la synthèse ne doit pas échouer sur ce détour
        return DEFAULT_VOICE_ID
    return voices[0]["voice_id"] if voices else DEFAULT_VOICE_ID


def generate_voice(
    *, text: str, api_key: str, voice_id: str | None = None,
    model_id: str = DEFAULT_TTS_MODEL, timeout: float = 60.0,
) -> tuple[bytes, str, float]:
    """Synthèse vocale → (bytes audio/mpeg, content_type, cost_usd).

    Sans `voice_id`, la voix est résolue **sur le compte** (cf. `resolve_default_voice`) —
    un identifiant en dur appartient à un compte qui n'est pas forcément celui qui paie.
    """
    vid = voice_id or resolve_default_voice(api_key)
    headers = {"xi-api-key": api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as http:
            resp = http.post(
                f"{API_BASE}/text-to-speech/{vid}",
                headers=headers,
                json={"text": text, "model_id": model_id},
            )
            raise_for_provider("ElevenLabs", resp)
            content = resp.content
            ctype = resp.headers.get("content-type", "audio/mpeg").split(";")[0].strip()
    except httpx.RequestError as exc:
        raise network_error("ElevenLabs", exc) from exc
    if not content:
        raise GenerationError("empty ElevenLabs response")
    cost = round(len(text) / 1000 * _VOICE_USD_PER_1K_CHARS, 4)
    return content, ctype or "audio/mpeg", cost


def generate_music(
    *, prompt: str, api_key: str, params: dict | None = None, timeout: float = 180.0,
) -> tuple[bytes, str, float]:
    """Génération musicale → (bytes, content_type, cost_usd). Best-effort (API Music récente).

    Les valeurs vides ne sont PAS transmises : un champ vide envoyé au provider, c'est un
    refus qu'on s'inflige.
    """
    body: dict = {"prompt": prompt}
    if params:
        body.update({k: v for k, v in params.items() if v not in (None, "")})
    length_ms = int(body.get("music_length_ms") or 30000)
    headers = {"xi-api-key": api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as http:
            resp = http.post(f"{API_BASE}/music", headers=headers, json=body)
            raise_for_provider("ElevenLabs", resp)
            content = resp.content
            ctype = resp.headers.get("content-type", "audio/mpeg").split(";")[0].strip()
    except httpx.RequestError as exc:
        raise network_error("ElevenLabs", exc) from exc
    if not content:
        raise GenerationError("empty ElevenLabs response")
    cost = round(length_ms / 60000 * _MUSIC_USD_PER_MIN, 4)
    return content, ctype or "audio/mpeg", cost


def list_voices(*, api_key: str, timeout: float = 8.0) -> list[dict]:
    """Les voix que CE compte peut réellement utiliser.

    Le catalogue embarque une liste d'identifiants de voix figée, et cette liste est fausse
    pour tout compte autre que celui contre lequel elle a été écrite : ElevenLabs traite la
    plupart d'entre elles comme des voix de « bibliothèque », qu'une offre gratuite se voit
    refuser (402) **au moment de la génération** — après que la personne a écrit son texte
    et appuyé sur le bouton. La liste du compte est la seule qui vaille d'être proposée.
    """
    with httpx.Client(timeout=timeout) as http:
        resp = http.get(f"{API_BASE}/voices", headers={"xi-api-key": api_key})
        raise_for_provider("ElevenLabs", resp)
        payload = resp.json()
    voices = payload.get("voices") if isinstance(payload, dict) else None
    if not isinstance(voices, list):
        return []
    return [
        {
            "voice_id": v.get("voice_id"),
            "name": v.get("name") or v.get("voice_id"),
            "category": v.get("category") or "",
        }
        for v in voices
        if isinstance(v, dict) and v.get("voice_id")
    ]


@dataclass(frozen=True)
class Quota:
    """Ce qu'une offre ElevenLabs permet encore, dans SON unité : des caractères."""

    tier: str
    used: int
    limit: int
    resets_at: datetime | None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def read_quota(payload: dict) -> Quota | None:
    """Construit un Quota depuis un corps `/v1/user`, ou None s'il n'en dit pas assez.

    None veut dire « je ne sais pas », jamais « il ne reste rien » : un corps qu'on ne sait
    pas lire ne doit JAMAIS se transformer en refus. Seul un nombre qu'on tient réellement
    peut en justifier un.
    """
    sub = payload.get("subscription") if isinstance(payload, dict) else None
    if not isinstance(sub, dict):
        return None
    limit, used = sub.get("character_limit"), sub.get("character_count")
    if not isinstance(limit, int) or not isinstance(used, int) or limit <= 0:
        return None
    reset = sub.get("next_character_count_reset_unix")
    resets_at = (datetime.fromtimestamp(reset, tz=UTC)
                 if isinstance(reset, (int, float)) and reset > 0 else None)
    return Quota(tier=str(sub.get("tier") or "?"), used=used, limit=limit, resets_at=resets_at)


def get_quota(*, api_key: str, timeout: float = 8.0) -> Quota | None:
    """Les caractères restants du compte, ou None si la question ne peut pas être tranchée
    maintenant (pas de réseau, provider indisponible, corps inattendu)."""
    with httpx.Client(timeout=timeout) as http:
        resp = http.get(f"{API_BASE}/user", headers={"xi-api-key": api_key})
        raise_for_provider("ElevenLabs", resp)
        return read_quota(resp.json())
