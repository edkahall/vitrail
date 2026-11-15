"""Mises à jour OS contrôlées (issue vitrail#6) — demandes au banc.

Le serveur ne CROIT pas un manifeste parce qu'un player l'envoie : il n'accepte un relevé de banc
que s'il répond à une demande de l'exploitant du serveur, émise pour CE player et encore valable.
La demande vit dans Redis (TTL), un identifiant aléatoire par demande, consommée à l'usage.
"""

from __future__ import annotations

import json
import secrets

import redis.asyncio as aioredis

from app.core.config import settings

# Une mise à jour de banc peut être longue (Raspberry Pi, archive lente, redémarrage).
BENCH_TTL_S = 6 * 3600


def _bench_key(id_player: int) -> str:
    return f"signflow:os_bench:{id_player}"


async def _publish_command(r, id_player: int, command: str, params: dict) -> None:
    await r.publish(f"signflow:player:{id_player}:cmds", json.dumps({
        "type": "player:command", "command": command, "params": params,
    }))


class BenchPending(Exception):
    """Un banc est déjà demandé pour ce player et n'a pas encore remonté son manifeste."""


async def request_bench(id_player: int, id_user: int) -> str:
    """Mémorise la demande puis l'envoie au player (commande `os_bench`). Renvoie son id.

    Une seule demande à la fois (revue du 15/11) : un second clic écrasait la première, dont le
    relevé était alors refusé — le banc mis à jour, et aucun manifeste, définitivement."""
    request_id = secrets.token_hex(8)
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        ok = await r.set(_bench_key(id_player),
                         json.dumps({"request_id": request_id, "id_user": id_user}),
                         ex=BENCH_TTL_S, nx=True)
        if not ok:
            raise BenchPending()
        await _publish_command(r, id_player, "os_bench", {"request_id": request_id})
    finally:
        await r.aclose()
    return request_id


async def request_check(id_player: int) -> None:
    """Demande au player de relever tout de suite ses mises à jour en attente (`os_check`)."""
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        await _publish_command(r, id_player, "os_check", {})
    finally:
        await r.aclose()


async def take_bench(id_player: int, request_id: str) -> dict | None:
    """Consomme la demande si elle correspond. None = aucune demande valable (relevé refusé)."""
    if not request_id:
        return None
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        raw = await r.get(_bench_key(id_player))
        if not raw:
            return None
        data = json.loads(raw)
        if not secrets.compare_digest(str(data.get("request_id") or ""), str(request_id)):
            return None
        # Consommation ATOMIQUE (revue du 15/11) : deux relevés identiques reçus en même temps ne
        # doivent pas créer deux manifestes. Seul celui qui supprime la clé gagne.
        if not await r.delete(_bench_key(id_player)):
            return None
        return data
    finally:
        await r.aclose()
