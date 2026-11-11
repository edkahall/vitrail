"""Templates promo « built-in » + résolution en arbre de rendu (§5 du plan).

Un template déclare un `theme` (couleurs/police, Inter par défaut = design system) et des
`layouts` par format (16:9 / 9:16) avec des slots positionnés en **%** (résolution-agnostique).
`resolve_template` transforme (template, format, résolution, saisie utilisateur) en une structure
prête pour le rendu Jinja2/Playwright : dimensions cibles + slots avec texte formaté (prix,
prix barré, badge remise). Les images (fond, produit détouré) sont injectées par le worker
(elles exigent la DB/MinIO) — ce module reste PUR.

Unités : positions en %, tailles de police en **vh** (le viewport Playwright = la toile, donc
1vh = 1% de la hauteur cible → net à toute résolution, texte vectoriel).
"""

from __future__ import annotations

import re

from app.services.promo import PromoError, fonts, layers

# ── Styling par slot (Phase A) ────────────────────────────────────────────────────
# Slots dont l'utilisateur peut styler police/taille/couleur/graisse/alignement.
_STYLEABLE = {"slogan", "price", "original_price", "mentions"}
_ALIGN = {"left", "center", "right"}
_THEME_COLOR_KEYS = {"accent", "ink", "paper", "badge_bg", "badge_ink"}
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _safe_color(value) -> str | None:
    """Couleur sûre pour l'inline CSS : hex (#rgb/#rrggbb/#rrggbbaa) ou clé de thème. Sinon None
    (une valeur arbitraire pourrait casser hors du contexte CSS → jamais injectée)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if (v in _THEME_COLOR_KEYS or _HEX_RE.match(v)) else None


def _apply_style(spec: dict, override, catalog: dict) -> dict:
    """Fusionne un override de style utilisateur (Phase A) sur un slot texte. Toutes les valeurs
    sont validées : police ∈ `catalog` (built-in + custom org), couleur hex/thème, align enum,
    poids/taille bornés → aucune injection CSS. Pose `font` (famille CSS) + `font_id` si une police
    valide est choisie ; poids snappé au plus proche embarqué (faces exactes → pas de synthèse)."""
    out = dict(spec)
    if not isinstance(override, dict):
        return out
    fid = override.get("font")
    family = fonts.resolve_family(catalog, fid) if fid else None
    if family:
        out["font"] = family
        out["font_id"] = fid
        try:
            base = override.get("weight") or out.get("weight") or 600
            out["weight"] = fonts.nearest_weight_in(catalog, fid, int(base))
        except (TypeError, ValueError):
            pass
    elif override.get("weight") not in (None, ""):
        try:
            out["weight"] = max(100, min(900, int(override["weight"])))
        except (TypeError, ValueError):
            pass
    color = _safe_color(override.get("color"))
    if color:
        out["color"] = color
    if override.get("align") in _ALIGN:
        out["align"] = override["align"]
    if override.get("size_vh") not in (None, ""):
        try:
            floor = float(out.get("min_vh") or 1)
            out["font_vh"] = max(floor, min(40.0, float(override["size_vh"])))
        except (TypeError, ValueError):
            pass
    return out

# ── Résolutions cibles (largeur, hauteur) par format ─────────────────────────────
RESOLUTIONS: dict[str, dict[str, tuple[int, int]]] = {
    "16:9": {"fhd": (1920, 1080), "4k": (3840, 2160)},
    "9:16": {"fhd": (1080, 1920), "4k": (2160, 3840)},
}

_CURRENCY_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£"}


def _text(left, top, width, height, align, font_vh, min_vh, color, weight, max_chars):
    """Slot texte (positions/tailles en % et vh) — garde les définitions lisibles et ≤100 col."""
    return {"left": left, "top": top, "width": width, "height": height, "align": align,
            "font_vh": font_vh, "min_vh": min_vh, "color": color, "weight": weight,
            "max_chars": max_chars}


def _price(left, top, width, align, font_vh, min_vh, color, weight):
    return {"left": left, "top": top, "width": width, "align": align, "font_vh": font_vh,
            "min_vh": min_vh, "color": color, "weight": weight, "max_chars": 10}


def _badge(left, top, width, height, font_vh, min_vh, weight):
    return {"left": left, "top": top, "width": width, "height": height, "font_vh": font_vh,
            "min_vh": min_vh, "weight": weight}


# ── Catalogue de templates (built-in) ────────────────────────────────────────────
TEMPLATES: dict[str, dict] = {
    "fresh": {
        "label": "Fresh / Food",
        "theme": {"accent": "#3F8F4F", "ink": "#1D2B3A", "paper": "#F8F7F5",
                  "badge_bg": "#E4572E", "badge_ink": "#FFFFFF", "font": "Inter"},
        "layouts": {
            "16:9": {
                "product": {"left": 55, "top": 12, "width": 40, "height": 76},
                "slogan": _text(6, 12, 46, 32, "left", 8.5, 4.5, "ink", 800, 42),
                "price": _price(6, 52, 40, "left", 16, 9, "accent", 900),
                "original_price": _price(6, 46, 40, "left", 6, 4, "ink", 600),
                "discount": _badge(78, 4, 17, 30, 6.5, 4.5, 900),
                "mentions": _text(6, 90, 88, 7, "left", 2.4, 2.0, "ink", 500, 220),
            },
            "9:16": {
                "product": {"left": 10, "top": 40, "width": 80, "height": 42},
                "slogan": _text(8, 6, 84, 22, "center", 5.5, 3.2, "ink", 800, 48),
                "price": _price(8, 82, 84, "center", 10, 6, "accent", 900),
                "original_price": _price(8, 78, 84, "center", 4, 2.6, "ink", 600),
                "discount": _badge(66, 30, 26, 15, 5, 3, 900),
                "mentions": _text(8, 94, 84, 5, "center", 1.8, 1.5, "ink", 500, 220),
            },
        },
    },
    "premium": {
        "label": "Premium / Gourmet",
        "theme": {"accent": "#C9A227", "ink": "#F3EEE4", "paper": "#1C1E2A",
                  "badge_bg": "#C9A227", "badge_ink": "#1C1E2A", "font": "Inter"},
        "layouts": {
            "16:9": {
                "product": {"left": 8, "top": 12, "width": 40, "height": 76},
                "slogan": _text(52, 14, 42, 30, "right", 8.5, 4.5, "ink", 700, 42),
                "price": _price(52, 54, 42, "right", 16, 9, "accent", 800),
                "original_price": _price(52, 48, 42, "right", 6, 4, "ink", 500),
                "discount": _badge(30, 8, 15, 26, 6.5, 4.5, 800),
                "mentions": _text(6, 90, 88, 7, "right", 2.4, 2.0, "ink", 400, 220),
            },
            "9:16": {
                "product": {"left": 12, "top": 38, "width": 76, "height": 44},
                "slogan": _text(8, 7, 84, 22, "center", 5.5, 3.2, "ink", 700, 48),
                "price": _price(8, 82, 84, "center", 10, 6, "accent", 800),
                "original_price": _price(8, 78, 84, "center", 4, 2.6, "ink", 500),
                "discount": _badge(64, 28, 26, 15, 5, 3, 800),
                "mentions": _text(8, 94, 84, 5, "center", 1.8, 1.5, "ink", 400, 220),
            },
        },
    },
}


def list_templates() -> list[dict]:
    """Catalogue exposé à l'UI : id, label, formats, theme complet + layouts (slots en %/vh)
    → l'UI rend un aperçu WYSIWYG côté client (unités CSS `cqh` = équivalent vh dans la boîte)."""
    return [
        {"id": tid, "label": t["label"], "formats": list(t["layouts"].keys()),
         "theme": t["theme"], "layouts": t["layouts"]}
        for tid, t in TEMPLATES.items()
    ]


def _num(value) -> float | None:
    """Parse un prix (accepte '4,99' / '4.99' / 4.99). None si non numérique."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def format_price(value, currency: str = "EUR") -> str | None:
    """Format FR : '4,99 €' (virgule décimale, symbole après)."""
    n = _num(value)
    if n is None:
        return None
    sym = _CURRENCY_SYMBOL.get((currency or "EUR").upper(), currency)
    txt = f"{n:.2f}".replace(".", ",")
    return f"{txt} {sym}"


def compute_discount_pct(price, original_price, explicit=None) -> int | None:
    """% de remise : explicite s'il est fourni, sinon calculé depuis (barré, prix)."""
    if explicit not in (None, ""):
        try:
            return int(round(float(str(explicit).replace("%", "").strip())))
        except ValueError:
            return None
    p, o = _num(price), _num(original_price)
    if p is not None and o is not None and o > 0 and o > p:
        return int(round((1 - p / o) * 100))
    return None


def resolve_template(template_id: str, fmt: str, resolution: str, inputs: dict,
                     font_catalog: dict | None = None,
                     theme_overrides: dict | None = None) -> dict:
    """(pur) → structure de rendu : dimensions + theme + slots avec texte formaté.

    `inputs` = {slogan, price, original_price, discount_pct, currency, mentions, style_overrides}.
    `font_catalog` = catalogue de polices (built-in + custom org) pour résoudre les overrides de
    police ; défaut = built-in seul. Les images (background/product) sont ajoutées par le worker
    (data URIs). Lève PromoError si invalide.

    `theme_overrides` = couleurs/police de la **charte de marque** (P3.4), déjà validées par
    `services.brand_kit.theme_overrides`. Fusionnées AVANT tout le reste, pour que les slots ET les
    calques (dont les couleurs par défaut dérivent du thème) voient la même palette. Ce module
    reste PUR : il reçoit un dict, il n'interroge jamais la base."""
    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        raise PromoError(f"unknown template: {template_id}")
    if fmt not in tpl["layouts"]:
        raise PromoError(f"unsupported format: {fmt}")
    res = (resolution or "fhd").lower()
    if fmt not in RESOLUTIONS or res not in RESOLUTIONS[fmt]:
        raise PromoError(f"unsupported resolution: {resolution}")
    width, height = RESOLUTIONS[fmt][res]
    layout = tpl["layouts"][fmt]
    theme = {**tpl["theme"], **(theme_overrides or {})}
    currency = inputs.get("currency") or "EUR"

    price_txt = format_price(inputs.get("price"), currency)
    original_txt = format_price(inputs.get("original_price"), currency)
    pct = compute_discount_pct(inputs.get("price"), inputs.get("original_price"),
                               inputs.get("discount_pct"))

    overrides = inputs.get("style_overrides") or {}
    catalog = font_catalog or fonts.builtin_catalog()

    def _slot(name: str, text: str | None):
        spec = layout.get(name)
        if spec is None or text in (None, ""):
            return None
        if name in _STYLEABLE:
            spec = _apply_style(spec, overrides.get(name), catalog)
        return {**spec, "text": text}

    slots: dict = {
        "slogan": _slot("slogan", (inputs.get("slogan") or "").strip() or None),
        "price": _slot("price", price_txt),
        "original_price": _slot("original_price", original_txt) if pct else None,
        "discount": _slot("discount", f"-{pct}%") if pct else None,
        "mentions": _slot("mentions", (inputs.get("mentions") or "").strip() or None),
        "product": {**layout["product"]},  # le worker y ajoute data_uri
    }
    return {
        "template_id": template_id, "label": tpl["label"], "format": fmt, "resolution": res,
        "width": width, "height": height, "theme": theme, "slots": slots,
        # Calques libres (Phase B) superposés au template — le worker y injecte les data-URI image.
        "layers": layers.resolve_layers(inputs.get("layers"), theme, catalog),
    }
