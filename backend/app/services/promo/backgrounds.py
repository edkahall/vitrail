"""Génération de fonds promo par IA (Phase 4, §9 du plan) — briques de prompt.

L'IA ne produit QUE le fond (esthétique) ; le prix/texte/produit sont composés de façon
déterministe par-dessus. Le prompt est donc orienté vers un **fond propre, sans texte ni
produit ni personne**, avec une zone calme pour le compositing. PUR (aucune I/O).
"""

from __future__ import annotations

# Négatif systématique : le fond ne doit contenir NI texte NI produit NI personne (tout ça est
# ajouté par le compositing déterministe → sinon doublons / incohérences).
NEGATIVE_PROMPT = (
    "text, words, letters, numbers, watermark, logo, brand, signage, "
    "product, packaging, bottle, box, people, person, hands, face, "
    "cluttered, busy, collage, frame, border, low quality"
)

# Ambiances built-in (id → libellé FR + fragment de prompt EN optimisé).
AMBIANCES: list[dict] = [
    {"id": "fresh", "label": "Fresh / Market",
     "prompt": "fresh food market ambiance, soft natural daylight, clean minimal surface, "
               "subtle bokeh, airy and bright"},
    {"id": "gourmand", "label": "Gourmet / Bakery",
     "prompt": "warm cozy bakery ambiance, golden hour light, rustic wooden surface, "
               "shallow depth of field, appetizing mood"},
    {"id": "premium", "label": "Premium / Luxury",
     "prompt": "elegant dark studio backdrop, subtle gold bokeh, soft rim light, "
               "luxurious minimal mood, high-end"},
    {"id": "festif", "label": "Festive",
     "prompt": "festive celebration backdrop, warm blurred bokeh lights, joyful, "
               "soft gradient, cheerful atmosphere"},
    {"id": "frais_bleu", "label": "Fresh / Blue",
     "prompt": "cool fresh blue gradient backdrop, clean water droplets bokeh, crisp, "
               "refreshing minimal composition"},
    {"id": "neutre", "label": "Neutral / Studio",
     "prompt": "clean neutral studio gradient background, soft even lighting, "
               "minimal, professional product backdrop"},
]

_FOCUS = {
    "left": "keep the left side clean and empty for product placement",
    "right": "keep the right side clean and empty for product placement",
    "top": "keep the top area clean and empty for text placement",
    "bottom": "keep the bottom area clean and empty for text placement",
    "center": "keep the center clean and empty for product placement",
}


def list_ambiances() -> list[dict]:
    return [{"id": a["id"], "label": a["label"]} for a in AMBIANCES]


def build_background_prompt(ambiance_id: str | None, description: str | None,
                            focus: str | None) -> tuple[str, str]:
    """(prompt, negative) pour un fond promo propre. `ambiance_id` = preset built-in,
    `description` = complément libre, `focus` = côté à laisser vide pour le compositing."""
    parts: list[str] = []
    amb = next((a for a in AMBIANCES if a["id"] == ambiance_id), None)
    if amb:
        parts.append(amb["prompt"])
    desc = (description or "").strip()
    if desc:
        parts.append(desc)
    if focus in _FOCUS:
        parts.append(_FOCUS[focus])
    # Garde-fous constants : c'est un FOND, pas une affiche finie.
    parts.append("professional promotional background, empty space for compositing, "
                 "no text, high quality, sharp")
    return ", ".join(parts), NEGATIVE_PROMPT
