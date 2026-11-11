"""
Playlist rotation — sous-playlists imbriquées « N items par tour » (retour beta 2026-07).

Principe (validé Ed 2026-07-16) : une playlist enfant posée comme item d'un programme
(`cms_playlist_items.item_type='sub_playlist'`) ne diffuse que `items_per_turn` item(s)
par tour de boucle ; au tour suivant, le(s) suivant(s) — séquentiel (curseur circulaire)
ou aléatoire SANS répétition jusqu'à épuisement (permutation seedée, re-mélange à
l'épuisement).

Architecture : le cycle complet est DÉROULÉ PAR LE SERVEUR dans le payload de sync
(ws.py). T tours = N/gcd(N,K) → la boucle plate envoyée aux players contient les T
tours successifs. Zéro modification player : wall-clock, LAN sync, PoP et injection
campagnes fonctionnent tels quels. Tout est déterministe (seed = contenu de l'enfant) :
deux players d'un même groupe LAN construisent le MÊME déroulé — la permutation ne
change que quand le contenu de l'enfant change (publish → force_sync org).

Un seul niveau d'imbrication : une sous-playlist ne peut pas contenir de sous-playlist.
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import replace
from math import gcd, lcm

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.playlist import CmsPlaylist, CmsPlaylistItem

logger = logging.getLogger(__name__)

# Garde-fou : cap du nombre de tours déroulés (rotations aux cardinalités premières
# entre elles → LCM explosif). Au-delà, le cycle est tronqué (léger biais au bouclage,
# loggé) — usage réaliste (5-20 items, K=1-3) reste très en dessous.
MAX_UNROLL_TURNS = 60


def rotation_turns(n: int, k: int) -> int:
    """Nombre de tours pour boucler proprement : T*K ≡ 0 (mod N) → T = N/gcd(N,K).
    Après T tours, chaque item a été diffusé exactement T*K/N fois."""
    if n <= 0:
        return 1
    k = max(1, min(k, n))
    return n // gcd(n, k)


def rotation_seed(id_playlist: int, item_ids: list[int]) -> int:
    """Seed déterministe inter-process/inter-players : ne dépend QUE du contenu publié
    de l'enfant (ids ordonnés). Changement de contenu = publish = force_sync org →
    tous les players reconstruisent le même nouveau déroulé."""
    payload = f"{id_playlist}:{','.join(str(i) for i in item_ids)}".encode()
    return zlib.crc32(payload)


def _seeded_permutation(n: int, seed: int, round_index: int, avoid_first: int | None) -> list[int]:
    """Permutation de range(n) — Fisher-Yates sur PRNG maison (LCG) pour être
    STABLE inter-versions Python (random.shuffle ne garantit pas la stabilité).
    `avoid_first` : si la permutation commence par l'item qui vient d'être joué
    (frontière de re-mélange), on échange les 2 premiers (pas de doublon dos à dos)."""
    state = (seed ^ (round_index * 0x9E3779B9)) & 0xFFFFFFFF
    perm = list(range(n))
    for i in range(n - 1, 0, -1):
        # LCG 32 bits (Numerical Recipes) — suffisant pour du mélange de playlist.
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        j = state % (i + 1)
        perm[i], perm[j] = perm[j], perm[i]
    if n > 1 and avoid_first is not None and perm[0] == avoid_first:
        perm[0], perm[1] = perm[1], perm[0]
    return perm


def build_turn_selections(mode: str, n: int, k: int, turns: int, seed: int) -> list[list[int]]:
    """Indices d'items de l'enfant diffusés à chaque tour (liste de `turns` listes de `k`).

    - one_sequential : curseur circulaire (t*K+j) mod N — ex. N=5 K=2 :
      [0,1] [2,3] [4,0] [1,2] [3,4].
    - one_random : permutations seedées concaténées (chaque permutation épuisée avant
      re-mélange ; garde anti-doublon à la frontière), découpées par paquets de K.
    """
    if n <= 0:
        return [[] for _ in range(turns)]
    k = max(1, min(k, n))
    if mode == "one_sequential":
        return [[(t * k + j) % n for j in range(k)] for t in range(turns)]
    # one_random : séquence = permutations successives, longueur ≥ turns*k.
    seq: list[int] = []
    round_index = 0
    while len(seq) < turns * k:
        prev_last = seq[-1] if seq else None
        seq.extend(_seeded_permutation(n, seed, round_index, prev_last))
        round_index += 1
    return [seq[t * k:(t + 1) * k] for t in range(turns)]


def _flatten(seq: list) -> list:
    """Aplatit un niveau de listes imbriquées : un document multipage compile en
    LISTE d'items-pages mais reste UNE unité de slot/rotation — l'aplatissement
    ne se fait qu'au déroulé final."""
    out: list = []
    for e in seq:
        if isinstance(e, list):
            out.extend(e)
        else:
            out.append(e)
    return out


def _emit_slot(units: list, protect: bool) -> list:
    """Items émis pour UNE occurrence de slot (un tour). `protect` (playlist
    no_ads_inside) : tout le bloc sauf son 1er item reçoit `no_ad_before` —
    l'injection Retail Media (splice_ad_items) ne coupera jamais le bloc.
    Copie les dicts marqués (un même item compilé est réutilisé à plusieurs
    tours — il peut être 1er du bloc à un tour et pas au suivant)."""
    flat = _flatten(units)
    if not protect:
        return flat
    out: list = []
    for j, e in enumerate(flat):
        if j > 0:
            if isinstance(e, dict):
                if not e.get("no_ad_before"):
                    e = {**e, "no_ad_before": True}
            elif getattr(e, "no_ad_before", None) is False:
                # Chemin GOUVERNANCE : les unités sont des `CompiledItem` (dataclass), pas
                # des dicts de payload. Sans cette branche, `no_ads_inside` était accepté
                # côté UI puis silencieusement sans effet sous gouvernance.
                e = replace(e, no_ad_before=True)
        out.append(e)
    return out


def unroll_slots(base_slots: list[dict], cap_items: int = 600) -> list:
    """Déroule le cycle complet d'une suite de slots en boucle plate.

    Slot = {"kind": "inline"|"rotation", "items": [...]} + pour rotation :
    {"mode", "k", "seed"} + optionnel {"protect": bool} (bloc sans pub interne).
    Une entrée d'items peut être une LISTE (document multipage = ses pages) —
    aplatie en sortie. Sans rotation → simple aplatissement. Avec → T = lcm des
    tours de chaque groupe (cap MAX_UNROLL_TURNS et `cap_items`)."""
    rot = [s for s in base_slots if s["kind"] == "rotation"]
    if not rot:
        out: list = []
        for s in base_slots:
            out.extend(_emit_slot(s["items"], s.get("protect", False)))
        return out
    turns = 1
    for s in rot:
        turns = lcm(turns, rotation_turns(len(s["items"]), s["k"]))
    per_turn = sum(s["k"] if s["kind"] == "rotation" else len(s["items"]) for s in base_slots)
    turns_cap = min(MAX_UNROLL_TURNS, max(1, cap_items // max(1, per_turn)))
    if turns > turns_cap:
        logger.warning("rotation: cycle tronqué %s -> %s tours", turns, turns_cap)
        turns = turns_cap
    selections = [
        build_turn_selections(s["mode"], len(s["items"]), s["k"], turns, s["seed"])
        if s["kind"] == "rotation" else None
        for s in base_slots
    ]
    out: list = []
    for t in range(turns):
        for s, sel in zip(base_slots, selections):
            units = [s["items"][i] for i in sel[t]] if sel is not None else s["items"]
            out.extend(_emit_slot(units, s.get("protect", False)))
    return out


async def child_playlist_ids(db: AsyncSession, playlist_ids: set[int]) -> dict[int, set[int]]:
    """parent → enfants (sous-playlists non supprimées) pour un lot de playlists.
    Consommé par prefetch/inventaire (les médias des enfants doivent être précachés)."""
    if not playlist_ids:
        return {}
    rows = (await db.execute(
        select(CmsPlaylistItem.id_playlist, CmsPlaylistItem.id_child_playlist)
        .join(CmsPlaylist, CmsPlaylist.id_playlist == CmsPlaylistItem.id_child_playlist)
        .where(
            CmsPlaylistItem.id_playlist.in_(playlist_ids),
            CmsPlaylistItem.id_child_playlist.isnot(None),
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).all()
    out: dict[int, set[int]] = {}
    for parent, child in rows:
        out.setdefault(parent, set()).add(child)
    return out


async def validate_child_for_nesting(
    db: AsyncSession, id_org: int, id_parent: int, id_child: int,
) -> CmsPlaylist:
    """Gardes d'imbrication (1 seul niveau) — lève ValueError avec message utilisateur :
    enfant existant même org, pas d'auto-référence, l'enfant ne contient pas lui-même de
    sous-playlist, et le parent n'est pas déjà référencé comme enfant ailleurs."""
    if id_child == id_parent:
        raise ValueError("A playlist cannot be nested inside itself")
    child = (await db.execute(
        select(CmsPlaylist).where(
            CmsPlaylist.id_playlist == id_child,
            CmsPlaylist.id_org == id_org,
            CmsPlaylist.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if child is None:
        raise ValueError("Sub-playlist not found")
    has_nested = (await db.execute(
        select(CmsPlaylistItem.id_item).where(
            CmsPlaylistItem.id_playlist == id_child,
            CmsPlaylistItem.id_child_playlist.isnot(None),
        ).limit(1)
    )).scalar_one_or_none()
    if has_nested is not None:
        raise ValueError(
            "This playlist already contains sub-playlists (only one nesting level)")
    parent_is_child = (await db.execute(
        select(CmsPlaylistItem.id_item).where(
            CmsPlaylistItem.id_child_playlist == id_parent,
        ).limit(1)
    )).scalar_one_or_none()
    if parent_is_child is not None:
        raise ValueError(
            "This playlist is itself used as a sub-playlist (only one level)")
    return child
