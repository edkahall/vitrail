"""Génération image/vidéo/audio via fal.ai (§ génération de contenu).

Image = appel SYNCHRONE `https://fal.run/{model}` (FLUX & co renvoient direct).
Vidéo/audio = file fal `https://queue.fal.run/{model}` (submit → poll → download), appelée
depuis un worker Celery. Strict BYOK : la clé vient du coffre par org. httpx direct.

Le catalogue (`catalog.py`) est la source de vérité des modèles/params/tarifs ; ce module ne
fait QUE l'appel réseau + le mapping des params vers la forme attendue par chaque modèle.
Toute erreur → `GenerationError` (gérée par l'endpoint / le worker).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from app.services.generation import GenerationError, catalog

FAL_BASE = "https://fal.run"
FAL_QUEUE = "https://queue.fal.run"

DEFAULT_IMAGE_MODEL = catalog.default_model("image", {"fal"})
DEFAULT_VIDEO_MODEL = catalog.default_model("video", {"fal"})
DEFAULT_MUSIC_MODEL = catalog.default_model("music", {"fal"})
DEFAULT_VOICE_MODEL = catalog.default_model("voice", {"fal"})

# Params « UI-only » jamais transmis tels quels au provider (gérés à part).
_UI_ONLY = {"aspect_ratio", "resolution", "tone", "reference_media_ids", "name", "strip_audio"}


@dataclass
class GeneratedImage:
    content: bytes
    content_type: str
    width: int | None
    height: int | None
    cost_usd: float = 0.0


@dataclass
class GeneratedAsset:
    """Résultat d'une génération asynchrone (vidéo/audio) via la file fal."""

    content: bytes
    content_type: str
    cost_usd: float = 0.0
    meta: dict = field(default_factory=dict)


def _clean_params(params: dict | None) -> dict:
    """Retire les clés UI-only et les valeurs nulles avant transmission au provider."""
    return {k: v for k, v in (params or {}).items() if k not in _UI_ONLY and v not in (None, "")}


def _drop_unsupported_negative(payload: dict, model_spec: dict) -> None:
    """Retire `negative_prompt` si le modèle ne le supporte pas (sinon 422 fal)."""
    if "negative_prompt" in payload and not model_spec.get("supports_negative_prompt"):
        payload.pop("negative_prompt", None)


def _require_fal_model(model: str, kind: str) -> dict:
    m = catalog.get_model(model)
    if m is None or m["kind"] != kind or m["provider"] != "fal":
        raise GenerationError(f"unknown fal {kind} model: {model}")
    return m


def _inject_refs(payload: dict, model_spec: dict, image_urls: list[str] | None) -> None:
    """Injecte la/les image(s) de référence (image→image / image→vidéo) dans le payload.
    Le nom du champ + single/liste viennent du catalogue. 400 si le modèle l'exige sans réf."""
    ref_field = model_spec.get("ref_field")
    if not ref_field:
        return
    if not image_urls:
        raise GenerationError("this model requires at least one reference image")
    payload[ref_field] = image_urls if model_spec.get("ref_multi") else image_urls[0]


def _build_image_payload(
    prompt: str, m: dict, aspect_ratio: str, p: dict,
    width: int | None, height: int | None,
) -> dict:
    """Construit le payload image. Les modèles d'ÉDITION (`no_image_size`/`prompt_optional`,
    ex. détourage) n'envoient ni `image_size`/`num_images` ni `prompt` vide — sinon 422 fal."""
    payload: dict = dict(_clean_params(p))
    if not m.get("no_image_size"):
        payload["image_size"] = _image_size(aspect_ratio, p, width, height)
        payload["num_images"] = 1
    if prompt or not m.get("prompt_optional"):
        payload["prompt"] = prompt
    return payload


def _extract_image(data: dict) -> dict:
    """Image du résultat fal — gère `images: [{url}]` (génération) ET `image: {url}` (détourage)."""
    images = data.get("images")
    if not images and isinstance(data.get("image"), dict):
        images = [data["image"]]
    if not images or not images[0].get("url"):
        raise GenerationError("fal response has no image")
    return images[0]


def _image_size(aspect_ratio: str, params: dict, width: int | None, height: int | None):
    """Dimensions image : explicites > palier 'high' (dims calculées) > preset fal (~1 Mpx)."""
    if width and height:
        return {"width": int(width), "height": int(height)}
    base = catalog.RATIO_DIMS.get(aspect_ratio)
    if base and (params.get("resolution") == "high"):
        scale = catalog.RES_SCALE["high"]
        w = int(round(base[0] * scale / 32) * 32)
        h = int(round(base[1] * scale / 32) * 32)
        return {"width": w, "height": h}
    preset = {
        "1:1": "square_hd", "16:9": "landscape_16_9", "9:16": "portrait_16_9",
        "4:3": "landscape_4_3", "3:4": "portrait_4_3",
    }
    return preset.get(aspect_ratio, "landscape_16_9")


async def generate_image(
    *,
    prompt: str,
    api_key: str,
    model: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = "16:9",
    width: int | None = None,
    height: int | None = None,
    params: dict | None = None,
    image_urls: list[str] | None = None,
    timeout: float = 180.0,
) -> GeneratedImage:
    """Génère une image → bytes + métadonnées + coût estimé. `GenerationError` sinon."""
    m = _require_fal_model(model, "image")
    p = params or {}
    payload = _build_image_payload(prompt, m, aspect_ratio, p, width, height)
    _inject_refs(payload, m, image_urls)
    _drop_unsupported_negative(payload, m)
    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(f"{FAL_BASE}/{model}", headers=headers, json=payload)
            if resp.status_code in (401, 403):
                raise GenerationError("fal.ai key rejected (401/403)")
            resp.raise_for_status()
            data = resp.json()
            img = _extract_image(data)
            dl = await http.get(img["url"])
            dl.raise_for_status()
            content = dl.content
            ctype = (img.get("content_type") or dl.headers.get("content-type", "image/jpeg"))
            ctype = ctype.split(";")[0].strip()
    except httpx.HTTPStatusError as exc:
        raise GenerationError(
            f"fal error {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GenerationError(f"fal network error: {exc.__class__.__name__}") from exc

    cost = catalog.estimate_cost(
        model, params={**p, "aspect_ratio": aspect_ratio}, aspect_ratio=aspect_ratio
    )
    return GeneratedImage(
        content=content,
        content_type=ctype or "image/jpeg",
        width=img.get("width") or width,
        height=img.get("height") or height,
        cost_usd=cost,
    )


def generate_image_sync(
    *,
    prompt: str,
    api_key: str,
    model: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = "16:9",
    params: dict | None = None,
    image_urls: list[str] | None = None,
    timeout: float = 180.0,
) -> GeneratedImage:
    """Variante SYNCHRONE de `generate_image` (appelée depuis le worker — fan-out campagne)."""
    m = _require_fal_model(model, "image")
    p = params or {}
    payload = _build_image_payload(prompt, m, aspect_ratio, p, None, None)
    _inject_refs(payload, m, image_urls)
    _drop_unsupported_negative(payload, m)
    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as http:
            resp = http.post(f"{FAL_BASE}/{model}", headers=headers, json=payload)
            if resp.status_code in (401, 403):
                raise GenerationError("fal.ai key rejected (401/403)")
            resp.raise_for_status()
            data = resp.json()
            img = _extract_image(data)
            dl = http.get(img["url"])
            dl.raise_for_status()
            content = dl.content
            ctype = (img.get("content_type") or dl.headers.get("content-type", "image/jpeg"))
            ctype = ctype.split(";")[0].strip()
    except httpx.HTTPStatusError as exc:
        raise GenerationError(
            f"fal error {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GenerationError(f"fal network error: {exc.__class__.__name__}") from exc
    cost = catalog.estimate_cost(
        model, params={**p, "aspect_ratio": aspect_ratio}, aspect_ratio=aspect_ratio
    )
    return GeneratedImage(
        content=content, content_type=ctype or "image/jpeg",
        width=img.get("width"), height=img.get("height"), cost_usd=cost,
    )


# ── File fal (vidéo / audio) — submit → poll → download (SYNC, worker Celery) ───

def _media_url(data: dict, *keys: str) -> str | None:
    """Extrait l'URL du résultat (formes : {video:{url}}, {video:'url'}, {videos:[{url}]})."""
    for k in keys:
        v = data.get(k)
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("url"):
            return v[0]["url"]
    return None


def _run_queue(model: str, payload: dict, api_key: str, *, kind: str,
               poll_interval: float, max_wait: float, dl_timeout: float) -> tuple[bytes, str, dict]:
    """Soumet à la file fal, attend, télécharge. Renvoie (bytes, content_type, raw_data)."""
    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
    default_ctype = "video/mp4" if kind == "video" else "audio/mpeg"
    try:
        with httpx.Client(timeout=60.0) as http:
            sub = http.post(f"{FAL_QUEUE}/{model}", headers=headers, json=payload)
            if sub.status_code in (401, 403):
                raise GenerationError("fal.ai key rejected (401/403)")
            sub.raise_for_status()
            j = sub.json()
            status_url, response_url = j.get("status_url"), j.get("response_url")
            if not status_url or not response_url:
                raise GenerationError("unexpected fal queue response")
            waited = 0.0
            while True:
                st = http.get(status_url, headers=headers)
                st.raise_for_status()
                status = (st.json().get("status") or "").upper()
                if status == "COMPLETED":
                    break
                if status in ("FAILED", "ERROR", "CANCELLED"):
                    raise GenerationError(f"fal generation {status}")
                if waited >= max_wait:
                    raise GenerationError(f"{kind} generation timed out")
                time.sleep(poll_interval)
                waited += poll_interval
            res = http.get(response_url, headers=headers)
            res.raise_for_status()
            data = res.json()
            url = _media_url(data, "video", "videos", "audio", "audio_url", "audio_file", "output")
            if not url:
                raise GenerationError(f"fal response has no {kind}")
            dl = http.get(url, timeout=dl_timeout)
            dl.raise_for_status()
            content = dl.content
            ctype = dl.headers.get("content-type", default_ctype).split(";")[0].strip()
    except httpx.HTTPStatusError as exc:
        raise GenerationError(
            f"fal error {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GenerationError(f"fal network error: {exc.__class__.__name__}") from exc
    return content, ctype or default_ctype, data


def generate_video_sync(
    *,
    prompt: str,
    api_key: str,
    model: str = DEFAULT_VIDEO_MODEL,
    aspect_ratio: str | None = "16:9",
    params: dict | None = None,
    image_urls: list[str] | None = None,
    poll_interval: float = 5.0,
    max_wait: float = 900.0,
) -> GeneratedAsset:
    """Génère une vidéo via la file fal. SYNCHRONE (worker Celery). `GenerationError` sinon."""
    m = _require_fal_model(model, "video")
    p = params or {}
    payload: dict = {"prompt": prompt, **_clean_params(p)}
    _inject_refs(payload, m, image_urls)
    _drop_unsupported_negative(payload, m)
    if p.get("resolution"):  # champ vidéo réel (UI-only côté image → image_size)
        payload.setdefault("resolution", p["resolution"])
    if aspect_ratio and m.get("supports_aspect_ratio", True):
        payload.setdefault("aspect_ratio", aspect_ratio)
    # Plafond d'attente par modèle (Kling premium est lent → 30 min ; défaut 15 min).
    effective_max_wait = float(m.get("max_wait_s") or max_wait)
    content, ctype, data = _run_queue(
        model, payload, api_key, kind="video",
        poll_interval=poll_interval, max_wait=effective_max_wait, dl_timeout=300.0,
    )
    cost = catalog.estimate_cost(model, params=p, aspect_ratio=aspect_ratio)
    return GeneratedAsset(content=content, content_type=ctype, cost_usd=cost, meta=data)


def generate_audio_sync(
    *,
    kind: str,                 # "music" | "voice"
    prompt: str,               # musique = description ; voix = texte à dire
    api_key: str,
    model: str | None = None,
    params: dict | None = None,
    poll_interval: float = 4.0,
    max_wait: float = 600.0,
) -> GeneratedAsset:
    """Génère un audio via la file fal (mécanique identique à la vidéo). `GenerationError` sinon."""
    mdl = model or (DEFAULT_MUSIC_MODEL if kind == "music" else DEFAULT_VOICE_MODEL)
    _require_fal_model(mdl, kind)
    p = params or {}
    if kind == "voice" and mdl == "fal-ai/gemini-tts":
        # gemini-tts : champ 'prompt' (pas 'text') + ton natif via style_instructions ;
        # jamais de language_code (enum fal = noms complets, un code ISO part en 422 —
        # Gemini détecte la langue du texte seul).
        payload = {"prompt": prompt}
        if p.get("tone"):
            payload["style_instructions"] = f"Speak in a {p['tone']} tone."
        payload.update({k: v for k, v in _clean_params(p).items() if k != "language_code"})
    elif kind == "voice":
        # eleven-v3 : champ 'text' + ton inline best-effort + language_code ISO 639-1.
        text = f"[{p['tone']}] {prompt}" if p.get("tone") else prompt
        payload = {"text": text}
        payload.update(_clean_params(p))
    else:
        payload = {"prompt": prompt}
        payload.update(_clean_params(p))
    content, ctype, data = _run_queue(
        mdl, payload, api_key, kind="audio",
        poll_interval=poll_interval, max_wait=max_wait, dl_timeout=180.0,
    )
    cost = catalog.estimate_cost(mdl, params=p, text=(prompt if kind == "voice" else None))
    return GeneratedAsset(content=content, content_type=ctype, cost_usd=cost, meta=data)
