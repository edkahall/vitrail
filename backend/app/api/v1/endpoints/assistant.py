"""AI Assistant RAG (§XXVII) — endpoints.

- `POST /admin/assistant/reindex` : (ré)indexe la documentation produit (super-admin —
  le corpus est GLOBAL/plateforme, pas tenant-scoped).
- `POST /assistant/chat` : chat SSE avec l'agent (RAG + intents tenant lecture seule).
"""

import json
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_db_worker, require_super_admin
from app.models.organization import CmsOrganization
from app.models.user import CmsUser
from app.schemas.assistant import ActionExecuteRequest, ChatRequest
from app.services import rbac
from app.services.ai_credentials import resolve_ai_credentials
from app.services.assistant_actions import (
    consume_action_token,
    execute_batch,
    sign_batch,
    verify_batch,
)
from app.services.assistant_agent import run_agent
from app.services.assistant_quota import (
    quota_status,
    usage_history,
    usage_in_period,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistant"])


@router.post("/admin/assistant/reindex")
async def reindex_docs(
    _: Annotated[CmsUser, Depends(require_super_admin)],
    force: bool = False,
) -> dict:
    """Lance l'indexation de la documentation en pgvector (tâche Celery asynchrone)."""
    from app.workers.assistant_worker import index_docs

    task = index_docs.delay(force=force)
    return {"status": "queued", "task_id": task.id}


@router.get("/admin/assistant/usage")
async def assistant_usage(
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
) -> list[dict]:
    """Suivi d'usage de l'assistant par org (super-admin, cross-org/BYPASSRLS) : tokens et
    messages consommés sur la période courante de chaque org, % du crédit, coût ESTIMÉ."""
    orgs = (
        await db.execute(select(CmsOrganization).order_by(CmsOrganization.org_name))
    ).scalars().all()
    today = date.today()
    out: list[dict] = []
    for o in orgs:
        u = await usage_in_period(db, o.id_org, o.ai_quota_period, today)
        st = quota_status(o, u["tokens"])
        out.append({
            "id_org": o.id_org,
            "org_name": o.org_name,
            "is_active": o.is_active,
            "period": o.ai_quota_period,
            "unit": st["unit"],
            "limit": st["limit"],
            "used": st["used"],
            "used_tokens": u["tokens"],
            "used_messages": u["messages"],
            "cost_eur": st["cost_eur"],
            "pct": st["pct"],
            "blocked": st["blocked"],
            "over_threshold": st["over_threshold"],
            "threshold_pct": st["threshold_pct"],
        })
    return out


@router.get("/admin/assistant/usage/{id_org}/history")
async def assistant_usage_history(
    id_org: int,
    _: Annotated[CmsUser, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db_worker)],
    days: int = 30,
) -> list[dict]:
    """Historique quotidien d'usage d'une org (tokens/messages/coût estimé) sur N jours
    (super-admin, cross-org/BYPASSRLS) → alimente le mini-graphe du drawer org."""
    days = max(1, min(days, 90))
    return await usage_history(db, id_org, days, date.today())


async def _persist_exchange(
    db: AsyncSession,
    user: CmsUser,
    payload: ChatRequest,
    answer: str,
    tokens: int,
) -> int | None:
    """Enregistre l'échange (session TTL 24h) + incrémente les stats. Best-effort."""
    history: list[dict[str, Any]] = [
        {"role": m.role, "content": m.content} for m in payload.messages
    ]
    history.append({"role": "assistant", "content": answer})
    messages_json = json.dumps(history, ensure_ascii=False)
    try:
        if payload.session_id:
            row = (
                await db.execute(
                    text(
                        "UPDATE cms_assistant_sessions "
                        "SET messages = CAST(:m AS jsonb), page_context = CAST(:ctx AS jsonb), "
                        "    updated_at = now(), expires_at = now() + interval '24 hours' "
                        "WHERE id_session = :sid AND id_org = :org RETURNING id_session"
                    ),
                    {
                        "m": messages_json,
                        "ctx": json.dumps(payload.page_context or {}),
                        "sid": payload.session_id,
                        "org": user.id_org,
                    },
                )
            ).first()
            session_id = row.id_session if row else None
        else:
            session_id = None
        if session_id is None:
            session_id = (
                await db.execute(
                    text(
                        "INSERT INTO cms_assistant_sessions "
                        "(id_org, id_user, messages, page_context) "
                        "VALUES (:org, :uid, CAST(:m AS jsonb), CAST(:ctx AS jsonb)) "
                        "RETURNING id_session"
                    ),
                    {
                        "org": user.id_org,
                        "uid": user.id_user,
                        "m": messages_json,
                        "ctx": json.dumps(payload.page_context or {}),
                    },
                )
            ).scalar_one()

        await db.execute(
            text(
                "INSERT INTO cms_assistant_stats (id_org, stat_date, message_count, token_count) "
                "VALUES (:org, current_date, 1, :tok) "
                "ON CONFLICT (id_org, stat_date) DO UPDATE SET "
                "  message_count = cms_assistant_stats.message_count + 1, "
                "  token_count = cms_assistant_stats.token_count + EXCLUDED.token_count, "
                "  updated_at = now()"
            ),
            {"org": user.id_org, "tok": tokens},
        )
        await db.commit()
        return session_id
    except Exception as exc:  # noqa: BLE001
        logger.info("assistant: persistance de session échouée: %s", exc)
        await db.rollback()
        return None


@router.post("/assistant/chat")
async def chat(
    payload: ChatRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Chat SSE : stream les fragments de réponse, puis émet `done` (session_id + tokens)."""
    roles = await rbac.get_user_role_keys(db, current_user.id_user)
    locale = payload.locale or current_user.locale or "en"
    msgs = [{"role": m.role, "content": m.content} for m in payload.messages]

    # ── Résolution de la clé Anthropic (BYOK §XXVII) + crédit de tokens (quota).
    # Politique « rien d'auto » (cf. docs/PLAN_BYOK_AI_KEYS.md) :
    #   - super-admin (Ed) → clé plateforme via proxy, sans quota.
    #   - org avec sa propre clé (byok) → appel direct, quota NON appliqué (le client paie).
    #   - org en mode platform (clé plateforme autorisée) → proxy + quota appliqué.
    #   - sinon (strict sans clé) → IA désactivée (frame ai_disabled, AUCUN appel modèle).
    # super-admin (Ed) → clé plateforme via proxy, sans quota (byok_key=None, ai_disabled=False).
    byok_key: str | None = None
    ai_disabled = False
    quota_blocked = False
    if "CMS_SUPER_ADMIN" not in roles:
        org = (
            await db.execute(
                select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
            )
        ).scalar_one_or_none()
        resolved = await resolve_ai_credentials(db, org, "anthropic") if org is not None else None
        if resolved is None:
            ai_disabled = True
        elif resolved.byok:
            byok_key = resolved.api_key  # clé du client → pas de quota
        elif org is not None:
            # clé plateforme explicitement autorisée → proxy + quota
            usage = await usage_in_period(
                db, current_user.id_org, org.ai_quota_period, date.today()
            )
            quota_blocked = quota_status(org, usage["tokens"])["blocked"]

    async def event_stream():
        if ai_disabled:
            # Aucune clé utilisable → pas d'appel modèle. Le widget invite à configurer la clé.
            yield f"data: {json.dumps({'done': True, 'ai_disabled': True})}\n\n"
            return
        if quota_blocked:
            # Pas d'appel modèle (économie) → done avec drapeau ; le widget affiche le message.
            yield f"data: {json.dumps({'done': True, 'quota_exceeded': True})}\n\n"
            return

        usage: dict[str, int] = {}
        proposals: list[dict] = []
        parts: list[str] = []
        async for frag in run_agent(
            db,
            id_org=current_user.id_org,
            id_user=current_user.id_user,
            roles=roles,
            locale=locale,
            messages=msgs,
            page_context=payload.page_context,
            usage_out=usage,
            proposals_out=proposals,
            byok_key=byok_key,
        ):
            parts.append(frag)
            yield f"data: {json.dumps({'delta': frag}, ensure_ascii=False)}\n\n"

        # Toutes les actions préparées de ce tour → UN SEUL lot signé (une seule carte, une
        # seule confirmation). À l'exécution, publish différé groupé par playlist → pas
        # d'état vide transitoire entre deux étapes (cf. execute_batch).
        if proposals:
            steps = [{"act": p["action_type"], "params": p["params"]} for p in proposals]
            token = sign_batch(current_user.id_user, current_user.id_org, steps)
            batch = {
                "token": token,
                "steps": [
                    {"description": p["description"], "warnings": p.get("warnings", [])}
                    for p in proposals
                ],
            }
            yield f"data: {json.dumps({'action': batch}, ensure_ascii=False)}\n\n"

        answer = "".join(parts)
        session_id = await _persist_exchange(
            db, current_user, payload, answer, usage.get("total", 0)
        )
        done = {"done": True, "session_id": session_id, "tokens": usage.get("total", 0)}
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/assistant/action/execute")
async def execute_assistant_action(
    body: ActionExecuteRequest,
    current_user: Annotated[CmsUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Exécute le LOT d'actions préparé par l'agent, après confirmation explicite.

    Sécurité : le token (signé, TTL 10 min) est lié à l'utilisateur + l'org ; le RBAC est
    re-vérifié pour chaque étape dans `execute_batch`. C'est le SEUL chemin d'écriture de
    l'assistant. Publish différé groupé par playlist → pas d'état vide transitoire.
    """
    steps = verify_batch(body.token, current_user)
    if steps is None:
        return {"ok": False, "error": "Action expired or invalid. Please rephrase your request."}
    # Anti-rejeu : un token confirmé ne peut être exécuté qu'UNE fois (single-use).
    if not await consume_action_token(body.token):
        return {"ok": False, "error": "Action already executed. Please rephrase your request."}
    return await execute_batch(db, current_user, steps)
