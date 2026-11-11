"""Décodage matériel par player — matrice de capacités & conformité (pré-Sprint 2.7).

Le player auto-détecte à l'install (`install.sh`/`install.ps1` → `capabilities.json`)
le décodage vidéo matériel disponible (VA-API Intel/AMD, V4L2 RPi, D3D11 Windows,
MediaCodec Android) et remonte le bloc `capabilities` au serveur dans son heartbeat
`player:status`. Ce module exploite cette donnée pour :

- `decode_health(player)` : déterminer si un player décode en matériel ou en logiciel,
  et lever les alertes de conformité (un CPU faible en software ne tient pas le 4K).
- `media_decode_risk(...)` : verdict 🟢/🟡/🔴 sur la diffusabilité d'un média donné sur
  un player donné (alimente le futur check passthrough / sync-readiness §XVI.2).

Aucune dépendance DB : fonctions pures sur l'objet `CmsPlayer`. Constat terrain de
référence (riri/fifi, Intel Celeron J4025 / UHD600) : software → plafond ≈ FHD 30 fps ;
4K ou 60 fps → décodage HW obligatoire. Cf. EVOLUTIONS.md « Décodage matériel auto-détecté ».
"""

from __future__ import annotations

from typing import Any, Optional

# Plafond de décodage logiciel (sans accélération matérielle) tenable par un CPU
# d'entrée de gamme type Celeron J4025. Au-delà : saccade + dérive de synchro.
SW_MAX_WIDTH = 1920
SW_MAX_HEIGHT = 1080
SW_MAX_FPS = 30
SW_CEILING_LABEL = "FHD 30 fps"

# Méthodes de décodage matériel attendues par famille d'OS (info indicative pour l'UI).
_EXPECTED_HW_BY_OS = {
    "rpi": "v4l2",
    "linux": "vaapi",
    "windows": "d3d11",
    "android": "mediacodec",
    "chromeos": "vaapi",
}

_VALID_HW_METHODS = {"vaapi", "v4l2", "d3d11", "mediacodec"}

# Méthodes où la plateforme gère le décodage matériel de façon TRANSPARENTE : le player
# n'expose pas (et ne peut pas mesurer) de drapeau `hw_decode_active`, mais le décodage
# est natif matériel. Cas ChromeOS : le `<video>` Chromium décode en HW via la plateforme
# (VAAPI/V4L2 selon le SoC) sans flag à poser. On fait donc confiance à la méthode seule.
_NATIVE_HW_METHODS = {"chromium-native"}

# Toutes les méthodes considérées comme « matériel » pour l'affichage UI.
_ALL_HW_METHODS = _VALID_HW_METHODS | _NATIVE_HW_METHODS

# Codecs décodables en logiciel par Chromium (lentement) — sert à distinguer
# « non lisible du tout » (rouge) de « lisible mais lourd » (ambre).
_SOFTWARE_DECODABLE_CODECS = {"h264", "avc", "hevc", "h265", "vp9", "vp8", "av1"}


def os_family(player_os: Optional[str]) -> str:
    """Normalise `player_os` (ex. 'linux-arm64', 'windows-x64') en famille."""
    if not player_os:
        return "unknown"
    p = player_os.lower()
    if "android" in p:
        return "android"
    if "win" in p:
        return "windows"
    if "chromeos" in p or "chrome_extension" in p:
        return "chromeos"
    if "brightsign" in p:
        return "brightsign"
    if "rpi" in p or "raspberry" in p:
        return "rpi"
    if "linux" in p:
        return "linux"
    return "unknown"


def expected_hw_method(player_os: Optional[str]) -> Optional[str]:
    return _EXPECTED_HW_BY_OS.get(os_family(player_os))


def decode_health(player: Any) -> dict:
    """Synthèse de l'état de décodage d'un player + alertes de conformité.

    Renvoie un dict sérialisable :
      effective_decode : 'hardware' | 'software' | 'unknown'
      method           : méthode HW effective (vaapi/v4l2/...) ou None
      severity         : 'ok' | 'warning' | 'unknown'
      sw_ceiling       : libellé du plafond logiciel
      warnings         : liste de messages (vide si tout va bien)
    """
    caps = player.capabilities if isinstance(getattr(player, "capabilities", None), dict) else {}
    method = caps.get("hw_decode")
    active = caps.get("hw_decode_active")
    sf_mode = caps.get("sf_hw_decode", "auto")
    warnings: list[str] = []

    if not caps:
        return {
            "effective_decode": "unknown",
            "method": None,
            "severity": "unknown",
            "sw_ceiling": SW_CEILING_LABEL,
            "gpu_vendor": None,
            "gpu_model": None,
            "warnings": ["Capabilities not yet reported by the player."],
        }

    if sf_mode == "off":
        effective = "software"
        warnings.append("Hardware decoding manually disabled (SF_HW_DECODE=off).")
    elif method in _NATIVE_HW_METHODS:
        # Plateforme à décodage natif (ChromeOS) : matériel par construction, pas de
        # drapeau `hw_decode_active` à attendre (le navigateur ne l'expose pas).
        effective = "hardware"
    elif active is True and method in _VALID_HW_METHODS:
        effective = "hardware"
    elif active is False:
        effective = "software"
        if method in _VALID_HW_METHODS:
            warnings.append(
                f"Compatible {method} GPU detected but hardware decoding is NOT "
                "active (the player is using software decoding)."
            )
        else:
            warnings.append("No hardware decoding available — software decoding.")
    else:
        effective = "unknown"
        warnings.append("Hardware decoding state unknown on this player.")

    if effective == "software":
        warnings.append(
            f"Software ceiling ≈ {SW_CEILING_LABEL}: 4K or 60 fps media will "
            "stutter and may desynchronize a video wall."
        )

    severity = {"hardware": "ok", "software": "warning", "unknown": "unknown"}[effective]
    return {
        "effective_decode": effective,
        "method": method if method in _ALL_HW_METHODS else None,
        "severity": severity,
        "sw_ceiling": SW_CEILING_LABEL,
        "gpu_vendor": caps.get("gpu_vendor"),
        "gpu_model": caps.get("gpu_model"),
        "warnings": warnings,
    }


def _exceeds_software_ceiling(width: int, height: int, fps: float) -> bool:
    return width > SW_MAX_WIDTH or height > SW_MAX_HEIGHT or fps > SW_MAX_FPS


def media_decode_risk(
    player: Any,
    width: Optional[int],
    height: Optional[int],
    fps: Optional[float],
    codec: Optional[str] = None,
) -> dict:
    """Verdict de diffusabilité d'un média sur un player (advisory).

    verdict : 'green' (sync-ready) | 'amber' (lisible non garanti) | 'red' (non conforme)
    """
    codec_norm = (codec or "").lower().strip()
    w = int(width or 0)
    h = int(height or 0)
    f = float(fps or 0)

    health = decode_health(player)
    effective = health["effective_decode"]

    # Codec non décodable même en logiciel → rouge franc.
    if codec_norm and codec_norm not in _SOFTWARE_DECODABLE_CODECS:
        return {
            "verdict": "red",
            "reason": f"Codec '{codec_norm}' not supported by the player.",
            "effective_decode": effective,
        }

    # Décodage matériel actif → le player encaisse 4K/60 (selon profils détectés).
    if effective == "hardware":
        return {
            "verdict": "green",
            "reason": "Hardware decoding active.",
            "effective_decode": effective,
        }

    # À partir d'ici : software ou inconnu.
    if not _exceeds_software_ceiling(w, h, f):
        verdict = "green" if effective != "unknown" else "amber"
        return {
            "verdict": verdict,
            "reason": f"Within the software ceiling ({SW_CEILING_LABEL}).",
            "effective_decode": effective,
        }

    # Dépasse le plafond logiciel : rouge si 4K ET haut framerate (quasi injouable
    # fluide en software), ambre sinon (lecture possible mais non garantie sans saccade).
    is_4k = w > 2560 or h > 1440
    if is_4k and f > SW_MAX_FPS:
        verdict = "red"
        reason = "4K above 30 fps with software decoding: stutter/desync almost certain."
    else:
        verdict = "amber"
        reason = (
            f"Above the software ceiling ({SW_CEILING_LABEL}) without hardware "
            "decoding: playback possible but not guaranteed stutter-free."
        )
    return {"verdict": verdict, "reason": reason, "effective_decode": effective}
