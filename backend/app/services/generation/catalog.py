"""Catalogue des modèles de génération IA — SOURCE DE VÉRITÉ UNIQUE.

Chaque modèle déclare : `kind` (image/video/music/voice), `provider`, libellé, **palier de
coût** (`tier` low|moderate|high → badge + avertissement UI), un **tarif** (`pricing` : mode
+ prix USD) et un **schéma de params** (`params`) que le frontend rend en contrôles dynamiques.

Le même `estimate_cost()` sert au frontend (via l'endpoint `/generation/catalog`, qui expose
le `pricing`) et au backend (coût réellement enregistré sur le job). Les ids de modèles, prix
et noms de champs providers sont **indicatifs / best-effort** (confirmés au 1er test live) —
cohérent avec la politique du projet (cf. CLAUDE.md « ids/prix de modèles best-effort »).

Modes de tarif (`pricing.mode`) :
  - `flat` / `per_image` / `per_video` : prix fixe par génération
  - `per_megapixel` : prix × mégapixels (image, déduits du ratio + résolution choisie)
  - `per_second`    : prix × durée (vidéo/musique, param marqué `affects_cost="seconds"`)
  - `per_1k_chars`  : prix × (longueur du texte / 1000) (voix TTS)

Schéma d'un param (rendu générique côté UI) :
  { key, control: select|number|text, label, default, options?, min?, max?, step?,
    affects_cost?: "seconds"|"megapixels", unit?, inject_tag?: bool, optional?: bool }
"""

from __future__ import annotations

# Dimensions de base (~1 Mpx) par ratio → estimatif mégapixels + dims explicites image.
RATIO_DIMS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1152, 896),
    "3:4": (896, 1152),
}
# Facteur d'échelle linéaire par palier de résolution (×1.5 ≈ ×2.25 mégapixels).
RES_SCALE: dict[str, float] = {"standard": 1.0, "high": 1.5}

# ── Schémas de params réutilisables ───────────────────────────────────────────

_RATIO_OPTIONS = [{"value": r, "label": f"ratio_{r.replace(':', '_')}"} for r in RATIO_DIMS]

_P_ASPECT = {
    "key": "aspect_ratio", "control": "select", "label": "ratio",
    "default": "16:9", "options": _RATIO_OPTIONS, "affects_cost": "megapixels",
}
_P_RESOLUTION = {
    "key": "resolution", "control": "select", "label": "resolution", "default": "standard",
    "options": [{"value": "standard", "label": "res_standard"},
                {"value": "high", "label": "res_high"}],
    "affects_cost": "megapixels",
}
_P_SEED = {"key": "seed", "control": "number", "label": "seed", "default": None,
           "min": 0, "max": 2_147_483_647, "step": 1, "optional": True}
# Force de transformation image→image (0 = identique à la référence, 1 = libre).
_P_STRENGTH = {"key": "strength", "control": "number", "label": "strength",
               "default": 0.85, "min": 0.1, "max": 1.0, "step": 0.05}
# Résolution vidéo (qualité ; n'entre pas dans l'estimatif — c'est la DURÉE le levier de coût).
_P_VIDEO_RES = {
    "key": "resolution", "control": "select", "label": "resolution", "default": "720p",
    "options": [{"value": "480p", "label_literal": "480p"},
                {"value": "720p", "label_literal": "720p"},
                {"value": "1080p", "label_literal": "1080p"}],
}

# Voix curées (genre/caractère). fal eleven-v3 prend un NOM (`voice`) ; ElevenLabs direct un id.
# ⚠️ fal valide le nom serveur-side (422 « Voice not found ») : les anciennes premade ElevenLabs
# (Bella/Elli/Antoni/Josh/Adam) n'existent plus — les 7 ci-dessous sondées OK le 2026-07-17.
_VOICES_FAL = [
    {"value": "Rachel", "label": "voice_rachel"},
    {"value": "Sarah", "label": "voice_sarah"},
    {"value": "Aria", "label": "voice_aria"},
    {"value": "Lily", "label": "voice_lily"},
    {"value": "George", "label": "voice_george"},
    {"value": "Brian", "label": "voice_brian"},
    {"value": "Daniel", "label": "voice_daniel"},
]
# ⚠️ REPLI DE DERNIER RECOURS, et rien de plus. Un identifiant de voix n'est pas une donnée
# de catalogue : il appartient au COMPTE dont la clé paie. L'endpoint `/generation/catalog`
# remplace cette liste par celle du compte (`with_account_voices` + `elevenlabs.list_voices`) ;
# elle ne sert que si l'appel échoue — et un formulaire qui s'ouvre avec la mauvaise voix vaut
# mieux qu'un formulaire qui ne s'ouvre pas.
# 🔴 Les 5 autres premade historiques (Bella/Elli/Antoni/Josh/Adam) ont été RETIRÉES le
# 2026-08-04 : on avait mesuré le 2026-07-17 qu'elles n'existent plus (fal : « Voice not
# found »), et on n'avait corrigé que `_VOICES_FAL` — cette liste-ci continuait de proposer
# cinq voix mortes, dont le refus n'arrivait qu'après la rédaction du texte.
_VOICES_ELEVEN = [
    {"value": "21m00Tcm4TlvDq8ikWAM", "label": "voice_rachel"},
]
_LANGS = ("en", "fr", "es", "de", "it", "pt")
_LANGUAGE_OPTIONS = [{"value": v, "label": f"lang_{v}"} for v in _LANGS]
_TONE_OPTIONS = [
    {"value": "", "label": "tone_neutral"},
    {"value": "warm", "label": "tone_warm"},
    {"value": "enthusiastic", "label": "tone_enthusiastic"},
    {"value": "calm", "label": "tone_calm"},
    {"value": "serious", "label": "tone_serious"},
]


def _voice_params(voice_options: list[dict], voice_key: str) -> list[dict]:
    return [
        {"key": voice_key, "control": "select", "label": "voice",
         "default": voice_options[0]["value"], "options": voice_options},
        {"key": "language_code", "control": "select", "label": "language",
         "default": "en", "options": _LANGUAGE_OPTIONS, "optional": True},
        {"key": "tone", "control": "select", "label": "tone", "default": "",
         "options": _TONE_OPTIONS, "inject_tag": True, "optional": True},
    ]


def _duration_param(default: int, choices: list[int]) -> dict:
    return {"key": "duration", "control": "select", "label": "duration", "default": str(default),
            "options": [{"value": str(c), "label_literal": f"{c}s"} for c in choices],
            "affects_cost": "seconds", "unit": "s"}


# ── CATALOGUE ──────────────────────────────────────────────────────────────────

MODELS: list[dict] = [
    # ---- IMAGE (fal, synchrone) ----
    {"id": "fal-ai/flux/schnell", "kind": "image", "provider": "fal", "label": "FLUX schnell",
     "tier": "low", "pricing": {"mode": "per_megapixel", "usd": 0.003},
     "params": [_P_ASPECT, _P_RESOLUTION, _P_SEED]},
    {"id": "fal-ai/flux/dev", "kind": "image", "provider": "fal", "label": "FLUX dev",
     "tier": "moderate", "pricing": {"mode": "per_megapixel", "usd": 0.025},
     "supports_negative_prompt": True,
     "params": [_P_ASPECT, _P_RESOLUTION,
                {"key": "num_inference_steps", "control": "number", "label": "steps",
                 "default": 28, "min": 1, "max": 50, "step": 1},
                {"key": "guidance_scale", "control": "number", "label": "guidance",
                 "default": 3.5, "min": 1, "max": 20, "step": 0.5, "optional": True}, _P_SEED]},
    {"id": "fal-ai/qwen-image", "kind": "image", "provider": "fal", "label": "Qwen Image",
     "tier": "moderate", "pricing": {"mode": "per_megapixel", "usd": 0.02},
     "supports_negative_prompt": True,
     "params": [_P_ASPECT, _P_RESOLUTION, _P_SEED]},
    {"id": "fal-ai/recraft-v3", "kind": "image", "provider": "fal", "label": "Recraft v3",
     "tier": "moderate", "pricing": {"mode": "per_megapixel", "usd": 0.04},
     "params": [_P_ASPECT, _P_RESOLUTION,
                {"key": "style", "control": "select", "label": "style",
                 "default": "realistic_image",
                 "options": [{"value": "realistic_image", "label": "style_realistic"},
                             {"value": "digital_illustration", "label": "style_illustration"},
                             {"value": "vector_illustration", "label": "style_vector"}]}]},
    {"id": "fal-ai/bytedance/seedream/v4/text-to-image", "kind": "image", "provider": "fal",
     "label": "Seedream v4", "tier": "high", "pricing": {"mode": "per_megapixel", "usd": 0.03},
     "params": [_P_ASPECT, _P_RESOLUTION, _P_SEED]},

    # ---- IMAGE → IMAGE (référence requise) ----
    {"id": "fal-ai/flux/dev/image-to-image", "kind": "image", "provider": "fal",
     "label": "FLUX dev (image→image)", "tier": "moderate",
     "pricing": {"mode": "per_megapixel", "usd": 0.025}, "supports_negative_prompt": True,
     "input": "image", "ref_field": "image_url", "ref_multi": False, "max_refs": 1,
     "params": [_P_STRENGTH, _P_SEED]},
    {"id": "fal-ai/nano-banana/edit", "kind": "image", "provider": "fal",
     "label": "Nano Banana (instruction-based editing)", "tier": "high",
     "pricing": {"mode": "per_image", "usd": 0.0398},  # fal : facturé par image
     "input": "image", "ref_field": "image_urls", "ref_multi": True, "max_refs": 4,
     "op": "edit", "params": [_P_SEED]},

    # ---- ÉDITION / RETOUCHE (transforme une image existante, pas de prompt ni de canvas) ----
    # Détourage : enlève le fond → PNG transparent. Aucun prompt/canvas (le modèle préserve les
    # dimensions). `prompt_optional`+`no_image_size` pilotent fal.py (payload réduit) et l'UI.
    {"id": "fal-ai/birefnet", "kind": "image", "provider": "fal",
     "label": "Background removal", "tier": "low",
     "pricing": {"mode": "flat", "usd": 0.01},
     "input": "image", "ref_field": "image_url", "ref_multi": False, "max_refs": 1,
     "op": "edit", "prompt_optional": True, "no_image_size": True, "params": []},

    # ---- VIDÉO (fal, asynchrone) ----
    {"id": "fal-ai/ltx-video", "kind": "video", "provider": "fal", "label": "LTX Video",
     "tier": "low", "pricing": {"mode": "per_video", "usd": 0.02}, "supports_aspect_ratio": False,
     "params": [_duration_param(5, [5, 8, 10]), _P_VIDEO_RES]},
    {"id": "fal-ai/minimax/video-01", "kind": "video", "provider": "fal",
     "label": "MiniMax / Hailuo", "tier": "moderate",
     "pricing": {"mode": "per_second", "usd": 0.08}, "supports_aspect_ratio": True,
     "supports_negative_prompt": True,
     "params": [_duration_param(6, [6]), _P_VIDEO_RES]},
    {"id": "fal-ai/wan-25-preview/text-to-video", "kind": "video", "provider": "fal",
     "label": "Wan 2.5", "tier": "high",
     # 0,15 $/s = facture REELLE relevée par Ed (0,75 $ / 5 s, 2026-07-31) au défaut du
     # studio (720p). fal tarife par seconde ET par palier de résolution (0,05 $/s en
     # 480p) ; l'estimateur ne modélise pas la résolution, on cale sur le défaut.
     # L'ancien forfait supposé de 3 $/clip surestimait d'un facteur 4.
     "pricing": {"mode": "per_second", "usd": 0.15},
     "supports_aspect_ratio": True, "supports_negative_prompt": True,
     "params": [_duration_param(5, [5, 8, 10]), _P_VIDEO_RES]},
    {"id": "fal-ai/kling-video/v2/master/text-to-video", "kind": "video", "provider": "fal",
     # 0,28 $/s = facture REELLE relevée par Ed (1,40 $ / 5 s, 2026-07-31) — l'ancien
     # 0,14 $/s était le tarif du Kling standard, pas du master.
     "label": "Kling v2 master", "tier": "high", "pricing": {"mode": "per_second", "usd": 0.28},
     "supports_aspect_ratio": True, "supports_negative_prompt": True, "max_wait_s": 1800,
     "params": [_duration_param(5, [5, 10]), _P_VIDEO_RES]},

    # ---- IMAGE → VIDÉO (référence requise : anime une image) ----
    {"id": "fal-ai/ltx-video/image-to-video", "kind": "video", "provider": "fal",
     "label": "LTX (image→video)", "tier": "low", "pricing": {"mode": "per_video", "usd": 0.02},
     "supports_aspect_ratio": False, "input": "image", "ref_field": "image_url",
     "ref_multi": False, "max_refs": 1, "params": [_duration_param(5, [5, 8, 10]), _P_VIDEO_RES]},
    {"id": "fal-ai/wan-25-preview/image-to-video", "kind": "video", "provider": "fal",
     "label": "Wan 2.5 (image→video)", "tier": "high",
     # 0,15 $/s = facture REELLE relevée par Ed (job 23 : 0,75 $ / 5 s, 2026-07-31) au
     # défaut 720p du studio — cf. le commentaire de l'entrée text-to-video.
     "pricing": {"mode": "per_second", "usd": 0.15},
     "supports_aspect_ratio": True, "supports_negative_prompt": True,
     "input": "image", "ref_field": "image_url", "ref_multi": False, "max_refs": 1,
     "params": [_duration_param(5, [5, 8, 10]), _P_VIDEO_RES]},
    {"id": "fal-ai/kling-video/v2/master/image-to-video", "kind": "video", "provider": "fal",
     "label": "Kling v2 (image→video)", "tier": "high",
     # 0,28 $/s = facture REELLE relevée par Ed (1,40 $ / 5 s, job 24 du 2026-07-31).
     "pricing": {"mode": "per_second", "usd": 0.28}, "supports_negative_prompt": True,
     "supports_aspect_ratio": True, "input": "image", "ref_field": "image_url",
     "ref_multi": False, "max_refs": 1, "max_wait_s": 1800,
     "params": [_duration_param(5, [5, 10]), _P_VIDEO_RES]},

    # ---- MUSIQUE (fal, asynchrone) ----
    {"id": "fal-ai/stable-audio", "kind": "music", "provider": "fal", "label": "Stable Audio",
     "tier": "low", "pricing": {"mode": "per_second", "usd": 0.0006},  # fal ~$0.000575/s calcul
     "params": [{"key": "seconds_total", "control": "number", "label": "music_duration",
                 "default": 30, "min": 1, "max": 47, "step": 1, "unit": "s",
                 "affects_cost": "seconds"}]},
    {"id": "cassetteai/music-generator", "kind": "music", "provider": "fal",
     "label": "CassetteAI Music", "tier": "low", "pricing": {"mode": "per_second", "usd": 0.001},
     "params": [{"key": "duration", "control": "number", "label": "music_duration",
                 "default": 30, "min": 1, "max": 180, "step": 1, "unit": "s",
                 "affects_cost": "seconds"}]},

    # ---- VOIX (fal eleven-v3 + gemini/f5) ----
    {"id": "fal-ai/elevenlabs/tts/eleven-v3", "kind": "voice", "provider": "fal",
     "label": "ElevenLabs v3 (via fal)", "tier": "low",
     "pricing": {"mode": "per_1k_chars", "usd": 0.10},  # fal : $0.10 / 1000 caractères
     "params": _voice_params(_VOICES_FAL, "voice")},
    # gemini-tts : PAS de param langue — l'enum fal = noms complets ("French (France)"),
    # un code ISO est rejeté 422 ; Gemini auto-détecte la langue du texte. Ton → style_instructions.
    {"id": "fal-ai/gemini-tts", "kind": "voice", "provider": "fal", "label": "Gemini TTS",
     # fal facture 1 $/MILLION de tokens (facture réelle d'Ed, 2026-07-31) ; à ~4
     # caractères/token cela fait ~0,00025 $/1000 caractères — l'ancien 0,01 $/1k
     # surestimait ×40 (des centimes, mais l'estimation doit dire vrai).
     "tier": "low", "pricing": {"mode": "per_1k_chars", "usd": 0.00025},
     "params": [{"key": "tone", "control": "select", "label": "tone", "default": "",
                 "options": _TONE_OPTIONS, "optional": True}]},
    # f5-tts retiré du catalogue : modèle de CLONAGE (ref_audio_url OBLIGATOIRE) — inutilisable
    # sans audio de référence, toute génération partait en 422.

    # ---- VOIX / MUSIQUE (ElevenLabs direct) ----
    {"id": "elevenlabs/tts", "kind": "voice", "provider": "elevenlabs",
     "label": "ElevenLabs (voice)", "tier": "moderate",
     "pricing": {"mode": "per_1k_chars", "usd": 0.10},
     "params": _voice_params(_VOICES_ELEVEN, "voice_id")},
    {"id": "elevenlabs/music", "kind": "music", "provider": "elevenlabs",
     "label": "ElevenLabs (music)", "tier": "moderate",
     "pricing": {"mode": "per_second", "usd": 0.003},
     "params": [{"key": "music_length_ms", "control": "number", "label": "music_duration",
                 "default": 30, "min": 5, "max": 120, "step": 1, "unit": "s", "scale_to_ms": True,
                 "affects_cost": "seconds"}]},
]

_BY_ID = {m["id"]: m for m in MODELS}


def get_model(model_id: str) -> dict | None:
    return _BY_ID.get(model_id)


def models_for(kind: str | None = None, providers: set[str] | None = None) -> list[dict]:
    """Modèles filtrés par `kind` et/ou par providers disponibles (clés présentes)."""
    out = []
    for m in MODELS:
        if kind and m["kind"] != kind:
            continue
        if providers is not None and m["provider"] not in providers:
            continue
        out.append(m)
    return out


def default_model(kind: str, providers: set[str] | None = None) -> str | None:
    avail = models_for(kind, providers)
    return avail[0]["id"] if avail else None


# Param portant la voix ElevenLabs, dans le vocabulaire du catalogue.
_VOICE_PARAM_KEY = "voice_id"


def with_account_voices(models: list[dict], voices: list[dict]) -> list[dict]:
    """Remplace la liste de voix figée du catalogue par celle du COMPTE.

    Le catalogue est de la donnée qu'on maîtrise, mais un identifiant de voix ne l'est pas
    au même titre : il appartient à celui dont la clé paie. Livrer une liste figée revient à
    proposer des voix que l'offre du client refuse — et à les refuser **au moment de la
    génération**, une fois le texte écrit (HTTP 402, « Free users cannot use library voices
    via the API »).

    `label_literal` et non `label` : ce sont de vrais noms venus du compte, pas des clés
    ayant une traduction derrière elles.

    Une liste vide ne change rien : un provider lent ou en panne ne doit pas coûter son
    formulaire à l'opérateur. ⚠️ Ne mute pas `models` (le catalogue est un module partagé) —
    les modèles touchés sont recopiés.
    """
    if not voices:
        return models
    options = [
        {"value": v["voice_id"],
         "label_literal": v["name"] + (f" ({v['category']})" if v.get("category") else "")}
        for v in voices if v.get("voice_id")
    ]
    if not options:
        return models

    out: list[dict] = []
    for model in models:
        if model.get("provider") != "elevenlabs":
            out.append(model)
            continue
        touched = False
        params = []
        for param in model.get("params", []):
            if param.get("key") == _VOICE_PARAM_KEY:
                # Le défaut retenu au catalogue peut ne pas exister sur ce compte.
                param = {**param, "options": options, "default": options[0]["value"]}
                touched = True
            params.append(param)
        out.append({**model, "params": params} if touched else model)
    return out


# ── Estimatif de coût (partagé frontend via pricing / backend pour le coût réel) ──

def _param_default(model: dict, key: str):
    for p in model.get("params", []):
        if p["key"] == key:
            return p.get("default")
    return None


def _seconds(model: dict, params: dict) -> float:
    """Durée en secondes pour les tarifs `per_second` (param marqué affects_cost=seconds)."""
    for p in model.get("params", []):
        if p.get("affects_cost") == "seconds":
            raw = params.get(p["key"], p.get("default"))
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = float(p.get("default") or 0)
            if p.get("scale_to_ms"):  # déjà saisi en secondes côté UI → garder
                pass
            return val
    return 0.0


def _megapixels(model: dict, params: dict, aspect_ratio: str | None) -> float:
    ratio = params.get("aspect_ratio") or aspect_ratio or "16:9"
    w, h = RATIO_DIMS.get(ratio, RATIO_DIMS["16:9"])
    scale = RES_SCALE.get(params.get("resolution") or "standard", 1.0)
    return round((w * h * scale * scale) / 1_000_000, 3)


def estimate_cost(
    model_id: str, *, params: dict | None = None,
    aspect_ratio: str | None = None, text: str | None = None,
) -> float:
    """Coût USD estimé pour une génération (mêmes règles côté UI et côté backend)."""
    m = get_model(model_id)
    if not m:
        return 0.0
    pricing = m["pricing"]
    mode, usd = pricing["mode"], float(pricing["usd"])
    p = params or {}
    if mode in ("flat", "per_image", "per_video"):
        return round(usd, 4)
    if mode == "per_megapixel":
        return round(usd * _megapixels(m, p, aspect_ratio), 4)
    if mode == "per_second":
        return round(usd * max(_seconds(m, p), 1.0), 4)
    if mode == "per_1k_chars":
        chars = len((text or "").strip())
        return round(usd * max(chars, 1) / 1000, 4)
    return round(usd, 4)
