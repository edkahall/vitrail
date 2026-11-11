"""Génération de contenu IA (§ génération de contenu).

Phase 1 — image (synchrone, strict BYOK) :
  POST /generation/image → résout la clé fal de l'org (coffre BYOK) → génère →
  crée un cms_media (source=generated_ai) → transcode (thumbnail + renditions auto) →
  renvoie le média. Pas de clé fal → 400 explicite (l'org doit la configurer).

Les images fal sont rapides → traitement inline dans la requête (pas de worker). La
vidéo (Phase 3) introduira la table de jobs + un worker async.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_any_permission
from app.models.generation import CmsGenerationCampaign, CmsGenerationJob
from app.models.media import CmsMedia
from app.models.organization import CmsOrgAiCredential, CmsOrganization
from app.models.user import CmsUser
from app.schemas.generation import (
    CampaignExecuteRequest,
    CampaignPlanRequest,
    CampaignPlanResponse,
    CampaignRead,
    GenerateAudioRequest,
    GenerateImageRequest,
    GenerateVideoRequest,
    GenerationJobRead,
    PromptAssistRequest,
    PromptAssistResponse,
)
from app.schemas.media import MediaRead
from app.services import brand_kit, rbac
from app.services.ai_credentials import resolve_ai_credentials
from app.services.generation import campaign as campaign_svc
from app.services import spend
from app.services.generation import catalog, elevenlabs, fal, refs
from app.services.generation import prompt_assistant as prompt_svc
from app.services.media import compute_sha256, get_signed_url, make_storage_key, upload_file

MAX_CAMPAIGN_ASSETS = 40  # garde-fou fan-out (images concepts×ratios + vidéo/voix/musique)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generation", tags=["generation"])

_PROVIDER_LABEL = {"fal": "fal.ai", "elevenlabs": "ElevenLabs"}


async def _org_providers(db: AsyncSession, id_org: int) -> set[str]:
    """Providers de génération dont l'org a une clé (filtre le catalogue exposé à l'UI)."""
    rows = await db.execute(
        select(CmsOrgAiCredential.provider).where(CmsOrgAiCredential.id_org == id_org)
    )
    return {p for (p,) in rows.all()}


@router.get("/catalog")
async def get_catalog(
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Catalogue des modèles (filtré sur les providers dont l'org a une clé) + tarifs/params.
    L'UI s'en sert pour les contrôles dynamiques par type et l'estimatif de coût en direct."""
    providers = await _org_providers(db, current_user.id_org)
    models = catalog.models_for(providers=providers)
    models = await _with_account_voices(db, current_user, models, providers)
    return {"providers": sorted(providers), "models": models}


async def _with_account_voices(
    db: AsyncSession, current_user: CmsUser, models: list[dict], providers: set[str]
) -> list[dict]:
    """Propose les voix que le compte ElevenLabs de l'org détient réellement.

    Joindre un provider depuis l'endpoint du catalogue est une exception ASSUMÉE : sans elle
    l'écran liste des voix que la clé refusera, et ne le dit qu'une fois le texte écrit et le
    bouton pressé. Tout échec — pas de clé, provider en panne, lenteur — laisse la liste du
    catalogue en place : un formulaire qui s'ouvre avec les mauvaises voix vaut mieux qu'un
    formulaire qui ne s'ouvre pas.
    """
    if "elevenlabs" not in providers:
        return models
    try:
        org = (await db.execute(
            select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
        )).scalar_one_or_none()
        resolved = await resolve_ai_credentials(db, org, "elevenlabs") if org else None
        if resolved is None or not resolved.api_key:
            return models
        voices = await run_in_threadpool(elevenlabs.list_voices, api_key=resolved.api_key)
    except Exception:  # noqa: BLE001 — jamais d'échec de formulaire pour une liste de voix
        logger.warning(
            "voix ElevenLabs illisibles pour l'org %s — catalogue laissé en place",
            current_user.id_org, exc_info=True,
        )
        return models
    return catalog.with_account_voices(models, voices)


async def _require_provider_key(db: AsyncSession, current_user: CmsUser, provider: str):
    """Résout la clé d'un provider de génération pour l'org (strict BYOK). HTTP 400 si absente."""
    org = (
        await db.execute(
            select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
        )
    ).scalar_one_or_none()
    resolved = await resolve_ai_credentials(db, org, provider) if org is not None else None
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No {_PROVIDER_LABEL.get(provider, provider)} key configured "
                   f"for this organization (Settings → AI keys).",
        )
    return resolved


@router.post("/image", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def generate_image_endpoint(
    body: GenerateImageRequest,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    prompt = body.prompt.strip()
    resolved = await _require_provider_key(db, current_user, "fal")
    model = body.model or fal.DEFAULT_IMAGE_MODEL
    spec = catalog.get_model(model)
    # Les modèles d'édition (détourage) n'exigent pas de prompt — ils transforment la référence.
    if not prompt and not (spec and spec.get("prompt_optional")):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty prompt")
    if spec and spec.get("input") == "image" and not body.reference_media_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="this model requires at least one reference image",
        )
    image_urls = await refs.data_uris_for_media(db, current_user.id_org, body.reference_media_ids)
    # Plafond de dépense BYOK (P3.2) : réservé AVANT l'appel — la seule position où un refus
    # ne coûte rien au client. Ce chemin n'écrivait AUCUNE ligne de job : sa dépense était
    # totalement invisible, alors que l'image est la génération la plus répétée.
    id_spend = await spend.reserve(
        db, id_org=current_user.id_org, id_user=current_user.id_user, provider="fal",
        kind="image", model=model,
        estimated_usd=catalog.estimate_cost(
            model, params=body.params, aspect_ratio=body.aspect_ratio),
    )
    try:
        img = await fal.generate_image(
            prompt=prompt,
            api_key=resolved.api_key,
            model=model,
            aspect_ratio=body.aspect_ratio,
            width=body.width,
            height=body.height,
            params=body.params or {},
            image_urls=image_urls,
        )
    except fal.GenerationError as exc:
        # ⚠️ On SOLDE, on ne relâche pas : un échec fal peut survenir APRÈS que l'appel a été
        # facturé. Mieux vaut compter une dépense qui n'a pas eu lieu que d'ignorer celle qui
        # a eu lieu — c'est l'argent du client.
        await spend.settle(db, id_spend, None)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Generation failed: {exc}"
        ) from exc
    await spend.settle(db, id_spend, img.cost_usd)

    ext = (img.content_type.split("/")[-1] or "jpg").replace("jpeg", "jpg")
    default_name = "Edited image" if (spec and spec.get("op") == "edit") else "AI image"
    name = ((body.name or "").strip() or default_name)[:120]
    storage_key = make_storage_key(current_user.id_org, f"{name}.{ext}")
    sha256 = compute_sha256(img.content)
    await upload_file(img.content, storage_key, img.content_type)

    media = CmsMedia(
        id_org=current_user.id_org,
        media_name=name,
        media_type="image",
        mime_type=img.content_type,
        storage_key=storage_key,
        file_size_bytes=len(img.content),
        width=img.width,
        height=img.height,
        checksum_sha256=sha256,
        source="generated_ai",
        transcode_status="pending",
        created_by=current_user.id_user,
        media_metadata={
            "generation": {
                "provider": "fal",
                "model": model,
                "prompt": prompt,
                "aspect_ratio": body.aspect_ratio,
                "params": body.params or {},
                "cost_usd": img.cost_usd,
            }
        },
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)

    result = MediaRead.model_validate(media)
    result.cdn_url_signed = get_signed_url(storage_key)
    return result


@router.post("/video", response_model=GenerationJobRead, status_code=status.HTTP_202_ACCEPTED)
async def generate_video_endpoint(
    body: GenerateVideoRequest,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationJobRead:
    """Lance une génération vidéo ASYNCHRONE (fal, file) → job pending. Poll via GET /generation."""
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty prompt")
    await _require_provider_key(db, current_user, "fal")  # 400 si pas de clé (relue dans le worker)
    model = body.model or fal.DEFAULT_VIDEO_MODEL
    spec = catalog.get_model(model)
    if spec and spec.get("input") == "image" and not body.reference_media_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="this model requires at least one reference image",
        )
    params = dict(body.params or {})
    if body.reference_media_ids:  # résolues en data URIs dans le worker (pas envoyées brutes)
        params["reference_media_ids"] = body.reference_media_ids
    if body.name and body.name.strip():  # nom du média (UI-only, lu par le worker)
        params["name"] = body.name.strip()
    if body.mute:  # retire la bande audio du master (ffmpeg dans le worker)
        params["strip_audio"] = True
    est = catalog.estimate_cost(model, params=body.params, aspect_ratio=body.aspect_ratio)
    # Réservé à la MISE EN FILE, pas au moment de l'appel : sans ça, dix vidéos lancées d'un
    # coup passent toutes le contrôle avant que la première n'ait rien dépensé.
    id_spend = await spend.reserve(
        db, id_org=current_user.id_org, id_user=current_user.id_user, provider="fal",
        kind="video", model=model, estimated_usd=est,
    )
    job = CmsGenerationJob(
        id_org=current_user.id_org,
        kind="video",
        provider="fal",
        model=model,
        prompt=prompt,
        aspect_ratio=body.aspect_ratio,
        params=params,
        status="pending",
        id_spend=id_spend,
        cost_usd=est,
        created_by=current_user.id_user,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.workers.generation_worker import generate_content
    generate_content.delay(job.id_job)

    return GenerationJobRead.model_validate(job)


async def _enforce_elevenlabs_quota(api_key: str, characters: int) -> None:
    """Refuse une synthèse vocale que l'offre du compte ne peut pas payer.

    Uniquement pour `voice` : ElevenLabs facture la parole en CARACTÈRES du texte, donc la
    comparaison est exacte. La musique se facture dans une tout autre unité, et deviner la
    conversion produirait des refus sur lesquels personne ne pourrait agir.

    Un quota qu'on ne sait pas lire n'est PAS un refus : `None` veut dire que la question
    n'a pas pu être tranchée — pas de réseau, provider en panne — et un hoquet du provider
    ne doit pas arrêter un travail qui serait passé.
    """
    try:
        quota = await run_in_threadpool(elevenlabs.get_quota, api_key=api_key)
    except Exception:  # noqa: BLE001 — l'inconnu ne bloque jamais
        logger.warning("quota ElevenLabs illisible — génération laissée passer", exc_info=True)
        return
    if quota is None or characters <= quota.remaining:
        return
    when = ""
    if quota.resets_at is not None:
        when = f" Quota resets on {quota.resets_at.strftime('%Y-%m-%d')}."
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=(f"Insufficient ElevenLabs quota: {characters} characters requested, "
                f"{quota.remaining} remaining out of {quota.limit} ({quota.tier} plan)." + when),
    )


@router.post("/audio", response_model=GenerationJobRead, status_code=status.HTTP_202_ACCEPTED)
async def generate_audio_endpoint(
    body: GenerateAudioRequest,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationJobRead:
    """Lance une génération audio ASYNCHRONE (musique/voix). Provider sélectionnable
    (fal pour les tests, elevenlabs pour les clients) — 400 si la clé n'existe pas."""
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty text/description"
        )
    resolved = await _require_provider_key(db, current_user, body.provider)
    if body.provider == "elevenlabs" and body.kind == "voice":
        await _enforce_elevenlabs_quota(resolved.api_key, len(prompt))

    if body.model:
        model = body.model
    elif body.provider == "fal":
        model = fal.DEFAULT_MUSIC_MODEL if body.kind == "music" else fal.DEFAULT_VOICE_MODEL
    else:  # ElevenLabs : ids catalogue dédiés (cost estimate + params voix/musique)
        model = "elevenlabs/music" if body.kind == "music" else "elevenlabs/tts"

    params = dict(body.params or {})
    if body.name and body.name.strip():  # nom du média (UI-only, lu par le worker)
        params["name"] = body.name.strip()
    est = catalog.estimate_cost(
        model, params=body.params, text=(prompt if body.kind == "voice" else None))
    id_spend = await spend.reserve(
        db, id_org=current_user.id_org, id_user=current_user.id_user,
        provider=body.provider, kind=body.kind, model=model, estimated_usd=est,
    )
    job = CmsGenerationJob(
        id_org=current_user.id_org,
        kind=body.kind,
        provider=body.provider,
        model=model,
        prompt=prompt,
        params=params,
        status="pending",
        id_spend=id_spend,
        cost_usd=est,
        created_by=current_user.id_user,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.workers.generation_worker import generate_content
    generate_content.delay(job.id_job)

    return GenerationJobRead.model_validate(job)


@router.get("/spend")
async def generation_spend(
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Dépense IA de la période courante + plafonds (P3.2).

    Ouvert à qui peut GÉNÉRER, pas seulement à l'administrateur : c'est celui qui va dépenser
    qui a besoin de savoir ce qu'il reste. Un plafond découvert au moment du refus est un
    plafond qui ressemble à une panne.
    """
    return await spend.usage(db, current_user.id_org, id_user=current_user.id_user)


@router.post("/prompt-assist", response_model=PromptAssistResponse)
async def prompt_assist(
    body: PromptAssistRequest,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PromptAssistResponse:
    """Aide OPTIONNELLE à la rédaction de prompt : idée (FR/EN) → prompt optimisé (anglais) +
    negative prompt model-aware. BYOK (comme l'assistant) ; strict / LLM indispo → repli brut."""
    last = body.messages[-1].content if body.messages else ""
    raw = (body.intent or last).strip()
    if not raw:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty idea")

    spec = catalog.get_model(body.model) if body.model else None
    reference_present = bool(body.reference_media_ids) or bool(
        spec and spec.get("input") == "image"
    )
    supports_neg = bool(spec and spec.get("supports_negative_prompt"))

    use_llm, byok_key = await _resolve_creative_llm(db, current_user)
    if not use_llm:  # strict / pas de clé → repli brut, non assisté (l'UI le signale)
        return PromptAssistResponse(
            **prompt_svc.fallback(raw), supports_negative_prompt=supports_neg
        )

    # Charte de marque (P3.4) : celle de l'annonceur visé, sinon celle de l'org, sinon aucune —
    # et l'assistant est alors exactement ce qu'il était.
    brand = await brand_kit.resolve_brand_kit(db, current_user.id_org, body.advertiser_id)

    result = await prompt_svc.assist_prompt(
        kind=body.kind,
        model_id=body.model,
        reference_present=reference_present,
        mode=body.mode,
        intent=body.intent,
        messages=[m.model_dump() for m in body.messages],
        locale=(getattr(current_user, "locale", None) or "en"),
        byok_key=byok_key,
        brand=brand,
    )
    return PromptAssistResponse(**result, supports_negative_prompt=supports_neg)


@router.get("", response_model=list[GenerationJobRead])
async def list_jobs(
    _: Annotated[CmsUser, Depends(require_any_permission(("generation", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
    active: bool = False,
    campaign: int | None = None,
) -> list[GenerationJobRead]:
    """Jobs de génération de l'org (RLS) — récents, actifs (`?active=true`, polling UI), ou
    ceux d'une campagne (`?campaign=ID`, suivi du fan-out dans le Studio)."""
    stmt = select(CmsGenerationJob).order_by(CmsGenerationJob.id_job.desc())
    if campaign is not None:
        stmt = stmt.where(CmsGenerationJob.id_campaign == campaign)
    elif active:
        stmt = stmt.where(CmsGenerationJob.status.in_(["pending", "processing"]))
    else:
        stmt = stmt.limit(20)
    result = await db.execute(stmt)
    return [GenerationJobRead.model_validate(j) for j in result.scalars()]


# ── Campagne one-shot (§ génération — Phase 4) ──────────────────────────────────

async def _resolve_creative_llm(db: AsyncSession, current_user: CmsUser) -> tuple[bool, str | None]:
    """Politique BYOK (comme l'assistant) pour le directeur créatif : (use_llm, byok_key).
    strict sans clé → (False, None) = repli brut ; byok → (True, clé) ; platform/super → proxy."""
    roles = await rbac.get_user_role_keys(db, current_user.id_user)
    if "CMS_SUPER_ADMIN" in roles:
        return True, None  # proxy plateforme
    org = (
        await db.execute(
            select(CmsOrganization).where(CmsOrganization.id_org == current_user.id_org)
        )
    ).scalar_one_or_none()
    resolved = await resolve_ai_credentials(db, org, "anthropic") if org is not None else None
    if resolved is None:
        return False, None
    return True, (resolved.api_key if resolved.byok else None)


@router.post("/campaign/plan", response_model=CampaignPlanResponse)
async def campaign_plan(
    body: CampaignPlanRequest,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignPlanResponse:
    """Claude = directeur créatif : brief → concepts (prompts soignés). Aucun coût engagé."""
    brief = body.brief.strip()
    if not brief:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty brief")
    use_llm, byok_key = await _resolve_creative_llm(db, current_user)
    if use_llm:
        plan = await campaign_svc.plan_campaign(
            brief=brief, n_concepts=body.n_concepts, types=body.types, byok_key=byok_key
        )
    else:
        plan = campaign_svc.fallback_plan(brief, body.n_concepts, body.types)
    return CampaignPlanResponse(**plan)


@router.post("/campaign", response_model=CampaignRead, status_code=status.HTTP_202_ACCEPTED)
async def campaign_execute(
    body: CampaignExecuteRequest,
    current_user: Annotated[CmsUser, Depends(require_any_permission(("generation", "create"), ("media", "create")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignRead:
    """Fan-out multi-type : images (concepts×ratios) + vidéo + voix + musique. BYOK strict."""
    types = list(body.types or ["image"])
    ratios = body.ratios or ["16:9"]
    refs = body.reference_media_ids or []

    def _require_model(model_id: str | None, kind: str, provider: str) -> str:
        mid = model_id or catalog.default_model(kind, {provider})
        spec = catalog.get_model(mid) if mid else None
        if spec is None or spec["kind"] != kind or spec["provider"] != provider:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid {kind} model",
            )
        return mid

    # Spécifications de jobs (kind, provider, model, prompt, aspect_ratio, params) à créer.
    specs: list[dict] = []

    if "image" in types:
        await _require_provider_key(db, current_user, "fal")
        img_model = _require_model(body.model, "image", "fal")
        img_is_i2i = (catalog.get_model(img_model) or {}).get("input") == "image"
        for c in body.concepts:
            if not c.prompt.strip():
                continue
            for r in ratios:
                p = {"reference_media_ids": refs} if (refs and img_is_i2i) else {}
                if c.title and c.title.strip():  # nom du média = titre du concept (pas le prompt)
                    p["name"] = c.title.strip()
                specs.append({"kind": "image", "provider": "fal", "model": img_model,
                              "prompt": c.prompt.strip(), "aspect_ratio": r, "params": p})

    if "video" in types and (body.video_prompt or "").strip():
        await _require_provider_key(db, current_user, "fal")
        vmodel = _require_model(body.video_model, "video", "fal")
        v_is_i2v = (catalog.get_model(vmodel) or {}).get("input") == "image"
        vrefs = body.video_reference_media_ids or []
        vneg = (body.video_negative_prompt or "").strip()
        for r in body.video_ratios or ["16:9"]:
            p: dict = {"reference_media_ids": vrefs} if (vrefs and v_is_i2v) else {}
            if vneg:  # la garde fal retire le negative si le modèle ne le supporte pas
                p["negative_prompt"] = vneg
            if body.video_mute:  # retire la bande audio du master (ffmpeg dans le worker)
                p["strip_audio"] = True
            specs.append({"kind": "video", "provider": "fal", "model": vmodel,
                          "prompt": body.video_prompt.strip(), "aspect_ratio": r, "params": p})

    if "voice" in types and (body.voiceover or "").strip():
        await _require_provider_key(db, current_user, body.voice_provider)
        vmodel = _require_model(body.voice_model, "voice", body.voice_provider)
        specs.append({"kind": "voice", "provider": body.voice_provider, "model": vmodel,
                      "prompt": body.voiceover.strip(), "aspect_ratio": None, "params": {}})

    if "music" in types and (body.music_prompt or "").strip():
        await _require_provider_key(db, current_user, body.music_provider)
        mmodel = _require_model(body.music_model, "music", body.music_provider)
        specs.append({"kind": "music", "provider": body.music_provider, "model": mmodel,
                      "prompt": body.music_prompt.strip(), "aspect_ratio": None, "params": {}})

    if not specs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no asset to generate"
        )
    if len(specs) > MAX_CAMPAIGN_ASSETS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"too many assets ({len(specs)} > {MAX_CAMPAIGN_ASSETS})",
        )

    camp = CmsGenerationCampaign(
        id_org=current_user.id_org, brief=body.brief.strip(),
        model=body.model or fal.DEFAULT_IMAGE_MODEL,
        ratios=ratios, status="pending", created_by=current_user.id_user,
    )
    db.add(camp)
    await db.commit()
    await db.refresh(camp)

    total = 0.0
    jobs: list[CmsGenerationJob] = []
    for s in specs:
        cost = catalog.estimate_cost(
            s["model"], params=s["params"], aspect_ratio=s["aspect_ratio"],
            text=(s["prompt"] if s["kind"] == "voice" else None),
        )
        total += cost
        # Une réservation PAR ASSET, pas une pour la campagne : le plafond doit pouvoir
        # arrêter une campagne AU MILIEU. Réserver le total d'un bloc rendrait le refus
        # tout-ou-rien alors que le client paie asset par asset.
        id_spend = await spend.reserve(
            db, id_org=current_user.id_org, id_user=current_user.id_user,
            provider=s["provider"], kind=s["kind"], model=s["model"], estimated_usd=cost,
        )
        jobs.append(CmsGenerationJob(
            id_org=current_user.id_org, kind=s["kind"], provider=s["provider"], model=s["model"],
            prompt=s["prompt"], aspect_ratio=s["aspect_ratio"], params=s["params"],
            status="pending", cost_usd=cost, id_campaign=camp.id_campaign,
            id_spend=id_spend, created_by=current_user.id_user,
        ))
    db.add_all(jobs)
    camp.total_cost_usd = round(total, 4)
    await db.commit()
    await db.refresh(camp)

    from app.workers.generation_worker import generate_content
    for j in jobs:
        generate_content.delay(j.id_job)

    return _campaign_read(camp, total=len(jobs), ready=0, failed=0)


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(
    _: Annotated[CmsUser, Depends(require_any_permission(("generation", "read"), ("media", "read")))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CampaignRead]:
    """Campagnes récentes de l'org (RLS) avec l'avancement du fan-out (pour le Studio)."""
    camps = (
        await db.execute(
            select(CmsGenerationCampaign).order_by(CmsGenerationCampaign.id_campaign.desc()).limit(20)
        )
    ).scalars().all()
    out = []
    for c in camps:
        rows = (
            await db.execute(
                select(CmsGenerationJob.status).where(CmsGenerationJob.id_campaign == c.id_campaign)
            )
        ).scalars().all()
        ready = sum(1 for s in rows if s == "ready")
        failed = sum(1 for s in rows if s == "failed")
        out.append(_campaign_read(c, total=len(rows), ready=ready, failed=failed))
    return out


def _campaign_read(
    c: CmsGenerationCampaign, *, total: int, ready: int, failed: int
) -> CampaignRead:
    r = CampaignRead.model_validate(c)
    r.jobs_total, r.jobs_ready, r.jobs_failed = total, ready, failed
    return r
