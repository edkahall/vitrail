"""Conformité & sync-readiness d'un média — check passthrough (§XVI.2 / EVOLUTIONS).

Quand un média est uploadé en mode `passthrough` (fichier conservé tel quel, sans
ré-encodage — cas mur d'images où le ré-encodage casse l'alignement frame-accurate),
ce service produit un **verdict intrinsèque** à partir du probe ffprobe enrichi :

  - **Diffusabilité** : le média se lit-il sans risque ? (codec connu, bitrate ≤ plafond)
  - **Sync-readiness** : peut-il se diffuser en synchro LAN (§2b) tel quel ?
    CFR (framerate constant), GOP/keyframes ≤ 2 s, faststart (seek rapide).

Verdict 3 états (calqué sur le badge `sync_ready` des playlists, §LAN Sync #1b) :
  - `ready`      🟢 conforme + synchronisable tel quel
  - `playable`   🟡 se diffuse mais NE se synchronisera PAS à la frame (raisons listées)
  - `nonconform` 🔴 risque de non-lecture (codec non décodable) → ré-encodage requis

Le verdict est **intrinsèque au média** (ne dépend pas d'un player précis). Le risque
de décodage *par player* (selon `player_os`/capabilities) reste l'advisory séparé
`player_capabilities.media_decode_risk`, exposé au moment du scheduling.

Fonctions pures (aucune dépendance DB / ffprobe) : reçoivent le dict `metadata`
produit par `media_worker._probe_full` (+ champs sync enrichis sur `video`).
"""

from __future__ import annotations

from typing import Any, Optional

# Cadence GOP maximale tolérée pour le seek/resync frame-accurate (#2b).
GOP_MAX_SEC = 2.0

# Codecs vidéo décodables par les players (Chromium soft + HW). Hors liste → 🔴.
_KNOWN_CODECS = {
    "h264", "avc", "avc1", "hevc", "h265", "hvc1", "vp9", "vp8", "av1", "av01",
}
_HEVC_CODECS = {"hevc", "h265", "hvc1"}


def _frac(rate: Optional[str]) -> Optional[float]:
    """'30000/1001' → 29.97 ; '25/1' → 25.0 ; None/0 → None."""
    if not rate:
        return None
    try:
        if "/" in str(rate):
            num, den = str(rate).split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


def bitrate_ceiling_kbps(codec: str, width: Optional[int], height: Optional[int]) -> Optional[int]:
    """Plafond de bitrate conseillé par (codec, résolution) — au-delà, risque
    d'underrun du décodeur sur les players faibles (saccade/désync). HEVC ≈ 60 %."""
    pixels = (width or 0) * (height or 0)
    if pixels >= int(3840 * 2160 * 0.8):      # ~4K
        base = 60000
    elif pixels >= int(2560 * 1440 * 0.8):    # ~1440p
        base = 35000
    elif pixels >= int(1920 * 1080 * 0.8):    # ~1080p
        base = 25000
    elif pixels > 0:
        base = 12000
    else:
        return None
    return round(base * 0.6) if (codec or "").lower() in _HEVC_CODECS else base


def is_cfr(r_frame_rate: Optional[str], avg_frame_rate: Optional[str]) -> Optional[bool]:
    """CFR si le frame rate déclaré (r) == le frame rate moyen (avg). None si indéterminable."""
    r = _frac(r_frame_rate)
    a = _frac(avg_frame_rate)
    if r is None or a is None:
        return None
    return abs(r - a) <= 0.01 * max(r, a, 1.0)


def evaluate(metadata: dict[str, Any]) -> dict[str, Any]:
    """Verdict de conformité d'un média à partir de son `metadata` (probe enrichi).

    Renvoie un dict sérialisable (stocké dans `metadata.conformance`) :
      { status, checks{codec,bitrate,cfr,gop,faststart}, reasons[], gop_max_sec }
    """
    video = metadata.get("video") or {}
    codec = (video.get("codec") or "").lower().strip()
    reasons: list[str] = []
    checks: dict[str, Any] = {}

    # ── Diffusabilité ────────────────────────────────────────────────────────
    decodable = (not codec) or codec in _KNOWN_CODECS
    checks["codec"] = {"value": codec or None, "ok": decodable}
    if codec and not decodable:
        reasons.append(f"Codec \"{codec}\" is not supported by the players.")

    br = video.get("bitrate_kbps")
    ceil = bitrate_ceiling_kbps(codec, video.get("width"), video.get("height"))
    br_ok = not (br and ceil and br > ceil)
    checks["bitrate"] = {"value_kbps": br, "ceiling_kbps": ceil, "ok": br_ok}
    if not br_ok:
        reasons.append(
            f"Bitrate {br} kbps above the recommended ceiling of {ceil} kbps "
            "(risk of decoder underrun on low-end players)."
        )

    # ── Sync-readiness ───────────────────────────────────────────────────────
    cfr = is_cfr(video.get("r_frame_rate"), video.get("avg_frame_rate"))
    checks["cfr"] = {
        "r_frame_rate": video.get("r_frame_rate"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "ok": cfr,
    }
    if cfr is False:
        reasons.append("Variable frame rate (VFR): frame-accurate synchronized switching will not hold.")

    gop = video.get("gop") or {}
    gop_sec = gop.get("max_gop_sec")
    gop_ok: Optional[bool] = None if gop_sec is None else (gop_sec <= GOP_MAX_SEC + 0.05)
    checks["gop"] = {"max_gop_sec": gop_sec, "max_allowed_sec": GOP_MAX_SEC, "ok": gop_ok}
    if gop_ok is False:
        reasons.append(
            f"Keyframes {gop_sec:.1f} s apart (> {GOP_MAX_SEC:g} s): "
            "imprecise seek/resync, LAN sync not guaranteed."
        )

    faststart = video.get("faststart")
    checks["faststart"] = {"value": faststart, "ok": faststart if faststart is not None else None}
    if faststart is False:
        reasons.append("No faststart (moov atom at end of file): slow start and seek.")

    # ── Verdict ──────────────────────────────────────────────────────────────
    if codec and not decodable:
        status = "nonconform"
    elif (cfr is False) or (gop_ok is False) or (faststart is False) or (not br_ok):
        status = "playable"
    else:
        status = "ready"

    return {
        "status": status,
        "checks": checks,
        "reasons": reasons,
        "gop_max_sec": GOP_MAX_SEC,
    }
