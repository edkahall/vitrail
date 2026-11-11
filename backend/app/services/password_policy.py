"""
Politique de mot de passe par organisation (audit sécurité 2026-06-25).

Avant ce module, AUCUNE contrainte de longueur/complexité n'était appliquée à la
création/réinitialisation d'un mot de passe — un compte pouvait être créé avec « a ».
Les colonnes `cms_org_auth_settings.password_min_len` / `.password_policy` existaient
en base mais n'étaient lues nulle part.

`validate_password_for_org` charge la politique de l'org (repli sur des valeurs saines
si la ligne de réglages n'existe pas encore) et lève 422 avec un message lisible.
Un PLANCHER ABSOLU (`_ABSOLUTE_MIN_LEN`) empêche qu'une org se mette une longueur
ridiculement basse.
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import CmsOrgAuthSettings

# Repli si l'org n'a pas (encore) de ligne cms_org_auth_settings — aligné sur les
# server_default du modèle.
_DEFAULT_MIN_LEN = 10
_DEFAULT_POLICY = {"uppercase": True, "digit": True, "special": False}

# Plancher dur : même si une org descend son password_min_len plus bas, on n'accepte
# jamais moins que ça.
_ABSOLUTE_MIN_LEN = 8


def check_password(password: str, min_len: int, policy: dict) -> list[str]:
    """Renvoie la liste des exigences NON satisfaites (vide = mot de passe conforme)."""
    effective_min = max(_ABSOLUTE_MIN_LEN, int(min_len or 0))
    missing: list[str] = []
    if len(password) < effective_min:
        missing.append(f"at least {effective_min} characters")
    if policy.get("uppercase") and not any(c.isupper() for c in password):
        missing.append("an uppercase letter")
    if policy.get("lowercase") and not any(c.islower() for c in password):
        missing.append("a lowercase letter")
    if policy.get("digit") and not any(c.isdigit() for c in password):
        missing.append("a digit")
    if policy.get("special") and not any(not c.isalnum() for c in password):
        missing.append("a special character")
    return missing


async def validate_password_for_org(db: AsyncSession, id_org: int, password: str) -> None:
    """Lève 422 si le mot de passe ne respecte pas la politique de l'organisation."""
    cfg = (
        await db.execute(
            select(CmsOrgAuthSettings).where(CmsOrgAuthSettings.id_org == id_org)
        )
    ).scalar_one_or_none()
    min_len = cfg.password_min_len if cfg else _DEFAULT_MIN_LEN
    policy = cfg.password_policy if cfg else _DEFAULT_POLICY

    missing = check_password(password, min_len, policy)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password too weak: it must contain " + ", ".join(missing) + ".",
        )
