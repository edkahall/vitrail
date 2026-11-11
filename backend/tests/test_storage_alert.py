"""Alerte de saturation du stockage d'un player (défaut mesuré le 2026-08-06).

Contexte, en réel : un stick Android de 4 Go s'est rempli à **100 %** en téléchargeant une
playlist de 1,85 Go. Le serveur a bien reçu `disk_usage = 100`… et n'a rien dit :
`alert_reasons` est resté **vide**, aucune notification n'est partie. L'exploitant ne l'a
découvert qu'en constatant qu'un écran ne téléchargeait plus, et l'appareil n'a pu être
récupéré qu'en désinstallant l'application — donc par un déplacement sur site.

Deux causes distinctes, toutes deux fermées ici :

1. `disk_high` (85 %) existait, mais il n'a **jamais notifié personne** : seul
   `offline_critical` déclenchait un envoi §XXII. C'était un badge, pas une alerte.
2. les motifs de ressources n'étaient calculés que pour un player **`online`/`warning`** —
   or un disque plein fait précisément **disparaître** le player. Au moment exact où il
   faut prévenir, plus aucun motif n'était évalué.

Ce que ces tests figent : le type d'événement est connu du dispatcher, le message dit à
l'exploitant **ce qu'il doit faire**, et le seuil de saturation est distinct du simple
avertissement.
"""

from app.services import notifications
from app.workers import event_worker


def test_storage_full_is_a_known_event() -> None:
    """Sans cette entrée, `dispatch_sync` ne trouverait aucune règle à servir : l'alerte
    serait écrite dans le code et muette en service."""
    assert "player_storage_full" in notifications.EVENT_TYPES


def test_message_says_what_to_do_and_reassures_on_playback() -> None:
    """Un « disque plein » sans conduite à tenir laisse l'exploitant chercher.

    Le message doit nommer l'écran, chiffrer l'occupation, dire que la diffusion CONTINUE
    (sinon on croit l'écran noir et on se déplace pour rien) et donner le geste correctif.
    """
    subject, body = notifications.build_message("player_storage_full", {
        "player_name": "Android Player", "id_player": 23,
        "disk_usage": 100, "blocked_count": 3,
    }, locale="fr")
    assert "Android Player" in subject
    assert "23" in body and "100" in body
    assert "diffuser" in body.lower()          # la diffusion continue
    assert "3 contenu" in body                 # ce qui n'a pas pu être mis en cache
    # Le geste correctif, sans lequel l'alerte n'est qu'un constat.
    assert "playlist" in body.lower() or "contenu" in body.lower()


def test_message_holds_without_the_optional_count() -> None:
    """Le nombre de contenus renoncés vient du player ; une version antérieure ne l'envoie
    pas. L'alerte doit rester lisible plutôt que d'afficher « None contenu(s) »."""
    _, body = notifications.build_message("player_storage_full", {
        "player_name": "NUC", "id_player": 22, "disk_usage": 97,
    }, locale="fr")
    assert "None" not in body
    assert "NUC" in body


def test_saturation_threshold_is_distinct_from_the_warning_one() -> None:
    """`disk_warn_pct` avertit, `disk_critical_pct` notifie : deux niveaux, deux effets.

    Les confondre ferait partir une notification dès 85 % — c'est-à-dire souvent, et pour
    rien — et le canal finirait ignoré le jour où il compte.
    """
    d = event_worker._DEFAULT_ALERT_SETTINGS
    assert "disk_critical_pct" in d
    assert d["disk_critical_pct"] > d["disk_warn_pct"]


def test_saturation_is_evaluated_outside_the_online_branch() -> None:
    """C'est LA correction de fond : le motif doit être calculé même hors ligne.

    Vérifié sur la source, faute de pouvoir instancier le beat psycopg ici. Le motif
    `storage_full` doit être ajouté en dehors du bloc `if cur_status in ("online",
    "warning")`, sinon on retombe exactement sur le cas mesuré : un player saturé passe
    hors ligne et n'émet plus aucune alerte.
    """
    import inspect

    src = inspect.getsource(event_worker)
    i_online = src.index('if cur_status in ("online", "warning"):')
    i_storage = src.index('reasons.append("storage_full")')
    i_transition = src.index("# Alerte à la TRANSITION offline→critical")
    # Le motif de saturation vient APRÈS la branche online (donc hors d'elle) et avant la
    # section des transitions.
    assert i_online < i_storage < i_transition
    # Et il n'est pas conditionné au statut.
    segment = src[i_storage - 400:i_storage]
    assert 'cur_status in ("online"' not in segment


def test_message_holds_without_any_percentage() -> None:
    """Les surfaces NAVIGATEUR ne remontent aucun pourcentage de disque.

    Mesuré le 2026-08-06 sur le parc : `disk_usage` est NULL sur les 2 BrightSign, le
    ChromeOS et l'extension — soit la moitié des écrans. Le message ne doit donc jamais
    afficher « None % », et l'alerte ne doit pas dépendre de ce chiffre.
    """
    _, body = notifications.build_message("player_storage_full", {
        "player_name": "BrightSign HD1024", "id_player": 13, "disk_usage": None,
    }, locale="fr")
    assert "None" not in body and "%" not in body
    assert "plus de place" in body


def test_saturation_also_triggers_on_the_pressure_declared_by_the_player() -> None:
    """LA correction : une alerte branchée sur le seul pourcentage serait aveugle sur la
    moitié du parc — exactement le défaut (`storage_pressure: false` en dur) qu'on venait
    de corriger côté player. Le second signal est la pression que le player DÉCLARE.
    """
    import inspect

    src = inspect.getsource(event_worker)
    i = src.index('reasons.append("storage_full")')
    segment = src[i - 900:i]
    assert "storage_pressure" in segment       # signal des surfaces navigateur
    assert "blocked_ids" in segment            # médias réellement renoncés
    assert "disk_usage" in segment             # signal des surfaces natives
    # Le beat doit RÉELLEMENT lire capabilities, sinon les deux signaux sont morts-nés.
    assert "id_site, display_config, capabilities" in src
