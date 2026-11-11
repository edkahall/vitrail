"""
Rate-limit global de l'API (audit sécurité 2026-06-25).

Avant ce middleware, seul `/auth/login` avait un garde-fou anti-brute-force ; le reste
de l'API n'avait AUCUNE limite (énumération, scraping, flood d'endpoints coûteux laissés
ouverts). Ce middleware applique une fenêtre fixe par IP sur l'ensemble des requêtes.

Choix de conception :
- **fail-open** : si Redis est indisponible, on laisse passer (ne pas transformer une panne
  de l'infra de comptage en déni de service de l'app).
- `/health` et les préflight CORS (OPTIONS) sont exemptés.
- L'IP réelle est lue dans `X-Forwarded-For` (1ʳᵉ valeur) derrière Caddy, comme le login.
- Limite généreuse (par défaut 600 req/min/IP) — c'est un filet anti-flood, pas un quota métier.
"""

import time

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

_EXEMPT_PATHS = frozenset({"/health"})


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, per_minute: int):
        super().__init__(app)
        self._per_minute = per_minute

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        ip = _client_ip(request)
        # Fenêtre fixe d'1 minute : la clé inclut le numéro de minute epoch → expire seule.
        window = int(time.time()) // 60
        key = f"signflow:ratelimit:{ip}:{window}"
        try:
            async with aioredis.from_url(settings.REDIS_URL, decode_responses=True) as r:
                count = await r.incr(key)
                if count == 1:
                    await r.expire(key, 60)
        except Exception:
            # fail-open : Redis down → on ne bloque pas
            return await call_next(request)

        if count > self._per_minute:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again in a moment."},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
