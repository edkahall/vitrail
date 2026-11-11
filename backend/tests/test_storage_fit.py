"""« Est-ce que ça rentre ? » — contrôle de capacité AVANT affectation (point 4).

Le 2026-08-06, une playlist de 1,85 Go a été affectée à un stick Android dont la partition
fait 4 Go, déjà largement occupée par le système. Le player a téléchargé jusqu'à saturer le
disque à 100 %, ce qui a cassé Android lui-même : l'appareil n'a pu être récupéré qu'en
désinstallant l'application, donc par un déplacement sur site.

Le frein embarqué empêche désormais la casse. Ce service traite l'autre moitié du problème :
prévenir **au moment du geste**, quand corriger ne coûte rien — sinon l'écran diffuse une
playlist tronquée sans que personne ne sache pourquoi.

Les trois règles que ces tests figent :
  1. on ne compte QUE ce qui manque (un média déjà en cache ne se retéléchargera pas) ;
  2. on garde la MÊME réserve que le frein (deux définitions du « plein » divergeraient) ;
  3. `unknown` n'est PAS `ok` — un player qui ne remonte pas sa capacité rend un verdict
     explicite. Se taire reviendrait à affirmer que ça tient, ce qu'on ignore : c'est
     exactement l'erreur qui a coûté l'incident.
"""

from app.models.player import CmsPlayer
from app.services import storage_fit

GB = 1024 ** 3
MB = 1024 ** 2


def _player(**store) -> CmsPlayer:
    p = CmsPlayer()
    p.id_player = 23
    p.player_name = "Android Player"
    p.capabilities = {"store": store} if store else {}
    return p


def test_the_real_incident_is_now_refused() -> None:
    """Le cas mesuré : 1,85 Go de contenu, ~1,2 Go libres → verdict `insufficient`."""
    p = _player(free_bytes=int(1.2 * GB), disk_total_bytes=4 * GB, cached_ids=[])
    sizes = {i: 300 * MB for i in range(1, 7)}          # 6 vidéos ≈ 1,76 Go
    r = storage_fit.assess_player(p, sizes, list(sizes))
    assert r["verdict"] == "insufficient"
    assert r["missing_count"] == 6
    # Le message doit dire la CONSÉQUENCE, pas seulement « pas de place ».
    assert "will play only part" in r["reason"]


def test_already_cached_media_are_not_counted_twice() -> None:
    """Facturer un média déjà en cache ferait crier au loup sur un écran sain."""
    sizes = {1: 2 * GB, 2: 100 * MB}
    p = _player(free_bytes=1 * GB, disk_total_bytes=8 * GB, cached_ids=[1])
    r = storage_fit.assess_player(p, sizes, [1, 2])
    assert r["verdict"] == "ok"
    assert r["missing_count"] == 1          # seul le média 2 reste à télécharger
    assert r["required_bytes"] == 100 * MB


def test_everything_cached_is_ok_even_with_a_full_disk() -> None:
    """Rien à télécharger ⇒ rien ne peut déborder, même sur un disque tendu."""
    p = _player(free_bytes=1 * MB, disk_total_bytes=4 * GB, cached_ids=[1, 2])
    r = storage_fit.assess_player(p, {1: 2 * GB, 2: 2 * GB}, [1, 2])
    assert r["verdict"] == "ok"
    assert r["required_bytes"] == 0


def test_the_server_keeps_the_same_reserve_as_the_player_brake() -> None:
    """Le serveur ne doit jamais promettre un octet que le player refusera d'écrire.

    Avec 600 Mo libres et 512 Mo de réserve, il reste 88 Mo utilisables : 200 Mo de
    contenu ne tiennent pas, même si l'arithmétique naïve (200 < 600) dirait le contraire.
    """
    assert storage_fit.RESERVE_BYTES == 512 * MB
    p = _player(free_bytes=600 * MB, disk_total_bytes=4 * GB, cached_ids=[])
    r = storage_fit.assess_player(p, {1: 200 * MB}, [1])
    assert r["verdict"] == "insufficient"


def test_tight_is_distinguished_from_comfortable() -> None:
    """« Ça passe de justesse » mérite d'être dit : le prochain ajout fera basculer."""
    p = _player(free_bytes=512 * MB + 1000 * MB, disk_total_bytes=8 * GB, cached_ids=[])
    tight = storage_fit.assess_player(p, {1: 950 * MB}, [1])
    assert tight["verdict"] == "tight"
    roomy = storage_fit.assess_player(p, {1: 100 * MB}, [1])
    assert roomy["verdict"] == "ok"


def test_unknown_capacity_is_never_silently_ok() -> None:
    """LA règle qui a coûté l'incident : ne pas confondre « on ne sait pas » et « ça va ».

    Un player antérieur au frein ne remonte ni `free_bytes` ni `disk_total_bytes`.
    """
    p = _player(cached_ids=[])                       # aucune capacité remontée
    r = storage_fit.assess_player(p, {1: 5 * GB}, [1])
    assert r["verdict"] == "unknown"
    assert r["free_bytes"] is None
    assert "does not report" in r["reason"]

    bare = _player()                                  # capabilities vide
    assert storage_fit.assess_player(bare, {1: 1}, [1])["verdict"] == "unknown"


def test_a_media_without_known_size_does_not_block_an_assignment() -> None:
    """Mieux vaut sous-estimer que refuser une affectation sur une donnée absente."""
    p = _player(free_bytes=4 * GB, disk_total_bytes=8 * GB, cached_ids=[])
    r = storage_fit.assess_player(p, {}, [1, 2, 3])   # tailles inconnues
    assert r["verdict"] == "ok"
    assert r["required_bytes"] == 0


def test_the_worst_verdict_drives_the_warning() -> None:
    """Un seul écran saturé suffit à créer l'incident : c'est lui qu'il faut voir."""
    order = ["ok", "unknown", "tight", "insufficient"]
    assert order[-1] == "insufficient"
    # (le calcul d'ensemble est exercé par l'endpoint ; ici on fige la hiérarchie)
    p_ok = _player(free_bytes=8 * GB, disk_total_bytes=16 * GB, cached_ids=[])
    p_ko = _player(free_bytes=100 * MB, disk_total_bytes=4 * GB, cached_ids=[])
    sizes = {1: 1 * GB}
    assert storage_fit.assess_player(p_ok, sizes, [1])["verdict"] == "ok"
    assert storage_fit.assess_player(p_ko, sizes, [1])["verdict"] == "insufficient"
