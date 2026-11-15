"""Mode démo (§II) — le garde middleware bloque TOUTE écriture d'un compte `is_demo`.

Tests unitaires de la liste blanche `_demo_allows` (le middleware n'est qu'un décodage de
JWT + cet arbitre) : les actions restent bloquées — y compris les NOUVEAUX endpoints
d'action (switch HDMI VOD, création de média pointeur, commandes player) — et seules les
écritures « sûres » passent (auth, profil self-service, feedback, checks read-only).
"""

from app.core.demo_guard import _demo_allows


def test_demo_allows_auth_and_profile():
    assert _demo_allows("POST", "/api/v1/auth/login")
    assert _demo_allows("POST", "/api/v1/auth/refresh")
    assert _demo_allows("PATCH", "/api/v1/users/me")
    assert _demo_allows("POST", "/api/v1/feedback")


def test_demo_allows_readonly_hdmi_check():
    """POST /schedules/hdmi-check = check READ-ONLY (warning d'affectation) : autorisé,
    sinon ouvrir le drawer schedule en démo déclenche un toast sur une action passive."""
    assert _demo_allows("POST", "/api/v1/schedules/hdmi-check")


def test_demo_allows_readonly_occupancy_check():
    """POST /campaigns/occupancy-check = projection SOV READ-ONLY (S2) : autorisé — le
    drawer campagne la déclenche automatiquement à l'ouverture en édition (cas hdmi-check)."""
    assert _demo_allows("POST", "/api/v1/campaigns/occupancy-check")


def test_demo_allows_readonly_portal_occupancy_check():
    """POST /portal/occupancy-check = dispo agrégée + devis (C2) : READ-ONLY, déclenché
    automatiquement par le formulaire de campagne du portail (compte annonceur démo)."""
    assert _demo_allows("POST", "/api/v1/portal/occupancy-check")


def test_demo_allows_readonly_import_dry_run():
    """POST /campaigns/import/dry-run = validation d'un ordre d'insertion SANS création
    (S6) : autorisé — un prospect démo peut tester le format ; l'import réel est bloqué."""
    assert _demo_allows("POST", "/api/v1/campaigns/import/dry-run")


def test_demo_blocks_actions():
    """Les actions (dont les features récentes) restent BLOQUÉES pour un compte démo."""
    blocked = [
        ("POST", "/api/v1/vod/1/play"),               # switch HDMI / lecture VOD (players réels !)
        ("POST", "/api/v1/vod"),                      # création config VOD
        ("POST", "/api/v1/media/hdmi-input"),         # création média pointeur
        ("POST", "/api/v1/players/1/command"),        # commandes player (hdmi_input, reboot…)
        ("POST", "/api/v1/players/1/display"),        # pilotage écran
        ("POST", "/api/v1/schedules"),                # création de programme
        ("PUT", "/api/v1/schedules/1/targets"),       # ré-affectation
        ("DELETE", "/api/v1/players/1"),              # suppression player (+ delete_pop)
        ("DELETE", "/api/v1/player-groups/1"),        # suppression groupe
        ("POST", "/api/v1/updates/deployments"),      # déploiement OTA
        ("POST", "/api/v1/updates/versions/1/promote"),  # promotion beta→stable
        ("POST", "/api/v1/assistant/action/execute"),    # actions de l'assistant IA
        ("PATCH", "/api/v1/users/1"),                 # PATCH users ≠ /users/me
        ("POST", "/api/v1/schedules/hdmi-check/x"),   # pas de préfixe laxiste (match exact)
        # Retail Media §2.8 (S1→S4, 2026-07-09) — création/mutation/partage = bloqués
        ("POST", "/api/v1/advertisers/"),             # création annonceur
        ("POST", "/api/v1/campaigns/"),               # création campagne
        ("POST", "/api/v1/campaigns/1/status"),       # publish/pause/resume/cancel
        ("POST", "/api/v1/campaigns/1/creatives"),    # ajout créatif
        ("DELETE", "/api/v1/campaigns/1"),            # suppression campagne
        ("POST", "/api/v1/campaigns/1/share"),        # lien annonceur (org-admin réel only)
        ("POST", "/api/v1/campaigns/occupancy-check/x"),  # pas de préfixe laxiste
        # Retail Media S5 (2026-07-10) — approbation des créatifs
        ("PUT", "/api/v1/campaigns/settings"),        # flag org approbation
        ("POST", "/api/v1/campaigns/creatives/1/approve"),
        ("POST", "/api/v1/campaigns/creatives/1/reject"),
        # Jeanine (migr 0073/UI 09/07) — un compte démo ne peut PAS lier Telegram :
        # sans code de liaison (POST bloqué), le webhook ne lui donne aucune identité.
        ("POST", "/api/v1/messaging/link-code"),
        ("DELETE", "/api/v1/messaging/identities/1"),
        # PIN actions sensibles (migr 0090)
        ("POST", "/api/v1/messaging/pin"),
        ("DELETE", "/api/v1/messaging/pin"),
        ("PUT", "/api/v1/players/1"),                 # campaigns_optout & co
        # S6 industrialisation (2026-07-10) — groupes de sites + import JSON
        ("POST", "/api/v1/site-groups/"),             # création groupe de sites
        ("PUT", "/api/v1/site-groups/1"),
        ("DELETE", "/api/v1/site-groups/1"),          # + purge des cibles orphelines
        ("POST", "/api/v1/site-groups/1/sites"),      # membership
        ("DELETE", "/api/v1/site-groups/1/sites/2"),
        ("POST", "/api/v1/campaigns/import"),         # import RÉEL (le dry-run est whitelist)
        ("POST", "/api/v1/campaigns/import/dry-run/x"),  # pas de préfixe laxiste
        # S7 portail annonceur (2026-07-10) — gestion des accès externes + branding
        ("POST", "/api/v1/advertisers/1/users"),      # invitation compte portail
        ("DELETE", "/api/v1/advertisers/1/users/9"),  # révocation
        ("PUT", "/api/v1/analytics/branding"),        # branding white-label §24.3
        # S7 C2 portail self-service — création/upload/soumission = bloqués en démo
        ("POST", "/api/v1/portal/campaigns"),
        ("PUT", "/api/v1/portal/campaigns/1"),
        ("POST", "/api/v1/portal/campaigns/1/creatives"),
        ("DELETE", "/api/v1/portal/campaigns/1/creatives/2"),
        ("POST", "/api/v1/portal/campaigns/1/submit"),
        ("POST", "/api/v1/portal/occupancy-check/x"),  # pas de préfixe laxiste
        # Listes de sites annonceur (retour Ed #11, migr 0085)
        ("POST", "/api/v1/portal/site-lists"),
        ("DELETE", "/api/v1/portal/site-lists/1"),
        # Cloche décisions (Ed #8) — marquage « vu » bloqué en démo (client n'appelle pas)
        ("POST", "/api/v1/portal/decisions/seen"),
        # S7 C1 Approval Workflow §XXI (migr 0081) — tout le cycle = mutations bloquées
        ("POST", "/api/v1/approvals/"),               # soumission
        ("POST", "/api/v1/approvals/1/approve"),
        ("POST", "/api/v1/approvals/1/reject"),
        ("POST", "/api/v1/approvals/1/request-changes"),
        ("POST", "/api/v1/approvals/1/resubmit"),
        ("POST", "/api/v1/approvals/1/comments"),
        # Exclusion concurrentielle #10 (migr 0087) — catégories d'industrie
        ("POST", "/api/v1/campaigns/industry-categories"),
        ("DELETE", "/api/v1/campaigns/industry-categories/1"),
        ("PUT", "/api/v1/sites/1"),                   # exclusions par site & co
        # Ménage de la file d'approbation (retour Ed 12/07)
        ("DELETE", "/api/v1/approvals/1"),
        # Clés d'import de campagne sfi_ (v2 API, 12/07)
        ("POST", "/api/v1/campaigns/import-keys"),
        ("DELETE", "/api/v1/campaigns/import-keys/1"),
    ]
    for method, path in blocked:
        assert not _demo_allows(method, path), f"{method} {path} ne doit PAS être autorisé en démo"


def test_demo_allows_readonly_governance_scope():
    """POST /schedules/governance-scope = aperçu du périmètre résolu : READ-ONLY et affiché
    EN PERMANENCE dans le tiroir de programme (queryFn armé dès qu'un nœud est choisi).
    Bloqué depuis sa création le 27/07 ⇒ toast « compte démo » à l'ouverture du tiroir."""
    assert _demo_allows("POST", "/api/v1/schedules/governance-scope")


def test_demo_allows_readonly_storage_check():
    """POST /players/storage-check = contrôle d'espace disque à l'affectation : READ-ONLY,
    armé automatiquement dès que le tiroir a une playlist ET une cible (06/08). Même cas
    que hdmi-check, dans le MÊME tiroir."""
    assert _demo_allows("POST", "/api/v1/players/storage-check")


# Mots qui, dans un chemin, annoncent une lecture déguisée en POST (corps trop gros ou trop
# structuré pour une query string). C'est là que se cachent les faux « action impossible ».
_READONLY_HINTS = (
    "check", "preview", "estimate", "simulate", "validate", "dry-run",
    "search", "verify", "resolve", "scope", "feasib", "occupancy",
)

# POST au nom évocateur DÉLIBÉRÉMENT laissés bloqués — décision, pas oubli.
# Tous sont derrière un CLIC EXPLICITE (`mutationFn` côté front, vérifié le 2026-08-07) :
# le prospect voit un refus sur un geste qu'il a demandé, pas un refus surgi de nulle part
# en ouvrant un tiroir. Les rouvrir est un choix produit, pas une correction de défaut.
_REVIEWED_BLOCKED = {
    "/api/v1/players/{id_player}/simulate-day",
    "/api/v1/playlists/{id_playlist}/simulate-tags",
    "/api/v1/playlists/{id_playlist}/simulate-timeline",
    "/api/v1/pop/archives/{id_archive}/verify",
    "/api/v1/promo/validate",
    # vitrail#6 : pas une lecture — fait tourner `apt-get update` sur l'appareil (clic explicite).
    "/api/v1/updates/os/check",
}


def test_no_unreviewed_readonly_post():
    """Aucun POST au nom « lecture seule » ne peut apparaître sans qu'on ait tranché.

    La liste blanche du garde est MANUELLE : elle avait pris du retard en silence sur
    `governance-scope` (27/07) et `storage-check` (06/08), tous deux armés automatiquement
    dans le tiroir de programme — un compte démo y recevait « action impossible » sur une
    action qui n'écrit rien. Ce test transforme la liste en DÉCISION FORCÉE : tout nouvel
    endpoint du même genre rend le test rouge tant que personne ne l'a classé.

    Il lit le schéma OpenAPI de l'app, donc les routes RÉELLEMENT servies — pas une liste
    recopiée qui, elle, raterait exactement ce qu'on cherche.
    """
    import re

    from app.main import app

    paths = app.openapi()["paths"]
    posts = [p for p, ops in paths.items() if "post" in ops]
    # Témoin : une sonde qui ne trouverait rien conclurait « tout va bien » sur du vide.
    assert len(posts) > 50, f"sonde suspecte : seulement {len(posts)} routes POST"

    unreviewed = []
    for p in posts:
        if not any(h in p for h in _READONLY_HINTS):
            continue
        if p in _REVIEWED_BLOCKED:
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", p)
        if not _demo_allows("POST", concrete):
            unreviewed.append(p)

    assert not unreviewed, (
        "POST au nom « lecture seule » ni autorisés en démo ni classés comme bloqués "
        "délibérément : " + ", ".join(sorted(unreviewed)) + ". Trancher, puis soit les "
        "ajouter à _demo_allows (read-only ET déclenché automatiquement), soit les inscrire "
        "dans _REVIEWED_BLOCKED (read-only derrière un clic explicite)."
    )
