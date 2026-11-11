"""Import d'un bundle player (LOT 2, §XXX #4) — détection, fetch anti-SSRF ET orchestration.

Les 4 plateformes INSTALLÉES sur l'appareil (linux, rpi, windows, android) prennent leur MAJ via un
artefact téléchargé. L'opérateur l'importe depuis la page de distribution (par URL) ou par upload ; on
le stocke dans le MinIO du CLIENT + une ligne catalogue à `download_url` RELATIF (le player tire depuis
SON serveur — invariant du #4).

⚠️ `import_bundle()` est la **définition unique** de « enregistrer un bundle chez le client » : elle
sert l'endpoint `POST /updates/versions/import` (opérateur) ET l'amorçage automatique à
l'installation/mise à jour (`scripts/seed_player_bundles.py`, qui déverse l'image OCI
`signflow-player-bundles`). Deux chemins qui écriraient chacun leur clé de stockage ou leur
`download_url` finiraient par diverger — et un player tirerait une URL qui ne sert rien.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import zipfile

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ssrf import url_is_safe
from app.models.player_version import CmsPlayerUpdateVersion
from app.schemas.updates import BundleImportResult
from app.services import media, ota

# Convention de nom de la page de distribution : signflow-player-<os>-<ver>.<ext>
_NAME_RE = re.compile(
    r"signflow-player-(linux|rpi|windows|android|chromeos|brightsign)-(\d+\.\d+\.\d+)"
    r"\.(tar\.gz|apk|zip)$",
    re.IGNORECASE,
)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# APK Android = quelques dizaines de Mo (app entière) ; les tar.gz Electron sont minuscules
# (package.json + src). 256 Mio = marge large sans permettre d'avaler n'importe quoi.
_MAX_BUNDLE_BYTES = 256 * 1024 * 1024

_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"
# Extension + type MIME de l'artefact par plateforme : les Electron reçoivent une archive à
# désarchiver par-dessus leur code, Android un APK installé par PackageInstaller.
BUNDLE_ARTIFACT = {
    "linux": (".tar.gz", "application/gzip"),
    "rpi": (".tar.gz", "application/gzip"),
    "windows": (".tar.gz", "application/gzip"),
    "android": (".apk", "application/vnd.android.package-archive"),
    "chromeos": (".zip", "application/zip"),
    "brightsign": (".zip", "application/zip"),
}

# ── Renderers SERVIS (décision 3 du « canal unique », 2026-07-28) ─────────────────────────
# ChromeOS (PWA) et BrightSign (autorun) ne déballent PAS une archive : ils consomment des
# fichiers INDIVIDUELS par HTTP (le bootstrap fetch `player.js`, `signflow.js`… un à un).
# L'import accepte donc un zip, l'EXTRAIT dans MinIO sous `player-renderers/<plat>/<ver>/…`,
# et `GET /updates/renderers/{plat}/{ver}/{fichier}` sert chaque fichier. ⚠️ Volontairement
# HORS de `ota.BUNDLE_PLATFORMS` : la garde plateforme des déploiements matche déjà ces
# familles par ÉGALITÉ `model_family == player_os` (c'est ainsi que les déploiements renderer
# BS fonctionnent depuis le 22/07) — les y ajouter changerait ce chemin sans nécessité.
RENDERER_PLATFORMS = {"chromeos", "brightsign"}
_RENDERER_MANIFEST = "renderer.json"          # {"platform": "...", "version": "..."} à la racine
_RENDERER_MAX_FILES = 200
_RENDERER_MAX_UNCOMPRESSED = 64 * 1024 * 1024   # renderers réels ≈ quelques centaines de Ko
# Chemin de membre sûr : segments alphanumériques/._- , jamais absolu, jamais « .. ».
_RENDERER_PATH_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*(/[A-Za-z0-9_\-][A-Za-z0-9_\-.]*)*$")
_RENDERER_EXT_MIME = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
    ".html": "text/html", ".json": "application/json", ".map": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain",
    ".wasm": "application/wasm", ".ico": "image/x-icon",
}


def renderer_file_mime(path: str) -> str:
    """Type MIME d'un fichier de renderer servi par `/updates/renderers/…` (extension connue,
    sinon octet-stream — un mauvais MIME sur du JS ferait échouer le chargement en module)."""
    dot = path.rfind(".")
    return _RENDERER_EXT_MIME.get(path[dot:].lower() if dot >= 0 else "", "application/octet-stream")


def meta_from_renderer_zip(data: bytes) -> tuple[str | None, str | None]:
    """(platform, version) lus dans `renderer.json` à la racine du zip, sinon (None, None) —
    même logique de repli que `version_from_targz` (le nom de fichier reste la 2e source)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if _RENDERER_MANIFEST not in zf.namelist():
                return None, None
            meta = json.loads(zf.read(_RENDERER_MANIFEST).decode("utf-8"))
            plat = str(meta.get("platform", "")).strip().lower() or None
            ver = str(meta.get("version", "")).strip() or None
            return plat, ver
    except (zipfile.BadZipFile, OSError, ValueError, json.JSONDecodeError):
        return None, None


def renderer_zip_members(data: bytes) -> list[tuple[str, bytes]]:
    """Fichiers (chemin, contenu) d'un zip de renderer, VALIDÉS : tout membre suspect (chemin
    absolu, « .. », caractères hors liste) ou un zip démesuré REJETTE le bundle entier — on ne
    « skippe » pas en silence un fichier qu'un bootstrap tentera peut-être de charger."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleImportError("Unreadable renderer zip.") from exc
    out: list[tuple[str, bytes]] = []
    total = 0
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise BundleImportError("Empty renderer zip.")
        if len(infos) > _RENDERER_MAX_FILES:
            raise BundleImportError(f"Renderer zip: too many files (max {_RENDERER_MAX_FILES}).")
        for info in infos:
            name = info.filename
            if name == _RENDERER_MANIFEST:
                continue   # le manifeste décrit le colis, il n'est pas servi
            if not _RENDERER_PATH_RE.match(name) or ".." in name.split("/"):
                raise BundleImportError(f"Renderer zip: file path rejected ({name!r}).")
            total += info.file_size
            if total > _RENDERER_MAX_UNCOMPRESSED:
                raise BundleImportError("Renderer zip: uncompressed content too large.")
            out.append((name, zf.read(info)))
    return out


async def inherited_min_firmware(db: AsyncSession, platform: str) -> str | None:
    """BOS minimum à reprendre pour un NOUVEAU renderer BrightSign : celui de la version la plus
    récente qui en porte un, sinon None.

    🔴 POURQUOI (piège rencontré DEUX fois, 06/08 et 07/08). `min_firmware_version` n'est pas dans
    le zip : un renderer importé naissait donc à NULL, et **NULL ne retient rien** — ni
    `ota.resolve_renderer_version` (qui ne sert pas un renderer à un device sous le minimum), ni
    l'orchestration firmware→renderer de `create_deployment`. La garde disparaissait en silence à
    chaque publication, et il fallait la reposer à la main par `PATCH /updates/versions/{id}`.
    Reprendre la valeur précédente est ce qu'un humain faisait chaque fois : c'est aussi le seul
    défaut sûr (au pire on retient un renderer de trop, jamais on ne le sert trop tôt).
    Elle reste modifiable par l'endpoint de mise à jour, et l'import l'ANNONCE dans son résultat —
    une valeur héritée en silence serait un autre angle mort.
    """
    if platform != "brightsign":
        return None
    rows = (await db.execute(
        select(CmsPlayerUpdateVersion.version, CmsPlayerUpdateVersion.min_firmware_version).where(
            CmsPlayerUpdateVersion.track == "player",
            CmsPlayerUpdateVersion.model_family == platform,
            CmsPlayerUpdateVersion.min_firmware_version.isnot(None),
        )
    )).all()
    if not rows:
        return None
    return max(rows, key=lambda r: ota.semver_key(r.version)).min_firmware_version


class BundleImportError(ValueError):
    """Bundle refusé (plateforme/version indétectable, type d'artefact incohérent, contenu vide).

    L'endpoint la traduit en 4xx, le script d'amorçage en ligne ignorée + message. Une seule
    définition des motifs de refus pour les deux appelants.
    """


def parse_bundle_filename(name: str) -> tuple[str | None, str | None]:
    """(platform, version) depuis un nom conventionnel `signflow-player-<os>-<ver>.<ext>`, sinon
    (None, None). Sert de source par défaut, l'opérateur peut toujours corriger dans l'UI."""
    if not name:
        return None, None
    m = _NAME_RE.search(name.strip())
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def version_from_targz(data: bytes) -> str | None:
    """Version lue dans le `package.json` À LA RACINE de l'archive tar.gz — là même où le player
    l'extrait (`tar -xzf … -C appRoot` remplace package.json + src). Robuste : archive corrompue /
    package.json absent / JSON invalide → None (l'appelant retombe sur le nom de fichier)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                nm = member.name.lstrip("./")
                if nm == "package.json" and member.isfile():
                    f = tf.extractfile(member)
                    if f is None:
                        return None
                    pkg = json.loads(f.read().decode("utf-8"))
                    v = str(pkg.get("version", "")).strip()
                    return v or None
    except (tarfile.TarError, OSError, ValueError, json.JSONDecodeError):
        return None
    return None


async def fetch_bundle(url: str) -> bytes:
    """Télécharge un bundle depuis une URL : garde anti-SSRF + plafond de taille, SANS suivre les
    redirections (un 3xx pourrait pointer vers une IP interne = contournement SSRF). Lève ValueError
    avec un motif clair (l'endpoint le remonte en 400)."""
    if not url_is_safe(url, schemes=("http", "https")):
        raise ValueError("URL not allowed (scheme or host).")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise ValueError(f"Download failed: HTTP {resp.status_code}.")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_BUNDLE_BYTES:
                        raise ValueError("Archive too large (max 256 MiB).")
                return bytes(buf)
    except httpx.HTTPError as exc:
        raise ValueError(f"Download failed: {exc}") from exc


async def import_bundle(
    db: AsyncSession,
    *,
    data: bytes,
    filename: str = "",
    platform: str | None = None,
    version: str | None = None,
    dry_run: bool = False,
) -> BundleImportResult:
    """Enregistre un bundle player chez le client : artefact dans MinIO + ligne au catalogue.

    DÉFINITION UNIQUE, partagée par l'endpoint d'import (opérateur) et l'amorçage automatique
    (image OCI `signflow-player-bundles` déversée à l'install/MAJ). Idempotente des DEUX côtés
    (artefact ET ligne), donc rejouable à chaque mise à jour sans rien dupliquer.

    Lève `BundleImportError` sur tout refus (contenu vide, trop gros, plateforme ou version
    indétectable, type d'artefact incohérent avec la plateforme).
    """
    if not data:
        raise BundleImportError("Empty content.")
    if len(data) > _MAX_BUNDLE_BYTES:
        raise BundleImportError("Archive too large (max 256 MiB).")

    # Plateforme : param explicite > nom de fichier > manifeste `renderer.json` (zip renderer).
    name_platform, name_version = parse_bundle_filename(filename)
    zip_platform, zip_version = (
        meta_from_renderer_zip(data) if data.startswith(_ZIP_MAGIC) else (None, None)
    )
    plat = (platform or name_platform or zip_platform or "").lower()
    is_renderer = plat in RENDERER_PLATFORMS
    if plat not in ota.BUNDLE_PLATFORMS and not is_renderer:
        raise BundleImportError(
            "Unknown or undetectable platform (linux/rpi/windows/android/chromeos/brightsign)."
        )
    ext, mime = BUNDLE_ARTIFACT[plat]

    # Type d'artefact cohérent avec la plateforme (magie) — évite d'importer un .apk en « linux ».
    if ext == ".tar.gz" and not data.startswith(_GZIP_MAGIC):
        raise BundleImportError("Expected a .tar.gz (gzip) archive for this platform.")
    if ext == ".apk" and not data.startswith(_ZIP_MAGIC):
        raise BundleImportError("Expected an .apk (zip) for Android.")
    if ext == ".zip" and not data.startswith(_ZIP_MAGIC):
        raise BundleImportError("Expected a renderer zip for this platform.")

    # Version : param explicite > contenu de l'archive > nom de fichier.
    ver = (version
           or (version_from_targz(data) if ext == ".tar.gz" else None)
           or zip_version or name_version)
    ver = (ver or "").strip()
    if not _SEMVER_RE.match(ver):
        raise BundleImportError("Undetectable version (expected MAJOR.MINOR.PATCH).")

    # Renderer : valider le zip DÈS l'aperçu (un chemin hostile ou un zip démesuré doit refuser
    # le dry_run, pas l'écriture qui suivra la confirmation de l'opérateur).
    members = renderer_zip_members(data) if is_renderer else []

    sha = media.compute_sha256(data)
    key = f"player-bundles/{plat}/{ver}{ext}"
    # RELATIF : le player le résout contre SON backend_url ⇒ un bundle est portable d'une
    # installation à l'autre (invariant du #4 — jamais d'URL pré-signée, qui expire).
    # Renderer : la « bonne adresse » n'est pas l'archive mais la BASE des fichiers individuels
    # que le bootstrap consommera (décision 3).
    download_url = (f"/api/v1/updates/renderers/{plat}/{ver}" if is_renderer
                    else f"/api/v1/updates/bundles/{plat}/{ver}")

    # Dédup : ligne catalogue + artefact MinIO (head_object, sans télécharger).
    existing = (
        await db.execute(
            select(CmsPlayerUpdateVersion).where(
                CmsPlayerUpdateVersion.track == "player",
                CmsPlayerUpdateVersion.version == ver,
                CmsPlayerUpdateVersion.model_family == plat,
            )
        )
    ).scalar_one_or_none()
    artifact_present = await media.object_exists(key)
    # Ligne neuve OU ligne existante qui n'en porte pas (cas réel : les renderers déclarés sans
    # artefact le 07/08) — dans les deux cas la garde manque et doit être reprise.
    existing_fw = existing.min_firmware_version if existing else None
    min_fw = existing_fw or await inherited_min_firmware(db, plat)

    result = BundleImportResult(
        dry_run=dry_run, platform=plat, version=ver, size_bytes=len(data), sha256=sha,
        already_registered=existing is not None, artifact_present=artifact_present,
        id_version=existing.id_version if existing else None, download_url=download_url,
        files_extracted=len(members) if is_renderer else None,
        min_firmware_version=min_fw,
    )
    if dry_run:
        return result

    # Écriture : artefact (si absent) puis ligne catalogue (si absente). Idempotent.
    if not artifact_present:
        await media.upload_file(data, key, mime)
        result.artifact_present = True
    # Renderer : (ré)extraire TOUJOURS les fichiers individuels — idempotent par contenu, et
    # une extraction interrompue (crash entre zip et fichiers) se répare au ré-import au lieu
    # de laisser un bootstrap télécharger des 404.
    for name, content in members:
        await media.upload_file(
            content, f"player-renderers/{plat}/{ver}/{name}", renderer_file_mime(name))
    if existing is None:
        row = CmsPlayerUpdateVersion(
            track="player", version=ver, channel="stable", model_family=plat,
            download_url=download_url, sha256_checksum=sha,
            # Renseigné (la colonne existait et restait vide) : l'écran des mises à jour affiche
            # le poids, et c'est le seul repère de l'opérateur avant de pousser sur un parc en 4G.
            file_size=len(data), original_filename=filename or None,
            min_firmware_version=min_fw,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        result.id_version = row.id_version
    elif (not existing.sha256_checksum or not existing.download_url
          or not existing.file_size or not existing.min_firmware_version):
        # Ligne présente mais INCOMPLÈTE — c'est le cas de réparation réel : une version déclarée
        # au catalogue sans artefact (`download_url` NUL) est déployable en apparence et sert des
        # 404. On aligne ce qui manque, jamais ce qui est déjà renseigné (une valeur posée par
        # l'opérateur, notamment un BOS minimum durci à la main, ne doit pas être écrasée).
        existing.sha256_checksum = existing.sha256_checksum or sha
        existing.download_url = existing.download_url or download_url
        existing.file_size = existing.file_size or len(data)
        existing.original_filename = existing.original_filename or (filename or None)
        existing.min_firmware_version = existing.min_firmware_version or min_fw
        await db.commit()
    return result
