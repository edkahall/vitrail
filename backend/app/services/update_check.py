"""Vérification « une MAJ serveur est-elle disponible ? » (self-hosted).

Sonde un MANIFESTE public (`versions.json`) et le compare à la version en cours
d'exécution (`SIGNFLOW_VERSION`). Best-effort et TRANSPARENT : toute erreur réseau
ou tout manifeste illisible = « pas de MAJ connue », jamais une exception qui
remonte à l'appelant (même philosophie que le circuit breaker ERP).

On ne lit PAS les tags du registre : le manifeste seul porte la version
*recommandée* (on peut proscrire une release cassée), les notes de release et un
flag *sécurité*. Il se sonde anonymement, sans identifiants registre.

⚠️ On ne lit QUE `latest` + `releases[]`. Les clés `canary` (version en cours de
validation sur le serveur banc) et `pending` (entrée de release rédigée mais pas
encore promue) sont VOLONTAIREMENT ignorées ici : c'est ce qui garde la cloche des
clients muette tant qu'une version n'a pas été validée puis promue (cf.
deploy/registry/versions.json et deploy/registry/bump-version.py).

Format attendu du manifeste :
    {
      "latest": "1.0.5",
      "releases": [
        {"version": "1.0.5", "date": "2026-07-20", "security": true,
         "notes": {"en": "...", "fr": "..."}},
        {"version": "1.0.4", "date": "2026-07-19", "notes": {"en": "...", "fr": "..."}}
      ]
    }
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Cache process-local : une MAJ serveur n'arrive pas à la seconde, mais un
# correctif de SÉCURITÉ promu ne doit pas attendre des heures avant d'être
# annoncé (l'ancien TTL de 6 h a été vécu comme « la cloche est muette » sur la
# 1.0.18). On ne peut pas savoir qu'une release est marquée `security` sans
# sonder le manifeste : le seul levier est donc la cadence de sonde elle-même.
# 15 min, avec requête CONDITIONNELLE (If-None-Match) : tant que le manifeste
# n'a pas changé, le serveur répond 304 sans corps — le coût d'une sonde est
# alors celui d'un aller-retour HTTP vide.
_CACHE_TTL_SECONDS = 15 * 60  # 15 min
_cache: dict[str, Any] = {"at": 0.0, "manifest": None, "etag": None}


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    """« 1.0.5 » -> (1, 0, 5). Renvoie None pour « dev », vide ou non-semver.

    Un build source ("dev") n'a pas de version comparable : renvoyer None ici
    suffit à rendre la cloche muette sans cas particulier plus haut.
    """
    if not value:
        return None
    parts = value.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    return (major, minor, patch)


async def _fetch_manifest() -> dict[str, Any] | None:
    """Récupère le manifeste (avec cache TTL). None si indisponible/illisible."""
    now = time.monotonic()
    if _cache["manifest"] is not None and (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["manifest"]
    url = settings.SIGNFLOW_UPDATE_MANIFEST_URL
    if not url:
        return None
    try:
        # Requête conditionnelle : si on détient déjà un manifeste et son ETag,
        # le serveur (raw GitHub le supporte) répond 304 sans corps quand rien
        # n'a changé — c'est ce qui rend la cadence de 15 min gratuite.
        headers: dict[str, str] = {}
        if _cache["manifest"] is not None and _cache.get("etag"):
            headers["If-None-Match"] = _cache["etag"]
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 304:
                _cache["at"] = now
                return _cache["manifest"]
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("manifest root is not an object")
    except Exception as exc:  # noqa: BLE001 — best-effort, jamais bloquant
        logger.info("update_check: manifeste indisponible (%s)", exc)
        # On garde le dernier bon manifeste s'il existe (réseau transitoire).
        return _cache["manifest"]
    _cache["at"] = now
    _cache["manifest"] = data
    _cache["etag"] = resp.headers.get("etag")
    return data


def _newer_releases(
    current: tuple[int, int, int],
    releases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sous-liste des releases STRICTEMENT plus récentes, plus récente d'abord."""
    out: list[dict[str, Any]] = []
    for rel in releases:
        ver = parse_version(rel.get("version") if isinstance(rel, dict) else None)
        if ver is not None and ver > current:
            out.append(rel)
    out.sort(key=lambda r: parse_version(r.get("version")) or (0, 0, 0), reverse=True)
    return out


async def check_for_update(current_raw: str, locale: str = "en") -> dict[str, Any]:
    """État de MAJ pour l'UI. Toujours un dict (best-effort).

    - enabled=False si la cloche est coupée (prod multi-tenant).
    - update_available=False si build source ("dev"), manifeste absent, ou déjà à jour.
    - releases = notes des versions plus récentes (localisées), sécurité = union
      des flags security des releases plus récentes (pousse l'urgence de la cloche).
    """
    if not settings.SIGNFLOW_UPDATE_BELL:
        return {"enabled": False, "current": current_raw, "update_available": False}

    current = parse_version(current_raw)
    if current is None:
        # Build source : rien à comparer, cloche muette.
        return {"enabled": True, "current": current_raw, "update_available": False}

    manifest = await _fetch_manifest()
    if not manifest:
        return {"enabled": True, "current": current_raw, "update_available": False}

    releases = manifest.get("releases")
    releases = releases if isinstance(releases, list) else []
    newer = _newer_releases(current, releases)

    # Fallback : manifeste réduit à {"latest": "x"} sans liste de releases.
    latest_raw = manifest.get("latest")
    if not newer:
        latest = parse_version(latest_raw)
        if latest is not None and latest > current:
            newer = [{"version": latest_raw}]

    if not newer:
        latest_str = latest_raw if isinstance(latest_raw, str) else current_raw
        return {"enabled": True, "current": current_raw, "latest": latest_str,
                "update_available": False}

    latest_str = newer[0].get("version") or (latest_raw if isinstance(latest_raw, str) else current_raw)
    security = any(bool(r.get("security")) for r in newer)

    def _notes(rel: dict[str, Any]) -> str | None:
        notes = rel.get("notes")
        if isinstance(notes, dict):
            return notes.get(locale) or notes.get("en") or next(iter(notes.values()), None)
        if isinstance(notes, str):
            return notes
        return None

    return {
        "enabled": True,
        "current": current_raw,
        "latest": latest_str,
        "update_available": True,
        "security": security,
        "releases": [
            {
                "version": r.get("version"),
                "date": r.get("date"),
                "security": bool(r.get("security")),
                "notes": _notes(r),
            }
            for r in newer
        ],
    }
