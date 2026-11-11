"""Validateur de conformité PROMO — LISIBILITÉ/FORME uniquement (§7).

PUR (aucune I/O). Ne juge JAMAIS le CONTENU des mentions (responsabilité utilisateur) : vérifie
seulement que la forme permet un rendu lisible — prix formatable, cohérence prix barré/remise,
et longueur des textes tenant dans leur zone au plancher de police (sinon rejet + combien
raccourcir). Retour `{valid, errors:[{field,message}]}`. La génération est bloquée si invalide.
"""

from __future__ import annotations

from app.services.promo.templates import TEMPLATES, _num, compute_discount_pct


def validate(template_id: str, fmt: str, inputs: dict) -> dict:
    errors: list[dict] = []

    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        return {"valid": False,
                "errors": [{"field": "template", "message": f"Unknown template: {template_id}"}]}
    layout = tpl["layouts"].get(fmt)
    if layout is None:
        return {"valid": False,
                "errors": [{"field": "format", "message": f"Unsupported format: {fmt}"}]}

    def _err(field: str, message: str) -> None:
        errors.append({"field": field, "message": message})

    def _check_len(field: str, text: str | None) -> None:
        spec = layout.get(field) or {}
        max_chars = spec.get("max_chars")
        if text and max_chars and len(text) > max_chars:
            over = len(text) - max_chars
            _err(field, f"Too long for the zone: shorten by about {over} character(s).")

    # Prix : requis + formatable
    price = _num(inputs.get("price"))
    if price is None:
        _err("price", "Price required (valid number, e.g. 4.99).")
    elif price < 0:
        _err("price", "The price cannot be negative.")

    # Prix barré : si fourni, doit être supérieur au prix
    original_raw = inputs.get("original_price")
    if original_raw not in (None, ""):
        original = _num(original_raw)
        if original is None:
            _err("original_price", "Invalid original price (number expected).")
        elif price is not None and original <= price:
            _err("original_price", "The original price must be higher than the promo price.")

    # Remise explicite : cohérence avec le calcul (tolérance 1 point)
    if inputs.get("discount_pct") not in (None, ""):
        explicit = compute_discount_pct(None, None, inputs.get("discount_pct"))
        computed = compute_discount_pct(inputs.get("price"), original_raw, None)
        if explicit is None:
            _err("discount_pct", "Invalid discount (percentage expected).")
        elif computed is not None and abs(explicit - computed) > 1:
            _err("discount_pct",
                 f"Inconsistent discount: {explicit}% shown vs {computed}% computed.")

    # Lisibilité des textes (longueur vs zone)
    _check_len("slogan", (inputs.get("slogan") or "").strip() or None)
    _check_len("mentions", (inputs.get("mentions") or "").strip() or None)

    return {"valid": len(errors) == 0, "errors": errors}
