"""
Système de tags & filtrage de contenu §XVII — algorithme de matching + seed.

Fonctions pures `compute_effective_tags` / `item_matches_player` (réutilisables
côté serveur ET portables côté player) + seed des namespaces standards par org.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tag import CmsTagNamespace, CmsTagValue

# Taxonomie standard livrée par défaut (doit rester cohérente avec la migration 0026).
# (namespace_key, label, match_mode, applies_to, [(value, label, color), ...])
STANDARD_NAMESPACES: list[tuple[str, str, str, list[str], list[tuple[str, str, str]]]] = [
    ("resolution", "Resolution", "any", ["video", "image"],
     [("SD", "SD", "#9CA3AF"), ("FHD", "FHD", "#4875B4"), ("UHD", "UHD", "#7C3AED")]),
    ("region", "Region", "any", [],
     [("EU", "EU", "#4875B4"), ("FR", "FR", "#3B82F6"), ("DE", "DE", "#F59E0B"),
      ("UK", "UK", "#10B981"), ("US", "US", "#EF4444")]),
    ("language", "Language", "any", [],
     [("fr", "Français", "#3B82F6"), ("en", "English", "#10B981"),
      ("de", "Deutsch", "#F59E0B"), ("es", "Español", "#EF4444")]),
    ("audience", "Audience", "any", [],
     [("adults", "Adults", "#6366F1"), ("children", "Children", "#F472B6"), ("all", "All", "#9CA3AF")]),
    ("channel", "Channel", "any", [],
     [("promo", "Promo", "#EF4444"), ("brand", "Brand", "#4875B4"),
      ("safety", "Safety", "#F59E0B"), ("info", "Info", "#10B981")]),
]


def compute_effective_tags(
    media_tags: dict[str, list[str]],
    item_tags: dict[str, list[str]],
    item_ns_overrides: set[str],
) -> dict[str, list[str]]:
    """Tags effectifs d'un item = union média+item (additif) ou item seul (override). §XVII.4."""
    effective: dict[str, list[str]] = {}
    for ns in set(media_tags) | set(item_tags):
        if ns in item_ns_overrides:
            effective[ns] = list(item_tags.get(ns, []))
        else:
            effective[ns] = list(set(media_tags.get(ns, [])) | set(item_tags.get(ns, [])))
    return effective


def item_matches_player(
    effective_tags: dict[str, list[str]],
    player_filters: dict[str, list[str]],
) -> bool:
    """True si le player peut diffuser ce contenu. §XVII.4.

    - namespace non filtré sur le player → accepte tout
    - contenu sans tag dans un namespace filtré → universel (accepté)
    - sinon : il faut ≥ 1 tag commun (OR / match_mode 'any')
    """
    for ns, ptags in player_filters.items():
        if not ptags:
            continue
        ctags = effective_tags.get(ns, [])
        if not ctags:
            continue
        if not set(ctags) & set(ptags):
            return False
    return True


async def seed_standard_namespaces(db: AsyncSession, id_org: int) -> None:
    """Crée les namespaces standards + valeurs pour une org (idempotent).

    Appelé à la création d'une org. Les orgs existantes sont seedées par la
    migration 0026.
    """
    existing = set((await db.execute(
        select(CmsTagNamespace.namespace_key).where(CmsTagNamespace.id_org == id_org)
    )).scalars().all())

    for sort_ns, (key, label, mode, applies, values) in enumerate(STANDARD_NAMESPACES):
        if key in existing:
            continue
        ns = CmsTagNamespace(
            id_org=id_org, namespace_key=key, label=label, match_mode=mode,
            applies_to=applies, is_system=True, sort_order=sort_ns,
        )
        db.add(ns)
        await db.flush()
        for sort_v, (val, vlabel, color) in enumerate(values):
            db.add(CmsTagValue(
                id_namespace=ns.id_namespace, tag_value=val, label=vlabel, color=color, sort_order=sort_v
            ))
    await db.flush()
