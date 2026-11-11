"""Chiffrement au repos des secrets applicatifs (Fernet — AES-128-CBC + HMAC authentifié).

⚠️ PREMIER helper de chiffrement du projet — introduit pour le coffre de clés API par
organisation (BYOK, §XXVII ; cf. docs/PLAN_BYOK_AI_KEYS.md). Avant lui, aucun champ-secret
n'était chiffré au repos dans le backend.

La clé maître vient de `settings.CMS_VAULT_KEY` (clé Fernet base64 urlsafe, 32 octets).
Si elle est absente/invalide, `encrypt_secret`/`decrypt_secret` lèvent `VaultKeyMissing`
→ les endpoints renvoient 503 explicite (JAMAIS de stockage en clair par accident).

Générer une clé :
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

⚠️ La perdre = toutes les clés stockées deviennent irrécupérables (re-saisie nécessaire).
À sauvegarder au même niveau de soin que CMS_JWT_SECRET, et à définir dans backend ET
celery-worker (feed_ai/feedback_ai tournent dans le worker).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class VaultKeyMissing(RuntimeError):
    """CMS_VAULT_KEY non configurée ou invalide → chiffrement indisponible."""


class VaultDecryptError(RuntimeError):
    """Token illisible (clé de coffre changée, donnée corrompue)."""


def _fernet() -> Fernet:
    key = settings.CMS_VAULT_KEY
    if not key:
        raise VaultKeyMissing("CMS_VAULT_KEY is not configured")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise VaultKeyMissing(f"CMS_VAULT_KEY invalid: {exc}") from exc


def vault_available() -> bool:
    """True si le coffre est utilisable (clé présente et valide)."""
    try:
        _fernet()
        return True
    except VaultKeyMissing:
        return False


def encrypt_secret(plaintext: str) -> str:
    """Chiffre un secret → token urlsafe stockable en TEXT."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Déchiffre un token produit par `encrypt_secret`."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise VaultDecryptError("unreadable secret (vault key changed?)") from exc
