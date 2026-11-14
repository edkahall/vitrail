"""Moteur Electron des players (issue vitrail#5) — récupération, vérification, reconditionnement.

Décision D1 (14/11) : le SERVEUR télécharge UNE fois le zip OFFICIEL d'Electron et son
`SHASUMS256.txt` (ce que `npm install electron` télécharge et vérifie lui-même), vérifie la
somme, puis le reconditionne en `.tar.gz` à racine `dist/` en gardant les droits d'exécution.
Les players n'utilisent alors que `tar` (déjà employé par l'OTA du code, `tar.exe` sous
Windows) et ne sortent jamais sur Internet : ils tirent l'archive de LEUR serveur.

Surface réseau fermée : l'URL est construite par GABARIT à partir d'une version SemVer validée
et d'une famille en liste fermée — aucune URL fournie par l'utilisateur. Les redirections
(GitHub → objects.githubusercontent.com) sont suivies À LA MAIN, chaque saut revalidé par
`ssrf.url_is_safe` contre une liste d'hôtes.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import tarfile
import zipfile
from collections.abc import Callable
from typing import IO

import httpx

from app.core import ssrf

# player_os (= model_family de la version) → nom de plateforme dans les zips Electron.
ELECTRON_PLATFORM = {
    "linux-arm64": "linux-arm64",
    "linux-x64": "linux-x64",
    "windows": "win32-x64",
}
RELEASE_BASE = "https://github.com/electron/electron/releases/download"
ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
MAX_ZIP_BYTES = 300 * 1024 * 1024
MAX_SHASUMS_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class EngineImportError(Exception):
    """Refus explicite (version, famille, somme, archive) — message affichable à l'exploitant."""


def validate(version: str, family: str) -> tuple[str, str]:
    v = (version or "").strip()
    if not _VERSION_RE.match(v):
        raise EngineImportError("Invalid Electron version (expected MAJOR.MINOR.PATCH).")
    if family not in ELECTRON_PLATFORM:
        raise EngineImportError(f"Unknown engine platform: {family!r}.")
    return v, family


def zip_name(version: str, family: str) -> str:
    return f"electron-v{version}-{ELECTRON_PLATFORM[family]}.zip"


def release_url(version: str, filename: str) -> str:
    return f"{RELEASE_BASE}/v{version}/{filename}"


def storage_key(family: str, version: str) -> str:
    return f"player-engines/{family}/{version}.tar.gz"


def download_url(family: str, version: str) -> str:
    """URL RELATIVE (comme les bundles) : le player la résout contre son propre backend_url."""
    return f"/api/v1/updates/engines/{family}/{version}"


def parse_shasums(text: str, filename: str) -> str:
    """Somme attendue de `filename` dans un SHASUMS256.txt (`<sha> *<nom>` ou `<sha>  <nom>`)."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            sha = parts[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", sha):
                return sha
    raise EngineImportError(f"{filename} is not listed in SHASUMS256.txt.")


def _check_url(url: str) -> bool:
    return ssrf.url_is_safe(url, schemes=("https",), allowed_host_suffixes=ALLOWED_HOSTS)


def download(
    client: httpx.Client, url: str, out: IO[bytes], max_bytes: int,
    url_check: Callable[[str], bool] = _check_url,
) -> str:
    """Télécharge `url` dans `out` en suivant les redirections À LA MAIN (chaque saut revalidé)
    et renvoie le sha256 du contenu. Refuse au-delà de `max_bytes`."""
    for _ in range(MAX_REDIRECTS + 1):
        if not url_check(url):
            raise EngineImportError(f"Refused download host: {httpx.URL(url).host}.")
        with client.stream("GET", url) as res:
            if res.status_code in (301, 302, 303, 307, 308):
                loc = res.headers.get("location")
                if not loc:
                    raise EngineImportError("Redirect without a location.")
                url = str(httpx.URL(url).join(loc))
                continue
            if res.status_code == 404:
                raise EngineImportError("This Electron version does not exist for this platform.")
            if res.status_code != 200:
                raise EngineImportError(f"Download failed: HTTP {res.status_code}.")
            h = hashlib.sha256()
            total = 0
            for chunk in res.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise EngineImportError("Download too large.")
                h.update(chunk)
                out.write(chunk)
            return h.hexdigest()
    raise EngineImportError("Too many redirects.")


def zip_to_targz(zip_path: str, out_path: str, version: str) -> None:
    """Reconditionne le zip officiel en `.tar.gz` à racine `dist/`.

    - droits repris des attributs Unix du zip (les zips Linux d'Electron les portent : `electron`,
      `chrome-sandbox`, `chrome_crashpad_handler` et 5 bibliothèques sont exécutables) ; à défaut
      (zip Windows) 0644 / 0755 ;
    - refuse tout chemin absolu ou remontant (`..`) et tout lien symbolique ;
    - exige un fichier `version` égal à la version annoncée : c'est lui que le player vérifie
      avant de basculer, il doit donc être là et juste dès le serveur.
    """
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        names = {zi.filename.rstrip("/") for zi in infos}
        if "version" not in names:
            raise EngineImportError("Not an Electron runtime archive (no `version` file).")
        if zf.read("version").decode("utf-8", "replace").strip().lstrip("v") != version:
            raise EngineImportError("The archive's `version` file does not match.")
        with tarfile.open(out_path, "w:gz") as tar:
            for zi in infos:
                name = zi.filename
                norm = posixpath.normpath(name)
                if name.startswith(("/", "\\")) or norm == ".." or norm.startswith("../") \
                        or "\\" in name or re.match(r"^[A-Za-z]:", name):
                    raise EngineImportError(f"Unsafe path in archive: {name!r}.")
                unix_mode = (zi.external_attr >> 16) & 0o177777
                if (unix_mode & 0o170000) == 0o120000:
                    raise EngineImportError(f"Symbolic link in archive: {name!r}.")
                perm = unix_mode & 0o777
                ti = tarfile.TarInfo(f"dist/{norm}")
                ti.mtime = 0
                if zi.is_dir():
                    ti.type = tarfile.DIRTYPE
                    ti.mode = perm or 0o755
                    tar.addfile(ti)
                    continue
                ti.mode = perm or 0o644
                ti.size = zi.file_size
                with zf.open(zi) as src:
                    tar.addfile(ti, src)


def fetch_and_package(
    version: str, family: str, workdir: str, client: httpx.Client,
    url_check: Callable[[str], bool] = _check_url,
) -> dict:
    """Télécharge le zip officiel + SHASUMS256.txt, vérifie, reconditionne. Renvoie
    {tar_path, zip_name, zip_sha256}. Ne touche ni au stockage ni à la base (testable seul)."""
    version, family = validate(version, family)
    name = zip_name(version, family)
    sums_path = os.path.join(workdir, "SHASUMS256.txt")
    with open(sums_path, "wb") as f:
        download(client, release_url(version, "SHASUMS256.txt"), f, MAX_SHASUMS_BYTES, url_check)
    with open(sums_path, encoding="utf-8", errors="replace") as f:
        expected = parse_shasums(f.read(), name)
    zip_path = os.path.join(workdir, name)
    with open(zip_path, "wb") as f:
        got = download(client, release_url(version, name), f, MAX_ZIP_BYTES, url_check)
    if got != expected:
        raise EngineImportError(
            f"Checksum mismatch for {name}: expected {expected[:12]}…, got {got[:12]}….")
    tar_path = os.path.join(workdir, f"{family}-{version}.tar.gz")
    try:
        zip_to_targz(zip_path, tar_path, version)
    except zipfile.BadZipFile as exc:
        raise EngineImportError(f"Corrupt archive: {exc}.") from exc
    return {"tar_path": tar_path, "zip_name": name, "zip_sha256": got}
