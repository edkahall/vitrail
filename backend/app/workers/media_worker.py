"""Tâches Celery pour le traitement des médias (§XVI.2).

Réencodage à l'upload (défauts intelligents — §XVI.2, encodage optimisé streaming) :

  VIDÉO :
    - codec : H.264 (libx264) par défaut ; H.265 (libx265) auto si résolution > 4K
    - HDR (color_transfer smpte2084/arib-std-b67) préservé en H.265 10-bit + métadonnées couleur
    - keyframes régulières toutes les 2s (-g 60) → seek rapide + sync LAN précis
    - +faststart (moov atom au début) → démarrage avant fin du téléchargement
    - audio (si présent) : AAC 192k 48kHz + normalisation loudnorm EBU R128 (-16 LUFS)
    - sans piste audio : -an

  AUDIO (paniers §XIX) :
    - OGG Vorbis (libvorbis -qscale 6 ≈ 192k VBR) + loudnorm → pas de saut de volume entre pistes
    - le storage_key passe en .ogg (mime_type mis à jour, ancien objet supprimé)

  loudnorm : 1 passe par défaut (rapide). 2 passes (broadcast) prévu via réglage org futur.

  TODO (voir CLAUDE.md / EVOLUTIONS.md) : interface de configuration de l'encodage
  (codec, passes loudnorm, politique HDR tone-map) au niveau org. Pour l'instant
  défauts codés en dur ci-dessous.
"""

import io
import json
import os
import logging
import subprocess  # nosec B404
import tempfile
import threading
import time

import boto3
import psycopg2
from psycopg2.extras import Json
from botocore.config import Config
from PIL import Image

from app.core.config import settings
from app.core import resources
from app.core import encoders
from app.services import encode_progress, media_conformance, worker_status
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _lower_priority() -> None:
    """preexec_fn du process ffmpeg enfant : abaisse sa priorité CPU (nice 19).

    Sous une forte charge de transcodage (upload massif), l'ordonnanceur garde ainsi la
    main pour l'API, la DB et le sync des players → le serveur reste réactif même quand
    tous les cœurs encodent. POSIX uniquement (le worker tourne en conteneur Linux)."""
    try:
        os.nice(19)
    except OSError:  # pragma: no cover
        pass


_NICE_KW: dict = {"preexec_fn": _lower_priority} if os.name == "posix" else {}


def _encode_timeout(duration_s: float, width: int, height: int, encoder: str) -> tuple[int, float]:
    """Budget de temps d'un encodage, d'après l'encodeur RÉELLEMENT retenu et la résolution.

    Renvoie `(timeout_secondes, multiplicateur)` — le second sert au journal.

    🔴 Défaut corrigé le 2026-08-02 (mesuré sur le banc d'installation) : la première tentative
    prenait TOUJOURS le budget matériel, et le budget logiciel — plus généreux — n'était
    atteignable que par la branche de repli, c'est-à-dire seulement APRÈS l'échec d'un encodeur
    matériel. Sur un serveur SANS GPU, où libx264/265 est le PREMIER choix, ce budget était donc
    inatteignable. Symptôme : une source 4K de 205 s en libx265 tuée à 1800 s sans avoir fini.

    🔴 Second défaut : un multiplicateur unique ignore que le coût de l'encodage LOGICIEL croît
    avec le nombre de PIXELS, et que libx265 est nettement plus lent que libx264 — or la règle
    produit envoie toute source > FHD vers HEVC, donc 4K et libx265 se cumulent toujours.
    Le matériel, lui, est bien moins sensible à la résolution (silicium dédié) : on y borne
    l'effet des pixels, la marge servant surtout à la contention entre encodages simultanés.

    ⏳ PROVISOIRE (remarque d'Ed) : « rallonger le délai déplace le mur, il ne l'enlève pas ».
    Ces facteurs restent des DEVINETTES et seront remplacés par `2 x estimation`, l'estimation
    venant d'un ÉTALONNAGE réel de la machine confronté à la source. Cf. RESTE_A_FAIRE.md.
    """
    px_factor = max(1.0, (width * height) / (1920 * 1080)) if width and height else 1.0
    if encoder.startswith("lib"):
        mult = 12 * px_factor * (1.5 if encoder == "libx265" else 1.0)
        return max(1800, min(int(duration_s * mult), 21600)), mult      # plafond 6 h
    mult = 6 * min(px_factor, 2.0)
    return max(1800, min(int(duration_s * mult), 10800)), mult          # plafond 3 h


def _sample_cmd(cmd: list[str], sample_out: str, offset_s: float, sample_s: int) -> list[str]:
    """La MÊME commande d'encodage, appliquée à une tranche de la source.

    🔑 « La même » n'est pas un détail : c'est ce qui fait que la mesure vaut quelque chose.
    Elle porte l'encodeur réellement retenu (matériel ou logiciel), les filtres, le codec,
    le débit plafond — mesurer autre chose reviendrait à étalonner une commande qu'on ne
    lancera jamais.

    `-ss` est placé AVANT `-i` (recherche à l'entrée, rapide) ; `-t` juste avant la sortie
    (limite la durée produite).
    """
    i = cmd.index("-i")
    return cmd[:i] + ["-ss", f"{offset_s:.3f}"] + cmd[i:-1] + ["-t", str(sample_s), sample_out]


def _estimate_encode_seconds(cmd: list[str], probe: dict) -> tuple[float | None, float | None]:
    """Durée estimée de l'encodage COMPLET, mesurée sur une tranche de la vraie source.

    Renvoie `(estimation_secondes, vitesse_constatée)`, ou `(None, None)` quand on ne sait
    pas — et dans ce cas l'appelant garde le comportement historique. On ne bloque jamais
    un upload sur une mesure ratée.

    Pourquoi un échantillon de la VRAIE source plutôt qu'un banc d'essai synthétique : le
    coût d'un encodage dépend autant du CONTENU (grain, mouvement — du simple au triple en
    x265) que de la machine. Une mire s'encode bien plus vite que n'importe quel vrai plan :
    on promettrait 15 min pour en prendre 40. L'échantillon mesure d'un coup la machine,
    l'encodeur retenu, le contenu, et la contention du moment (les autres encodages en
    cours ralentissent la sonde exactement comme ils ralentiront l'encodage).

    ⚠️ Biais connu, volontairement conservateur : le démarrage du process, l'initialisation
    des filtres et du GPU sont comptés dans la tranche mais ne se répètent pas sur la durée
    totale — on SURESTIME donc légèrement les sources longues. Mieux vaut annoncer plus que
    promettre moins.
    """
    dur_s = (probe.get("duration_ms") or 0) / 1000
    # Trop courte pour être échantillonnée utilement : de toute façon en dessous de la
    # première bande, la question ne se pose pas.
    if dur_s <= ENCODE_SAMPLE_S * 3:
        return None, None

    sample_out = tempfile.mktemp(suffix=".mp4")  # nosec B306 — chemin interne, pas un secret
    offset = dur_s / 3.0                          # au tiers : évite les génériques noirs du début,
    try:                                          # qui s'encodent trop vite pour être représentatifs
        started = time.monotonic()
        try:
            r = subprocess.run(  # nosec B603
                _sample_cmd(cmd, sample_out, offset, ENCODE_SAMPLE_S),
                capture_output=True, timeout=ENCODE_SAMPLE_TIMEOUT_S, **_NICE_KW,
            )
            elapsed = time.monotonic() - started
            if r.returncode != 0:
                logger.warning("Étalonnage : l'échantillon a échoué (rc=%d) — on garde le "
                               "comportement historique", r.returncode)
                return None, None
        except subprocess.TimeoutExpired:
            # Une tranche de 10 s qui ne tient pas en 10 min : la mesure EST le résultat.
            elapsed = float(ENCODE_SAMPLE_TIMEOUT_S)
    finally:
        if os.path.exists(sample_out):
            os.unlink(sample_out)

    if elapsed <= 0:
        return None, None
    speed = ENCODE_SAMPLE_S / elapsed          # × le temps réel
    return dur_s / speed, speed


def _parse_progress_line(line: str, state: dict) -> bool:
    """Absorbe une ligne `clé=valeur` de `ffmpeg -progress`.

    Renvoie True quand le bloc est complet (`progress=continue|end`), c'est-à-dire quand il
    y a réellement quelque chose de nouveau à publier — ffmpeg émet une dizaine de lignes
    par bloc, publier à chacune multiplierait les écritures Redis par dix.

    Fonction PURE (aucun process, aucun réseau) : c'est elle qui porte les pièges de format,
    donc c'est elle qui doit être testable partout, y compris sur le poste Windows.

    ⚠️ `out_time_ms` est un **nom trompeur** : ffmpeg y écrit des MICROsecondes, pas des
    millisecondes (défaut historique conservé pour ne pas casser les consommateurs
    existants). Les deux clés portent donc la même unité. Lire `out_time_ms` comme des ms
    donnerait une progression **1000× trop rapide** — barre à 100 % en une seconde.
    """
    k, sep, v = line.strip().partition("=")
    if not sep:
        return False
    k, v = k.strip(), v.strip()
    if k in ("out_time_us", "out_time_ms"):
        try:
            state["done_ms"] = max(0, int(v) // 1000)
        except ValueError:  # "N/A" tant que rien n'est encodé
            pass
    elif k == "out_time" and "done_ms" not in state:
        # Repli si les clés numériques manquent : "HH:MM:SS.micro"
        try:
            hh, mm, ss = v.split(":")
            state["done_ms"] = int((int(hh) * 3600 + int(mm) * 60 + float(ss)) * 1000)
        except (ValueError, TypeError):
            pass
    elif k == "speed":
        try:
            state["speed"] = float(v.rstrip("x"))
        except ValueError:  # "N/A" au démarrage
            pass
    elif k == "progress":
        state["ended"] = v == "end"
        return True
    return False


def _progress_snapshot(state: dict, total_ms: int | None) -> tuple[float | None, int | None, float | None]:
    """(pourcentage, secondes restantes, vitesse) à partir de l'état accumulé.

    Sans durée totale connue, le pourcentage reste None : on affiche une activité, jamais
    un chiffre inventé. L'ETA se déduit de la vitesse INSTANTANÉE rapportée par ffmpeg —
    elle vaut ce qu'elle vaut sur une source hétérogène, mais elle a le mérite de refléter
    la contention réelle du moment (3 encodages en parallèle ⇒ speed divisée, ETA allongée).
    """
    done_ms = state.get("done_ms")
    speed = state.get("speed")
    pct = None
    eta = None
    if total_ms and done_ms is not None and total_ms > 0:
        pct = min(100.0, done_ms * 100.0 / total_ms)
        if speed and speed > 0:
            eta = int(max(0, total_ms - done_ms) / 1000.0 / speed)
    if state.get("ended"):
        # ffmpeg rapporte un dernier `out_time` légèrement inférieur à la durée nominale
        # (arrondis de conteneur) : mesuré à 99,665 % sur une source de 20 s. Sans ce
        # calage, la barre resterait figée à 99 % pendant l'upload MinIO et le contrôle de
        # conformité qui SUIVENT l'encodage — ce qui se lit comme un blocage.
        pct, eta = (100.0 if total_ms else None), 0
    return pct, eta, speed


# Cadence de publication : ffmpeg émet un bloc ~1×/s, mais bien plus vite sur un encodage
# matériel court. On ne publie pas plus souvent que ça — la barre n'a pas besoin de 30 Hz.
_PROGRESS_MIN_INTERVAL_S = 1.5


def _run_ffmpeg(
    cmd: list[str],
    timeout: int,
    *,
    on_progress=None,
    total_ms: int | None = None,
) -> "subprocess.CompletedProcess[bytes]":
    """Lance un ffmpeg lourd (transcode/rendition) en priorité basse (cf. _lower_priority).

    Sans `on_progress`, comportement **strictement inchangé** (`subprocess.run` bloquant) :
    les appels qui n'ont rien à afficher (remux, extraction de sous-titres) ne changent pas
    de chemin d'exécution.

    Avec `on_progress`, on passe en `Popen` + `-progress pipe:1` et on appelle le callback
    au fil de l'eau. Le contrat d'erreur est **identique dans les deux modes** : dépassement
    du budget ⇒ `subprocess.TimeoutExpired`. C'est ce que `transcode_media` attrape pour ne
    PAS rejouer un encodage expiré (règle de la 1.0.24 : l'expiration est déterministe,
    la rejouer ne fait que brûler du processeur) — casser ce type d'exception ici
    ressusciterait silencieusement les 4 tentatives.
    """
    if on_progress is None:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **_NICE_KW)  # nosec B603

    # Options globales : elles doivent précéder la première entrée (-i).
    full = [cmd[0], "-nostats", "-progress", "pipe:1", *cmd[1:]]
    state: dict = {}
    killed = False
    last_pub = 0.0

    # stderr vers un FICHIER, jamais un tube : le journal d'un encodage long dépasse la
    # taille du tampon de tube, et personne ne le lit pendant qu'on lit stdout ⇒ ffmpeg
    # se bloquerait sur son écriture, indéfiniment. On le relit à la fin pour rendre un
    # `CompletedProcess` de la même forme qu'avant (les appelants lisent `r.stderr`).
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(  # nosec B603
            full, stdout=subprocess.PIPE, stderr=errf, **_NICE_KW
        )

        def _on_deadline() -> None:
            nonlocal killed
            killed = True
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — le process peut être déjà mort
                pass

        timer = threading.Timer(timeout, _on_deadline)
        timer.daemon = True
        timer.start()
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                if not _parse_progress_line(raw.decode("utf-8", "replace"), state):
                    continue
                now = time.monotonic()
                if not state.get("ended") and now - last_pub < _PROGRESS_MIN_INTERVAL_S:
                    continue
                last_pub = now
                try:
                    on_progress(*_progress_snapshot(state, total_ms))
                except Exception as exc:  # noqa: BLE001 — l'affichage ne casse pas l'encodage
                    logger.debug("on_progress failed: %s", exc)
            proc.wait()
        finally:
            timer.cancel()
            if proc.stdout:
                proc.stdout.close()
        errf.seek(0)
        err = errf.read()

    if killed:
        raise subprocess.TimeoutExpired(cmd, timeout, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, b"", err)

# ── Étalonnage et bandes de décision (2026-08-03, décisions Ed) ───────────────
#
# 🔑 Recadrage d'Ed le 2026-08-02 : « rallonger le délai déplace le mur, il ne l'enlève
# pas ». La 1.0.24 a supprimé le GASPILLAGE (plus de reprise sur une expiration, qui est
# déterministe) mais pas le mur lui-même. La réponse est de MESURER la machine face à la
# source, et de dire à l'utilisateur ce qui l'attend AVANT de lancer.
#
# ⚠️ Contrainte d'architecture assumée : le backend n'a NI ffmpeg NI ffprobe (allègement
# des images du 2026-07-19). Seul le worker peut sonder. L'avertissement ne peut donc pas
# précéder la mise en FILE — il précède l'ENCODAGE. Pour l'utilisateur c'est équivalent
# (le mur, c'est l'encodage), mais le mot « avant la mise en file » du cadrage est
# factuellement faux et il vaut mieux l'écrire ici que de le laisser croire.
ENCODE_BAND_AUTO_S = 20 * 60        # en dessous : on lance, sans rien demander
ENCODE_BAND_REFUSE_S = 4 * 3600     # au-dessus : refus, contournement org-admin seulement
ENCODE_SAMPLE_S = 10                # tranche de la VRAIE source mesurée
ENCODE_SAMPLE_TIMEOUT_S = 600       # une tranche de 10 s qui dépasse ça = source désespérée


def _decision_band(estimate_s: float | None) -> str:
    """`auto` | `confirm` | `refuse` — les 3 bandes validées par Ed.

    Estimation absente (source trop courte pour être échantillonnée, sonde en échec) =
    `auto` : on ne bloque JAMAIS un upload sur une mesure qu'on n'a pas pu faire.
    """
    if estimate_s is None:
        return "auto"
    if estimate_s < ENCODE_BAND_AUTO_S:
        return "auto"
    if estimate_s <= ENCODE_BAND_REFUSE_S:
        return "confirm"
    return "refuse"


def _scaled_pixels(width: int, height: int, box: int) -> int:
    """Nombre de pixels après mise à l'échelle dans un cadre `box` (aspect préservé)."""
    if not width or not height:
        return 0
    factor = min(1.0, box / max(width, height))
    return int(width * height * factor * factor)


def _downscale_options(estimate_s: float, width: int, height: int) -> list[dict]:
    """Replis proposés à l'utilisateur quand l'encodage est trop long, avec leur estimation.

    🔑 C'est ce qui transforme un refus en CHOIX (demande d'Ed) : « 4 h » sans alternative
    est une impasse ; « 4 h, ou 25 min en 1080p » est une décision.

    Le coût d'un encodage logiciel suit approximativement le nombre de PIXELS — d'où la
    mise à l'échelle quadratique. L'estimation est même CONSERVATRICE : descendre sous la
    4K fait aussi basculer la sortie de HEVC vers H.264 (cf. `_build_video_cmd`), nettement
    plus rapide, ce dont ce calcul ne tient pas compte.
    """
    src_px = (width or 0) * (height or 0)
    if not src_px:
        return []
    out: list[dict] = []
    for profile, box in (("fhd", RENDITION_PROFILES["fhd"]["box"]),
                         ("hd720", RENDITION_PROFILES["hd720"]["box"])):
        if max(width, height) <= box:
            continue  # la source est déjà dans ce cadre : ce n'est pas un repli
        px = _scaled_pixels(width, height, box)
        out.append({
            "profile": profile,
            "box": box,
            "estimate_s": int(estimate_s * px / src_px),
        })
    return out


# ── Paramètres d'encodage (défauts intelligents) ──────────────────────────────
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"   # EBU R128, -16 LUFS
GOP_SECONDS = 2.0                             # cadence keyframe cible (sync-readiness ≤2s)
GOP_FALLBACK = 60                             # repli si fps indéterminable (~2s à 30fps)
UHD_PIXELS = 3840 * 2160                      # seuil 4K → bascule H.265


def _gop_frames(fps: float | None) -> int:
    """Nombre d'images entre keyframes pour viser GOP_SECONDS, dérivé de la cadence réelle.

    Un GOP en nombre d'images fixe (ex. 60) ne fait 2s qu'à 30fps : à 25fps (PAL) il
    donne 2.4s → recalé par le check sync-readiness (≤2s). On le calcule donc depuis la fps.
    """
    if not fps or fps <= 0:
        return GOP_FALLBACK
    return max(1, round(GOP_SECONDS * fps))
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"} # PQ (HDR10) / HLG

# Extensions d'origine renommées vers l'extension RÉELLEMENT servie (le contenu est
# re-encodé : vidéo→.mp4, audio→.ogg Vorbis). Cosmétique (le nom affiché doit refléter
# le fichier serveur, pas l'upload — retour Ed). On ne renomme PAS un fichier déjà au bon
# conteneur (ex. mp4 vidéo / ogg audio).
_VIDEO_SRC_EXTS = ("mov", "mkv", "avi", "webm", "m4v", "mpg", "mpeg", "wmv", "flv", "ts", "m2ts")
_AUDIO_SRC_EXTS = ("mp3", "wav", "flac", "m4a", "aac", "wma", "opus", "aiff", "aif", "alac", "ape", "wv", "mp4")


def _served_display_name(media_type: str, media_name: str | None, served_key: str | None) -> str | None:
    """Nouveau `media_name` reflétant l'extension réellement servie (.mp4 vidéo / .ogg audio),
    ou None si aucun renommage n'est nécessaire."""
    if not media_name:
        return None
    base, dot, ext = media_name.rpartition(".")
    if not dot:
        return None
    ext = ext.lower()
    key = (served_key or "").lower()
    if media_type == "video" and key.endswith(".mp4") and ext in _VIDEO_SRC_EXTS:
        return f"{base}.mp4"
    if media_type == "audio" and key.endswith(".ogg") and ext in _AUDIO_SRC_EXTS:
        return f"{base}.ogg"
    return None

# ── Renditions adaptatives (« artefacts » vidéo) ──────────────────────────────
# Chaque profil = arête maximale du cadre englobant (aspect ratio préservé) + codec.
# Une source 4K 16:9 (3840×2160) → fhd 1920×1080 ; un portrait 2160×3840 → 1080×1920.
# `hevc_4k` = transcode HEVC en GARDANT la résolution (box 3840 ≥ 4K → pas de downscale) :
# pour les box Android TV (ExoPlayer) dont le décodeur h264 ne tient pas le 4K@60 mais
# dont le décodeur HEVC le fait (cf. project_exoplayer_android). h264 = compat universelle.
RENDITION_PROFILES = {
    "fhd":     {"box": 1920, "codec": "h264"},
    "hd720":   {"box": 1280, "codec": "h264"},
    "hevc_4k": {"box": 3840, "codec": "hevc"},
    # Rotation À GRAVER (BrightSign portrait, 2026-08-14) : cadre PAYSAGE, contenu pré-roté
    # par `transpose` (1 = 90° horaire, tête à droite · 2 = 90° anti-horaire, tête à gauche).
    # box/codec None = dérivés de la SOURCE au moment d'encoder (4K HEVC si > FHD, sinon FHD
    # H.264) — cf. _rotated_frame. Le mapping dalle→profil (relation INVERSÉE) vit dans
    # services/renditions.BAKE_PROFILE_BY_ROTATION, une seule définition.
    "rot_cw":  {"box": None, "codec": None, "transpose": 1},
    "rot_ccw": {"box": None, "codec": None, "transpose": 2},
    # Variantes rotées PAR DÉFINITION (demande d'Ed 2026-08-14 soir) : cadre paysage FIXE
    # 1920×1080 H.264 pour une SORTIE ≤ 1080 (HD1024 forcé FHD) — ~4× plus légères que le
    # cadre 4K HEVC, décodage FHD (le HD4 n'a qu'un décodeur). La sélection vit dans
    # renditions.bake_variant_profile ; une source déjà FHD reste sur le profil de base
    # (même cadre, pas de doublon d'encodage).
    "rot_cw_fhd":  {"box": None, "codec": "h264", "transpose": 1, "frame": (1920, 1080)},
    "rot_ccw_fhd": {"box": None, "codec": "h264", "transpose": 2, "frame": (1920, 1080)},
}
RENDITION_BOXES = {k: v["box"] for k, v in RENDITION_PROFILES.items()}  # compat (arête seule)


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=f"{'https' if settings.MINIO_USE_SSL else 'http'}://{settings.MINIO_ENDPOINT}",
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def _db_conn():
    url = settings.db_worker_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


@celery_app.task(name="app.workers.media_worker.publish_capabilities")
def publish_capabilities() -> None:
    """Publie ce que cette machine sait faire (page « Serveur »).

    Routée sur la file `media` : c'est le worker média qui porte ffmpeg et voit le GPU.
    Le backend ne peut pas répondre à ces questions lui-même — il n'a aucun des deux.

    ⚠️ `disk_free_bytes` porte sur le disque de TRAVAIL du worker (les encodages y écrivent
    leur fichier temporaire), pas sur le stockage des médias qui vit dans MinIO. C'est
    volontaire : c'est ce disque-là qui fait échouer un encodage quand il se remplit, et
    personne ne le regarde jamais.
    """
    import platform
    import shutil

    try:
        hw = list(encoders.detect_hw_encoders())
        tmp_dir = tempfile.gettempdir()
        usage = shutil.disk_usage(tmp_dir)
        try:
            ver = subprocess.run(  # nosec B603,B607
                ["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True, timeout=15,
            ).stdout.splitlines()[0].split(" ")[2]
        except Exception:  # noqa: BLE001
            ver = None
        worker_status.publish(platform.node(), {
            "hostname": platform.node(),
            "hw_encoders": hw,
            "encoder_h264": encoders.best_encoder("h264")["name"],
            "encoder_hevc": encoders.best_encoder("hevc")["name"],
            "cpu_count": os.cpu_count(),
            "ffmpeg_threads": resources.ffmpeg_threads(),
            "ffmpeg_version": ver,
            "work_dir": tmp_dir,
            "disk_total_bytes": usage.total,
            "disk_free_bytes": usage.free,
        })
    except Exception as exc:  # noqa: BLE001 — une signalétique ne casse pas un worker
        logger.warning("publish_capabilities failed: %s", exc)


@celery_app.task(name="app.workers.media_worker.transcode_media", bind=True, max_retries=3)
def transcode_media(self, id_media: int) -> None:
    """
    Traitement post-upload : thumbnail + métadonnées + réencodage optimisé.
    - image : thumbnail 320×180 JPEG via Pillow
    - video : ffprobe + thumbnail + réencodage H.264/H.265 streaming + AAC normalisé
    - audio : ffprobe + réencodage OGG Vorbis normalisé
    """
    conn = _db_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE cms_media SET transcode_status='processing', updated_at=now() WHERE id_media=%s",
            (id_media,),
        )
        conn.commit()

        cur.execute(
            "SELECT storage_key, media_type, encode_mode, media_name, metadata FROM cms_media "
            "WHERE id_media=%s AND is_deleted=false",
            (id_media,),
        )
        row = cur.fetchone()
        if not row:
            return
        storage_key, media_type, encode_mode, media_name, meta_col = row
        encode_mode = encode_mode or "auto"

        # Remplacement en attente (cf. endpoint /media/{id}/replace) : on transcode le
        # fichier de STAGING sans toucher au contenu actuellement diffusé (storage_key
        # reste l'ancien jusqu'à la bascule atomique en fin de tâche). Les players ne
        # voient donc jamais le fichier brut (ex. .mkv illisible) pendant le traitement.
        pending = (meta_col or {}).get("pending_replace") if isinstance(meta_col, dict) else None
        is_replace = bool(pending and pending.get("staging_key"))
        source_key = pending["staging_key"] if is_replace else storage_key
        if is_replace and pending.get("encode_mode"):
            encode_mode = pending["encode_mode"]

        s3 = _s3()
        updates: dict = {"transcode_status": "done"}
        doc_pages_meta: list[dict] = []  # pages converties (document) — pour le cleanup replace

        if media_type == "image":
            obj = s3.get_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=source_key)
            content = obj["Body"].read()
            _process_image(s3, content, source_key, updates)
        elif media_type in ("video", "audio"):
            # Décision déjà rendue par l'utilisateur (« lancer quand même » / « encoder en
            # définition inférieure ») : on ne re-sonde pas et on ne repose pas la question.
            decision = (meta_col or {}).get("encode_decision") if isinstance(meta_col, dict) else None
            if isinstance(decision, dict) and not decision.get("resolved"):
                decision = None   # une attente non tranchée n'est PAS une décision
            _process_av_streaming(
                s3, source_key, media_type, updates, encode_mode, id_media, decision
            )
        elif media_type == "document":
            doc_pages_meta = _process_document(s3, source_key, media_name, meta_col, updates)

        # Pour un replace, la bascule du contenu servi est ATOMIQUE : si le traitement n'a
        # pas produit de nouvelle clé (ex. .mp4 conforme conservé tel quel), on force le
        # storage_key vers la clé de staging traitée. On reporte aussi taille/checksum/mime
        # du nouveau fichier, et on s'assure que `pending_replace` disparaît de `metadata`.
        if is_replace:
            updates.setdefault("storage_key", source_key)
            if pending.get("checksum"):
                updates["checksum_sha256"] = pending["checksum"]
            if pending.get("size") is not None:
                updates["file_size_bytes"] = pending["size"]
            if "metadata" not in updates:
                cleaned = dict(meta_col or {})
                cleaned.pop("pending_replace", None)
                updates["metadata"] = Json(cleaned)

        # Refléter l'extension réellement servie (.mp4 vidéo / .ogg audio) dans le NOM AFFICHÉ
        # (le contenu re-encodé EST .mp4/.ogg, mais la médiathèque montrait encore l'extension
        # d'origine — source de confusion, retour Ed).
        renamed = _served_display_name(media_type, media_name, updates.get("storage_key", storage_key))
        if renamed:
            updates["media_name"] = renamed

        # Étalonnage : l'encodage dépasse une bande, on rend la main à l'utilisateur. La clé
        # privée `_decision` n'est PAS une colonne — elle est fondue dans `metadata` ici,
        # là où on dispose de l'état existant à préserver.
        pending_decision = updates.pop("_decision", None)
        if pending_decision:
            merged = dict(meta_col or {})
            merged.pop("pending_replace", None)   # le remplacement n'a pas eu lieu
            merged["encode_decision"] = {**pending_decision, "resolved": False}
            updates["metadata"] = Json(merged)

        set_sql = ", ".join(f"{k}=%s" for k in updates)
        cur.execute(  # nosec B608 — keys are internal constants, never user input
            f"UPDATE cms_media SET {set_sql}, updated_at=now() WHERE id_media=%s",
            [*updates.values(), id_media],
        )
        conn.commit()

        # En attente de décision : le média n'a PAS de contenu diffusable. Générer ses
        # artefacts adaptatifs ou prévenir les players serait au mieux inutile, au pire
        # coûteux — un downscale automatique brûlerait justement le processeur qu'on vient
        # de refuser de brûler.
        if pending_decision:
            logger.info("media %d en attente de décision (%s) — traitement suspendu",
                        id_media, pending_decision.get("band"))
            return

        # Bascule atomique effectuée → purger les objets MinIO de l'ANCIENNE version et
        # ses lignes de renditions (devenues périmées), puis régénérer plus bas. À faire
        # APRÈS le commit du swap : avant, l'ancien contenu sert encore la diffusion.
        if is_replace:
            # Pages de l'ANCIEN document (clés dérivées de l'ancien storage_key) : périmées.
            old_page_keys = [
                p.get("key") for p in (((meta_col or {}).get("document") or {}).get("pages") or [])
                if isinstance(p, dict)
            ]
            new_page_keys = {p.get("key") for p in doc_pages_meta}
            for key in [*(pending.get("cleanup_keys") or []), *old_page_keys]:
                if not key or key == updates.get("storage_key") or key in new_page_keys:
                    continue
                try:
                    s3.delete_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("replace cleanup: delete %s failed: %s", key, exc)
            cur.execute("DELETE FROM cms_media_renditions WHERE id_parent=%s", (id_media,))
            conn.commit()

        # Document (re)converti → re-résoudre le nb de pages des items de playlist qui
        # portent une SÉLECTION (doc_pages) : le doc_page_count dénormalisé (agrégats SQL)
        # doit suivre le nouveau page_count (replace d'un PDF par une version plus courte…).
        if media_type == "document" and updates.get("page_count"):
            from app.services.document_media import parse_page_selection
            cur.execute(
                "SELECT id_item, doc_pages FROM cms_playlist_items "
                "WHERE id_media=%s AND doc_pages IS NOT NULL",
                (id_media,),
            )
            for iid, spec in cur.fetchall():
                cur.execute(
                    "UPDATE cms_playlist_items SET doc_page_count=%s, updated_at=now() "
                    "WHERE id_item=%s",
                    (len(parse_page_selection(spec, updates["page_count"])), iid),
                )
            conn.commit()

        # Renditions adaptatives : déclenche la génération auto si l'org l'a activée
        # (rendition_policy=auto_fhd) et que la source est plus grande que la cible.
        if media_type == "video":
            _maybe_autogenerate_renditions(cur, id_media)

        # Transcodage terminé → le storage_key servi peut avoir changé (ré-encodage,
        # remux passthrough). Notifier (graceful) les players de l'org pour qu'ils
        # re-résolvent le payload : indispensable après un « replace » (le contenu a
        # changé sur un média déjà en playlist) ; no-op pour un média pas encore diffusé.
        try:
            cur.execute("SELECT id_org FROM cms_media WHERE id_media=%s", (id_media,))
            row = cur.fetchone()
            if row:
                cur.execute("SELECT id_player FROM cms_players WHERE id_org=%s", (row[0],))
                pids = [r[0] for r in cur.fetchall()]
                if pids:
                    from app.workers.event_worker import _push_sync
                    _push_sync(pids, is_preemptive=False)
        except Exception as e:  # noqa: BLE001 — notification best-effort
            logger.warning("transcode force_sync failed for media %s: %s", id_media, e)

    except Exception as exc:
        logger.exception("transcode_media(%d) failed: %s", id_media, exc)
        conn.rollback()
        cur.execute(
            "UPDATE cms_media SET transcode_status='error', updated_at=now() WHERE id_media=%s",
            (id_media,),
        )
        conn.commit()
        # ⚠️ Une EXPIRATION n'est pas une panne passagère : rejouer le même encodage avec les
        # mêmes paramètres expirera de la même façon, c'est déterministe. Avec max_retries=3 on
        # brûlait 4 × le budget — mesuré sur le banc le 2026-08-02 : 4 × 30 min de processeur
        # saturé sur une source 4K, ce qui affame au passage tous les autres transcodages du
        # serveur. On échoue franchement, une fois, et le journal dit quoi faire.
        if isinstance(exc, subprocess.TimeoutExpired):
            logger.error(
                "transcode_media(%d): encodage expiré — PAS de reprise (échec déterministe). "
                "Source trop lourde pour cette machine sans accélération matérielle : "
                "activer un GPU (setup-gpu.sh) ou fournir une source moins lourde.",
                id_media,
            )
            return
        raise self.retry(exc=exc, countdown=60)
    finally:
        # Succès, erreur ou expiration : la barre n'a plus de raison d'être. Sans cet
        # effacement elle resterait affichée jusqu'au TTL sur un média déjà marqué `done`.
        encode_progress.clear(id_media)
        cur.close()
        conn.close()


def _process_image(s3, content: bytes, storage_key: str, updates: dict) -> None:
    img = Image.open(io.BytesIO(content))
    updates["width"] = img.width
    updates["height"] = img.height

    thumb = img.copy()
    thumb.thumbnail((320, 180))
    buf = io.BytesIO()
    # Préserve la transparence (produits détourés = PNG à fond transparent) : sinon convert("RGB")
    # aplatit l'alpha en NOIR → vignette/aperçu sur fond noir. Sources opaques restent en JPEG (léger).
    has_alpha = thumb.mode in ("RGBA", "LA") or (thumb.mode == "P" and "transparency" in thumb.info)
    if has_alpha:
        thumb.convert("RGBA").save(buf, format="PNG")
        thumb_content_type = "image/png"
    else:
        thumb.convert("RGB").save(buf, format="JPEG", quality=85)
        thumb_content_type = "image/jpeg"

    thumb_key = _thumb_key(storage_key)
    thumb_kwargs: dict = {"ContentType": thumb_content_type}
    if settings.MINIO_SSE_ENABLED:
        thumb_kwargs["ServerSideEncryption"] = "AES256"
    s3.put_object(
        Bucket=settings.MINIO_BUCKET_MEDIA,
        Key=thumb_key,
        Body=buf.getvalue(),
        **thumb_kwargs,
    )
    updates["thumbnail_url"] = thumb_key


# ── Documents multipages (PDF / PPT / PPTX) — §3.2 « rendu page par page » ──────
# PPT/PPTX → PDF (LibreOffice headless, profil isolé) puis PDF → 1 JPEG par page
# (pdftoppm/poppler, subprocess — licence-safe). Les pages sont servies comme des
# items IMAGE déroulés par le serveur (ws.py) : zéro modification player.

DOC_PAGE_SCALE = 1920          # arête max du rendu de page (px)
DOC_PAGE_JPEG_QUALITY = 90
DOC_MAX_PAGES = 100            # garde-fou payload (documents énormes tronqués + loggés)
_SOFFICE_TIMEOUT = 600
_PDFTOPPM_TIMEOUT = 600


def _doc_pages_prefix(storage_key: str) -> str:
    base = storage_key.rsplit(".", 1)[0] if "." in storage_key else storage_key
    return f"{base}_pages"


def _doc_page_key(storage_key: str, page_no: int) -> str:
    return f"{_doc_pages_prefix(storage_key)}/{page_no:04d}.jpg"


def _soffice_to_pdf(src_path: str, workdir: str) -> str:
    """Convertit un PPT/PPTX (ou autre format bureautique) en PDF via LibreOffice
    headless. Profil utilisateur ISOLÉ par conversion (2 soffice concurrents sur le
    même profil = lock silencieux → conversion vide)."""
    profile = os.path.join(workdir, "lo_profile")
    cmd = [
        "soffice",
        f"-env:UserInstallation=file://{profile}",
        "--headless", "--norestore", "--invisible",
        "--convert-to", "pdf", "--outdir", workdir, src_path,
    ]
    env = {**os.environ, "HOME": workdir}
    r = subprocess.run(  # nosec B603 — binaire fixe, chemins internes
        cmd, capture_output=True, timeout=_SOFFICE_TIMEOUT, env=env, **_NICE_KW
    )
    out_pdf = os.path.join(
        workdir, os.path.splitext(os.path.basename(src_path))[0] + ".pdf"
    )
    if r.returncode != 0 or not os.path.exists(out_pdf):
        raise RuntimeError(
            "soffice → pdf failed (rc=%d): %s" % (
                r.returncode, (r.stderr or r.stdout or b"").decode(errors="replace")[:500],
            )
        )
    return out_pdf


def _rasterize_pdf(pdf_path: str, workdir: str) -> list[str]:
    """PDF → JPEG par page (pdftoppm), retourne les chemins ordonnés par n° de page."""
    prefix = os.path.join(workdir, "page")
    cmd = [
        "pdftoppm", "-jpeg", "-jpegopt", f"quality={DOC_PAGE_JPEG_QUALITY}",
        "-scale-to", str(DOC_PAGE_SCALE), "-cropbox",
        "-l", str(DOC_MAX_PAGES),
        pdf_path, prefix,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=_PDFTOPPM_TIMEOUT, **_NICE_KW)  # nosec B603
    files: list[tuple[int, str]] = []
    for name in os.listdir(workdir):
        if name.startswith("page-") and name.endswith(".jpg"):
            try:
                files.append((int(name[5:-4]), os.path.join(workdir, name)))
            except ValueError:
                continue
    if r.returncode != 0 or not files:
        raise RuntimeError(
            "pdftoppm failed (rc=%d): %s" % (
                r.returncode, (r.stderr or b"").decode(errors="replace")[:500],
            )
        )
    return [p for _, p in sorted(files)]


def _process_document(
    s3, source_key: str, media_name: str | None, meta_col: dict | None, updates: dict,
) -> list[dict]:
    """Conversion d'un document en pages images + vignette (page 1) + métadonnées.

    Pose updates: width/height (page 1), page_count, thumbnail_url,
    metadata["document"] = {page_count, pages:[{key, checksum, width, height}],
    source, truncated?}. Retourne la liste des pages (cleanup replace).
    Échec soffice/pdftoppm → exception → transcode_status='error' + retry Celery.
    """
    import hashlib

    ext = ""
    for candidate in (source_key, media_name or ""):
        if "." in candidate:
            ext = candidate.rsplit(".", 1)[-1].lower()
            break
    with tempfile.TemporaryDirectory(prefix="sfdoc_") as td:
        src = os.path.join(td, f"src.{ext or 'bin'}")
        s3.download_file(settings.MINIO_BUCKET_MEDIA, source_key, src)
        if ext == "pdf":
            pdf_path, source_kind = src, "pdf"
        else:
            pdf_path, source_kind = _soffice_to_pdf(src, td), (ext or "office")
        page_files = _rasterize_pdf(pdf_path, td)
        truncated = len(page_files) >= DOC_MAX_PAGES
        if truncated:
            logger.warning("document %s: > %d pages — rendu tronqué", source_key, DOC_MAX_PAGES)

        pages: list[dict] = []
        put_kwargs: dict = {"ContentType": "image/jpeg"}
        if settings.MINIO_SSE_ENABLED:
            put_kwargs["ServerSideEncryption"] = "AES256"
        first_content: bytes | None = None
        for page_no, path in enumerate(page_files, start=1):
            with open(path, "rb") as f:
                content = f.read()
            if first_content is None:
                first_content = content
            with Image.open(io.BytesIO(content)) as img:
                w, h = img.width, img.height
            key = _doc_page_key(source_key, page_no)
            s3.put_object(
                Bucket=settings.MINIO_BUCKET_MEDIA, Key=key, Body=content, **put_kwargs
            )
            pages.append({
                "key": key,
                "checksum": hashlib.sha256(content).hexdigest(),
                "width": w,
                "height": h,
            })

    # Vignette médiathèque = page 1 (même pipeline Pillow que les images).
    if first_content:
        _process_image(s3, first_content, source_key, updates)

    new_meta = dict(meta_col or {}) if isinstance(meta_col, dict) else {}
    new_meta.pop("pending_replace", None)
    doc_meta: dict = {
        "page_count": len(pages),
        "pages": pages,
        "source": source_kind,
        "render": {"format": "jpeg", "scale_to": DOC_PAGE_SCALE,
                   "quality": DOC_PAGE_JPEG_QUALITY},
    }
    if truncated:
        doc_meta["truncated"] = True
    new_meta["document"] = doc_meta
    updates["metadata"] = Json(new_meta)
    updates["page_count"] = len(pages)
    return pages


def _probe(tmp: str) -> dict:
    """ffprobe → {width, height, has_audio, is_hdr, duration_ms, fps}.

    `fps` est nécessaire dès l'encodage pour calculer un GOP ≤2s (cf. _gop_frames) :
    sans elle, le repli 60 images donne 2.4s à 25fps (PAL). Distinct de _probe_full
    (métadonnées détaillées calculées APRÈS encodage)."""
    out = {"width": None, "height": None, "has_audio": False, "is_hdr": False,
           "duration_ms": None, "fps": None, "src_kbps": None}
    result = subprocess.run(  # nosec B603,B607
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", tmp],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        logger.error("ffprobe failed (rc=%d): %s", result.returncode, result.stderr[:1000])
        return out
    info = json.loads(result.stdout)
    for stream in info.get("streams", []):
        ctype = stream.get("codec_type")
        if ctype == "video" and out["width"] is None:
            out["width"] = stream.get("width")
            out["height"] = stream.get("height")
            out["fps"] = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
            transfer = stream.get("color_transfer", "")
            if transfer in HDR_TRANSFERS:
                out["is_hdr"] = True
            if dur := stream.get("duration"):
                out["duration_ms"] = int(float(dur) * 1000)
            if vb := stream.get("bit_rate"):  # débit de la PISTE vidéo source (anti-gonflement)
                try:
                    out["src_kbps"] = round(int(vb) / 1000)
                except (ValueError, TypeError):
                    pass
        elif ctype == "audio":
            out["has_audio"] = True
            if out["duration_ms"] is None and (dur := stream.get("duration")):
                out["duration_ms"] = int(float(dur) * 1000)
    # Fallback durée via format si absente des streams
    if out["duration_ms"] is None:
        fmt_dur = info.get("format", {}).get("duration")
        if fmt_dur:
            out["duration_ms"] = int(float(fmt_dur) * 1000)
    # Fallback débit source via le format (inclut l'audio, approx) si la piste vidéo ne l'expose
    # pas (fréquent en MKV) — sert à borner le ré-encodage au plus près de la source.
    if out["src_kbps"] is None:
        fb = info.get("format", {}).get("bit_rate")
        if fb:
            try:
                out["src_kbps"] = round(int(fb) / 1000)
            except (ValueError, TypeError):
                pass
    return out


def _parse_fps(rate: str | None) -> float | None:
    """'30000/1001' → 29.97 ; '25/1' → 25.0 ; None/0 → None."""
    if not rate or "/" not in rate:
        return None
    try:
        num, den = rate.split("/")
        den_f = float(den)
        if den_f == 0:
            return None
        return round(float(num) / den_f, 3)
    except (ValueError, ZeroDivisionError):
        return None


def _probe_full(path: str) -> dict:
    """ffprobe complet → métadonnées techniques structurées pour le drawer média (§6).

    Probe l'état RÉELLEMENT diffusé (fichier après ré-encodage). Retourne
    {container, size_bytes, duration_ms, video:{...}, audio:{...}}. Les sous-titres
    sont ajoutés séparément par _extract_subtitles (ils sont en sidecar VTT, pas
    dans le mp4 ré-encodé).
    """
    meta: dict = {}
    result = subprocess.run(  # nosec B603,B607
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        return meta
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return meta

    fmt = info.get("format", {})
    if fmt.get("format_name"):
        meta["container"] = fmt["format_name"]
    if fmt.get("size"):
        try:
            meta["size_bytes"] = int(fmt["size"])
        except (ValueError, TypeError):
            pass
    if fmt.get("duration"):
        try:
            meta["duration_ms"] = int(float(fmt["duration"]) * 1000)
        except (ValueError, TypeError):
            pass

    def _kbps(stream: dict) -> int | None:
        br = stream.get("bit_rate")
        try:
            return round(int(br) / 1000) if br else None
        except (ValueError, TypeError):
            return None

    for stream in info.get("streams", []):
        ctype = stream.get("codec_type")
        if ctype == "video" and "video" not in meta:
            meta["video"] = {
                "codec": stream.get("codec_name"),
                "profile": stream.get("profile"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "fps": _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                "bitrate_kbps": _kbps(stream),
                "pix_fmt": stream.get("pix_fmt"),
                "hdr": stream.get("color_transfer") in HDR_TRANSFERS,
                # Champs sync-readiness (conformité passthrough, cf. media_conformance) :
                "r_frame_rate": stream.get("r_frame_rate"),
                "avg_frame_rate": stream.get("avg_frame_rate"),
                "nb_frames": stream.get("nb_frames"),
            }
        elif ctype == "audio" and "audio" not in meta:
            meta["audio"] = {
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "sample_rate": int(stream["sample_rate"]) if stream.get("sample_rate") else None,
                "bitrate_kbps": _kbps(stream),
                "language": (stream.get("tags") or {}).get("language"),
            }
    return meta


def _probe_keyframe_gop(path: str) -> dict:
    """Espacement max entre keyframes sur les ~30 premières secondes (cadence GOP).

    `-skip_frame nokey` ne décode que les keyframes ; on lit leurs timestamps et on
    calcule l'écart max. Borné à 30 s (`-read_intervals`) pour rester rapide.
    Renvoie {max_gop_sec, keyframes}. max_gop_sec=None si indéterminable.
    """
    out: dict = {"max_gop_sec": None, "keyframes": 0}
    result = subprocess.run(  # nosec B603,B607
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=best_effort_timestamp_time", "-of", "json",
         "-read_intervals", "%+30", path],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return out
    try:
        frames = json.loads(result.stdout).get("frames", [])
    except json.JSONDecodeError:
        return out
    times: list[float] = []
    for fr in frames:
        t = fr.get("best_effort_timestamp_time")
        if t is None:
            continue
        try:
            times.append(float(t))
        except (ValueError, TypeError):
            pass
    times.sort()
    out["keyframes"] = len(times)
    if len(times) >= 2:
        gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
        if gaps:
            out["max_gop_sec"] = round(max(gaps), 3)
    return out


def _detect_faststart(path: str) -> bool | None:
    """MP4/MOV : moov AVANT mdat (faststart) ? None si non applicable/illisible.

    Scanne les atomes de premier niveau (taille 32 bits, ou 64 bits si size==1).
    Pour les conteneurs non-ISOBMFF (mkv/webm) → pas de moov/mdat → None.
    """
    try:
        with open(path, "rb") as f:
            moov_pos: int | None = None
            mdat_pos: int | None = None
            offset = 0
            for _ in range(256):  # garde-fou anti-boucle
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[0:4], "big")
                box = header[4:8]
                if box == b"moov" and moov_pos is None:
                    moov_pos = offset
                elif box == b"mdat" and mdat_pos is None:
                    mdat_pos = offset
                if moov_pos is not None and mdat_pos is not None:
                    break
                if size == 1:  # largesize 64 bits
                    large = f.read(8)
                    if len(large) < 8:
                        break
                    size = int.from_bytes(large, "big")
                if size < 8:
                    break
                offset += size
                f.seek(offset)
            if moov_pos is None or mdat_pos is None:
                return None
            return moov_pos < mdat_pos
    except (OSError, ValueError):
        return None


def _enrich_video_conformance(meta: dict, path: str) -> dict:
    """Complète meta['video'] (gop, faststart) et calcule meta['conformance'].

    Renvoie le verdict (dict) ; le caller pose updates['conformance_status'].
    """
    video = meta.get("video")
    if not video:
        return {"status": "ready", "checks": {}, "reasons": []}
    video["gop"] = _probe_keyframe_gop(path)
    video["faststart"] = _detect_faststart(path)
    verdict = media_conformance.evaluate(meta)
    meta["conformance"] = verdict
    return verdict


def _probe_subtitle_streams(tmp: str) -> list[dict]:
    """Liste les pistes sous-titres de l'original → [{index, language, title}] (§7)."""
    result = subprocess.run(  # nosec B603,B607
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", tmp],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return []
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    subs = []
    sidx = 0  # index relatif aux pistes sous-titres (pour -map 0:s:N)
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags") or {}
        subs.append({
            "s_index": sidx,
            "language": (tags.get("language") or "").lower() or None,
            "title": tags.get("title"),
        })
        sidx += 1
    return subs


def _extract_subtitles(s3, tmp: str, storage_key: str, sub_streams: list[dict]) -> list[dict]:
    """Extrait chaque piste sous-titre de l'original en WebVTT + upload MinIO (§7).

    HTML5 <video> ne rend QUE du WebVTT via <track> (pas le mov_text embarqué) →
    on stocke un .vtt sidecar par langue à côté de la vidéo. Retourne
    [{lang, label, vtt_key}] pour media_metadata. Les pistes sans langue sont
    nommées sub1/sub2… (label) et indexées par leur position.
    """
    out: list[dict] = []
    base = storage_key.rsplit(".", 1)[0] if "." in storage_key else storage_key
    for sub in sub_streams:
        lang = sub.get("language") or f"sub{sub['s_index'] + 1}"
        vtt_path = f"{tmp}_{lang}.vtt"
        r = subprocess.run(  # nosec B603,B607
            ["ffmpeg", "-y", "-i", tmp, "-map", f"0:s:{sub['s_index']}", "-c:s", "webvtt", vtt_path],
            capture_output=True, timeout=300,
        )
        if r.returncode != 0 or not os.path.exists(vtt_path):
            logger.warning(
                "Subtitle extract failed (s:%d lang=%s) for %s: %s",
                sub["s_index"], lang, storage_key,
                r.stderr.decode(errors="replace")[:500] if r.stderr else "",
            )
            continue
        vtt_key = f"{base}.{lang}.vtt"
        up_kwargs: dict = {"ContentType": "text/vtt"}
        if settings.MINIO_SSE_ENABLED:
            up_kwargs["ServerSideEncryption"] = "AES256"
        try:
            s3.upload_file(vtt_path, settings.MINIO_BUCKET_MEDIA, vtt_key, ExtraArgs=up_kwargs)
            out.append({
                "lang": (sub.get("language") or lang),
                "label": sub.get("title") or (sub.get("language") or lang).upper(),
                "vtt_key": vtt_key,
            })
        finally:
            if os.path.exists(vtt_path):
                os.unlink(vtt_path)
    return out


# ── Sélection d'encodeur (matériel NVENC/QSV/VAAPI ou logiciel) ────────────────
def _select_encoder(codec: str, force_sw: bool) -> dict:
    if force_sw:
        sw = "libx264" if codec == "h264" else "libx265"
        return {"name": sw, "kind": "sw", "vaapi_device": None}
    return encoders.best_encoder(codec)


def _enc_pre_input(enc: dict) -> list[str]:
    """Args AVANT -i (VAAPI exige -vaapi_device en amont)."""
    if enc["kind"] == "vaapi" and enc.get("vaapi_device"):
        return ["-vaapi_device", enc["vaapi_device"]]
    return []


def _enc_filter(enc: dict, scale_box: int | None) -> str | None:
    """Chaîne -vf selon l'encodeur. scale_box = arête max du cadre (rendition) ou None.

    VAAPI encode en mémoire GPU → format=nv12,hwupload + scale_vaapi. Les autres (nvenc/qsv/sw)
    gardent le scale software (le frame scalé part vers l'encodeur)."""
    if enc["kind"] == "vaapi":
        parts = ["format=nv12", "hwupload"]
        if scale_box:
            parts.append(
                f"scale_vaapi=w='min({scale_box},iw)':h='min({scale_box},ih)':"
                f"force_original_aspect_ratio=decrease"
            )
        return ",".join(parts)
    if scale_box:
        return (f"scale=w='min({scale_box},iw)':h='min({scale_box},ih)':"
                f"force_original_aspect_ratio=decrease:force_divisible_by=2")
    return None


def _maxrate_kbps(width: int | None, height: int | None) -> int:
    """Plafond de débit (kbps) par résolution — politique « équilibrée » (validée avec Ed).

    C'est un PLAFOND (VBV), pas un débit cible : une source légère encodée en CRF/CQ reste
    sous ce plafond et garde sa qualité ; seuls les ré-encodages qui gonfleraient sont bornés.
    Anti-gonflement : empêche qu'un ré-encodage produise un fichier plus lourd que nécessaire.
    """
    edge = max(width or 0, height or 0)
    if edge >= 3840:   # 4K (HEVC)
        return 20000
    if edge >= 2560:   # 1440p
        return 12000
    if edge >= 1920:   # 1080p
        return 8000
    if edge >= 1280:   # 720p
        return 4000
    if edge > 0:
        return 2500
    return 0


def _maxrate_args(width: int | None, height: int | None, src_kbps: int | None = None) -> list[str]:
    """Args ffmpeg `-maxrate`/`-bufsize` (VBV) pour borner le débit de sortie. [] si
    résolution inconnue. bufsize = 2× maxrate (réservoir VBV standard).

    Le plafond = min(plafond par résolution, ~1,1× le débit de la SOURCE). Sans le terme
    source, ré-encoder une source déjà compacte (ex. 4K ~10 Mbps) en HEVC CQ produit
    ~14-16 Mbps < 20 Mbps → fichier plus gros que la source (gonflement). On se cale donc
    au plus près de la source (HEVC à débit égal = qualité ≥ source, taille ≈ source)."""
    cap = _maxrate_kbps(width, height)
    if not cap:
        return []
    if src_kbps and src_kbps > 0:
        cap = min(cap, max(round(src_kbps * 1.1), 800))  # plancher 800k = sécurité qualité
    return ["-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k"]


def _enc_codec_args(enc: dict, codec: str) -> list[str]:
    """Args -c:v + qualité selon l'encodeur. `-cq`/`-global_quality`/`-qp` ≈ le `-crf` sw."""
    kind, name = enc["kind"], enc["name"]
    if kind == "nvenc":
        a = ["-c:v", name, "-preset", "p5", "-tune", "hq", "-rc", "vbr",
             "-cq", "23" if codec == "hevc" else "21", "-b:v", "0"]
    elif kind == "qsv":
        a = ["-c:v", name, "-preset", "medium",
             "-global_quality", "23" if codec == "hevc" else "21"]
    elif kind == "vaapi":
        a = ["-c:v", name, "-qp", "24" if codec == "hevc" else "22"]
    elif codec == "hevc":
        a = ["-c:v", "libx265", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p"]
    else:
        a = ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p"]
    if codec == "hevc":
        a += ["-tag:v", "hvc1"]
    return a


def _build_video_cmd(
    tmp: str, out_path: str, probe: dict, force_sw: bool = False, box: int | None = None,
) -> list[str]:
    """Réencodage vidéo : encodeur matériel auto (NVENC/QSV/VAAPI) sinon libx264/265.

    HDR → toujours logiciel libx265 10-bit (params délicats, chemin éprouvé conservé).
    `force_sw` = repli après un échec d'encode matériel.
    `box` = arête maximale imposée (choix « encoder en définition inférieure » quand
    l'étalonnage annonce un encodage trop long). Réutilise `_enc_filter`, le même
    downscale que les artefacts adaptatifs — pas une seconde façon de rétrécir une image.
    """
    w = probe.get("width") or 0
    h = probe.get("height") or 0
    if box:
        # 🔑 Les dimensions EFFECTIVES décident du codec : une 4K ramenée en 1080p n'est
        # plus une 4K, donc elle sort en H.264 (compatibilité universelle) au lieu de HEVC.
        # Sans ce recalcul, le repli « définition inférieure » garderait l'encodeur le plus
        # lent — celui-là même qu'on essaie de fuir.
        factor = min(1.0, box / max(w, h)) if (w and h) else 1.0
        w, h = int(w * factor), int(h * factor)
    # >= (et non >) : une vidéo 4K STANDARD fait EXACTEMENT 3840×2160 = UHD_PIXELS pixels. Avec `>`
    # elle n'était PAS détectée UHD → encodée en H.264 (au lieu de HEVC). Or H.264 en 4K@60 = Level 6,
    # très mal décodé (SIGILL sur ChromeOS/Chromebox) ET gonfle énormément le débit (HEVC ~11 Mbps →
    # H.264 ~37 Mbps). Le 4K doit sortir en HEVC.
    is_uhd = (w * h) >= UHD_PIXELS
    is_hdr = probe["is_hdr"]
    gop = str(_gop_frames(probe.get("fps")))

    if is_hdr:
        # HDR10/HLG → libx265 10-bit + métadonnées de couleur (software uniquement).
        # Le downscale éventuel passe ici par un scale LOGICIEL : cette branche n'utilise
        # pas `_enc_filter` (pas d'encodeur matériel), et une chaîne de filtres VAAPI y
        # serait invalide.
        cmd = ["ffmpeg", "-y", "-i", tmp]
        if box:
            cmd += ["-vf", f"scale=w='min({box},iw)':h='min({box},ih)':"
                           f"force_original_aspect_ratio=decrease:force_divisible_by=2"]
        cmd += ["-c:v", "libx265", "-preset", "medium", "-crf", "22",
                "-pix_fmt", "yuv420p10le",
                "-x265-params",
                "hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                *_maxrate_args(w, h, probe.get("src_kbps")),
                "-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-movflags", "+faststart",
                "-threads", str(resources.ffmpeg_threads())]
        cmd += (["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-af", LOUDNORM]
                if probe["has_audio"] else ["-an"])
        cmd.append(out_path)
        return cmd

    codec = "hevc" if is_uhd else "h264"
    enc = _select_encoder(codec, force_sw)
    cmd = ["ffmpeg", "-y"] + _enc_pre_input(enc) + ["-i", tmp]
    vf = _enc_filter(enc, box)
    if vf:
        cmd += ["-vf", vf]
    cmd += _enc_codec_args(enc, codec)
    cmd += _maxrate_args(w, h, probe.get("src_kbps"))   # plafond de débit (anti-gonflement)
    cmd += ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-movflags", "+faststart"]
    # Plafond de threads SEULEMENT pour l'encode logiciel (l'encode GPU n'est pas CPU-bound).
    if enc["kind"] == "sw":
        cmd += ["-threads", str(resources.ffmpeg_threads())]
    if probe["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-af", LOUDNORM]
    else:
        cmd += ["-an"]
    cmd.append(out_path)
    return cmd


def _process_av_streaming(
    s3, storage_key: str, media_type: str, updates: dict, encode_mode: str = "auto",
    id_media: int | None = None, decision: dict | None = None,
) -> None:
    """Télécharge en streaming puis réencode (RAM constante), ou conserve tel quel
    si encode_mode=passthrough."""
    suffix = _ext(storage_key)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        tmp = f.name
    try:
        _download_with_progress(s3, storage_key, tmp, id_media)

        probe = _probe(tmp)
        if probe["width"]:
            updates.setdefault("width", probe["width"])
            updates.setdefault("height", probe["height"])
        if probe["duration_ms"]:
            updates.setdefault("duration_ms", probe["duration_ms"])

        if media_type == "video":
            _transcode_video(s3, tmp, storage_key, probe, updates, encode_mode, id_media, decision)
        elif media_type == "audio":
            _transcode_audio(
                s3, tmp, storage_key, updates, encode_mode, id_media,
                total_ms=probe.get("duration_ms") or 0,
            )
    finally:
        os.unlink(tmp)


_PASSTHROUGH_CODECS = {"h264", "avc1", "hevc", "h265", "hvc1"}


def _auto_passthrough_eligible(tmp: str, probe: dict) -> tuple[bool, dict | None]:
    """En mode `auto`, faut-il SAUTER le ré-encodage parce que la source est DÉJÀ conforme ?

    Anti-gonflement : ré-encoder une source déjà compacte et diffusable la grossit pour rien.
    On passthrough (remux -c copy si besoin, sinon conserve tel quel) quand la source est :
      - `ready` au sens media_conformance (codec décodable + bitrate OK + CFR + GOP≤2s + faststart),
      - en codec UNIVERSEL h264/hevc (vp9/av1 décodent mal sur certains players <video>/WebView83),
      - PAS du H.264 en 4K (= SIGILL Chromebox Level 6 → toujours ré-encoder en HEVC),
      - audio déjà en AAC (ou muet ; sinon le ré-encode normalise + loudnorm),
      - conteneur mp4/mov/m4v/webm (remuxable vers .mp4 sans ré-encoder),
      - débit source ≤ plafond ×1,5 (une source conforme mais TRÈS lourde est ré-encodée pour
        la rapetisser au plafond — sinon on garderait un fichier inutilement gros).
    Renvoie (eligible, meta_enrichi) ; meta réutilisable par l'appelant (évite un 2e probe).
    """
    if probe.get("is_hdr"):
        return False, None  # HDR → toujours libx265 10-bit (chemin éprouvé, params délicats)
    meta = _probe_full(tmp)
    video = meta.get("video")
    if not video:
        return False, None
    verdict = _enrich_video_conformance(meta, tmp)
    if verdict["status"] != "ready":
        return False, meta
    codec = (video.get("codec") or "").lower()
    if codec not in _PASSTHROUGH_CODECS:
        return False, meta
    w = video.get("width") or probe.get("width") or 0
    h = video.get("height") or probe.get("height") or 0
    if codec in ("h264", "avc1") and (w * h) >= UHD_PIXELS:
        return False, meta
    audio = meta.get("audio")
    if audio and (audio.get("codec") or "").lower() != "aac":
        return False, meta
    container = (meta.get("container") or "").lower()
    if not any(c in container for c in ("mp4", "mov", "m4v", "webm")):
        return False, meta
    br = video.get("bitrate_kbps")
    cap = _maxrate_kbps(w, h)
    if br and cap and br > cap * 1.5:
        return False, meta
    return True, meta


def _encode_reporter(id_media: int | None, phase: str, profile: str | None = None):
    """Callback `on_progress` publiant vers Redis, ou None si on ne sait pas quoi identifier.

    Renvoyer None quand `id_media` est absent est volontaire : `_run_ffmpeg` retombe alors
    sur son chemin bloquant historique, au lieu de faire tourner la machinerie de streaming
    pour une progression que personne ne pourra lire."""
    if not id_media:
        return None

    def _report(pct, eta_s, speed):
        encode_progress.publish(
            id_media,
            encode_progress.payload(
                pct=pct, eta_s=eta_s, speed=speed, phase=phase, profile=profile
            ),
        )

    return _report


def _download_with_progress(
    s3, key: str, dest: str, id_media: int | None, phase: str = "download",
) -> None:
    """Télécharge un objet MinIO en publiant l'avancement.

    Sans ce report, le média restait « en cours » SANS barre pendant tout le
    téléchargement — mesuré à ~9 s pour 1,7 Go en local (≈190 Mo/s), donc invisible sur un
    fichier courant mais **~105 s de silence sur un master 4K de 20 Go**, ce qui se lit
    comme un blocage. Le jour où le stockage passe sur du S3 distant, ce silence devient
    la norme plutôt que l'exception.

    ⚠️ On ne publie PAS de `speed` ici : le champ signifie « × le temps réel » pour un
    encodage. Y mettre un débit en octets afficherait « 190× le temps réel » sur un
    téléchargement, ce qui ne veut rien dire.
    """
    report = _encode_reporter(id_media, phase)
    if report is None:
        s3.download_file(settings.MINIO_BUCKET_MEDIA, key, dest)
        return
    try:
        total = s3.head_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=key)["ContentLength"]
    except Exception:  # noqa: BLE001 — taille inconnue : barre indéterminée, pas d'échec
        total = 0
    state = {"done": 0, "last": 0.0, "t0": time.monotonic()}

    def _cb(chunk: int) -> None:
        state["done"] += chunk
        now = time.monotonic()
        if now - state["last"] < _PROGRESS_MIN_INTERVAL_S:
            return
        state["last"] = now
        pct = eta = None
        if total:
            pct = min(100.0, state["done"] * 100.0 / total)
            rate = state["done"] / max(0.001, now - state["t0"])
            if rate > 0:
                eta = int((total - state["done"]) / rate)
        try:
            report(pct, eta, None)
        except Exception as exc:  # noqa: BLE001 — l'affichage ne casse pas un téléchargement
            logger.debug("download progress failed: %s", exc)

    s3.download_file(settings.MINIO_BUCKET_MEDIA, key, dest, Callback=_cb)


def _transcode_video(
    s3, tmp: str, storage_key: str, probe: dict, updates: dict, encode_mode: str = "auto",
    id_media: int | None = None, decision: dict | None = None,
) -> None:
    # Thumbnail à 1s
    thumb_path = tmp + "_thumb.jpg"
    subprocess.run(  # nosec B603,B607
        ["ffmpeg", "-y", "-ss", "1", "-i", tmp, "-vframes", "1", "-vf", "scale=320:-1", thumb_path],
        capture_output=True, timeout=120,
    )
    if os.path.exists(thumb_path):
        thumb_key = _thumb_key(storage_key)
        with open(thumb_path, "rb") as tf:
            s3.put_object(
                Bucket=settings.MINIO_BUCKET_MEDIA,
                Key=thumb_key,
                Body=tf.read(),
                ContentType="image/jpeg",
            )
        updates["thumbnail_url"] = thumb_key
        os.unlink(thumb_path)

    # Encodage : passthrough = on conserve le fichier tel quel (cas mur d'images,
    # alignement frame-accurate à préserver) ; auto = ré-encodage streaming-optimisé.
    out_path = tmp + "_enc.mp4"
    encoded_ok = False
    encode_stats: dict | None = None   # débit constaté, reporté dans le metadata final
    src_meta: dict | None = None  # meta du fichier source (réutilisable si passthrough sans remux)
    do_encode = encode_mode != "passthrough"
    if do_encode and encode_mode == "auto":
        # Anti-gonflement : si la source est déjà conforme, on ne ré-encode PAS (cf. helper).
        eligible, src_meta = _auto_passthrough_eligible(tmp, probe)
        if eligible:
            logger.info("Auto-passthrough: source deja conforme, pas de re-encodage pour %s",
                        storage_key)
            do_encode = False
    if not do_encode:
        logger.info("Video passthrough: fichier conserve tel quel pour %s", storage_key)
    else:
        # Timeout ADAPTATIF à la durée de la vidéo (au lieu d'un 1800s fixe qui faisait échouer les
        # grosses vidéos >3 Go : 3 encodages parallèles saturent le GPU/CPU → chacun bien plus lent
        # que le temps réel → TimeoutExpired).
        #
        # 🔴 Le budget dépend de l'encodeur RÉELLEMENT retenu, pas de la branche. Jusqu'au
        # 2026-08-02, la 1ʳᵉ tentative prenait toujours le budget MATÉRIEL et le budget logiciel
        # n'était atteignable que par le repli — donc jamais sur un serveur SANS GPU, où
        # libx264/265 est le PREMIER choix. Mesuré sur le banc : une source 4K de 205 s en
        # libx265 tuée à 1800 s alors qu'elle n'avait pas fini, et — pire — l'échec est
        # SILENCIEUX (on garde l'original, `transcode_status` reste 'done').
        #
        # 🔴 Et un multiplicateur unique ne convient pas : le coût de l'encodage logiciel croît
        # avec le nombre de PIXELS, et libx265 est nettement plus lent que libx264. Or la règle
        # produit envoie justement toute source > FHD vers HEVC (`_build_video_cmd`) : 4K et
        # libx265 se cumulent donc systématiquement. 12x convenait au 1080p/H.264, pas à ça.
        #
        # ✅ Ces facteurs ne servent plus que de REPLI (source trop courte pour être
        # échantillonnée, ou sonde en échec) : le budget nominal vaut désormais 2× une
        # estimation MESURÉE. Cf. `_estimate_encode_seconds`.
        _dur_s = (probe.get("duration_ms") or 0) / 1000
        _box = (decision or {}).get("downscale_box")
        cmd = _build_video_cmd(tmp, out_path, probe, box=_box)
        enc_used = next((cmd[i + 1] for i, a in enumerate(cmd) if a == "-c:v"), "?")

        # ── Étalonnage : combien de temps CETTE machine met-elle sur CETTE source ? ──
        # On ne sonde pas quand la question est déjà tranchée (l'utilisateur a répondu, ou
        # il a choisi une définition inférieure) : ce serait refaire payer la mesure et,
        # pire, risquer de re-poser la même question en boucle.
        _estimate = _speed = None
        if decision is None:
            _estimate, _speed = _estimate_encode_seconds(cmd, probe)
            band = _decision_band(_estimate)
            if band != "auto":
                logger.info(
                    "Encodage estimé à %.0f s (%.2f× le temps réel) → bande '%s' : on rend "
                    "la main à l'utilisateur pour %s", _estimate, _speed or 0, band, storage_key,
                )
                updates["transcode_status"] = "awaiting_decision"
                updates["_decision"] = {
                    "band": band,
                    "estimate_s": int(_estimate),
                    "measured_speed": round(_speed, 3) if _speed else None,
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "encoder": enc_used,
                    "downscale": _downscale_options(
                        _estimate, probe.get("width") or 0, probe.get("height") or 0
                    ),
                }
                return

        if _estimate:
            # Budget = 2× la mesure. Le facteur 2 absorbe la contention (d'autres encodages
            # peuvent démarrer après la sonde) sans rouvrir la porte aux 4 h de processeur
            # brûlées pour rien de la 1.0.24.
            _timeout = max(1800, int(_estimate * 2))
            _sw_timeout, _mult = _timeout, 0.0
        else:
            _timeout, _mult = _encode_timeout(
                _dur_s, probe.get("width") or 0, probe.get("height") or 0, enc_used
            )
            # Le repli logiciel après un échec matériel a son propre budget, calculé pareil.
            _sw_timeout, _ = _encode_timeout(
                _dur_s, probe.get("width") or 0, probe.get("height") or 0, "libx265"
            )
        logger.info("Video transcode: encoder=%s hdr=%s %dx%d box=%s timeout=%ds (dur=%.0fs, "
                    "estimé=%s)", enc_used, probe["is_hdr"], probe.get("width") or 0,
                    probe.get("height") or 0, _box or "-", _timeout, _dur_s,
                    f"{_estimate:.0f}s" if _estimate else "non mesuré")
        _dur_ms = probe.get("duration_ms") or 0
        _report = _encode_reporter(id_media, "encode")
        _t0 = time.monotonic()
        r = _run_ffmpeg(cmd, timeout=_timeout, on_progress=_report, total_ms=_dur_ms)
        encoded_ok = r.returncode == 0 and os.path.exists(out_path)
        # Repli auto sur libx264/265 si l'encode MATÉRIEL échoue (driver/GPU capricieux ne
        # doit jamais bloquer la chaîne). On ne retente qu'une fois, en logiciel.
        if not encoded_ok and not enc_used.startswith("lib"):
            logger.warning("ffmpeg HW encode (%s) failed rc=%d → repli libx264/265 for %s",
                           enc_used, r.returncode, storage_key)
            cmd = _build_video_cmd(tmp, out_path, probe, force_sw=True, box=_box)
            # La barre repart de zéro : c'est un SECOND encodage, pas la suite du premier.
            # Laisser filer les 40 % déjà affichés ferait reculer la progression sous les
            # yeux de l'utilisateur, ce qui se lit comme une panne.
            r = _run_ffmpeg(cmd, timeout=_sw_timeout, on_progress=_report, total_ms=_dur_ms)
            encoded_ok = r.returncode == 0 and os.path.exists(out_path)
        if not encoded_ok:
            # Échec d'encodage : on garde l'original (transcode_status reste 'done')
            logger.error(
                "ffmpeg video transcode failed (rc=%d) for %s: %s",
                r.returncode, storage_key,
                r.stderr.decode(errors="replace")[:2000] if r.stderr else "",
            )
        # Débit RÉELLEMENT constaté sur cette machine. Conservé sur le média (aucune table
        # nouvelle) et agrégé par la page « Serveur » : c'est la seule façon de répondre
        # « ce serveur encode-t-il vite ? » avec des chiffres qu'on n'a pas inventés — et
        # de vérifier après coup que l'estimation ne raconte pas d'histoires.
        _elapsed = time.monotonic() - _t0
        if encoded_ok and _elapsed > 0 and _dur_ms:
            encode_stats = {
                "encoder": enc_used,
                "seconds": round(_elapsed, 1),
                "speed": round((_dur_ms / 1000.0) / _elapsed, 3),
                "width": probe.get("width"),
                "height": probe.get("height"),
                "box": _box,
                "estimated_s": int(_estimate) if _estimate else None,
            }

        # L'encodage n'est pas la fin du traitement : restent l'extraction des sous-titres,
        # le remux éventuel, l'upload MinIO (plusieurs minutes sur un gros fichier) et le
        # contrôle de conformité. Sans ce changement de phase, l'écran afficherait 100 %
        # pendant tout ce temps sans dire de quoi il retourne.
        if _report:
            encode_progress.publish(
                id_media,
                encode_progress.payload(pct=None, eta_s=None, speed=None, phase="finalize"),
            )

    # Sous-titres (§7) : extraits de l'ORIGINAL en sidecar WebVTT (le ré-encodage
    # les jette de toute façon, et HTML5 ne rend que du WebVTT via <track>).
    sub_streams = _probe_subtitle_streams(tmp)
    subtitles = _extract_subtitles(s3, tmp, storage_key, sub_streams) if sub_streams else []

    # Conteneur de sortie TOUJOURS .mp4 : un <video> de navigateur (ChromeOS PWA, extension,
    # Electron/WebView hors mpv/exo) ne lit pas le MKV/MOV/AVI. Le ré-encodage 'auto' produit
    # déjà du .mp4 ; en passthrough (ou échec d'encodage), si l'original n'est pas .mp4 on REMUX
    # (copie des flux, sans ré-encoder → alignement frame-accurate préservé) vers .mp4. Le
    # storage_key bascule alors en .mp4 et l'ancien objet est supprimé (comme l'audio → .ogg).
    served = out_path if encoded_ok else tmp
    remux_path = None
    if not encoded_ok and _ext(storage_key).lower() != ".mp4":
        remux_path = tmp + "_remux.mp4"
        rr = _run_ffmpeg(
            ["ffmpeg", "-y", "-i", tmp, "-c", "copy", "-movflags", "+faststart", remux_path],
            timeout=900,
        )
        if rr.returncode == 0 and os.path.exists(remux_path):
            served = remux_path
            encoded_ok = True
        else:
            logger.warning("Remux .mp4 echoue pour %s (rc=%d) -> conserve tel quel",
                           storage_key, rr.returncode)
            remux_path = None

    new_key = f"{storage_key.rsplit('.', 1)[0]}.mp4" if "." in storage_key else f"{storage_key}.mp4"

    if encoded_ok:
        up_kwargs: dict = {"ContentType": "video/mp4"}
        if settings.MINIO_SSE_ENABLED:
            up_kwargs["ServerSideEncryption"] = "AES256"
        s3.upload_file(served, settings.MINIO_BUCKET_MEDIA, new_key, ExtraArgs=up_kwargs)
        if new_key != storage_key:
            try:
                s3.delete_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to delete old video object %s: %s", storage_key, exc)
            updates["storage_key"] = new_key
            updates["mime_type"] = "video/mp4"

    # Métadonnées techniques (§6) : état réellement diffusé (output .mp4 encodé/remuxé, sinon
    # l'original conservé). Colonne DB = "metadata".
    # Conformité & sync-readiness (verdict intrinsèque, cf. media_conformance).
    # En `auto` ré-encodé → CFR/GOP2s/faststart → 'ready' par construction ; en passthrough
    # (explicite ou auto) on obtient le vrai verdict du fichier d'origine. Si on a déjà le meta
    # de la source (auto-passthrough sans remux), on le réutilise pour éviter un 2e probe.
    if src_meta is not None and served == tmp:
        meta = src_meta
        verdict = meta.get("conformance") or media_conformance.evaluate(meta)
    else:
        meta = _probe_full(served)
        verdict = _enrich_video_conformance(meta, served)
    if subtitles:
        meta["subtitles"] = subtitles
    if encode_stats:
        meta["encode"] = encode_stats
    updates["conformance_status"] = verdict["status"]
    updates["metadata"] = Json(meta)

    # 🔴 Les dimensions de la LIGNE doivent décrire le fichier SERVI, pas la source.
    # Jusqu'ici les deux étaient toujours identiques (aucun encodage ne changeait la
    # taille de l'image) et `_process_av_streaming` les posait depuis le probe SOURCE.
    # Le choix « encoder en définition inférieure » casse cette hypothèse : mesuré, une
    # 4K ramenée en 1080p gardait `width=3840` en base alors que le fichier servi faisait
    # 1920 — le produit se serait cru en 4K (génération d'un artefact « FHD » identique
    # au parent, sélection de rendition faussée, affichage mensonger).
    served_video = meta.get("video") or {}
    if served_video.get("width") and served_video.get("height"):
        updates["width"] = served_video["width"]
        updates["height"] = served_video["height"]

    for p in (out_path, remux_path):
        if p and p != tmp and os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass


def _transcode_audio(
    s3, tmp: str, storage_key: str, updates: dict, encode_mode: str = "auto",
    id_media: int | None = None, total_ms: int = 0,
) -> None:
    """Réencode en OGG Vorbis normalisé (change le storage_key en .ogg), ou conserve
    le fichier tel quel si encode_mode=passthrough."""
    if encode_mode == "passthrough":
        logger.info("Audio passthrough: fichier conserve tel quel pour %s", storage_key)
        updates["metadata"] = Json(_probe_full(tmp))
        return
    out_path = tmp + "_enc.ogg"
    cmd = [  # nosec B603,B607
        "ffmpeg", "-y", "-i", tmp,
        "-c:a", "libvorbis", "-qscale:a", "6", "-ar", "48000",
        "-af", LOUDNORM, "-vn", out_path,
    ]
    r = _run_ffmpeg(
        cmd, timeout=900,
        on_progress=_encode_reporter(id_media, "encode"),
        total_ms=total_ms,
    )
    if r.returncode != 0:
        # libvorbis absent ou échec : on garde l'original sans casser
        logger.error(
            "ffmpeg audio transcode failed (rc=%d) for %s: %s",
            r.returncode, storage_key,
            r.stderr.decode(errors="replace")[:2000] if r.stderr else "",
        )
        return
    if not os.path.exists(out_path):
        return

    # Nouveau storage_key en .ogg
    base = storage_key.rsplit(".", 1)[0] if "." in storage_key else storage_key
    new_key = f"{base}.ogg"
    up_kwargs: dict = {"ContentType": "audio/ogg"}
    if settings.MINIO_SSE_ENABLED:
        up_kwargs["ServerSideEncryption"] = "AES256"
    s3.upload_file(out_path, settings.MINIO_BUCKET_MEDIA, new_key, ExtraArgs=up_kwargs)
    # Métadonnées techniques (§6) — état réellement diffusé (.ogg encodé). Colonne DB = "metadata".
    updates["metadata"] = Json(_probe_full(out_path))
    os.unlink(out_path)

    if new_key != storage_key:
        # Supprimer l'ancien objet et mettre à jour la référence DB
        try:
            s3.delete_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete old audio object %s: %s", storage_key, exc)
        updates["storage_key"] = new_key
        updates["mime_type"] = "audio/ogg"


def _build_rendition_cmd(tmp: str, out_path: str, probe: dict, box: int,
                         force_sw: bool = False, codec: str = "h264") -> list[str]:
    """Commande ffmpeg pour un artefact : downscale dans le cadre `box`×`box` (aspect
    préservé, dims paires), encodé en `codec` (h264 par défaut ; hevc pour le profil HEVC)
    via l'encodeur matériel auto (NVENC/QSV/VAAPI) sinon libx264/libx265. Audio AAC normalisé
    conservé. `force_sw` = repli après échec HW. HEVC taggé hvc1 (compat ExoPlayer/players)."""
    gop = str(_gop_frames(probe.get("fps")))
    enc = _select_encoder(codec, force_sw)
    cmd = ["ffmpeg", "-y"] + _enc_pre_input(enc) + ["-i", tmp]
    cmd += ["-vf", _enc_filter(enc, box)]   # downscale (scale ou scale_vaapi selon l'encodeur)
    cmd += _enc_codec_args(enc, codec)
    cmd += _maxrate_args(box, box)   # plafond de débit selon le cadre de downscale (anti-gonflement)
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1"]   # hvc1 (pas hev1) → lecture <video>/ExoPlayer fiable
    cmd += ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-movflags", "+faststart"]
    if enc["kind"] == "sw":
        cmd += ["-threads", str(resources.ffmpeg_threads())]
    if probe["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-af", LOUDNORM]
    else:
        cmd += ["-an"]
    cmd.append(out_path)
    return cmd


def _rotated_frame(probe: dict) -> tuple[int, int, str]:
    """(largeur, hauteur, codec) du cadre PAYSAGE d'une rendition rotée, d'après la source.

    Source au-delà de la FHD (arête > 1920) → cadre 4K 3840×2160 en HEVC (validé HW XT1144 ;
    les BrightSign décodent le HEVC hvc1 en matériel) ; sinon cadre FHD 1920×1080 en H.264.
    Le contenu roté est inscrit dans ce cadre (scale decrease) puis complété par un `pad`
    noir — un portrait 2160×3840 roté remplit 100 % du 4K, un 2160×3778 en remplit 98 %.
    """
    edge = max(probe.get("width") or 0, probe.get("height") or 0)
    if edge > 1920:
        return 3840, 2160, "hevc"
    return 1920, 1080, "h264"


def _build_rotated_cmd(tmp: str, out_path: str, probe: dict, transpose: int,
                       frame_w: int, frame_h: int, codec: str,
                       force_sw: bool = False) -> list[str]:
    """Commande ffmpeg d'une rendition ROTÉE (BrightSign portrait) — la commande validée HW
    du 2026-08-14 : `transpose` grave la rotation dans les pixels, le fichier reste PAYSAGE
    (le widget BS n'est plus roté ; lui rajouter une rotation la DÉFERAIT — mesuré, il ne
    reste que 32 % de la largeur).

    ⚠️ Pièges consignés ce jour-là : `-tag:v hvc1` obligatoire (hev1 = refusé par Chromium) ·
    `-b_ref_mode disabled` sur NVENC = la traduction du b_pyramid=0 recommandé par BrightSign ·
    ⛔ jamais de `-level` (5.1 en tier main plafonne à 40 Mbit/s → NVENC refuse d'initialiser).
    La rotation est un filtre LOGICIEL (transpose/scale/pad) même pour VAAPI : on n'upload en
    mémoire GPU qu'après (hwupload en fin de chaîne).
    """
    gop = str(_gop_frames(probe.get("fps")))
    enc = _select_encoder(codec, force_sw)
    vf = (
        f"transpose={transpose},"
        f"scale={frame_w}:{frame_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
        + ("format=nv12,hwupload" if enc["kind"] == "vaapi" else "format=yuv420p")
    )
    cmd = ["ffmpeg", "-y"] + _enc_pre_input(enc) + ["-i", tmp, "-vf", vf]
    cmd += _enc_codec_args(enc, codec)
    if enc["kind"] == "nvenc":
        cmd += ["-b_ref_mode", "disabled"]   # b_pyramid=0 (reco BrightSign), dialecte nvenc
    cmd += _maxrate_args(frame_w, frame_h)
    if codec == "hevc" and "-tag:v" not in cmd:
        cmd += ["-tag:v", "hvc1"]   # ceinture : _enc_codec_args le pose déjà pour hevc
    cmd += ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-movflags", "+faststart"]
    if enc["kind"] == "sw":
        cmd += ["-threads", str(resources.ffmpeg_threads())]
    if probe["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-af", LOUDNORM]
    else:
        cmd += ["-an"]
    cmd.append(out_path)
    return cmd


def _rendition_key(parent_key: str, profile: str) -> str:
    base = parent_key.rsplit(".", 1)[0] if "." in parent_key else parent_key
    return f"{base}.{profile}.mp4"


def _refresh_has_renditions(cur, id_parent: int) -> None:
    """Pose cms_media.has_renditions = (au moins une rendition prête existe)."""
    cur.execute(
        "UPDATE cms_media SET has_renditions = EXISTS("
        "  SELECT 1 FROM cms_media_renditions "
        "  WHERE id_parent=%s AND status='ready'), updated_at=now() "
        "WHERE id_media=%s",
        (id_parent, id_parent),
    )


def _maybe_autogenerate_renditions(cur, id_media: int) -> None:
    """À l'upload d'une vidéo : si l'org a `rendition_policy=auto_fhd` et que la source
    dépasse la cible FHD, programme la génération de la rendition FHD (opt-in par org)."""
    cur.execute(
        "SELECT m.width, m.height, o.rendition_policy "
        "FROM cms_media m JOIN cms_organizations o ON o.id_org = m.id_org "
        "WHERE m.id_media=%s",
        (id_media,),
    )
    row = cur.fetchone()
    if not row:
        return
    width, height, policy = row
    if policy != "auto_fhd":
        return
    box = RENDITION_BOXES["fhd"]
    # Génère uniquement si la source est réellement plus grande que la cible (sinon inutile).
    if max(width or 0, height or 0) <= box:
        return
    generate_rendition.delay(id_media, "fhd")


@celery_app.task(name="app.workers.media_worker.generate_rendition", bind=True, max_retries=2)
def generate_rendition(self, id_parent: int, profile: str = "fhd") -> None:
    """Génère (ou régénère) un artefact `profile` du média parent (idempotent par
    (parent, profile)). Downscale dans le cadre du profil + ré-encode H.264, upload
    MinIO, conformité. Pose cms_media.has_renditions. Cf. EVOLUTIONS.md."""
    prof = RENDITION_PROFILES.get(profile)
    box = prof["box"] if prof else None
    codec = prof["codec"] if prof else "h264"
    transpose = prof.get("transpose") if prof else None
    conn = _db_conn()
    cur = conn.cursor()
    id_rendition = None
    try:
        cur.execute(
            "SELECT storage_key, media_type, width, height FROM cms_media "
            "WHERE id_media=%s AND is_deleted=false",
            (id_parent,),
        )
        row = cur.fetchone()
        if not row:
            return
        storage_key, media_type, src_w, src_h = row
        if media_type != "video" or (box is None and transpose is None):
            logger.warning("generate_rendition: media %d not eligible (type=%s profile=%s)",
                           id_parent, media_type, profile)
            return

        # Anti-double-encodage : les profils ROTÉS sont enfilés à la demande depuis le sync
        # (_resolve_variant_key) — deux syncs simultanés (deux BS de même rotation) peuvent
        # enfiler la même tâche avant que la 1re ait posé sa ligne. Un 'processing' FRAIS
        # (< 30 min) = un autre worker s'en occupe → on sort sans rien toucher. Au-delà de
        # 30 min (worker tué net, ligne orpheline), on reprend la main.
        cur.execute(
            "SELECT status, updated_at > now() - interval '30 minutes' "
            "FROM cms_media_renditions WHERE id_parent=%s AND profile=%s",
            (id_parent, profile),
        )
        existing = cur.fetchone()
        if existing and existing[0] == "processing" and existing[1]:
            logger.info("generate_rendition(%d, %s): déjà en cours (processing frais) — skip",
                        id_parent, profile)
            return

        # Upsert ligne rendition en 'processing'
        cur.execute(
            "INSERT INTO cms_media_renditions (id_parent, profile, status, updated_at) "
            "VALUES (%s, %s, 'processing', now()) "
            "ON CONFLICT (id_parent, profile) DO UPDATE "
            "  SET status='processing', error_detail=NULL, updated_at=now() "
            "RETURNING id_rendition",
            (id_parent, profile),
        )
        id_rendition = cur.fetchone()[0]
        conn.commit()

        s3 = _s3()
        suffix = _ext(storage_key)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            tmp = f.name
        out_path = tmp + f"_{profile}.mp4"
        try:
            _download_with_progress(s3, storage_key, tmp, id_parent)
            probe = _probe(tmp)
            if transpose is not None:
                if prof.get("frame"):
                    # Profil roté PAR DÉFINITION (_fhd) : cadre paysage FIXE + codec du profil.
                    frame_w, frame_h = prof["frame"]
                    codec = prof["codec"] or "h264"
                else:
                    # Profil ROTÉ de base : cadre paysage + codec dérivés de la SOURCE réelle
                    # (probe, pas les colonnes DB — c'est le fichier qu'on encode qui fait foi).
                    frame_w, frame_h, codec = _rotated_frame(probe)
                cmd = _build_rotated_cmd(tmp, out_path, probe, transpose, frame_w, frame_h, codec)
            else:
                cmd = _build_rendition_cmd(tmp, out_path, probe, box, codec=codec)
            enc_used = next((cmd[i + 1] for i, a in enumerate(cmd) if a == "-c:v"), "?")
            logger.info("Rendition %s (%s, %dx%d→%s) encoder=%s for media %d",
                        profile, codec, src_w or 0, src_h or 0,
                        (f"frame {frame_w}x{frame_h} transpose={transpose}"
                         if transpose is not None else f"box {box}"),
                        enc_used, id_parent)
            # La progression d'un artefact est publiée sur la clé du PARENT (c'est lui que
            # l'utilisateur regarde ; l'artefact n'a pas de vignette propre), avec sa phase
            # et son profil pour que l'écran dise « rendition 720p » et pas « encodage ».
            _rep = _encode_reporter(id_parent, "rendition", profile)
            _rend_ms = probe.get("duration_ms") or 0
            r = _run_ffmpeg(cmd, timeout=1800, on_progress=_rep, total_ms=_rend_ms)
            # Repli auto sur l'encodeur logiciel (libx264/libx265) si l'encode matériel échoue.
            if (r.returncode != 0 or not os.path.exists(out_path)) and not enc_used.startswith("lib"):
                logger.warning("HW rendition (%s) failed rc=%d → repli software (media %d)",
                               enc_used, r.returncode, id_parent)
                if transpose is not None:
                    cmd = _build_rotated_cmd(tmp, out_path, probe, transpose,
                                             frame_w, frame_h, codec, force_sw=True)
                else:
                    cmd = _build_rendition_cmd(tmp, out_path, probe, box, force_sw=True, codec=codec)
                r = _run_ffmpeg(cmd, timeout=1800, on_progress=_rep, total_ms=_rend_ms)
            if r.returncode != 0 or not os.path.exists(out_path):
                raise RuntimeError(
                    "ffmpeg rendition failed (rc=%d): %s" % (
                        r.returncode,
                        r.stderr.decode(errors="replace")[:1000] if r.stderr else "",
                    )
                )

            rend_key = _rendition_key(storage_key, profile)
            up_kwargs: dict = {"ContentType": "video/mp4"}
            if settings.MINIO_SSE_ENABLED:
                up_kwargs["ServerSideEncryption"] = "AES256"
            s3.upload_file(out_path, settings.MINIO_BUCKET_MEDIA, rend_key, ExtraArgs=up_kwargs)

            meta = _probe_full(out_path)
            verdict = _enrich_video_conformance(meta, out_path)
            vid = meta.get("video") or {}
            size_bytes = os.path.getsize(out_path)

            cur.execute(
                "UPDATE cms_media_renditions SET status='ready', storage_key=%s, "
                "width=%s, height=%s, codec=%s, file_size_bytes=%s, file_meta=%s, "
                "conformance_status=%s, error_detail=NULL, updated_at=now() "
                "WHERE id_rendition=%s",
                (rend_key, vid.get("width"), vid.get("height"), vid.get("codec"),
                 size_bytes, Json(meta), verdict["status"], id_rendition),
            )
            _refresh_has_renditions(cur, id_parent)
            conn.commit()

            # Rendition prête → notifier (graceful) les players de l'org pour qu'ils
            # re-résolvent la variante servie (passage 4K parent -> FHD). Sinon ils
            # gardent l'ancien payload jusqu'au prochain sync naturel et continuent
            # de jouer le 4K (qui rame sur les players FHD).
            try:
                cur.execute("SELECT id_org FROM cms_media WHERE id_media=%s", (id_parent,))
                row = cur.fetchone()
                if row:
                    cur.execute("SELECT id_player FROM cms_players WHERE id_org=%s", (row[0],))
                    pids = [r[0] for r in cur.fetchall()]
                    from app.workers.event_worker import _push_sync
                    _push_sync(pids, is_preemptive=False)
            except Exception as e:  # noqa: BLE001 — notification best-effort
                logger.warning("rendition force_sync failed for media %s: %s", id_parent, e)
        finally:
            for p in (tmp, out_path):
                if os.path.exists(p):
                    os.unlink(p)

    except Exception as exc:
        logger.exception("generate_rendition(%d, %s) failed: %s", id_parent, profile, exc)
        conn.rollback()
        if id_rendition is not None:
            cur.execute(
                "UPDATE cms_media_renditions SET status='failed', error_detail=%s, "
                "updated_at=now() WHERE id_rendition=%s",
                (str(exc)[:2000], id_rendition),
            )
            _refresh_has_renditions(cur, id_parent)
            conn.commit()
        # ⚠️ MÊME RÈGLE QUE `transcode_media` (1.0.24) — elle n'avait jamais été appliquée
        # ici. Une EXPIRATION est déterministe : rejouer le même encodage avec les mêmes
        # paramètres expirera de la même façon. Avec `max_retries=2` on brûlait **3 fois**
        # le budget (jusqu'à 90 min de processeur pour un échec garanti), en affamant au
        # passage tous les autres encodages du serveur. Atteignable pour de vrai : le
        # profil `hevc_4k` encode en 4K HEVC — sur une machine sans GPU (0,387× mesuré le
        # 2026-08-03), toute source de plus de ~12 min dépasse le budget.
        if isinstance(exc, subprocess.TimeoutExpired):
            logger.error(
                "generate_rendition(%d, %s): encodage expiré — PAS de reprise (échec "
                "déterministe). Activer un GPU (setup-gpu.sh) ou choisir un profil plus "
                "léger que %s.", id_parent, profile, profile,
            )
            return
        raise self.retry(exc=exc, countdown=120)
    finally:
        encode_progress.clear(id_parent)
        cur.close()
        conn.close()


@celery_app.task(name="app.workers.media_worker.delete_media_files")
def delete_media_files(
    storage_key: str, thumbnail_url: str | None = None, extra_keys: list[str] | None = None
) -> None:
    """Supprime les objets MinIO après suppression logique d'un média (+ artefacts/renditions)."""
    s3 = _s3()
    keys = [storage_key]
    if thumbnail_url:
        keys.append(thumbnail_url)
    if extra_keys:
        keys.extend(k for k in extra_keys if k)
    logger = logging.getLogger(__name__)
    for key in keys:
        try:
            s3.delete_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete MinIO object %s: %s", key, exc)


def _thumb_key(storage_key: str) -> str:
    base = storage_key.rsplit(".", 1)[0] if "." in storage_key else storage_key
    return f"{base}_thumb.jpg"


def _ext(storage_key: str) -> str:
    return f".{storage_key.rsplit('.', 1)[1]}" if "." in storage_key else ""


# ── Téléchargement d'un média distant (import de campagne S6) ─────────────────
# Le média a été créé `pending` par l'endpoint d'import avec l'URL source dans
# metadata.download_url ; ici on télécharge (gardes anti-SSRF re-validées côté
# worker — défense en profondeur), on pousse dans MinIO puis on chaîne le
# pipeline transcode NORMAL. Échec → transcode_status='error' + download_error.

_DOWNLOAD_MAX_BYTES = 512 * 1024 * 1024   # 512 MiB par créatif
_DOWNLOAD_CT_PREFIXES = ("video/", "image/", "application/octet-stream", "binary/")


@celery_app.task(name="app.workers.media_worker.download_remote_media", bind=True, max_retries=2)
def download_remote_media(self, id_media: int) -> None:
    import hashlib

    import httpx

    from app.core.ssrf import url_is_safe

    conn = _db_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT storage_key, metadata FROM cms_media "
            "WHERE id_media=%s AND is_deleted=false AND transcode_status='pending'",
            (id_media,),
        )
        row = cur.fetchone()
        if not row:
            return
        storage_key, meta = row
        url = (meta or {}).get("download_url")
        if not url:
            return

        def _fail(reason: str) -> None:
            cur.execute(
                "UPDATE cms_media SET transcode_status='error', "
                "metadata = metadata || %s, updated_at=now() WHERE id_media=%s",
                (Json({"download_error": reason[:500]}), id_media),
            )
            conn.commit()

        if not url_is_safe(url, schemes=("http", "https")):
            _fail("URL not allowed (scheme/host)")
            return

        try:
            digest = hashlib.sha256()
            buf = io.BytesIO()
            # follow_redirects=False : un 3xx vers une IP interne ne serait pas re-validé
            with httpx.Client(timeout=httpx.Timeout(20.0, read=120.0),
                              follow_redirects=False) as http:
                with http.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        _fail(f"HTTP {resp.status_code}")
                        return
                    ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if ct and not any(ct.startswith(p) for p in _DOWNLOAD_CT_PREFIXES):
                        _fail(f"Content-type {ct} rejected")
                        return
                    total = 0
                    for chunk in resp.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > _DOWNLOAD_MAX_BYTES:
                            _fail(f"File > {_DOWNLOAD_MAX_BYTES // (1024 * 1024)} MiB")
                            return
                        digest.update(chunk)
                        buf.write(chunk)
        except httpx.HTTPError as exc:
            # Erreur réseau transitoire → retry (2×) puis échec définitif
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc, countdown=60)
            _fail(f"Download failed: {exc}")
            return

        content = buf.getvalue()
        if not content:
            _fail("Empty file")
            return
        s3 = _s3()
        s3.put_object(Bucket=settings.MINIO_BUCKET_MEDIA, Key=storage_key, Body=content,
                      ContentType=ct or "application/octet-stream")
        cur.execute(
            "UPDATE cms_media SET file_size_bytes=%s, checksum_sha256=%s, mime_type=%s, "
            "updated_at=now() WHERE id_media=%s",
            (len(content), digest.hexdigest(), ct or None, id_media),
        )
        conn.commit()
        transcode_media.delay(id_media)
    finally:
        cur.close()
        conn.close()
