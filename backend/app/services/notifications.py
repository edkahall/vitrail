"""Dispatch d'alertes (§XXII) — canaux **webhook** (POST JSON) et **EMAIL** (SMTP
`email_delivery`, config = {"recipients": [...]}, ajouté 2026-07-12), journalisés.

Deux entrées selon le contexte : `dispatch_sync` (workers Celery, connexion psycopg2 BYPASSRLS →
filtre `id_org` explicite — ⚠️ SMTP_* doit être déclaré dans l'env du service celery-worker) et
`dispatch_async` (endpoints). Le message est construit à partir de `(event_type, ctx)` par
`build_message` ; la livraison par type de canal est factorisée dans `deliver_to_channel`.
Chaque tentative est tracée dans `cms_notification_log` (status sent/failed).
"""

from __future__ import annotations

import json

import httpx

from app.services.doc_i18n import norm_locale, tr
from app.services.webhook_signature import headers_for

# Textes des alertes, dans les 5 langues du produit. ⚠️ Un canal porte la langue de la
# personne qui l'a créé (`config["locale"]`, posée à la création) : le message est donc
# construit PAR CANAL, jamais une fois pour toute la règle. Canal sans langue → anglais.
DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # Libellés des types d'événements (liste déroulante de l'UI)
        "label_player_offline": "Player offline (critical)",
        "label_player_storage_full": "Player running out of storage",
        "label_deployment_failed": "OTA deployment failed",
        "label_feed_error": "Feed error",
        "label_campaign_started": "Campaign started (retail media)",
        "label_campaign_ended": "Campaign ended (retail media)",
        "label_campaign_underserved": "Campaign underserved (served SOV < sold)",
        "label_approval_requested": "Approval request submitted",
        "label_approval_approved": "Approval request approved",
        "label_approval_rejected": "Approval request rejected",
        "label_approval_changes_requested": "Changes requested (approval)",
        "label_test": "Test",
        # player_offline
        "offline_subject": "⚠️ Player offline: {player_name}",
        "offline_body": "Player “{player_name}” (#{id_player}) has been offline for "
                        "{offline_min} min.",
        # player_storage_full
        "storage_subject": "🗄️ Storage full: {player_name}",
        "storage_blocked": " {blocked} item(s) could not be cached.",
        "storage_used_pct": "is using {pct} % of its storage",
        "storage_no_room": "has no room left for new content",
        "storage_body": "Player “{player_name}” (#{id_player}) {used}.{detail} It keeps "
                        "playing what it already has, but can no longer download. Remove "
                        "some content from it or assign it a lighter playlist.",
        # deployment_failed
        "deploy_subject": "❌ OTA deployment failed",
        "deploy_body": "Deployment #{id_deployment} failed on {failed_count} player(s).",
        # feed_error
        "feed_subject": "⚠️ Feed error: {feed_name}",
        "feed_body": "Feed “{feed_name}” (#{id_feed}) is in error: {error}",
        # campaigns
        "camp_started_subject": "📣 Campaign started: {campaign_name}",
        "camp_started_body": "Campaign “{campaign_name}” from advertiser {advertiser_name} "
                             "is on air (SOV {sov_percent} %, from {starts_on} to {ends_on}).",
        "camp_ended_subject": "🏁 Campaign ended: {campaign_name}",
        "camp_ended_body": "Campaign “{campaign_name}” from advertiser {advertiser_name} has "
                           "ended ({starts_on} → {ends_on}).{extra}",
        "camp_ended_plays": " Summary: {plays} plays ({signed_plays} signed)",
        "camp_ended_sov": ", served SOV {sov} %",
        "camp_under_subject": "⚠️ Campaign underserved: {campaign_name}",
        "camp_under_body": "Campaign “{campaign_name}” from advertiser {advertiser_name} "
                           "only reaches {sov_served_pct} % served SOV for {sov_percent} % "
                           "sold (threshold: {threshold_pct} % of sold), over the period "
                           "from {period_from} to {period_until}.",
        # approvals
        "appr_head_approval_requested": "📝 Approval request",
        "appr_head_approval_approved": "✅ Approval granted",
        "appr_head_approval_rejected": "❌ Approval rejected",
        "appr_head_approval_changes_requested": "✏️ Changes requested",
        "appr_body_approval_requested": "“{resource_name}” ({resource_type} #{resource_id}) "
                                        "was submitted for approval by {actor_name}.",
        "appr_body_approval_approved": "“{resource_name}” ({resource_type} #{resource_id}) "
                                       "was approved by {actor_name}.",
        "appr_body_approval_rejected": "“{resource_name}” ({resource_type} #{resource_id}) "
                                       "was rejected by {actor_name}.",
        "appr_body_approval_changes_requested": "“{resource_name}” ({resource_type} "
                                                "#{resource_id}) needs changes according to "
                                                "{actor_name}.",
        "appr_comment": " Comment: {comment}",
        "appr_due": " Due date: {due_date}.",
        "appr_subject": "{head}: {resource_name}",
        # test
        "test_subject": "🔔 Vitrail alert (test)",
        "test_body": "This is a Vitrail test message.",
        # delivery details (notification log)
        "detail_missing_url": "missing url",
        "detail_missing_recipients": "missing recipients",
        "detail_unsupported_type": "unsupported channel type: {channel_type}",
        "detail_email_sent": "email → {count} recipient(s)",
    },
    "fr": {
        "label_player_offline": "Player hors ligne (critique)",
        "label_player_storage_full": "Player à court de stockage",
        "label_deployment_failed": "Déploiement OTA en échec",
        "label_feed_error": "Feed en erreur",
        "label_campaign_started": "Campagne démarrée (retail media)",
        "label_campaign_ended": "Campagne terminée (retail media)",
        "label_campaign_underserved": "Campagne sous-servie (SOV servi < vendu)",
        "label_approval_requested": "Demande d'approbation soumise",
        "label_approval_approved": "Demande d'approbation approuvée",
        "label_approval_rejected": "Demande d'approbation rejetée",
        "label_approval_changes_requested": "Modifications demandées (approbation)",
        "label_test": "Test",
        "offline_subject": "⚠️ Player hors ligne : {player_name}",
        "offline_body": "Le player « {player_name} » (#{id_player}) est hors ligne depuis "
                        "{offline_min} min.",
        "storage_subject": "🗄️ Stockage saturé : {player_name}",
        "storage_blocked": " {blocked} contenu(s) n'ont pas pu être mis en cache.",
        "storage_used_pct": "occupe {pct} % de son stockage",
        "storage_no_room": "n'a plus de place pour de nouveaux contenus",
        "storage_body": "Le player « {player_name} » (#{id_player}) {used}.{detail} Il "
                        "continue de diffuser ce qu'il a déjà, mais ne peut plus télécharger. "
                        "Retirez-lui du contenu ou affectez-lui une playlist plus légère.",
        "deploy_subject": "❌ Déploiement OTA en échec",
        "deploy_body": "Le déploiement #{id_deployment} a échoué sur {failed_count} player(s).",
        "feed_subject": "⚠️ Feed en erreur : {feed_name}",
        "feed_body": "Le feed « {feed_name} » (#{id_feed}) est en erreur : {error}",
        "camp_started_subject": "📣 Campagne démarrée : {campaign_name}",
        "camp_started_body": "La campagne « {campaign_name} » de l'annonceur "
                             "{advertiser_name} est en diffusion (SOV {sov_percent} %, du "
                             "{starts_on} au {ends_on}).",
        "camp_ended_subject": "🏁 Campagne terminée : {campaign_name}",
        "camp_ended_body": "La campagne « {campaign_name} » de l'annonceur {advertiser_name} "
                           "est terminée ({starts_on} → {ends_on}).{extra}",
        "camp_ended_plays": " Bilan : {plays} passages (dont {signed_plays} signés)",
        "camp_ended_sov": ", SOV servi {sov} %",
        "camp_under_subject": "⚠️ Campagne sous-servie : {campaign_name}",
        "camp_under_body": "La campagne « {campaign_name} » de l'annonceur {advertiser_name} "
                           "n'atteint que {sov_served_pct} % de SOV servi pour {sov_percent} % "
                           "vendus (seuil : {threshold_pct} % du vendu), sur la période du "
                           "{period_from} au {period_until}.",
        "appr_head_approval_requested": "📝 Demande d'approbation",
        "appr_head_approval_approved": "✅ Approbation accordée",
        "appr_head_approval_rejected": "❌ Approbation rejetée",
        "appr_head_approval_changes_requested": "✏️ Modifications demandées",
        "appr_body_approval_requested": "« {resource_name} » ({resource_type} #{resource_id}) "
                                        "a été soumis pour approbation par {actor_name}.",
        "appr_body_approval_approved": "« {resource_name} » ({resource_type} #{resource_id}) "
                                       "a été approuvé par {actor_name}.",
        "appr_body_approval_rejected": "« {resource_name} » ({resource_type} #{resource_id}) "
                                       "a été rejeté par {actor_name}.",
        "appr_body_approval_changes_requested": "« {resource_name} » ({resource_type} "
                                                "#{resource_id}) nécessite des modifications "
                                                "selon {actor_name}.",
        "appr_comment": " Commentaire : {comment}",
        "appr_due": " Échéance : {due_date}.",
        "appr_subject": "{head} : {resource_name}",
        "test_subject": "🔔 Alerte Vitrail (test)",
        "test_body": "Ceci est un message de test Vitrail.",
        "detail_missing_url": "url manquante",
        "detail_missing_recipients": "destinataires manquants",
        "detail_unsupported_type": "type de canal non supporté : {channel_type}",
        "detail_email_sent": "email → {count} destinataire(s)",
    },
    "es": {
        "label_player_offline": "Player sin conexión (crítico)",
        "label_player_storage_full": "Player con poco almacenamiento",
        "label_deployment_failed": "Despliegue OTA fallido",
        "label_feed_error": "Feed con error",
        "label_campaign_started": "Campaña iniciada (retail media)",
        "label_campaign_ended": "Campaña finalizada (retail media)",
        "label_campaign_underserved": "Campaña infraservida (SOV servido < vendido)",
        "label_approval_requested": "Solicitud de aprobación enviada",
        "label_approval_approved": "Solicitud de aprobación aprobada",
        "label_approval_rejected": "Solicitud de aprobación rechazada",
        "label_approval_changes_requested": "Cambios solicitados (aprobación)",
        "label_test": "Prueba",
        "offline_subject": "⚠️ Player sin conexión: {player_name}",
        "offline_body": "El player «{player_name}» (#{id_player}) lleva {offline_min} min "
                        "sin conexión.",
        "storage_subject": "🗄️ Almacenamiento lleno: {player_name}",
        "storage_blocked": " {blocked} contenido(s) no se han podido almacenar en caché.",
        "storage_used_pct": "ocupa el {pct} % de su almacenamiento",
        "storage_no_room": "ya no tiene espacio para nuevos contenidos",
        "storage_body": "El player «{player_name}» (#{id_player}) {used}.{detail} Sigue "
                        "reproduciendo lo que ya tiene, pero ya no puede descargar. Quítele "
                        "contenido o asígnele una playlist más ligera.",
        "deploy_subject": "❌ Despliegue OTA fallido",
        "deploy_body": "El despliegue #{id_deployment} ha fallado en {failed_count} "
                       "player(s).",
        "feed_subject": "⚠️ Feed con error: {feed_name}",
        "feed_body": "El feed «{feed_name}» (#{id_feed}) tiene un error: {error}",
        "camp_started_subject": "📣 Campaña iniciada: {campaign_name}",
        "camp_started_body": "La campaña «{campaign_name}» del anunciante {advertiser_name} "
                             "está en emisión (SOV {sov_percent} %, del {starts_on} al "
                             "{ends_on}).",
        "camp_ended_subject": "🏁 Campaña finalizada: {campaign_name}",
        "camp_ended_body": "La campaña «{campaign_name}» del anunciante {advertiser_name} ha "
                           "finalizado ({starts_on} → {ends_on}).{extra}",
        "camp_ended_plays": " Balance: {plays} pases ({signed_plays} firmados)",
        "camp_ended_sov": ", SOV servido {sov} %",
        "camp_under_subject": "⚠️ Campaña infraservida: {campaign_name}",
        "camp_under_body": "La campaña «{campaign_name}» del anunciante {advertiser_name} "
                           "solo alcanza un {sov_served_pct} % de SOV servido para un "
                           "{sov_percent} % vendido (umbral: {threshold_pct} % de lo vendido), "
                           "en el periodo del {period_from} al {period_until}.",
        "appr_head_approval_requested": "📝 Solicitud de aprobación",
        "appr_head_approval_approved": "✅ Aprobación concedida",
        "appr_head_approval_rejected": "❌ Aprobación rechazada",
        "appr_head_approval_changes_requested": "✏️ Cambios solicitados",
        "appr_body_approval_requested": "«{resource_name}» ({resource_type} #{resource_id}) "
                                        "ha sido enviado para aprobación por {actor_name}.",
        "appr_body_approval_approved": "«{resource_name}» ({resource_type} #{resource_id}) "
                                       "ha sido aprobado por {actor_name}.",
        "appr_body_approval_rejected": "«{resource_name}» ({resource_type} #{resource_id}) "
                                       "ha sido rechazado por {actor_name}.",
        "appr_body_approval_changes_requested": "«{resource_name}» ({resource_type} "
                                                "#{resource_id}) necesita cambios según "
                                                "{actor_name}.",
        "appr_comment": " Comentario: {comment}",
        "appr_due": " Fecha límite: {due_date}.",
        "appr_subject": "{head}: {resource_name}",
        "test_subject": "🔔 Alerta Vitrail (prueba)",
        "test_body": "Este es un mensaje de prueba de Vitrail.",
        "detail_missing_url": "falta la url",
        "detail_missing_recipients": "faltan destinatarios",
        "detail_unsupported_type": "tipo de canal no admitido: {channel_type}",
        "detail_email_sent": "email → {count} destinatario(s)",
    },
    "pt": {
        "label_player_offline": "Player offline (crítico)",
        "label_player_storage_full": "Player com pouco armazenamento",
        "label_deployment_failed": "Implementação OTA falhada",
        "label_feed_error": "Feed com erro",
        "label_campaign_started": "Campanha iniciada (retail media)",
        "label_campaign_ended": "Campanha terminada (retail media)",
        "label_campaign_underserved": "Campanha subservida (SOV servido < vendido)",
        "label_approval_requested": "Pedido de aprovação submetido",
        "label_approval_approved": "Pedido de aprovação aprovado",
        "label_approval_rejected": "Pedido de aprovação rejeitado",
        "label_approval_changes_requested": "Alterações pedidas (aprovação)",
        "label_test": "Teste",
        "offline_subject": "⚠️ Player offline: {player_name}",
        "offline_body": "O player «{player_name}» (#{id_player}) está offline há "
                        "{offline_min} min.",
        "storage_subject": "🗄️ Armazenamento cheio: {player_name}",
        "storage_blocked": " {blocked} conteúdo(s) não puderam ser guardados em cache.",
        "storage_used_pct": "ocupa {pct} % do seu armazenamento",
        "storage_no_room": "já não tem espaço para novos conteúdos",
        "storage_body": "O player «{player_name}» (#{id_player}) {used}.{detail} Continua a "
                        "reproduzir o que já tem, mas já não consegue descarregar. Retire-lhe "
                        "conteúdo ou atribua-lhe uma playlist mais leve.",
        "deploy_subject": "❌ Implementação OTA falhada",
        "deploy_body": "A implementação #{id_deployment} falhou em {failed_count} player(s).",
        "feed_subject": "⚠️ Feed com erro: {feed_name}",
        "feed_body": "O feed «{feed_name}» (#{id_feed}) está com erro: {error}",
        "camp_started_subject": "📣 Campanha iniciada: {campaign_name}",
        "camp_started_body": "A campanha «{campaign_name}» do anunciante {advertiser_name} "
                             "está em emissão (SOV {sov_percent} %, de {starts_on} a "
                             "{ends_on}).",
        "camp_ended_subject": "🏁 Campanha terminada: {campaign_name}",
        "camp_ended_body": "A campanha «{campaign_name}» do anunciante {advertiser_name} "
                           "terminou ({starts_on} → {ends_on}).{extra}",
        "camp_ended_plays": " Balanço: {plays} passagens ({signed_plays} assinadas)",
        "camp_ended_sov": ", SOV servido {sov} %",
        "camp_under_subject": "⚠️ Campanha subservida: {campaign_name}",
        "camp_under_body": "A campanha «{campaign_name}» do anunciante {advertiser_name} só "
                           "atinge {sov_served_pct} % de SOV servido para {sov_percent} % "
                           "vendidos (limiar: {threshold_pct} % do vendido), no período de "
                           "{period_from} a {period_until}.",
        "appr_head_approval_requested": "📝 Pedido de aprovação",
        "appr_head_approval_approved": "✅ Aprovação concedida",
        "appr_head_approval_rejected": "❌ Aprovação rejeitada",
        "appr_head_approval_changes_requested": "✏️ Alterações pedidas",
        "appr_body_approval_requested": "«{resource_name}» ({resource_type} #{resource_id}) "
                                        "foi submetido para aprovação por {actor_name}.",
        "appr_body_approval_approved": "«{resource_name}» ({resource_type} #{resource_id}) "
                                       "foi aprovado por {actor_name}.",
        "appr_body_approval_rejected": "«{resource_name}» ({resource_type} #{resource_id}) "
                                       "foi rejeitado por {actor_name}.",
        "appr_body_approval_changes_requested": "«{resource_name}» ({resource_type} "
                                                "#{resource_id}) precisa de alterações segundo "
                                                "{actor_name}.",
        "appr_comment": " Comentário: {comment}",
        "appr_due": " Prazo: {due_date}.",
        "appr_subject": "{head}: {resource_name}",
        "test_subject": "🔔 Alerta Vitrail (teste)",
        "test_body": "Esta é uma mensagem de teste do Vitrail.",
        "detail_missing_url": "url em falta",
        "detail_missing_recipients": "destinatários em falta",
        "detail_unsupported_type": "tipo de canal não suportado: {channel_type}",
        "detail_email_sent": "email → {count} destinatário(s)",
    },
    "de": {
        "label_player_offline": "Player offline (kritisch)",
        "label_player_storage_full": "Speicher des Players fast voll",
        "label_deployment_failed": "OTA-Verteilung fehlgeschlagen",
        "label_feed_error": "Feed-Fehler",
        "label_campaign_started": "Kampagne gestartet (Retail Media)",
        "label_campaign_ended": "Kampagne beendet (Retail Media)",
        "label_campaign_underserved": "Kampagne unterversorgt (ausgelieferter SOV < verkauft)",
        "label_approval_requested": "Freigabeanfrage eingereicht",
        "label_approval_approved": "Freigabeanfrage genehmigt",
        "label_approval_rejected": "Freigabeanfrage abgelehnt",
        "label_approval_changes_requested": "Änderungen angefordert (Freigabe)",
        "label_test": "Test",
        "offline_subject": "⚠️ Player offline: {player_name}",
        "offline_body": "Der Player „{player_name}“ (#{id_player}) ist seit {offline_min} min "
                        "offline.",
        "storage_subject": "🗄️ Speicher voll: {player_name}",
        "storage_blocked": " {blocked} Inhalt(e) konnten nicht zwischengespeichert werden.",
        "storage_used_pct": "belegt {pct} % seines Speichers",
        "storage_no_room": "hat keinen Platz mehr für neue Inhalte",
        "storage_body": "Der Player „{player_name}“ (#{id_player}) {used}.{detail} Er spielt "
                        "weiter ab, was er bereits hat, kann aber nichts mehr herunterladen. "
                        "Entfernen Sie Inhalte oder weisen Sie ihm eine leichtere Playlist zu.",
        "deploy_subject": "❌ OTA-Verteilung fehlgeschlagen",
        "deploy_body": "Die Verteilung #{id_deployment} ist auf {failed_count} Player(n) "
                       "fehlgeschlagen.",
        "feed_subject": "⚠️ Feed-Fehler: {feed_name}",
        "feed_body": "Der Feed „{feed_name}“ (#{id_feed}) ist fehlerhaft: {error}",
        "camp_started_subject": "📣 Kampagne gestartet: {campaign_name}",
        "camp_started_body": "Die Kampagne „{campaign_name}“ des Werbekunden "
                             "{advertiser_name} läuft (SOV {sov_percent} %, vom {starts_on} "
                             "bis {ends_on}).",
        "camp_ended_subject": "🏁 Kampagne beendet: {campaign_name}",
        "camp_ended_body": "Die Kampagne „{campaign_name}“ des Werbekunden {advertiser_name} "
                           "ist beendet ({starts_on} → {ends_on}).{extra}",
        "camp_ended_plays": " Bilanz: {plays} Ausspielungen (davon {signed_plays} signiert)",
        "camp_ended_sov": ", ausgelieferter SOV {sov} %",
        "camp_under_subject": "⚠️ Kampagne unterversorgt: {campaign_name}",
        "camp_under_body": "Die Kampagne „{campaign_name}“ des Werbekunden {advertiser_name} "
                           "erreicht nur {sov_served_pct} % ausgelieferten SOV bei "
                           "{sov_percent} % verkauft (Schwelle: {threshold_pct} % des "
                           "Verkauften), im Zeitraum vom {period_from} bis {period_until}.",
        "appr_head_approval_requested": "📝 Freigabeanfrage",
        "appr_head_approval_approved": "✅ Freigabe erteilt",
        "appr_head_approval_rejected": "❌ Freigabe abgelehnt",
        "appr_head_approval_changes_requested": "✏️ Änderungen angefordert",
        "appr_body_approval_requested": "„{resource_name}“ ({resource_type} #{resource_id}) "
                                        "wurde von {actor_name} zur Freigabe eingereicht.",
        "appr_body_approval_approved": "„{resource_name}“ ({resource_type} #{resource_id}) "
                                       "wurde von {actor_name} genehmigt.",
        "appr_body_approval_rejected": "„{resource_name}“ ({resource_type} #{resource_id}) "
                                       "wurde von {actor_name} abgelehnt.",
        "appr_body_approval_changes_requested": "„{resource_name}“ ({resource_type} "
                                                "#{resource_id}) benötigt laut {actor_name} "
                                                "Änderungen.",
        "appr_comment": " Kommentar: {comment}",
        "appr_due": " Fälligkeit: {due_date}.",
        "appr_subject": "{head}: {resource_name}",
        "test_subject": "🔔 Vitrail-Alarm (Test)",
        "test_body": "Dies ist eine Testnachricht von Vitrail.",
        "detail_missing_url": "URL fehlt",
        "detail_missing_recipients": "Empfänger fehlen",
        "detail_unsupported_type": "nicht unterstützter Kanaltyp: {channel_type}",
        "detail_email_sent": "E-Mail → {count} Empfänger",
    },
}

# Types d'événements alertables (whitelist ; l'UI ne propose que ceux-ci). Valeur = libellé
# anglais (défaut produit) ; l'UI reçoit le libellé dans la langue du demandeur via
# `event_type_label`.
_EVENT_KEYS = (
    "player_offline", "player_storage_full", "deployment_failed", "feed_error",
    # Retail media §2.8 (campaign_worker) — cycle de vie + sous-diffusion des campagnes.
    "campaign_started", "campaign_ended", "campaign_underserved",
    # Approval Workflow §XXI (endpoints /approvals) — cycle de validation.
    "approval_requested", "approval_approved", "approval_rejected",
    "approval_changes_requested",
    "test",
)
EVENT_TYPES: dict[str, str] = {k: DOC_TEXTS["en"][f"label_{k}"] for k in _EVENT_KEYS}

_APPROVAL_EVENTS = ("approval_requested", "approval_approved", "approval_rejected",
                    "approval_changes_requested")


def event_type_label(event_type: str, locale: str | None = None) -> str:
    """Libellé d'un type d'événement dans `locale` (repli anglais)."""
    return tr(DOC_TEXTS, locale, f"label_{event_type}")


def channel_locale(config: dict | None) -> str:
    """Langue d'un canal = celle de son créateur (`config["locale"]`), repli anglais."""
    return norm_locale((config or {}).get("locale"))


def build_message(event_type: str, ctx: dict, locale: str | None = None) -> tuple[str, str]:
    """(pur) → (subject, body) lisible pour l'alerte, dans `locale` (défaut anglais)."""
    ctx = ctx or {}

    def g(key: str, default="?"):
        return ctx.get(key, default)

    def t(key: str, **kw) -> str:
        return tr(DOC_TEXTS, locale, key, **kw)

    if event_type == "player_offline":
        return (
            t("offline_subject", player_name=g("player_name")),
            t("offline_body", player_name=g("player_name"), id_player=g("id_player"),
              offline_min=g("offline_min")),
        )
    if event_type == "player_storage_full":
        # Le message doit dire ce qu'il FAUT FAIRE : un « disque plein » sans conduite à tenir
        # laisse l'exploitant chercher. Le contenu déjà en cache continue de diffuser — c'est
        # la playlist qui ne pourra pas être complétée.
        blocked = ctx.get("blocked_count")
        detail = t("storage_blocked", blocked=blocked) if blocked else ""
        # Les surfaces navigateur (ChromeOS, extension, BrightSign) ne remontent AUCUN
        # pourcentage de disque : on ne fabrique pas « None % », on décrit la situation.
        pct = ctx.get("disk_usage")
        used = t("storage_used_pct", pct=pct) if pct is not None else t("storage_no_room")
        return (
            t("storage_subject", player_name=g("player_name")),
            t("storage_body", player_name=g("player_name"), id_player=g("id_player"),
              used=used, detail=detail),
        )
    if event_type == "deployment_failed":
        return (
            t("deploy_subject"),
            t("deploy_body", id_deployment=g("id_deployment"), failed_count=g("failed_count")),
        )
    if event_type == "feed_error":
        return (
            t("feed_subject", feed_name=g("feed_name")),
            t("feed_body", feed_name=g("feed_name"), id_feed=g("id_feed"), error=g("error")),
        )
    if event_type == "campaign_started":
        return (
            t("camp_started_subject", campaign_name=g("campaign_name")),
            t("camp_started_body", campaign_name=g("campaign_name"),
              advertiser_name=g("advertiser_name"), sov_percent=g("sov_percent"),
              starts_on=g("starts_on"), ends_on=g("ends_on")),
        )
    if event_type == "campaign_ended":
        extra = ""
        if ctx.get("plays") is not None:
            sov = ctx.get("sov_served_pct")
            extra = (
                t("camp_ended_plays", plays=ctx.get("plays"),
                  signed_plays=ctx.get("signed_plays", 0))
                + (t("camp_ended_sov", sov=sov) if sov is not None else "") + "."
            )
        return (
            t("camp_ended_subject", campaign_name=g("campaign_name")),
            t("camp_ended_body", campaign_name=g("campaign_name"),
              advertiser_name=g("advertiser_name"), starts_on=g("starts_on"),
              ends_on=g("ends_on"), extra=extra),
        )
    if event_type in _APPROVAL_EVENTS:
        head = t(f"appr_head_{event_type}")
        body = t(f"appr_body_{event_type}", resource_name=g("resource_name"),
                 resource_type=g("resource_type"), resource_id=g("resource_id"),
                 actor_name=g("actor_name"))
        if ctx.get("comment"):
            body += t("appr_comment", comment=ctx["comment"])
        if ctx.get("due_date"):
            body += t("appr_due", due_date=ctx["due_date"])
        return (t("appr_subject", head=head, resource_name=g("resource_name")), body)
    if event_type == "campaign_underserved":
        return (
            t("camp_under_subject", campaign_name=g("campaign_name")),
            t("camp_under_body", campaign_name=g("campaign_name"),
              advertiser_name=g("advertiser_name"), sov_served_pct=g("sov_served_pct"),
              sov_percent=g("sov_percent"), threshold_pct=g("threshold_pct"),
              period_from=g("period_from"), period_until=g("period_until")),
        )
    return (t("test_subject"), t("test_body"))


# Codes qui méritent un réessai : le destinataire est en panne, saturé ou lent. ⛔ Un 4xx
# (hors 408/429) ne deviendra jamais un 2xx en le renvoyant — c'est l'URL, l'authentification
# ou le format qui ne va pas. Réessayer ces cas-là, c'est marteler un serveur qui a déjà
# répondu clairement, et masquer une erreur de configuration derrière quatre échecs de plus.
_RETRYABLE_STATUS = {408, 429}


def _is_retryable(status_code: int | None) -> bool:
    if status_code is None:
        return True  # pas de réponse du tout : réseau, DNS, délai dépassé
    return status_code >= 500 or status_code in _RETRYABLE_STATUS


def webhook_post(
    url: str, payload: dict, secret: str = "", event_type: str = "", attempt: int = 1
) -> tuple[bool, str, bool]:
    """POST JSON SIGNÉ, synchrone (worker / threadpool). → (ok, détail, réessayable).

    🔑 Le corps est sérialisé UNE FOIS et les mêmes octets partent à la signature et à
    `httpx` (`content=`, jamais `json=`) : signer une re-sérialisation produirait des
    signatures que le destinataire ne pourrait pas reproduire.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = headers_for(secret, raw, event_type or payload.get("event", ""), attempt)
    try:
        r = httpx.post(url, content=raw, headers=headers, timeout=8.0)
    except Exception as exc:  # noqa: BLE001 — best-effort, l'échec est tracé
        return False, str(exc)[:200], True
    ok = 200 <= r.status_code < 300
    return ok, f"HTTP {r.status_code}", (not ok and _is_retryable(r.status_code))


# Réessai des webhooks — 4 tentatives différées après le premier essai synchrone, soit
# ~15 min de fenêtre (1, 3, 5 puis 6 min). Ni plus long ni plus dense : une alerte de
# signalétique perd sa valeur passé le quart d'heure, et marteler un serveur déjà en
# difficulté ne fait qu'aggraver sa panne.
RETRY_BACKOFF_S = (60, 180, 300, 360)


def _queue_retry(url: str, payload: dict, secret: str, event_type: str,
                 id_org: int | None, id_rule: int | None) -> None:
    """Confie le renvoi à la file `notifications`. Best-effort et silencieux par
    construction : si le courtier est injoignable, on a déjà tracé l'échec du premier
    essai — une alerte ne doit jamais casser l'action qui l'a déclenchée, et un réessai
    qui échoue à s'inscrire ne doit pas devenir une seconde panne.

    ⛔ Sans `id_org` on ne fait rien : la ligne de journal du réessai ne pourrait pas être
    rattachée à une organisation, et une trace hors tenant n'est lisible par personne.
    """
    if id_org is None:
        return
    try:
        from app.workers.celery_app import celery_app

        celery_app.send_task(
            "app.workers.notification_worker.retry_webhook",
            args=[url, payload, secret, event_type, id_org, id_rule, 2],
            countdown=RETRY_BACKOFF_S[0],
        )
    except Exception:  # noqa: BLE001 — cf. docstring
        pass


def email_send(recipients: list[str], subject: str, body: str,
               locale: str | None = None) -> tuple[bool, str]:
    """Envoi SMTP synchrone (worker / threadpool) via email_delivery. → (ok, détail)."""
    from app.services.email_delivery import EmailError, send_email

    try:
        send_email(to=recipients, subject=f"[Vitrail] {subject}", body=body)
        return True, tr(DOC_TEXTS, locale, "detail_email_sent", count=len(recipients))
    except EmailError as exc:
        return False, str(exc)[:200]


def deliver_to_channel(
    channel_type: str, config: dict | None, event_type: str,
    subject: str, body: str, ctx: dict,
    id_org: int | None = None, id_rule: int | None = None,
) -> tuple[bool, str]:
    """Livraison SYNCHRONE selon le type de canal (webhook | email). → (ok, détail).

    Le détail (journal) est rédigé dans la langue du canal, comme le message."""
    config = config or {}
    loc = channel_locale(config)
    if channel_type == "webhook":
        url = config.get("url")
        if not url:
            return False, tr(DOC_TEXTS, loc, "detail_missing_url")
        payload = _payload(event_type, subject, body, ctx)
        ok, detail, retryable = webhook_post(
            url, payload, secret=config.get("secret", ""), event_type=event_type)
        if not ok and retryable:
            # Le destinataire est injoignable ou en panne : on repasse la main à la file
            # `notifications` plutôt que de perdre l'alerte. ⚠️ Le premier essai reste
            # SYNCHRONE, volontairement — c'est lui qui alimente le journal et qui rend
            # le bouton « Tester » utile ; un envoi entièrement différé répondrait
            # « en file d'attente » à quelqu'un qui veut savoir si son URL marche.
            _queue_retry(url, payload, config.get("secret", ""), event_type, id_org, id_rule)
        return ok, detail
    if channel_type == "email":
        recipients = [r.strip() for r in config.get("recipients", []) if r and r.strip()]
        if not recipients:
            return False, tr(DOC_TEXTS, loc, "detail_missing_recipients")
        return email_send(recipients, subject, body, locale=loc)
    return False, tr(DOC_TEXTS, loc, "detail_unsupported_type", channel_type=channel_type)


def _payload(event_type: str, subject: str, body: str, ctx: dict) -> dict:
    return {"event": event_type, "subject": subject, "body": body, "context": ctx or {},
            "source": "signflow"}


async def dispatch_async(db, id_org: int, event_type: str, ctx: dict) -> int:
    """(endpoints FastAPI) Miroir de `dispatch_sync` sur la session SQLAlchemy async (RLS déjà
    posée). Appelé APRÈS le commit de la transition métier ; commite lui-même ses lignes de log.
    Best-effort : l'appelant enveloppe dans try/except — une alerte ne doit jamais casser
    l'action qui la déclenche."""
    from fastapi.concurrency import run_in_threadpool
    from sqlalchemy import select

    from app.models.notification import CmsAlertRule, CmsNotificationChannel, CmsNotificationLog

    rows = (await db.execute(
        select(CmsAlertRule.id_rule, CmsNotificationChannel.channel_type,
               CmsNotificationChannel.config)
        .join(CmsNotificationChannel, CmsNotificationChannel.id_channel == CmsAlertRule.id_channel)
        .where(
            CmsAlertRule.id_org == id_org,
            CmsAlertRule.event_type == event_type,
            CmsAlertRule.enabled == True,  # noqa: E712
            CmsNotificationChannel.enabled == True,  # noqa: E712
        )
    )).all()
    sent = 0
    for id_rule, ctype, config in rows:
        # Message construit PAR CANAL, dans la langue de son créateur ; le journal garde
        # le texte réellement envoyé.
        subject, body = build_message(event_type, ctx, locale=channel_locale(config))
        ok, detail = await run_in_threadpool(
            deliver_to_channel, ctype, config, event_type, subject, body, ctx,
            id_org, id_rule)
        db.add(CmsNotificationLog(
            id_org=id_org, id_rule=id_rule, event_type=event_type,
            subject=subject[:200], body=body,
            status="sent" if ok else "failed", detail=detail,
        ))
        sent += 1 if ok else 0
    if rows:
        await db.commit()
    return sent


def dispatch_sync(conn, id_org: int, event_type: str, ctx: dict) -> int:
    """(worker psycopg2) Envoie l'alerte à tous les canaux webhook des règles actives de l'org pour
    ce type d'événement, et journalise. Renvoie le nombre de succès. BYPASSRLS → filtre id_org."""
    cur = conn.cursor()
    cur.execute(
        "SELECT r.id_rule, c.channel_type, c.config FROM cms_alert_rules r "
        "JOIN cms_notification_channels c ON c.id_channel = r.id_channel "
        "WHERE r.id_org = %s AND r.event_type = %s AND r.enabled AND c.enabled",
        (id_org, event_type),
    )
    sent = 0
    for id_rule, ctype, config in cur.fetchall():
        # psycopg2 rend le JSONB en dict ; message construit PAR CANAL (langue du créateur).
        subject, body = build_message(event_type, ctx, locale=channel_locale(config))
        ok, detail = deliver_to_channel(
            ctype, config, event_type, subject, body, ctx, id_org, id_rule)
        cur.execute(
            "INSERT INTO cms_notification_log (id_org, id_rule, event_type, subject, body, status, "
            "detail, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s, now())",
            (id_org, id_rule, event_type, subject[:200], body, "sent" if ok else "failed", detail),
        )
        sent += 1 if ok else 0
    conn.commit()
    return sent
