from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1.router import api_router

# Garde-fou démarrage (audit 2026-06-24) : refuse de démarrer avec le secret JWT par défaut.
# Avec "changeme", n'importe qui peut forger des tokens (impersonation cross-tenant) → on
# échoue fermé (mieux arrêté que compromis). Toute instance réelle définit CMS_JWT_SECRET.
if settings.CMS_JWT_SECRET in ("", "changeme"):
    raise RuntimeError(
        "CMS_JWT_SECRET not configured (default value detected). "
        "Set a strong secret before starting."
    )

app = FastAPI(
    title="Vitrail",
    version="0.1.0",
    description="SaaS Digital Signage — API v1",
    # Doc interactive désactivable (à couper en prod, cf. CMS_DOCS_ENABLED).
    docs_url="/api/docs" if settings.CMS_DOCS_ENABLED else None,
    redoc_url="/api/redoc" if settings.CMS_DOCS_ENABLED else None,
    openapi_url="/api/openapi.json" if settings.CMS_DOCS_ENABLED else None,
)

# Rate-limit global anti-flood par IP (audit sécurité 2026-06-25). Enregistré AVANT le CORS
# pour que le CORS reste le middleware le plus EXTERNE (Starlette wrappe le dernier ajouté en
# dernier = outermost) → une éventuelle réponse 429 porte quand même les en-têtes CORS.
if settings.CMS_RATE_LIMIT_ENABLED:
    from app.core.ratelimit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware, per_minute=settings.CMS_RATE_LIMIT_PER_MINUTE)

# Mode démo (§II) : bloque les écritures des comptes `is_demo`. Ajouté AVANT le CORS → CORS reste le
# plus externe (les 403 `demo_blocked` portent les en-têtes CORS).
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.core.demo_guard import DemoGuardMiddleware  # noqa: E402
from app.services.spend import SpendCapExceeded  # noqa: E402

app.add_middleware(DemoGuardMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Rendition-Advisory"],  # advisory conformité lu par le front (toast)
)

# Plafond de dépense BYOK atteint (P3.2) → 402, avec de quoi DISTINGUER un plafond d'une
# panne à l'écran (scope / période / plafond / dépensé / estimé / restant).
#
# 🔑 UN SEUL convertisseur, ici, plutôt qu'un `try/except` recopié dans chacun des SIX
# chemins qui dépensent : le septième l'oublierait, et son refus sortirait en 500 —
# indistinguable d'une panne, ce que ce mécanisme existe justement pour éviter.
@app.exception_handler(SpendCapExceeded)
async def _spend_cap_handler(_request: Request, exc: SpendCapExceeded) -> JSONResponse:
    return JSONResponse(status_code=402, content={"detail": exc.as_detail()})


app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def health():
    return {
        "status": "ok",
        "version": settings.SIGNFLOW_VERSION,
    }
