"""Générateur de visuels promotionnels produit — Phase 1 (§9, docs/PLAN_GENERATEUR_PROMO.md).

Compositing DÉTERMINISTE (prix/%/mentions = aucune erreur) : templates built-in + rendu image
Jinja2→Playwright (worker `render_promo_image`, file cms_screenshot). 100% local (aucun fal).

- GET  /promo/templates  : catalogue de templates built-in.
- GET  /promo/presets    : bibliothèque de préréglages de mentions (FR).
- POST /promo/validate   : conformité (lisibilité) — pour le bandeau live de l'UI.
- POST /promo/image      : crée un job de rendu (kind=promo_image) → dispatch worker.
Suivi via GET /generation (le job partage la table cms_generation_jobs).
"""

from typing import Annotated
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_org_admin, require_permission
from app.core.ssrf import url_is_safe
from app.models.audit_log import CmsAuditLog
from app.models.brand_kit import CmsBrandKit
from app.models.campaign import CmsAdvertiser
from app.models.generation import CmsGenerationJob
from app.models.media import CmsMedia
from app.models.org_font import CmsOrgFont
from app.models.organization import CmsOrganization
from app.models.promo_template import CmsPromoTemplate
from app.models.user import CmsUser
from app.schemas.generation import GenerationJobRead
from app.schemas.media import MediaRead
from app.schemas.promo import (
    BrandKitRead,
    BrandKitUpsert,
    PromoBackgroundRequest,
    PromoCutoutRequest,
    PromoExtractRequest,
    PromoExtractResponse,
    PromoImageRequest,
    PromoImportImageRequest,
    PromoSavedCreate,
    PromoSavedListItem,
    PromoSavedRead,
    PromoSavedUpdate,
    PromoValidateRequest,
    PromoValidateResponse,
    PromoVideoRequest,
)
from app.services.ai_credentials import resolve_ai_credentials
from app.services import brand_kit, spend
from app.services.generation import catalog, fal, refs
from app.services.media import (
    compute_sha256,
    delete_file,
    download_file,
    make_storage_key,
    upload_file,
)
from app.services.promo import backgrounds, conformity, fonts, org_fonts, url_extract
from app.services.promo.fonts import list_fonts
from app.services.promo.presets import list_presets
from app.services.promo.templates import RESOLUTIONS, list_templates

router = APIRouter(prefix="/promo", tags=["promo"])


async def _saved_or_404(db: AsyncSession, id_saved: int, id_org: int) -> CmsPromoTemplate:
    row = (await db.execute(
        select(CmsPromoTemplate).where(CmsPromoTemplate.id_promo_template == id_saved,
                                       CmsPromoTemplate.id_org == id_org)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    return row


# Modèle de détourage (background removal) — sortie PNG transparent (bylo.ai, catalog.py).
_CUTOUT_MODEL = "fal-ai/birefnet"
# Modèle de fond (négatif supporté → évite texte/produit/personne dans le fond).
_BG_MODEL = "fal-ai/flux/dev"

_UA = "Mozilla/5.0 (compatible; VitrailPromoBot/1.0)"
_MAX_IMAGE_BYTES = 15 * 1024 * 1024


async def _fetch_safe(url: str, *, accept: str, max_bytes: int) -> tuple[str, bytes, str]:
    """GET anti-SSRF (no-redirect auto : chaque saut est re-validé). Renvoie
    (url_finale, contenu, content_type). Lève HTTPException si non sûr / trop gros / échec."""
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=12.0,
        headers={"User-Agent": _UA, "Accept": accept},
    ) as client:
        for _ in range(4):
            if not url_is_safe(current):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="URL not allowed (internal host or invalid scheme).")
            try:
                resp = await client.get(current)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail=f"Page unreachable: {exc.__class__.__name__}") from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    break
                current = urljoin(current, loc)  # re-validé au tour suivant
                continue
            if resp.status_code >= 400:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail=f"Page returned an error (HTTP {resp.status_code}).")
            clen = resp.headers.get("content-length")
            if clen and clen.isdigit() and int(clen) > max_bytes:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    detail="Content too large.")
            content = resp.content[:max_bytes]
            return str(resp.url), content, resp.headers.get("content-type", "")
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Too many redirects.")


async def _require_fal(db: AsyncSession, id_org: int):
    """Résout la clé fal de l'org (strict BYOK) — 400 si absente."""
    org = (await db.execute(
        select(CmsOrganization).where(CmsOrganization.id_org == id_org)
    )).scalar_one_or_none()
    resolved = await resolve_ai_credentials(db, org, "fal") if org is not None else None
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fal.ai key configured for this organization (Settings → AI keys).",
        )
    return resolved


@router.get("/templates")
async def promo_templates(
    _: Annotated[CmsUser, Depends(require_permission("media", "create"))],
) -> dict:
    """Catalogue de templates built-in + résolutions disponibles par format."""
    return {
        "templates": list_templates(),
        "resolutions": {fmt: list(res.keys()) for fmt, res in RESOLUTIONS.items()},
    }


@router.get("/presets")
async def promo_presets(
    _: Annotated[CmsUser, Depends(require_permission("media", "create"))],
) -> dict:
    """Préréglages de mentions (textes-types FR, à compléter par l'utilisateur)."""
    return {"presets": list_presets()}


@router.get("/saved", response_model=list[PromoSavedListItem])
async def promo_saved_list(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CmsPromoTemplate]:
    """Modèles promo réutilisables de l'org (Phase C), plus récents d'abord."""
    return list((await db.execute(
        select(CmsPromoTemplate).where(CmsPromoTemplate.id_org == current_user.id_org)
        .order_by(CmsPromoTemplate.updated_at.desc())
    )).scalars().all())


@router.get("/saved/{id_saved}", response_model=PromoSavedRead)
async def promo_saved_get(
    id_saved: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPromoTemplate:
    """Charge un modèle enregistré (config complète pour restaurer l'éditeur)."""
    row = await _saved_or_404(db, id_saved, current_user.id_org)
    return row


@router.post("/saved", response_model=PromoSavedRead, status_code=status.HTTP_201_CREATED)
async def promo_saved_create(
    body: PromoSavedCreate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPromoTemplate:
    """Enregistre l'état courant de l'éditeur comme modèle réutilisable."""
    name = (body.name or "").strip()[:120] or "Promo template"
    row = CmsPromoTemplate(id_org=current_user.id_org, name=name, config=body.config or {},
                           created_by=current_user.id_user)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.put("/saved/{id_saved}", response_model=PromoSavedRead)
async def promo_saved_update(
    id_saved: int,
    body: PromoSavedUpdate,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsPromoTemplate:
    """Renomme et/ou écrase un modèle enregistré."""
    row = await _saved_or_404(db, id_saved, current_user.id_org)
    if body.name is not None:
        row.name = (body.name or "").strip()[:120] or row.name
    if body.config is not None:
        row.config = body.config
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/saved/{id_saved}", status_code=status.HTTP_204_NO_CONTENT)
async def promo_saved_delete(
    id_saved: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Supprime un modèle enregistré."""
    row = await _saved_or_404(db, id_saved, current_user.id_org)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/fonts")
async def promo_fonts(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Catalogue de polices : built-in (auto-hébergées) + **polices custom de l'org** (§12.7).
    Les custom portent `files:[{id_font,weight}]` → chargées par le front via `/fonts/file/{id}`."""
    catalog = await org_fonts.org_font_catalog(db, current_user.id_org)
    customs = [{
        "id": fid, "label": e["family"], "family": e["family"], "category": "custom",
        "weights": e["weights"],
        "files": [{"id_font": e["file_ids"][w], "weight": w} for w in e["weights"]],
    } for fid, e in catalog.items()]
    return {"fonts": list_fonts() + customs}


@router.post("/fonts/upload", status_code=status.HTTP_201_CREATED)
async def promo_font_upload(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
    family: str = Form(...),
    weight: int = Form(400),
    license_ack: bool = Form(False),
) -> dict:
    """Upload d'une police custom pour l'org (org-admin). Exige l'**attestation de licence** (§12.7
    — beaucoup d'EULA interdisent l'embarquement ; responsabilité de l'org). Format détecté par
    magic-bytes (woff2/woff/ttf/otf), taille bornée. 1 fichier = (famille, graisse)."""
    if not license_ack:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A license attestation is required to upload a font.")
    fam = (family or "").strip()
    if not (1 <= len(fam) <= 80):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid font family name.")
    # Le nom de famille entre tel quel dans le CSS du composite : on REFUSE les caractères qui
    # permettraient d'en sortir, au lieu de mutiler discrètement ce que l'opérateur a écrit.
    if not fonts.is_safe_family(fam):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid font family name: letters, digits, space, hyphen and period only.")
    if not (100 <= int(weight) <= 900):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid font weight (100-900).")
    data = await file.read()
    if len(data) > org_fonts.MAX_FONT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="Font file too large (max 4 MB).")
    fmt = org_fonts.detect_format(data)
    if fmt not in org_fonts.ALLOWED_FORMATS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Unrecognized format (woff2/woff/ttf/otf expected).")

    existing = (await db.execute(
        select(CmsOrgFont).where(CmsOrgFont.id_org == current_user.id_org,
                                 CmsOrgFont.family == fam, CmsOrgFont.weight == int(weight))
    )).scalar_one_or_none()
    if existing is not None:
        await delete_file(existing.storage_key)  # remplace le fichier de ce (famille, graisse)

    storage_key = "org-fonts/" + make_storage_key(current_user.id_org, f"{fam}.{fmt}")
    await upload_file(data, storage_key, f"font/{fmt}")

    if existing is not None:
        existing.storage_key, existing.font_format = storage_key, fmt
        existing.file_size_bytes, existing.license_ack = len(data), True
        row = existing
    else:
        row = CmsOrgFont(id_org=current_user.id_org, family=fam, weight=int(weight),
                         storage_key=storage_key, font_format=fmt, file_size_bytes=len(data),
                         license_ack=True, created_by=current_user.id_user)
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id_font": row.id_font, "family": row.family, "weight": row.weight, "format": fmt}


@router.get("/fonts/file/{id_font}")
async def promo_font_file(
    id_font: int,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Sert les octets d'une police custom (même origine → chargeable par la FontFace API du front
    sans CORS). RLS garantit qu'on ne lit que les polices de son org."""
    row = (await db.execute(
        select(CmsOrgFont).where(CmsOrgFont.id_font == id_font,
                                 CmsOrgFont.id_org == current_user.id_org)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Font not found.")
    data = await download_file(row.storage_key)
    return Response(content=data, media_type=f"font/{row.font_format}",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/fonts/{id_font}", status_code=status.HTTP_204_NO_CONTENT)
async def promo_font_delete(
    id_font: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Supprime une police custom (org-admin) : ligne + fichier MinIO."""
    row = (await db.execute(
        select(CmsOrgFont).where(CmsOrgFont.id_font == id_font,
                                 CmsOrgFont.id_org == current_user.id_org)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Font not found.")
    await delete_file(row.storage_key)
    await db.execute(sa_delete(CmsOrgFont).where(CmsOrgFont.id_font == id_font))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Chartes de marque (P3.4) ───────────────────────────────────────────────────────
# Lecture large (`media:create` : qui fabrique des visuels a besoin de la charte pour pré-remplir
# son éditeur), écriture réservée à l'org-admin — la charte décide de l'identité de marque, comme
# les polices custom du §12.7.


async def _org_font_catalog(db: AsyncSession, id_org: int) -> dict:
    """Catalogue complet de l'org (built-in + custom) — même composition que le worker de rendu."""
    catalog = fonts.builtin_catalog()
    catalog.update(await org_fonts.org_font_catalog(db, id_org))
    return catalog


async def _validated_kit_fields(db: AsyncSession, id_org: int, body: BrandKitUpsert) -> dict:
    """Champs prêts à écrire. Les couleurs sont déjà validées par le schéma (422) ; restent les
    deux références qui exigent la base : la police doit exister au catalogue et le logo
    appartenir à l'organisation. On REFUSE au moment du geste plutôt que de laisser découvrir au
    rendu que la police n'a pas été appliquée."""
    data = body.model_dump()
    family = (data.get("font_family") or "").strip() or None
    if family:
        catalog = await _org_font_catalog(db, id_org)
        if not any((e or {}).get("family") == family for e in catalog.values()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Font not in the organization's catalog: {family}")
        if not fonts.is_safe_family(family):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid font name.")
    data["font_family"] = family
    if data.get("id_logo_media") is not None:
        logo = (await db.execute(
            select(CmsMedia).where(CmsMedia.id_media == data["id_logo_media"],
                                   CmsMedia.id_org == id_org,
                                   CmsMedia.is_deleted.is_(False))
        )).scalar_one_or_none()
        if logo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Logo not found.")
    return data


async def _upsert_kit(db: AsyncSession, user: CmsUser, body: BrandKitUpsert,
                      id_advertiser: int | None) -> CmsBrandKit:
    data = await _validated_kit_fields(db, user.id_org, body)
    row = (await db.execute(
        select(CmsBrandKit).where(
            CmsBrandKit.id_org == user.id_org,
            CmsBrandKit.id_advertiser == id_advertiser if id_advertiser is not None
            else CmsBrandKit.id_advertiser.is_(None),
        )
    )).scalar_one_or_none()
    if row is None:
        row = CmsBrandKit(id_org=user.id_org, id_advertiser=id_advertiser,
                          created_by=user.id_user, **data)
        db.add(row)
    else:
        for key, value in data.items():
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


async def _advertiser_or_404(db: AsyncSession, id_advertiser: int, id_org: int) -> CmsAdvertiser:
    row = (await db.execute(
        select(CmsAdvertiser).where(CmsAdvertiser.id_advertiser == id_advertiser,
                                    CmsAdvertiser.id_org == id_org)
    )).scalar_one_or_none()
    if row is None:  # 404 et jamais 403 : on ne révèle pas l'existence d'un annonceur voisin
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Advertiser not found.")
    return row


@router.get("/brand-kits", response_model=list[BrandKitRead])
async def brand_kits_list(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BrandKitRead]:
    """Chartes de l'organisation (celle de l'org + une par annonceur qui en a une)."""
    rows = (await db.execute(
        select(CmsBrandKit, CmsAdvertiser.advertiser_name)
        .outerjoin(CmsAdvertiser, CmsAdvertiser.id_advertiser == CmsBrandKit.id_advertiser)
        .where(CmsBrandKit.id_org == current_user.id_org)
        .order_by(CmsBrandKit.id_advertiser.nulls_first(), CmsBrandKit.kit_name)
    )).all()
    return [BrandKitRead(**BrandKitRead.model_validate(kit).model_dump(
        exclude={"advertiser_name"}), advertiser_name=name) for kit, name in rows]


@router.get("/brand-kits/resolved")
async def brand_kit_resolved(
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    advertiser_id: int | None = None,
) -> dict:
    """Charte EFFECTIVE pour ce visuel (annonceur d'abord, repli organisation) — c'est elle qui
    pré-remplit l'éditeur. Elle doit rendre exactement ce que le rendu appliquera, sinon l'aperçu
    ment. `{}` quand aucune charte n'est définie."""
    if advertiser_id is not None:
        await _advertiser_or_404(db, advertiser_id, current_user.id_org)
    kit = await brand_kit.resolve_brand_kit(db, current_user.id_org, advertiser_id)
    return kit or {}


@router.put("/brand-kits/org", response_model=BrandKitRead)
async def brand_kit_upsert_org(
    body: BrandKitUpsert,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsBrandKit:
    """Crée ou met à jour LA charte de l'organisation (il n'y en a qu'une — index unique partiel)."""
    row = await _upsert_kit(db, current_user, body, None)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="update",
        resource_type="brand_kit", resource_id=str(row.id_brand_kit), source="cms",
        detail={"scope": "org", "kit_name": row.kit_name,
                "fields": sorted(body.model_fields_set)},
    ))
    await db.commit()
    return row


@router.put("/brand-kits/advertiser/{id_advertiser}", response_model=BrandKitRead)
async def brand_kit_upsert_advertiser(
    id_advertiser: int,
    body: BrandKitUpsert,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CmsBrandKit:
    """Crée ou met à jour la charte d'un annonceur — elle l'emporte sur celle de l'organisation."""
    adv = await _advertiser_or_404(db, id_advertiser, current_user.id_org)
    row = await _upsert_kit(db, current_user, body, adv.id_advertiser)
    db.add(CmsAuditLog(
        id_org=current_user.id_org, id_user=current_user.id_user, action="update",
        resource_type="brand_kit", resource_id=str(row.id_brand_kit), source="cms",
        detail={"scope": "advertiser", "advertiser_name": adv.advertiser_name,
                "kit_name": row.kit_name, "fields": sorted(body.model_fields_set)},
    ))
    await db.commit()
    return row


@router.delete("/brand-kits/org", status_code=status.HTTP_204_NO_CONTENT)
async def brand_kit_delete_org(
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Retire la charte de l'organisation : les visuels repartent du thème de leur template."""
    await _delete_kit(db, current_user, None, "org")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/brand-kits/advertiser/{id_advertiser}", status_code=status.HTTP_204_NO_CONTENT)
async def brand_kit_delete_advertiser(
    id_advertiser: int,
    current_user: Annotated[CmsUser, Depends(require_org_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Retire la charte d'un annonceur : ses visuels retombent sur celle de l'organisation."""
    adv = await _advertiser_or_404(db, id_advertiser, current_user.id_org)
    await _delete_kit(db, current_user, adv.id_advertiser, "advertiser")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _delete_kit(db: AsyncSession, user: CmsUser, id_advertiser: int | None,
                      scope: str) -> None:
    row = (await db.execute(
        select(CmsBrandKit).where(
            CmsBrandKit.id_org == user.id_org,
            CmsBrandKit.id_advertiser == id_advertiser if id_advertiser is not None
            else CmsBrandKit.id_advertiser.is_(None),
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand kit not found.")
    kit_name, id_kit = row.kit_name, row.id_brand_kit
    await db.execute(sa_delete(CmsBrandKit).where(CmsBrandKit.id_brand_kit == id_kit))
    # Le nom survit à la ligne supprimée — sans lui le journal ne dit pas ce qui a disparu.
    db.add(CmsAuditLog(
        id_org=user.id_org, id_user=user.id_user, action="delete",
        resource_type="brand_kit", resource_id=str(id_kit), source="cms",
        detail={"scope": scope, "kit_name": kit_name},
    ))
    await db.commit()


@router.post("/validate", response_model=PromoValidateResponse)
async def promo_validate(
    body: PromoValidateRequest,
    _: Annotated[CmsUser, Depends(require_permission("media", "create"))],
) -> PromoValidateResponse:
    """Vérifie la conformité (lisibilité) sans rendre — pour le retour live de l'UI."""
    result = conformity.validate(body.template_id, body.format, body.inputs.model_dump())
    return PromoValidateResponse(**result)


@router.post("/image", response_model=GenerationJobRead, status_code=status.HTTP_202_ACCEPTED)
async def promo_image(
    body: PromoImageRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationJobRead:
    """Crée un job de rendu du visuel promo (dispatché au worker Playwright). Barrière de
    conformité serveur : un visuel non conforme (lisibilité) est refusé avant tout rendu."""
    inputs = body.inputs.model_dump()
    verdict = conformity.validate(body.template_id, body.format, inputs)
    if not verdict["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"conformity": verdict["errors"]},
        )
    if body.id_advertiser is not None:  # cible d'une charte : vérifiée AVANT de mettre en file
        await _advertiser_or_404(db, body.id_advertiser, current_user.id_org)

    job = CmsGenerationJob(
        id_org=current_user.id_org,
        kind="promo_image",
        provider="promo",
        model=body.template_id,
        prompt=(body.inputs.slogan or "").strip() or body.template_id,
        aspect_ratio=body.format,
        params={
            "template_id": body.template_id,
            "format": body.format,
            "resolution": body.resolution,
            "inputs": inputs,
            "background_media_id": body.background_media_id,
            "product_media_id": body.product_media_id,
            "name": (body.name or "").strip() or None,
            "id_advertiser": body.id_advertiser,
        },
        status="pending",
        cost_usd=0,
        created_by=current_user.id_user,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.workers.promo_worker import render_promo_image
    render_promo_image.delay(job.id_job)

    return GenerationJobRead.model_validate(job)


@router.post("/video", response_model=GenerationJobRead, status_code=status.HTTP_202_ACCEPTED)
async def promo_video(
    body: PromoVideoRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationJobRead:
    """Crée un job de rendu VIDÉO promo (≤15 s) : même composite déterministe, animé
    (Ken Burns + fade) par FFmpeg. Barrière de conformité serveur avant tout rendu."""
    inputs = body.inputs.model_dump()
    verdict = conformity.validate(body.template_id, body.format, inputs)
    if not verdict["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"conformity": verdict["errors"]},
        )
    if body.id_advertiser is not None:
        await _advertiser_or_404(db, body.id_advertiser, current_user.id_org)
    job = CmsGenerationJob(
        id_org=current_user.id_org, kind="promo_video", provider="promo",
        model=body.template_id, prompt=(body.inputs.slogan or "").strip() or body.template_id,
        aspect_ratio=body.format,
        params={
            "template_id": body.template_id, "format": body.format,
            "resolution": body.resolution, "inputs": inputs,
            "background_media_id": body.background_media_id,
            "product_media_id": body.product_media_id,
            "name": (body.name or "").strip() or None,
            "id_advertiser": body.id_advertiser,
            "video": {"duration_s": body.duration_s, "animation": body.animation},
        },
        status="pending", cost_usd=0, created_by=current_user.id_user,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.workers.promo_worker import render_promo_video
    render_promo_video.delay(job.id_job)
    return GenerationJobRead.model_validate(job)


@router.post("/cutout", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def promo_cutout(
    body: PromoCutoutRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Détoure un produit (retire le fond → PNG transparent) via birefnet (BYOK fal).

    **Cache par hash** : si un détourage existe déjà pour la MÊME image source (même
    checksum), on le renvoie sans re-payer fal. Le PNG transparent devient un `cms_media`
    utilisable comme produit dans le générateur promo."""
    id_org = current_user.id_org
    source = (await db.execute(
        select(CmsMedia).where(
            CmsMedia.id_media == body.source_media_id,
            CmsMedia.id_org == id_org,
            CmsMedia.media_type == "image",
            CmsMedia.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if source is None or not source.storage_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="source image not found")

    # Cache : un détourage déjà produit pour cette source (par checksum) est réutilisé.
    if source.checksum_sha256:
        cached = (await db.execute(
            select(CmsMedia).where(
                CmsMedia.id_org == id_org,
                CmsMedia.is_deleted == False,  # noqa: E712
                CmsMedia.media_metadata["cutout"]["source_sha256"].astext == source.checksum_sha256,
            ).limit(1)
        )).scalar_one_or_none()
        if cached is not None:
            return MediaRead.model_validate(cached)

    resolved = await _require_fal(db, id_org)
    image_urls = await refs.data_uris_for_media(db, id_org, [body.source_media_id])
    # Plafond BYOK (P3.2). Ce chemin écrivait des jobs à `cost_usd = 0` alors que l'appel fal
    # est bien facturé : sa dépense était comptée pour rien.
    id_spend = await spend.reserve(
        db, id_org=id_org, id_user=current_user.id_user, provider="fal",
        kind="promo", model=_CUTOUT_MODEL,
        estimated_usd=catalog.estimate_cost(_CUTOUT_MODEL),
    )
    try:
        img = await fal.generate_image(
            prompt="", api_key=resolved.api_key, model=_CUTOUT_MODEL,
            aspect_ratio=None, width=None, height=None, params={}, image_urls=image_urls,
        )
    except fal.GenerationError as exc:
        await spend.settle(db, id_spend, None)   # dans le doute, on compte
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Background removal failed: {exc}"
        ) from exc
    await spend.settle(db, id_spend, img.cost_usd)

    name = (f"{source.media_name} (background removed)" if source.media_name else "Product (background removed)")[:120]
    storage_key = make_storage_key(id_org, f"{name}.png")
    await upload_file(img.content, storage_key, img.content_type)
    media = CmsMedia(
        id_org=id_org, media_name=name, media_type="image", mime_type=img.content_type,
        storage_key=storage_key, file_size_bytes=len(img.content),
        width=img.width, height=img.height, checksum_sha256=compute_sha256(img.content),
        source="generated_ai", transcode_status="pending", created_by=current_user.id_user,
        media_metadata={
            "cutout": {"source_media_id": body.source_media_id,
                       "source_sha256": source.checksum_sha256},
            "generation": {"provider": "fal", "model": _CUTOUT_MODEL, "cost_usd": img.cost_usd},
        },
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)
    return MediaRead.model_validate(media)


@router.get("/ambiances")
async def promo_ambiances(
    _: Annotated[CmsUser, Depends(require_permission("media", "create"))],
) -> dict:
    """Ambiances built-in pour la génération de fond IA."""
    return {"ambiances": backgrounds.list_ambiances()}


@router.post("/background", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def promo_background(
    body: PromoBackgroundRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Génère un FOND promo par IA (FLUX, BYOK fal) : propre, sans texte/produit/personne,
    avec une zone calme pour le compositing. Le résultat est un `cms_media` utilisable comme
    fond dans le générateur promo."""
    id_org = current_user.id_org
    resolved = await _require_fal(db, id_org)
    prompt, negative = backgrounds.build_background_prompt(
        body.ambiance_id, body.description, body.focus)
    id_spend = await spend.reserve(
        db, id_org=id_org, id_user=current_user.id_user, provider="fal",
        kind="promo", model=_BG_MODEL, estimated_usd=catalog.estimate_cost(_BG_MODEL),
    )
    try:
        img = await fal.generate_image(
            prompt=prompt, api_key=resolved.api_key, model=_BG_MODEL,
            aspect_ratio=body.format, width=None, height=None,
            params={"negative_prompt": negative}, image_urls=[],
        )
    except fal.GenerationError as exc:
        await spend.settle(db, id_spend, None)   # dans le doute, on compte
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Background generation failed: {exc}"
        ) from exc
    await spend.settle(db, id_spend, img.cost_usd)

    name = ((body.name or "").strip() or "AI promo background")[:120]
    ext = (img.content_type.split("/")[-1] or "jpg").replace("jpeg", "jpg")
    storage_key = make_storage_key(id_org, f"{name}.{ext}")
    await upload_file(img.content, storage_key, img.content_type)
    media = CmsMedia(
        id_org=id_org, media_name=name, media_type="image", mime_type=img.content_type,
        storage_key=storage_key, file_size_bytes=len(img.content),
        width=img.width, height=img.height, checksum_sha256=compute_sha256(img.content),
        source="generated_ai", transcode_status="pending", created_by=current_user.id_user,
        media_metadata={
            "promo_background": {"ambiance_id": body.ambiance_id, "focus": body.focus,
                                 "format": body.format},
            "generation": {"provider": "fal", "model": _BG_MODEL, "prompt": prompt,
                           "cost_usd": img.cost_usd},
        },
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)
    return MediaRead.model_validate(media)


@router.post("/extract-url", response_model=PromoExtractResponse)
async def promo_extract_url(
    body: PromoExtractRequest,
    _: Annotated[CmsUser, Depends(require_permission("media", "create"))],
) -> PromoExtractResponse:
    """Extrait les données produit d'une URL e-commerce (JSON-LD → OG → DOM) pour
    **pré-remplir** le formulaire. Aide à la saisie sous responsabilité de l'utilisateur
    (§8 : jamais un crawl de masse ; il valide les données et assume les droits image)."""
    if not url_is_safe(body.url):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid or disallowed URL.")
    final_url, content, _ct = await _fetch_safe(
        body.url, accept="text/html", max_bytes=2 * 1024 * 1024)
    html = content.decode("utf-8", errors="replace")
    return PromoExtractResponse(**url_extract.extract_product(html, final_url))


@router.post("/import-image", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def promo_import_image(
    body: PromoImportImageRequest,
    current_user: Annotated[CmsUser, Depends(require_permission("media", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Importe une image externe (ex. photo produit extraite d'une page) comme `cms_media`,
    utilisable comme produit. L'utilisateur assume les droits (§8)."""
    if not url_is_safe(body.url):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid or disallowed URL.")
    _final, content, content_type = await _fetch_safe(
        body.url, accept="image/*", max_bytes=_MAX_IMAGE_BYTES)
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype.startswith("image/") or not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="The URL does not point to an image.")

    name = ((body.name or "").strip() or "Imported image")[:120]
    ext = (ctype.split("/")[-1] or "jpg").replace("jpeg", "jpg")
    storage_key = make_storage_key(current_user.id_org, f"{name}.{ext}")
    await upload_file(content, storage_key, ctype)
    media = CmsMedia(
        id_org=current_user.id_org, media_name=name, media_type="image", mime_type=ctype,
        storage_key=storage_key, file_size_bytes=len(content),
        checksum_sha256=compute_sha256(content),
        source="upload", transcode_status="pending", created_by=current_user.id_user,
        media_metadata={"imported_from": body.url[:500]},
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    from app.workers.media_worker import transcode_media
    transcode_media.delay(media.id_media)
    return MediaRead.model_validate(media)
