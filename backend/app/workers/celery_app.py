from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "signflow",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.media_worker",
        "app.workers.event_worker",
        "app.workers.screenshot_worker",
        "app.workers.feed_worker",
        "app.workers.assistant_worker",
        "app.workers.feedback_worker",
        "app.workers.generation_worker",
        "app.workers.pop_worker",
        "app.workers.promo_worker",
        "app.workers.ota_worker",
        "app.workers.engine_worker",
        "app.workers.analytics_worker",
        "app.workers.report_worker",
        "app.workers.campaign_worker",
        "app.workers.notification_worker",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # ── Robustesse sous charge (upload massif de vidéos) ──────────────────────────
    # prefetch=1 : un worker ne réserve qu'UNE tâche à la fois → les transcodes lourds
    # ne s'accumulent pas en mémoire d'un worker et restent équilibrés entre process.
    worker_prefetch_multiplier=1,
    # acks_late : la tâche n'est acquittée qu'à la fin → si un worker est tué (OOM,
    # restart) au milieu d'un transcode, la tâche est requeue, pas perdue.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # recycle le process worker après N tâches → libère toute mémoire résiduelle
    # (gros transcodes) et évite la dérive mémoire sur un long batch.
    worker_max_tasks_per_child=50,
    task_routes={
        "app.workers.media_worker.*": {"queue": "media"},
        "app.workers.event_worker.*": {"queue": "default"},
        # La capture Playwright (lourde, Chromium) part sur la file dédiée cms_screenshot
        # (worker celery-screenshot). Le scan « à rafraîchir » reste léger → file default.
        "app.workers.screenshot_worker.capture_url_display": {"queue": "cms_screenshot"},
        "app.workers.screenshot_worker.capture_due_url_displays": {"queue": "default"},
        # Rendu promo (Jinja2→Playwright, Chromium) → file dédiée cms_screenshot.
        "app.workers.promo_worker.render_promo_image": {"queue": "cms_screenshot"},
        "app.workers.promo_worker.render_promo_video": {"queue": "cms_screenshot"},
        # Animation vidéo promo (ffmpeg) → file media (celery-media a ffmpeg, pas Playwright).
        "app.workers.promo_worker.animate_promo_video": {"queue": "media"},
        "app.workers.feed_worker.*": {"queue": "default"},
        "app.workers.assistant_worker.*": {"queue": "default"},
        "app.workers.feedback_worker.*": {"queue": "default"},
        # Génération vidéo/audio : tâche I/O longue (poll fal) → file default (pas CPU).
        "app.workers.generation_worker.*": {"queue": "default"},
        # Réessai des webhooks (P1.7) : file `notifications`, déjà servie par le worker
        # léger (`--queues=default,notifications,analytics`) mais qui n'avait jusqu'ici
        # AUCUNE tâche routée vers elle. Séparée de `default` à dessein : un réessai
        # attend des minutes, il ne doit pas occuper un créneau derrière lequel une
        # synchronisation de player patienterait.
        "app.workers.notification_worker.*": {"queue": "notifications"},
        "app.workers.pop_worker.*": {"queue": "default"},
        "app.workers.ota_worker.*": {"queue": "default"},
        "app.workers.engine_worker.*": {"queue": "default"},
    },
    beat_schedule={
        # Signalétique du worker média (encodeurs matériels, cœurs, disque de travail)
        # pour la page « Serveur ». Le backend n'a ni ffmpeg ni GPU : sans cette passe,
        # il n'a aucun moyen de savoir ce que la machine sait faire.
        "publish-worker-capabilities-60s": {
            "task": "app.workers.media_worker.publish_capabilities",
            "schedule": 60.0,
        },
        "expire-events-30s": {
            "task": "app.workers.event_worker.expire_events",
            "schedule": 30.0,
        },
        "finalize-reverting-events-30s": {
            "task": "app.workers.event_worker.finalize_reverting_events",
            "schedule": 30.0,
        },
        "activate-scheduled-events-10s": {
            "task": "app.workers.event_worker.activate_scheduled_events",
            "schedule": 10.0,
        },
        "lift-media-embargoes-30s": {
            "task": "app.workers.event_worker.lift_media_embargoes",
            "schedule": 30.0,
        },
        # Ré-évaluation des fenêtres horaires (item-rules + règles de schedule) :
        # repousse un sync graceful aux players en ligne pour qu'un contenu limité
        # à un créneau (ex. 18h-19h) s'active/s'éteigne tout seul à la frontière.
        "reapply-time-windows-30s": {
            "task": "app.workers.event_worker.reapply_time_windows",
            "schedule": 30.0,
        },
        "check-player-health-5min": {
            "task": "app.workers.event_worker.check_player_health",
            "schedule": 300.0,
        },
        # Auto-power écrans : allume/éteint les dalles selon les heures d'ouverture des
        # sites. Edge-triggered (transitions seulement, dédup last_power_state).
        "apply-site-power-60s": {
            "task": "app.workers.event_worker.apply_site_power",
            "schedule": 60.0,
        },
        # URL Display screenshot_server : rafraîchit les captures arrivées à échéance (§XXV.2).
        "capture-due-url-displays-60s": {
            "task": "app.workers.screenshot_worker.capture_due_url_displays",
            "schedule": 60.0,
        },
        # Feed Display (§XXV.3) : enqueue les feeds RSS/Atom arrivés à échéance + purge rétention.
        "poll-due-feeds-60s": {
            "task": "app.workers.feed_worker.poll_due_feeds",
            "schedule": 60.0,
        },
        "purge-old-feed-items-1h": {
            "task": "app.workers.feed_worker.purge_old_feed_items",
            "schedule": 3600.0,
        },
        # Proof of Play (§XXIV) : crée les partitions JOUR à venir (aujourd'hui + 7),
        # une fois par jour → les preuves ne tombent jamais dans la partition DEFAULT.
        "ensure-pop-partitions-daily": {
            "task": "app.workers.pop_worker.ensure_pop_partitions",
            "schedule": 86400.0,
        },
        # Proof of Play — archivage & rotation (§XXIV, PLAN_POP_ARCHIVAL) : exporte les
        # partitions JOUR plus vieilles que la rétention chaude vers MinIO (CSV.gz + PDF +
        # index merkle_root) puis DROP → disque rendu quotidiennement.
        "archive-pop-daily": {
            "task": "app.workers.pop_worker.archive_pop",
            "schedule": 86400.0,
        },
        # OTA (§XXX slice 2) : échoue les cibles bloquées en cours d'apply (player qui ne
        # confirme jamais) → le déploiement peut se terminer + alerter deployment_failed.
        "ota-timeout-5min": {
            "task": "app.workers.ota_worker.ota_timeout",
            "schedule": 300.0,
        },
        # Analytics (§XXIV Executive Dashboard) : rollup quotidien des métriques réseau
        # (uptime, conformité, alertes, health score) → séries temporelles du dashboard.
        # Toutes les heures : rafraîchit la ligne du jour (UPSERT) → dashboard à jour même
        # en cours de journée, l'historique reste 1 point/jour.
        "rollup-network-metrics-hourly": {
            "task": "app.workers.analytics_worker.rollup_network_metrics",
            "schedule": 3600.0,
        },
        # Rapports planifiés (§24.3) : génère + envoie par email les rapports exécutifs échus
        # (hebdo/mensuel). Vérifie toutes les heures (granularité de send_hour = l'heure).
        "run-due-report-schedules-hourly": {
            "task": "app.workers.report_worker.run_due_report_schedules",
            "schedule": 3600.0,
        },
        # Retail media §2.8 : notifications de cycle de vie des campagnes (démarrée /
        # terminée, edge-triggered dédup migr 0077) + sous-diffusion SOV (1 éval/jour).
        "check-campaign-notifications-5min": {
            "task": "app.workers.campaign_worker.check_campaign_notifications",
            "schedule": 300.0,
        },
    },
)
