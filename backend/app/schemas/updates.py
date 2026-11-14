from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UpdateVersionCreate(BaseModel):
    track: str = "player"                 # player | server
    version: str                          # SemVer MAJOR.MINOR.PATCH
    channel: str = "stable"               # stable | beta | lts
    download_url: str | None = None
    sha256_checksum: str | None = None
    model_family: str | None = None       # None = générique ; sinon famille/OS ciblé
    release_notes: str | None = None
    release_notes_fr: str | None = None
    # Décision 2 « canal unique » : BOS minimum exigé par un renderer BrightSign (sinon None).
    min_firmware_version: str | None = None


class UpdateVersionRead(BaseModel):
    id_version: int
    track: str
    version: str
    channel: str
    download_url: str | None = None
    model_family: str | None = None
    release_notes: str | None = None
    release_notes_fr: str | None = None
    min_firmware_version: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class EngineFetchRequest(BaseModel):
    """Récupérer un moteur Electron officiel (vitrail#5) pour une ou plusieurs plateformes."""
    version: str                          # version Electron, ex. 42.11.18
    families: list[str]                   # linux-arm64 | linux-x64 | windows


class EngineFetchJob(BaseModel):
    job_id: str
    family: str
    version: str


class UpdateVersionPatch(BaseModel):
    """Seul champ éditable après coup : le BOS minimum d'un renderer BrightSign (décision 2
    « canal unique »). Le flux réel crée la ligne par `served-versions/register`, SANS minimum —
    il faut pouvoir le poser ensuite. None = effacer l'exigence."""
    min_firmware_version: str | None = None


class ServedVersionRead(BaseModel):
    """Une plateforme SERVIE par le serveur (BrightSign/ChromeOS) : version servie (constante du code,
    figée au manifeste) + si elle est déjà déclarée au catalogue + la dernière déclarée."""
    platform: str
    served_version: str
    registered: bool
    latest_registered: str | None = None


class ServedVersionRegister(BaseModel):
    platform: str                         # chromeos | brightsign


class RendererVersionRead(BaseModel):
    """Version de renderer ÉPINGLÉE qu'un player servi doit charger au boot (résolution du pin)."""
    platform: str
    version: str | None = None            # None → le bootstrap retombe sur le chemin plat (fail-safe)


class BundleImportResult(BaseModel):
    """Résultat d'un import de bundle player (URL/fichier). En dry_run : aperçu (rien écrit)."""
    dry_run: bool
    platform: str
    version: str
    size_bytes: int
    sha256: str
    already_registered: bool              # une ligne catalogue existait déjà
    artifact_present: bool                # l'artefact existait déjà dans MinIO
    id_version: int | None = None         # ligne catalogue créée ou existante
    download_url: str | None = None       # relatif (servi par le serveur du player)
    # Renderers (chromeos/brightsign, décision 3) : nb de fichiers extraits du zip vers
    # `player-renderers/<plat>/<ver>/` — None pour les bundles installés (tar.gz/apk).
    files_extracted: int | None = None
    # BOS minimum effectif de la ligne (repris de la version BS précédente s'il manquait) : une
    # garde héritée en silence serait invérifiable par l'opérateur qui publie.
    min_firmware_version: str | None = None


class DeploymentTargetSpec(BaseModel):
    target_type: str                      # org | player | group | site | tags
    target_id: int | None = None
    tag_expr: dict[str, list[str]] = {}


class DeploymentCreate(BaseModel):
    id_version: int
    targets: list[DeploymentTargetSpec]
    strategy: str = "immediate"           # immediate | gradual | scheduled
    strategy_params: dict = {}            # gradual: {wave_size, wave_delay_min} ; scheduled: {at}


class DeploymentTargetRead(BaseModel):
    id_target: int
    id_player: int
    status: str
    previous_version: str | None = None
    error_message: str | None = None
    model_config = ConfigDict(from_attributes=True)


class DeploymentRead(BaseModel):
    id_deployment: int
    id_version: int
    strategy: str
    status: str
    total_targets: int
    success_count: int
    failed_count: int
    approved_at: datetime
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DeploymentDetail(DeploymentRead):
    targets: list[DeploymentTargetRead] = []
