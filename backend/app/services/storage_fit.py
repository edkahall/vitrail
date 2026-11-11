"""Est-ce que le contenu affecté TIENT sur l'écran ? (point 4 du chantier « stockage »).

Le 2026-08-06, une playlist de 1,85 Go a été affectée à un stick Android dont la partition
fait 4 Go, déjà largement occupée par Android. Le player a téléchargé jusqu'à saturer le
disque à 100 %, ce qui a cassé l'OS lui-même ; l'appareil n'a pu être récupéré qu'en
désinstallant l'application, donc par un déplacement sur site.

Le frein posé dans les players empêche désormais la casse — mais il ne dit rien à celui qui
affecte le contenu : l'écran diffuserait une playlist tronquée. Ce service répond à la
question **au moment du geste**, quand elle coûte encore zéro : *ça rentre, ou pas ?*

Trois principes :

1. **On ne compte que ce qui manque.** Un média déjà en cache ne se retéléchargera pas ;
   le facturer une seconde fois ferait crier au loup sur des écrans parfaitement sains.
2. **On garde la même réserve que le frein** (`RESERVE_BYTES`). Annoncer « ça rentre »
   jusqu'au dernier octet contredirait le player, qui s'arrêtera avant — deux définitions
   du « plein » finiraient par diverger.
3. **`unknown` n'est pas `ok`.** Un player qui ne remonte pas encore sa capacité (version
   antérieure au frein) rend un verdict explicite « on ne sait pas ». Se taire reviendrait
   à affirmer que ça tient, ce qu'on ignore — et c'est exactement l'erreur qui a coûté
   l'incident.

⚠️ La source est `capabilities.store`, **déjà remontée à chaque battement** et déjà lue par
l'inventaire (`players.py`). On ne duplique pas ces valeurs dans des colonnes dédiées : deux
sources de vérité sur la même mesure finiraient par se contredire.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import CmsMedia
from app.models.player import CmsPlayer

# Même réserve que le frein embarqué (`MIN_FREE_BYTES` côté players) : le serveur ne doit
# jamais promettre un octet que le player refusera d'écrire.
RESERVE_BYTES = 512 * 1024 * 1024

# Marge au-delà de laquelle on parle de « juste » plutôt que de « ça passe » : sous 15 % de
# marge, la moindre rendition supplémentaire ou mise à jour de contenu fera basculer.
TIGHT_RATIO = 0.15


async def playlist_media_ids(db: AsyncSession, id_playlist: int | None) -> list[int]:
    """Médias d'une playlist, **sous-playlists comprises**.

    On réutilise `child_playlist_ids` — le même dépliage que l'inventaire et le prefetch.
    Réécrire la règle ici donnerait un contrôle qui ne compte pas ce que l'écran
    téléchargera vraiment : une playlist de rotation semblerait légère alors que ses
    enfants pèsent le plus.
    """
    if not id_playlist:
        return []
    from app.models.playlist import CmsPlaylistItem
    from app.services.playlist_rotation import child_playlist_ids

    ids = {int(id_playlist)}
    for childs in (await child_playlist_ids(db, set(ids))).values():
        ids |= childs
    rows = (await db.execute(
        select(CmsPlaylistItem.id_media).where(
            CmsPlaylistItem.id_playlist.in_(ids),
            CmsPlaylistItem.id_media.isnot(None),
        )
    )).all()
    return sorted({int(r[0]) for r in rows})


def player_storage(player: CmsPlayer) -> dict:
    """Capacité/occupation remontées par un player, ou `{}` s'il n'en remonte pas."""
    return ((player.capabilities or {}).get("store") or {}) if player.capabilities else {}


async def media_sizes(db: AsyncSession, id_medias: Iterable[int]) -> dict[int, int]:
    """Taille en octets par média. Un média sans taille connue compte 0 : mieux vaut
    sous-estimer que refuser une affectation sur une donnée absente."""
    ids = [int(m) for m in set(id_medias or [])]
    if not ids:
        return {}
    rows = (await db.execute(
        select(CmsMedia.id_media, CmsMedia.file_size_bytes).where(CmsMedia.id_media.in_(ids))
    )).all()
    return {r[0]: int(r[1] or 0) for r in rows}


def assess_player(
    player: CmsPlayer,
    sizes: dict[int, int],
    id_medias: Sequence[int],
) -> dict:
    """Verdict pour UN écran. Formes : ok · tight · insufficient · unknown."""
    store = player_storage(player)
    free = store.get("free_bytes")
    total = store.get("disk_total_bytes")
    cached = set(store.get("cached_ids") or [])

    # (1) seul ce qui n'est pas déjà en cache sera téléchargé.
    missing = [m for m in set(id_medias or []) if m not in cached]
    required = sum(sizes.get(m, 0) for m in missing)

    base = {
        "id_player": player.id_player,
        "player_name": player.player_name,
        "required_bytes": required,
        "missing_count": len(missing),
        "free_bytes": free if isinstance(free, (int, float)) else None,
        "disk_total_bytes": total if isinstance(total, (int, float)) else None,
    }

    # (3) pas de capacité remontée ⇒ on le DIT, on ne suppose pas que ça tient.
    if not isinstance(free, (int, float)):
        return {**base, "verdict": "unknown",
                "reason": "This screen does not report its free space yet "
                          "(player version older than the storage check)."}

    if required == 0:
        return {**base, "verdict": "ok", "reason": "All content is already cached."}

    # (2) même réserve que le frein embarqué.
    usable = float(free) - RESERVE_BYTES
    if required > usable:
        return {**base, "verdict": "insufficient",
                "reason": "The content to download exceeds the free space: "
                          "the screen will not be able to cache everything and will play "
                          "only part of it."}
    if usable > 0 and (usable - required) / usable < TIGHT_RATIO:
        return {**base, "verdict": "tight",
                "reason": "It fits, but only just: any additional content "
                          "will push this screen over the limit."}
    return {**base, "verdict": "ok", "reason": "There is enough free space."}


async def assess(
    db: AsyncSession,
    id_org: int,
    id_players: Sequence[int],
    id_medias: Sequence[int],
) -> dict:
    """Verdict pour un ENSEMBLE d'écrans (le geste d'affectation en vise plusieurs).

    Filtré sur l'organisation : un contrôle de capacité ne doit pas devenir un moyen de
    savoir qu'un écran existe ailleurs.
    """
    if not id_players:
        return {"players": [], "worst": "ok", "required_bytes": 0}

    players = (await db.execute(
        select(CmsPlayer).where(
            CmsPlayer.id_player.in_([int(p) for p in id_players]),
            CmsPlayer.id_org == id_org,
        )
    )).scalars().all()

    sizes = await media_sizes(db, id_medias)
    results = [assess_player(p, sizes, id_medias) for p in players]

    # Le pire verdict pilote l'avertissement : un seul écran saturé suffit à créer
    # l'incident, et c'est celui-là que l'exploitant doit voir.
    order = {"ok": 0, "unknown": 1, "tight": 2, "insufficient": 3}
    worst = max((r["verdict"] for r in results), key=lambda v: order[v], default="ok")
    return {
        "players": results,
        "worst": worst,
        "required_bytes": sum(sizes.values()),
    }
