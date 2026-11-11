"""Coffre-fort des clés API IA par organisation — stockage chiffré + résolution (BYOK §XXVII).

Décision produit verrouillée (Ed, cf. docs/PLAN_BYOK_AI_KEYS.md) : AUCUN repli automatique
sur la clé plateforme. La résolution suit :

  - clé org présente                                              → clé org (byok=True, le client paie)
  - pas de clé org + provider == "anthropic" + org.ai_byok_mode == "platform"
                                                                  → clé plateforme (byok=False, quota appliqué)
  - sinon (mode strict, OU provider de génération fal/elevenlabs sans clé)
                                                                  → None (fonction IA désactivée)

`byok=True` ⇒ le quota tokens (§XXVII, migr 0047/0048) n'est PAS appliqué (le client paie son
propre fournisseur). `byok=False` ⇒ quota appliqué comme aujourd'hui.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import VaultDecryptError, decrypt_secret, encrypt_secret
from app.models.organization import CmsOrgAiCredential, CmsOrganization

# Providers reconnus. anthropic = LLM (assistant / résumé feeds / triage feedback) ;
# fal + elevenlabs = génération de contenu (strict BYOK d'office, aucune clé plateforme).
KNOWN_PROVIDERS: tuple[str, ...] = ("anthropic", "fal", "elevenlabs")
GENERATION_PROVIDERS: tuple[str, ...] = ("fal", "elevenlabs")


@dataclass
class ResolvedKey:
    api_key: str
    byok: bool   # True = clé de l'org (client paie) → quota NON appliqué
    source: str  # 'org' | 'platform'


def _last4(api_key: str) -> str | None:
    return api_key[-4:] if len(api_key) >= 4 else (api_key or None)


async def get_org_key_row(
    db: AsyncSession, id_org: int, provider: str
) -> CmsOrgAiCredential | None:
    res = await db.execute(
        select(CmsOrgAiCredential).where(
            CmsOrgAiCredential.id_org == id_org,
            CmsOrgAiCredential.provider == provider,
        )
    )
    return res.scalar_one_or_none()


async def upsert_org_key(
    db: AsyncSession,
    id_org: int,
    provider: str,
    api_key: str,
    created_by: int | None = None,
) -> CmsOrgAiCredential:
    """Chiffre et stocke (ou remplace) la clé d'un provider. Repasse le statut à 'unverified'."""
    enc = encrypt_secret(api_key)
    now = datetime.now(timezone.utc)
    row = await get_org_key_row(db, id_org, provider)
    if row is None:
        row = CmsOrgAiCredential(
            id_org=id_org,
            provider=provider,
            secret_encrypted=enc,
            key_last4=_last4(api_key),
            status="unverified",
            created_by=created_by,
            updated_at=now,
        )
        db.add(row)
    else:
        row.secret_encrypted = enc
        row.key_last4 = _last4(api_key)
        row.status = "unverified"
        row.validated_at = None
        row.updated_at = now
    await db.flush()
    return row


async def delete_org_key(db: AsyncSession, id_org: int, provider: str) -> bool:
    """Supprime la clé d'un provider (retour au comportement par défaut). True si supprimée."""
    row = await get_org_key_row(db, id_org, provider)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def resolve_ai_credentials(
    db: AsyncSession, org: CmsOrganization, provider: str
) -> ResolvedKey | None:
    """Retourne la clé à utiliser pour (org, provider), ou None si l'IA est désactivée.

    AUCUN repli automatique sur la clé plateforme : seule une clé org, ou Anthropic
    explicitement autorisé en mode 'platform', est rendue.
    """
    row = await get_org_key_row(db, org.id_org, provider)
    if row is not None:
        try:
            plaintext = decrypt_secret(row.secret_encrypted)
        except VaultDecryptError:
            plaintext = ""  # clé de coffre changée / donnée corrompue → traitée comme absente
        if plaintext:
            return ResolvedKey(api_key=plaintext, byok=True, source="org")

    # Mode platform (Anthropic uniquement) → on passe par le PROXY LiteLLM, qui détient la clé
    # plateforme. On ne dépend PAS de settings.ANTHROPIC_API_KEY côté backend (le proxy en est la
    # source) → byok=False signale au caller d'utiliser le proxy. fal/elevenlabs n'ont pas de
    # plateforme → jamais ici. Aucun repli automatique.
    if provider == "anthropic" and getattr(org, "ai_byok_mode", "strict") == "platform":
        return ResolvedKey(api_key=settings.ANTHROPIC_API_KEY or "", byok=False, source="platform")

    return None


async def validate_provider_key(provider: str, api_key: str) -> tuple[bool | None, str]:
    """Teste une clé provider par un appel cheap. Retour (ok, detail) :
      - True  = clé valide (statut → 'valid')
      - False = clé refusée / erreur réseau (statut → 'invalid')
      - None  = non vérifiable en v1 (ex. fal) → statut laissé 'unverified'.
    """
    try:
        if provider == "anthropic":
            # GET /v1/models = cheap, sans coût de tokens, confirme la clé.
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            return (r.status_code == 200, f"HTTP {r.status_code}")
        if provider == "elevenlabs":
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(
                    "https://api.elevenlabs.io/v1/user",
                    headers={"xi-api-key": api_key},
                )
            return (r.status_code == 200, f"HTTP {r.status_code}")
        if provider == "fal":
            # Pas d'endpoint whoami stable → validée à la 1ʳᵉ génération (sprint génération).
            return (None, "validated on first generation")
    except httpx.HTTPError as exc:
        return (False, f"network error: {exc.__class__.__name__}")
    return (None, "unknown provider")


def sdk_model_name(model: str) -> str:
    """Nom de modèle pour le SDK litellm en process (appel BYOK direct, hors proxy).
    Le proxy utilise l'alias court (`claude-sonnet-4-6`) ; le SDK exige le préfixe provider."""
    return model if "/" in model else f"anthropic/{model}"


def resolve_anthropic_for_worker(conn, id_org: int) -> tuple[str | None, str | None]:
    """Résolution SYNC de la clé Anthropic pour un worker Celery (psycopg2). Retourne :
      ("<clé org>", "org")            → l'org a sa propre clé (le client paie)
      (ANTHROPIC_API_KEY, "platform") → mode platform autorisé + clé plateforme dispo
      (None, None)                    → sinon → IA désactivée (AUCUN repli auto).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT secret_encrypted FROM cms_org_ai_credentials "
            "WHERE id_org = %s AND provider = 'anthropic'",
            (id_org,),
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                return decrypt_secret(row[0]), "org"
            except VaultDecryptError:
                pass
        cur.execute("SELECT ai_byok_mode FROM cms_organizations WHERE id_org = %s", (id_org,))
        o = cur.fetchone()
        mode = (o[0] if o else "strict")
        if mode == "platform":
            # proxy LiteLLM (clé plateforme) → pas de clé renvoyée, l'appel passe par le proxy.
            return None, "platform"
        return None, None
    finally:
        cur.close()


def resolve_provider_key_sync(conn, id_org: int, provider: str) -> str | None:
    """Version SYNC (workers Celery) pour les providers de GÉNÉRATION (fal/elevenlabs) :
    strict BYOK, AUCUNE clé plateforme → renvoie la clé de l'org ou None."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT secret_encrypted FROM cms_org_ai_credentials "
            "WHERE id_org = %s AND provider = %s",
            (id_org, provider),
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                return decrypt_secret(row[0])
            except VaultDecryptError:
                return None
        return None
    finally:
        cur.close()
