"""Tests Retail Media S5 (§2.8) — notifications de cycle de vie des campagnes (§XXII).

- build_message : campaign_started / campaign_ended / campaign_underserved.
- campaign_worker._collect : démarrée (edge + dédup started_notified_at), terminée
  (garde started_notified_at, bilan PoP), sous-servie (jours pleins écoulés, seuil
  min_served_ratio_pct par règle, 1 évaluation/jour via underserved_checked_on) ;
  pas d'émission sans règle active (dédup quand même posée → zéro rafale rétroactive).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import CmsAdvertiser, CmsCampaign, CmsCampaignCreative, CmsCampaignTarget
from app.models.media import CmsMedia
from app.models.notification import CmsAlertRule, CmsNotificationChannel
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.services.notifications import build_message
from app.workers.campaign_worker import _collect

TZ = ZoneInfo("Europe/Paris")


def test_build_message_campaign_events() -> None:
    subj, body = build_message("campaign_started", {
        "campaign_name": "Été Brightwell", "advertiser_name": "Brightwell",
        "sov_percent": 20.0, "starts_on": "2026-07-01", "ends_on": "2026-07-31"}, locale="fr")
    assert "Été Brightwell" in subj and "Brightwell" in body and "20.0 %" in body

    subj, body = build_message("campaign_ended", {
        "campaign_name": "Été Brightwell", "advertiser_name": "Brightwell",
        "starts_on": "2026-07-01", "ends_on": "2026-07-31",
        "plays": 232, "signed_plays": 232, "sov_served_pct": 19.8}, locale="fr")
    assert "terminée" in subj and "232 passages" in body and "19.8 %" in body

    subj, body = build_message("campaign_underserved", {
        "campaign_name": "Été Brightwell", "advertiser_name": "Brightwell",
        "sov_percent": 20.0, "sov_served_pct": 4.8, "threshold_pct": 80.0,
        "period_from": "2026-07-01", "period_until": "2026-07-08"}, locale="fr")
    assert "sous-servie" in subj and "4.8 %" in body and "20.0 % vendus" in body


@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="RMN", org_slug=f"rmn-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    return o


async def _rule(db: AsyncSession, org: CmsOrganization, event_type: str,
                params: dict | None = None) -> None:
    ch = CmsNotificationChannel(id_org=org.id_org, name=f"wh-{event_type}",
                                channel_type="webhook",
                                config={"url": "https://hooks.example.com/x"}, enabled=True)
    db.add(ch)
    await db.flush()
    db.add(CmsAlertRule(id_org=org.id_org, event_type=event_type,
                        id_channel=ch.id_channel, params=params or {}, enabled=True))
    await db.commit()


def _campaign(org: CmsOrganization, adv: CmsAdvertiser, **kw) -> CmsCampaign:
    defaults = dict(
        id_org=org.id_org, id_advertiser=adv.id_advertiser, campaign_name="Été Brightwell",
        status="approved", starts_on=date.today(), ends_on=date.today() + timedelta(days=7),
        sov_percent=20.0, pricing_model="sov", price_amount=1500,
    )
    defaults.update(kw)
    return CmsCampaign(**defaults)


@pytest.mark.asyncio
async def test_collect_started_edge_and_dedup(db_session, org):
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Brightwell")
    db_session.add(adv)
    await db_session.flush()
    camp = _campaign(org, adv)                                   # live aujourd'hui
    future = _campaign(org, adv, campaign_name="Plus tard",      # scheduled → rien
                       starts_on=date.today() + timedelta(days=3))
    draft = _campaign(org, adv, campaign_name="Brouillon", status="draft")
    db_session.add_all([camp, future, draft])
    await db_session.commit()
    await _rule(db_session, org, "campaign_started")

    pending = await _collect(db_session, TZ)
    await db_session.commit()
    assert [(e, c["campaign_name"]) for _, e, c in pending] == [
        ("campaign_started", "Été Brightwell")]
    _, _, ctx = pending[0]
    assert ctx["advertiser_name"] == "Brightwell" and ctx["sov_percent"] == 20.0
    assert camp.started_notified_at is not None
    assert future.started_notified_at is None

    # Dédup : plus rien au 2e passage.
    assert await _collect(db_session, TZ) == []


@pytest.mark.asyncio
async def test_collect_no_rule_marks_but_stays_silent(db_session, org):
    """Sans règle active, la dédup est quand même posée (zéro rafale rétroactive si
    l'org configure une règle plus tard)."""
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Brightwell")
    db_session.add(adv)
    await db_session.flush()
    camp = _campaign(org, adv)
    db_session.add(camp)
    await db_session.commit()

    assert await _collect(db_session, TZ) == []
    await db_session.commit()
    assert camp.started_notified_at is not None


@pytest.mark.asyncio
async def test_collect_ended_requires_prior_start(db_session, org):
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Brightwell")
    db_session.add(adv)
    await db_session.flush()
    ended = _campaign(org, adv, starts_on=date.today() - timedelta(days=10),
                      ends_on=date.today() - timedelta(days=1),
                      started_notified_at=datetime.now(UTC) - timedelta(days=9))
    # Finie AVANT le déploiement de la feature (jamais notifiée démarrée) → silence.
    legacy = _campaign(org, adv, campaign_name="Ancienne",
                       starts_on=date.today() - timedelta(days=30),
                       ends_on=date.today() - timedelta(days=20))
    db_session.add_all([ended, legacy])
    await db_session.commit()
    await _rule(db_session, org, "campaign_ended")

    pending = await _collect(db_session, TZ)
    await db_session.commit()
    assert [(e, c["campaign_name"]) for _, e, c in pending] == [
        ("campaign_ended", "Été Brightwell")]
    _, _, ctx = pending[0]
    assert ctx["plays"] == 0  # bilan présent même sans PoP
    assert ended.ended_notified_at is not None
    assert legacy.ended_notified_at is None

    assert await _collect(db_session, TZ) == []


@pytest.mark.asyncio
async def test_collect_underserved_threshold_and_daily_dedup(db_session, org):
    """SOV servi (jours pleins) 4,8 % < 80 % × 20 % vendus → notifie ; avec un seuil
    de règle à 20 % (4 % du vendu), plus rien ; 1 évaluation par jour."""
    adv = CmsAdvertiser(id_org=org.id_org, advertiser_name="Brightwell")
    player = CmsPlayer(id_org=org.id_org, player_name="P-notif", player_os="linux")
    db_session.add_all([adv, player])
    await db_session.flush()
    creative = CmsMedia(id_org=org.id_org, media_name="spot", media_type="video",
                        storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="c",
                        transcode_status="done", duration_ms=27_000)
    home = CmsMedia(id_org=org.id_org, media_name="maison", media_type="video",
                    storage_key=f"t/{uuid.uuid4().hex}.mp4", checksum_sha256="h",
                    transcode_status="done", duration_ms=30_000)
    db_session.add_all([creative, home])
    await db_session.flush()
    camp = _campaign(org, adv, starts_on=date.today() - timedelta(days=1),
                     started_notified_at=datetime.now(UTC) - timedelta(days=1))
    db_session.add(camp)
    await db_session.flush()
    db_session.add_all([
        CmsCampaignCreative(id_org=org.id_org, id_campaign=camp.id_campaign,
                            id_media=creative.id_media, duration_ms=27_000),
        CmsCampaignTarget(id_org=org.id_org, id_campaign=camp.id_campaign,
                          target_type="player", target_id=player.id_player),
    ])
    # PoP d'HIER (le jour en cours est exclu de l'évaluation) : 3 × 27 s créatif
    # + 54 × 30 s maison → SOV servi = 81 / 1701 ≈ 4,8 %.
    yesterday = datetime.now(UTC) - timedelta(days=1)
    pops = [CmsProofOfPlay(
        id_org=org.id_org, id_player=player.id_player, id_media=creative.id_media,
        played_at=yesterday - timedelta(minutes=i), duration_ms=27_000,
        merkle_hash="m" * 64, signature="sig") for i in range(3)]
    pops += [CmsProofOfPlay(
        id_org=org.id_org, id_player=player.id_player, id_media=home.id_media,
        played_at=yesterday - timedelta(minutes=i + 10), duration_ms=30_000,
        merkle_hash="m" * 64, signature="sig") for i in range(54)]
    db_session.add_all(pops)
    await db_session.commit()
    await _rule(db_session, org, "campaign_underserved")

    pending = await _collect(db_session, TZ)
    await db_session.commit()
    assert [e for _, e, _ in pending] == ["campaign_underserved"]
    _, _, ctx = pending[0]
    assert ctx["sov_served_pct"] == pytest.approx(4.8, abs=0.2)
    assert ctx["threshold_pct"] == 80.0
    assert camp.underserved_checked_on == date.today()

    # Dédup quotidienne : plus d'évaluation aujourd'hui.
    assert await _collect(db_session, TZ) == []

    # Seuil par règle : à 20 % du vendu (= 4 %), 4,8 % servi ne déclenche plus.
    camp.underserved_checked_on = None
    rule = (await db_session.execute(
        select(CmsAlertRule).where(
            CmsAlertRule.id_org == org.id_org,
            CmsAlertRule.event_type == "campaign_underserved"))).scalar_one()
    rule.params = {"min_served_ratio_pct": 20}
    await db_session.commit()
    assert await _collect(db_session, TZ) == []
    await db_session.commit()
    assert camp.underserved_checked_on == date.today()
