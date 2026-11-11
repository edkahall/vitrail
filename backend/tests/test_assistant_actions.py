"""Tests des actions agentiques de l'AI Assistant (§XXVII v2) — confirmer avant d'exécuter.

Vérifie : résolution noms→ids + mapping de zone (1-based UI → 0-based modèle), désambiguïsation
et not-found, rejet audio, exécution (add/remove → publié + audit), garde RBAC, et le token signé
(expiré rejeté). Le LLM n'écrit jamais : seul `execute_action` mute, derrière un token + RBAC.
"""

import time

import pytest
from jose import jwt
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models.audit_log import CmsAuditLog
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization
from app.models.playlist import CmsPlaylist, CmsPlaylistItem
from app.models.role import CmsPermission, CmsRole, CmsRolePermission, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services.assistant_actions import (
    execute_action,
    execute_batch,
    resolve_action,
    sign_action,
    sign_batch,
    verify_action,
    verify_batch,
)

pytestmark = pytest.mark.asyncio

_SEQ = [0]


def _uid() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


async def _org(db) -> CmsOrganization:
    o = CmsOrganization(org_name="ActOrg", org_slug=f"act-{_uid()}")
    db.add(o)
    await db.flush()
    return o


async def _media(db, org, name, media_type="video") -> CmsMedia:
    m = CmsMedia(
        id_org=org.id_org, media_name=name, media_type=media_type,
        storage_key=f"k/{name}", checksum_sha256="x" * 64,
    )
    db.add(m)
    await db.flush()
    return m


async def _playlist(db, org, name) -> CmsPlaylist:
    pl = CmsPlaylist(id_org=org.id_org, playlist_name=name)
    db.add(pl)
    await db.flush()
    return pl


async def _user_with_perm(db, org, perms) -> CmsUser:
    from app.core.security import hash_password

    u = CmsUser(id_org=org.id_org, email=f"act-{_uid()}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    db.add(u)
    await db.flush()
    role = CmsRole(id_org=org.id_org, role_key=f"ACT_{_uid()}", label="Act", is_system=False)
    db.add(role)
    await db.flush()
    for module, op in perms:
        await db.execute(
            pg_insert(CmsPermission)
            .values(module=module, operation=op, label=f"{module}.{op}")
            .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        )
        await db.flush()
        perm = (await db.execute(
            select(CmsPermission).where(
                CmsPermission.module == module, CmsPermission.operation == op
            )
        )).scalar_one()
        db.add(CmsRolePermission(id_role=role.id_role, id_permission=perm.id_permission))
    await db.flush()
    db.add(CmsUserRoleAssignment(id_user=u.id_user, id_role=role.id_role))
    await db.flush()
    return u


async def _user_with_playlist_edit(db, org) -> CmsUser:
    return await _user_with_perm(db, org, [("playlists", "edit"), ("playlists", "read")])


# ── Résolution ────────────────────────────────────────────────────────────────

async def test_resolve_add_maps_names_and_zone(db_session):
    org = await _org(db_session)
    await _playlist(db_session, org, "erer")
    media = await _media(db_session, org, "DYS_UK_Supersonic Blue-Gold_1_1.mp4")

    res = await resolve_action(
        db_session, org.id_org, "add_playlist_item",
        {"playlist": "erer", "media": "Supersonic", "zone": 1},
    )
    assert "error" not in res
    assert res["params"]["id_media"] == media.id_media
    assert res["params"]["zone_index"] == 0          # « zone 1 » (UI) → index 0
    assert "erer" in res["description"] and "zone 1" in res["description"]

    # zone 2 → index 1
    res2 = await resolve_action(
        db_session, org.id_org, "add_playlist_item",
        {"playlist": "erer", "media": "Supersonic", "zone": 2},
    )
    assert res2["params"]["zone_index"] == 1


async def test_resolve_not_found_and_ambiguous(db_session):
    org = await _org(db_session)
    await _playlist(db_session, org, "main")
    await _media(db_session, org, "clip alpha")
    await _media(db_session, org, "clip beta")

    nf = await resolve_action(db_session, org.id_org, "add_playlist_item",
                              {"playlist": "main", "media": "introuvable"})
    assert "error" in nf and "candidates" not in nf

    amb = await resolve_action(db_session, org.id_org, "add_playlist_item",
                               {"playlist": "main", "media": "clip"})
    assert "error" in amb and len(amb["candidates"]) == 2


async def test_resolve_rejects_audio(db_session):
    org = await _org(db_session)
    await _playlist(db_session, org, "p1")
    await _media(db_session, org, "jingle", media_type="audio")
    res = await resolve_action(db_session, org.id_org, "add_playlist_item",
                               {"playlist": "p1", "media": "jingle"})
    assert "error" in res and "audio" in res["error"].lower()


# ── Exécution ───────────────────────────────────────────────────────────────────

async def test_execute_add_then_publish_and_audit(db_session):
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "erer")
    media = await _media(db_session, org, "Supersonic.mp4")
    user = await _user_with_playlist_edit(db_session, org)

    res = await resolve_action(db_session, org.id_org, "add_playlist_item",
                               {"playlist": "erer", "media": "Supersonic", "zone": 1})
    out = await execute_action(db_session, user, "add_playlist_item", res["params"])
    assert out["ok"] is True

    items = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalars().all()
    assert len(items) == 1 and items[0].id_media == media.id_media and items[0].zone_index == 0

    # brouillon vidé (publié) + audit tracé source=assistant
    refreshed = (await db_session.execute(
        select(CmsPlaylist).where(CmsPlaylist.id_playlist == pl.id_playlist)
    )).scalar_one()
    assert refreshed.draft_items is None
    audit = (await db_session.execute(
        select(CmsAuditLog).where(CmsAuditLog.action == "assistant_add_playlist_item")
    )).scalars().all()
    assert len(audit) == 1 and audit[0].source == "assistant" and audit[0].id_user == user.id_user


async def test_execute_remove(db_session):
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "p")
    media = await _media(db_session, org, "toremove.mp4")
    db_session.add(
        CmsPlaylistItem(id_playlist=pl.id_playlist, id_media=media.id_media, sort_order=0)
    )
    await db_session.flush()
    user = await _user_with_playlist_edit(db_session, org)

    res = await resolve_action(db_session, org.id_org, "remove_playlist_item",
                               {"playlist": "p", "media": "toremove"})
    assert "1 occurrence" in res["description"]
    out = await execute_action(db_session, user, "remove_playlist_item", res["params"])
    assert out["ok"] is True
    remaining = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalars().all()
    assert remaining == []


async def test_execute_denied_without_permission(db_session):
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "p")
    media = await _media(db_session, org, "x.mp4")
    # user SANS rôle/permission
    user = CmsUser(id_org=org.id_org, email=f"noperm-{_uid()}@t.com",
                   password_hash="x", is_active=True)
    db_session.add(user)
    await db_session.flush()

    out = await execute_action(db_session, user, "add_playlist_item",
                               {"id_playlist": pl.id_playlist, "id_media": media.id_media,
                                "zone_index": 0, "duration_ms": None})
    assert out["ok"] is False and "Permission denied" in out["error"]
    # rien n'a été écrit
    items = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalars().all()
    assert items == []


# ── Token ────────────────────────────────────────────────────────────────────────

async def test_action_token_expired_rejected(db_session):
    org = await _org(db_session)
    user = CmsUser(id_user=999, id_org=org.id_org, email="t@t.com", password_hash="x")
    # token expiré (exp dans le passé)
    expired = jwt.encode(
        {"type": "assistant_action", "uid": 999, "org": org.id_org,
         "act": "add_playlist_item", "params": {}, "exp": int(time.time()) - 10},
        settings.CMS_JWT_SECRET, algorithm=settings.CMS_JWT_ALGORITHM,
    )
    assert verify_action(expired, user) is None

    good = sign_action(999, org.id_org, "add_playlist_item", {"id_playlist": 1})
    assert verify_action(good, user) is not None


# ── Lot (batch) : publish différé, un seul resync, pas d'état vide transitoire ────

async def test_batch_token_roundtrip_and_validation(db_session):
    org = await _org(db_session)
    user = CmsUser(id_user=42, id_org=org.id_org, email="b@t.com", password_hash="x")

    steps = [
        {"act": "remove_playlist_item", "params": {"id_playlist": 1, "id_media": 10}},
        {"act": "add_playlist_item", "params": {"id_playlist": 1, "id_media": 11}},
    ]
    token = sign_batch(42, org.id_org, steps)
    assert verify_batch(token, user) == steps

    # un token mono-action ne passe PAS pour verify_batch (mauvais type)
    mono = sign_action(42, org.id_org, "add_playlist_item", {"id_playlist": 1})
    assert verify_batch(mono, user) is None

    # étape avec action hors whitelist → rejet du lot
    bad = sign_batch(42, org.id_org, [{"act": "drop_database", "params": {}}])
    assert verify_batch(bad, user) is None

    # token expiré → rejet
    expired = jwt.encode(
        {"type": "assistant_batch", "uid": 42, "org": org.id_org, "steps": steps,
         "exp": int(time.time()) - 10},
        settings.CMS_JWT_SECRET, algorithm=settings.CMS_JWT_ALGORITHM,
    )
    assert verify_batch(expired, user) is None


async def test_batch_token_single_use_anti_replay(db_session):
    """Anti-rejeu (audit 2026-06-24) : un token avec jti n'est consommable qu'une fois ;
    un token legacy sans jti reste accepté (compat)."""
    from app.services.assistant_actions import consume_action_token

    org = await _org(db_session)
    steps = [{"act": "add_playlist_item", "params": {"id_playlist": 1, "id_media": 11}}]
    token = sign_batch(42, org.id_org, steps)

    assert await consume_action_token(token) is True   # 1ʳᵉ exécution OK
    assert await consume_action_token(token) is False  # rejeu refusé

    # Token legacy sans jti → pas de blocage.
    legacy = jwt.encode(
        {"type": "assistant_batch", "uid": 42, "org": org.id_org, "steps": steps,
         "exp": int(time.time()) + 600},
        settings.CMS_JWT_SECRET, algorithm=settings.CMS_JWT_ALGORITHM,
    )
    assert await consume_action_token(legacy) is True


async def test_batch_single_publish_no_empty_state(db_session, monkeypatch):
    """Exemple décisif d'Ed : playlist [CONT01] → « retire CONT01, mets CONT02 » en un lot
    → UN SEUL publish (pas d'état VIDE transitoire visible sur les players)."""
    import app.api.v1.endpoints.playlists as pl_mod

    org = await _org(db_session)
    pl = await _playlist(db_session, org, "loop")
    cont01 = await _media(db_session, org, "CONT01.mp4")
    cont02 = await _media(db_session, org, "CONT02.mp4")
    db_session.add(
        CmsPlaylistItem(id_playlist=pl.id_playlist, id_media=cont01.id_media, sort_order=0)
    )
    await db_session.flush()
    user = await _user_with_playlist_edit(db_session, org)

    # Compte les publish réels (= resyncs graceful poussés aux players).
    real_publish = pl_mod.publish_playlist
    calls = {"n": 0}

    async def counting_publish(id_playlist, current_user, db, response):
        calls["n"] += 1
        return await real_publish(id_playlist, current_user, db, response)

    monkeypatch.setattr(pl_mod, "publish_playlist", counting_publish)

    out = await execute_batch(db_session, user, [
        {"act": "remove_playlist_item",
         "params": {"id_playlist": pl.id_playlist, "id_media": cont01.id_media}},
        {"act": "add_playlist_item",
         "params": {"id_playlist": pl.id_playlist, "id_media": cont02.id_media,
                    "zone_index": 0, "duration_ms": None}},
    ])
    assert out["ok"] is True
    assert calls["n"] == 1  # un SEUL publish → transition directe CONT01→CONT02

    items = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalars().all()
    assert [i.id_media for i in items] == [cont02.id_media]
    refreshed = (await db_session.execute(
        select(CmsPlaylist).where(CmsPlaylist.id_playlist == pl.id_playlist)
    )).scalar_one()
    assert refreshed.draft_items is None  # brouillon publié


async def test_batch_denied_if_one_step_lacks_permission(db_session):
    """Le lot est atomique côté RBAC : une étape non permise refuse tout le lot."""
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "p")
    media = await _media(db_session, org, "x.mp4")
    # user SANS permission playlists:edit
    user = CmsUser(id_org=org.id_org, email=f"noperm-{_uid()}@t.com",
                   password_hash="x", is_active=True)
    db_session.add(user)
    await db_session.flush()

    out = await execute_batch(db_session, user, [
        {"act": "add_playlist_item",
         "params": {"id_playlist": pl.id_playlist, "id_media": media.id_media,
                    "zone_index": 0, "duration_ms": None}},
    ])
    assert out["ok"] is False and "Permission denied" in out["error"]
    items = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalars().all()
    assert items == []


# ── Extensions v2 (feed / playlist / tag / audio) ────────────────────────────────

async def test_resolve_create_feed_catalog_and_unknown(db_session):
    org = await _org(db_session)
    res = await resolve_action(db_session, org.id_org, "create_feed", {"source": "BFM Info"})
    assert "error" not in res
    assert res["params"]["url"].startswith("http") and "BFMTV" in res["params"]["feed_name"]

    unknown = await resolve_action(db_session, org.id_org, "create_feed",
                                   {"source": "Radio Inconnue"})
    assert "error" in unknown  # source absente du catalogue → demande l'URL


async def test_execute_create_feed_makes_datafeed_and_pointer(db_session):
    from app.models.feed import CmsDatafeed
    org = await _org(db_session)
    user = await _user_with_perm(db_session, org, [("media", "create")])
    res = await resolve_action(db_session, org.id_org, "create_feed", {"source": "CNN"})
    out = await execute_action(db_session, user, "create_feed", res["params"])
    assert out["ok"] is True
    feed = (await db_session.execute(
        select(CmsDatafeed).where(CmsDatafeed.id_org == org.id_org)
    )).scalars().first()
    assert feed is not None and feed.config.get("url", "").startswith("http")
    pointer = (await db_session.execute(
        select(CmsMedia).where(CmsMedia.id_org == org.id_org, CmsMedia.media_type == "feed_display")
    )).scalars().first()
    assert pointer is not None and pointer.media_metadata.get("id_feed") == feed.id_feed


async def test_execute_create_playlist(db_session):
    org = await _org(db_session)
    user = await _user_with_perm(db_session, org, [("playlists", "create")])
    out = await execute_action(db_session, user, "create_playlist", {"name": "Ma Playlist"})
    assert out["ok"] is True
    pl = (await db_session.execute(
        select(CmsPlaylist).where(CmsPlaylist.id_org == org.id_org)
    )).scalars().first()
    assert pl is not None and pl.playlist_name == "Ma Playlist"


async def test_tagged_content_resolve_and_execute(db_session):
    from app.models.tag import CmsMediaTagValue, CmsTagNamespace, CmsTagValue
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "promo-pl")
    m1 = await _media(db_session, org, "promo1.mp4")
    m2 = await _media(db_session, org, "promo2.mp4")
    await _media(db_session, org, "autre.mp4")  # non tagué → exclu
    ns = CmsTagNamespace(id_org=org.id_org, namespace_key="campaign", label="Campaign")
    db_session.add(ns)
    await db_session.flush()
    tv = CmsTagValue(id_namespace=ns.id_namespace, tag_value="promo")
    db_session.add(tv)
    await db_session.flush()
    db_session.add_all([
        CmsMediaTagValue(id_media=m1.id_media, id_tag=tv.id_tag),
        CmsMediaTagValue(id_media=m2.id_media, id_tag=tv.id_tag),
    ])
    await db_session.flush()

    res = await resolve_action(db_session, org.id_org, "add_tagged_content_to_playlist",
                               {"playlist": "promo-pl", "tag": "promo"})
    assert "error" not in res and set(res["params"]["id_medias"]) == {m1.id_media, m2.id_media}

    user = await _user_with_perm(db_session, org, [("playlists", "edit")])
    out = await execute_action(db_session, user, "add_tagged_content_to_playlist", res["params"])
    assert out["ok"] is True
    items = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalars().all()
    assert {i.id_media for i in items} == {m1.id_media, m2.id_media}


async def test_audio_track_add_and_remove(db_session):
    from app.models.audio_basket import CmsAudioBasket, CmsAudioBasketItem
    org = await _org(db_session)
    basket = CmsAudioBasket(id_org=org.id_org, basket_name="Ambiance", basket_type="music")
    db_session.add(basket)
    await db_session.flush()
    track = await _media(db_session, org, "track.ogg", media_type="audio")
    user = await _user_with_perm(db_session, org, [("audio_baskets", "configure")])

    res = await resolve_action(db_session, org.id_org, "add_audio_track",
                               {"basket": "Ambiance", "media": "track"})
    assert "error" not in res
    out = await execute_action(db_session, user, "add_audio_track", res["params"])
    assert out["ok"] is True
    n = (await db_session.execute(
        select(CmsAudioBasketItem).where(CmsAudioBasketItem.id_basket == basket.id_basket)
    )).scalars().all()
    assert len(n) == 1 and n[0].id_media == track.id_media

    res_r = await resolve_action(db_session, org.id_org, "remove_audio_track",
                                 {"basket": "Ambiance", "media": "track"})
    out_r = await execute_action(db_session, user, "remove_audio_track", res_r["params"])
    assert out_r["ok"] is True
    n2 = (await db_session.execute(
        select(CmsAudioBasketItem).where(CmsAudioBasketItem.id_basket == basket.id_basket)
    )).scalars().all()
    assert n2 == []


async def test_create_event_resolves_and_executes(db_session):
    org = await _org(db_session)
    await _playlist(db_session, org, "Promo Noël")
    user = await _user_with_perm(db_session, org, [("events", "create")])

    res = await resolve_action(db_session, org.id_org, "create_event", {
        "name": "Black Friday", "content": "Promo Noël", "target": "tous",
        "duration_seconds": 30,
    })
    assert "error" not in res
    assert res["params"]["target_config"] == {"target_type": "org", "target_ids": []}
    assert res["params"]["duration_ms"] == 30000 and res["params"]["id_playlist"] is not None
    assert res["params"]["id_media"] is None

    out = await execute_action(db_session, user, "create_event", res["params"])
    assert out["ok"] is True

    from app.models.event import CmsEvent
    ev = (await db_session.execute(
        select(CmsEvent).where(CmsEvent.event_name == "Black Friday")
    )).scalar_one()
    assert ev.event_type == "manual" and ev.event_kind == "content"
    assert ev.status == "inactive" and ev.duration_ms == 30000
    audit = (await db_session.execute(
        select(CmsAuditLog).where(CmsAuditLog.action == "assistant_create_event")
    )).scalars().all()
    assert len(audit) == 1 and audit[0].source == "assistant"


async def test_remove_feed_from_active_playlists(db_session):
    org = await _org(db_session)
    feed = await _media(db_session, org, "BBC News", media_type="feed_display")
    other = await _media(db_session, org, "filler.mp4")
    pl1 = await _playlist(db_session, org, "Hall")
    pl2 = await _playlist(db_session, org, "Caisse")
    for pl in (pl1, pl2):
        db_session.add(CmsPlaylistItem(
            id_playlist=pl.id_playlist, id_media=feed.id_media, sort_order=0))
        db_session.add(CmsPlaylistItem(
            id_playlist=pl.id_playlist, id_media=other.id_media, sort_order=1))
    await db_session.flush()
    user = await _user_with_playlist_edit(db_session, org)

    res = await resolve_action(db_session, org.id_org, "remove_feed_from_active_playlists",
                               {"feed": "BBC"})
    assert "error" not in res
    assert set(res["params"]["id_playlists"]) == {pl1.id_playlist, pl2.id_playlist}
    assert "2 playlist" in res["description"]

    out = await execute_action(db_session, user, "remove_feed_from_active_playlists", res["params"])
    assert out["ok"] is True
    # Le feed est retiré des deux playlists, le filler reste.
    for pl in (pl1, pl2):
        items = (await db_session.execute(
            select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
        )).scalars().all()
        ids = {i.id_media for i in items}
        assert feed.id_media not in ids and other.id_media in ids


async def _org_admin(db, org) -> CmsUser:
    from app.core.security import hash_password
    u = CmsUser(id_org=org.id_org, email=f"adm-{_uid()}@t.com",
                password_hash=hash_password("Password1!"), is_active=True)
    db.add(u)
    await db.flush()
    await db.execute(
        pg_insert(CmsRole).values(role_key="CMS_ORG_ADMIN", label="Org Admin", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    await db.flush()
    role = (await db.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN")
    )).scalar_one()
    db.add(CmsUserRoleAssignment(id_user=u.id_user, id_role=role.id_role))
    await db.flush()
    return u


async def test_player_command_dispatch_and_audit(db_session, monkeypatch):
    from app.models.player import CmsPlayer
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    p1 = CmsPlayer(id_org=org.id_org, player_name="Hall-1", status="online")
    p2 = CmsPlayer(id_org=org.id_org, player_name="Hall-2", status="offline")
    db_session.add_all([p1, p2])
    await db_session.flush()
    admin = await _org_admin(db_session, org)

    res = await resolve_action(db_session, org.id_org, "restart_player_app", {"target": "tous"})
    assert "error" not in res
    assert set(res["params"]["player_ids"]) == {p1.id_player, p2.id_player}

    sent = []
    async def _fake(ids, cmd):
        sent.append((sorted(ids), cmd))
    monkeypatch.setattr(aa, "_dispatch_player_command", _fake)
    out = await execute_action(db_session, admin, "restart_player_app", res["params"])
    assert out["ok"] is True
    assert sent and sent[0][1] == "restart" and set(sent[0][0]) == {p1.id_player, p2.id_player}
    audit = (await db_session.execute(
        select(CmsAuditLog).where(CmsAuditLog.action == "assistant_restart_player_app")
    )).scalars().all()
    assert len(audit) == 1 and audit[0].source == "assistant"


async def test_player_command_denied_for_non_admin(db_session, monkeypatch):
    from app.models.player import CmsPlayer
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    p1 = CmsPlayer(id_org=org.id_org, player_name="X1", status="online")
    db_session.add(p1)
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("players", "read")])  # PAS org-admin

    res = await resolve_action(db_session, org.id_org, "reboot_player", {"target": "X1"})
    assert "error" not in res and res["params"]["player_ids"] == [p1.id_player]

    called = []
    async def _fake(ids, cmd):
        called.append((ids, cmd))
    monkeypatch.setattr(aa, "_dispatch_player_command", _fake)
    out = await execute_action(db_session, user, "reboot_player", res["params"])
    assert out["ok"] is False and "Permission denied" in out["error"]
    assert not called


async def test_play_vod_resolves_and_dispatches(db_session, monkeypatch):
    from app.models.player import CmsPlayer
    from app.models.vod import CmsVodConfig, CmsVodConfigMedia, CmsVodConfigPlayer
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    media = await _media(db_session, org, "Promo Clip", media_type="image")
    p1 = CmsPlayer(id_org=org.id_org, player_name="Vitrine-1", status="online")
    db_session.add(p1)
    await db_session.flush()
    cfg = CmsVodConfig(id_org=org.id_org, vod_name="Vitrines")
    db_session.add(cfg)
    await db_session.flush()
    db_session.add_all([
        CmsVodConfigMedia(id_vod=cfg.id_vod, id_media=media.id_media),
        CmsVodConfigPlayer(id_vod=cfg.id_vod, id_player=p1.id_player),
    ])
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("vod", "trigger")])

    res = await resolve_action(db_session, org.id_org, "play_vod",
                               {"media": "Promo", "mode": "loop"})
    assert "error" not in res
    assert res["params"]["player_ids"] == [p1.id_player] and res["params"]["mode"] == "loop"

    pub = []
    class _Fake:
        async def publish(self, ch, msg):
            pub.append((ch, msg))
        async def aclose(self):
            pass
    monkeypatch.setattr(aa.aioredis, "from_url", lambda _url: _Fake())
    out = await execute_action(db_session, user, "play_vod", res["params"])
    assert out["ok"] is True and len(pub) == 1 and "play_now" in pub[0][1]


async def test_play_vod_rejects_media_not_in_config(db_session):
    org = await _org(db_session)
    await _media(db_session, org, "HorsVOD", media_type="video")
    res = await resolve_action(db_session, org.id_org, "play_vod", {"media": "HorsVOD"})
    assert "error" in res and "VOD" in res["error"]


async def test_rename_player(db_session):
    from app.models.player import CmsPlayer
    org = await _org(db_session)
    p = CmsPlayer(id_org=org.id_org, player_name="Old Name", status="online")
    db_session.add(p)
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("players", "edit")])

    res = await resolve_action(db_session, org.id_org, "rename_player",
                               {"player": "Old Name", "new_name": "Caisse 1"})
    assert "error" not in res and res["params"]["new_name"] == "Caisse 1"
    out = await execute_action(db_session, user, "rename_player", res["params"])
    assert out["ok"] is True
    refreshed = (await db_session.execute(
        select(CmsPlayer).where(CmsPlayer.id_player == p.id_player)
    )).scalar_one()
    assert refreshed.player_name == "Caisse 1"


async def test_create_schedule_resolves_and_executes(db_session, monkeypatch):
    from app.models.player_group import CmsPlayerGroup
    from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "Promo")
    grp = CmsPlayerGroup(id_org=org.id_org, group_name="Boutiques")
    db_session.add(grp)
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("schedules", "create")])

    res = await resolve_action(db_session, org.id_org, "create_schedule", {
        "name": "Matin Promo", "playlist": "Promo", "target": "Boutiques",
        "days": ["lun", "mar", "mer"], "time_start": "09:00", "time_end": "12:00",
        "priority": 5,
    })
    assert "error" not in res
    assert res["params"]["target_type"] == "player_group"
    assert res["params"]["id_playlist"] == pl.id_playlist
    assert res["params"]["rule"]["rule_type"] == "weekly"
    assert res["params"]["rule"]["days_of_week"] == [0, 1, 2]

    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(aa, "_dispatch_player_command", _noop)
    out = await execute_action(db_session, user, "create_schedule", res["params"])
    assert out["ok"] is True

    sched = (await db_session.execute(
        select(CmsSchedule).where(CmsSchedule.schedule_name == "Matin Promo")
    )).scalar_one()
    assert sched.id_playlist == pl.id_playlist and sched.priority == 5
    rule = (await db_session.execute(
        select(CmsScheduleRule).where(CmsScheduleRule.id_schedule == sched.id_schedule)
    )).scalar_one()
    assert rule.rule_type == "weekly" and rule.days_of_week == [0, 1, 2]
    assert rule.time_start.hour == 9 and rule.time_end.hour == 12
    tgt = (await db_session.execute(
        select(CmsScheduleTarget).where(CmsScheduleTarget.id_schedule == sched.id_schedule)
    )).scalar_one()
    assert tgt.target_type == "player_group" and tgt.target_id == grp.id_group


async def test_delete_schedule(db_session, monkeypatch):
    from app.models.schedule import CmsSchedule
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "P")
    sched = CmsSchedule(id_org=org.id_org, schedule_name="Vieux Programme",
                        schedule_kind="content", id_playlist=pl.id_playlist)
    db_session.add(sched)
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("schedules", "delete")])

    res = await resolve_action(db_session, org.id_org, "delete_schedule",
                               {"schedule": "Vieux Programme"})
    assert "error" not in res

    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(aa, "_dispatch_player_command", _noop)
    out = await execute_action(db_session, user, "delete_schedule", res["params"])
    assert out["ok"] is True
    refreshed = (await db_session.execute(
        select(CmsSchedule).where(CmsSchedule.id_schedule == sched.id_schedule)
    )).scalar_one()
    assert refreshed.is_deleted is True


async def test_set_playlist_item_rules(db_session):
    from app.models.playlist import CmsPlaylistItemRule
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "Promo")
    media = await _media(db_session, org, "Clip.mp4")
    db_session.add(CmsPlaylistItem(
        id_playlist=pl.id_playlist, id_media=media.id_media, sort_order=0))
    await db_session.flush()
    user = await _user_with_playlist_edit(db_session, org)

    res = await resolve_action(db_session, org.id_org, "set_playlist_item_rules", {
        "playlist": "Promo", "media": "Clip", "days": ["lun", "ven"],
        "time_start": "08:00", "time_end": "10:00",
    })
    assert "error" not in res
    assert res["params"]["rules"][0]["rule_type"] == "weekly"
    assert res["params"]["rules"][0]["days_of_week"] == [0, 4]

    out = await execute_action(db_session, user, "set_playlist_item_rules", res["params"])
    assert out["ok"] is True

    item = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalar_one()
    rule = (await db_session.execute(
        select(CmsPlaylistItemRule).where(CmsPlaylistItemRule.id_item == item.id_item)
    )).scalar_one()
    assert rule.rule_type == "weekly" and rule.days_of_week == [0, 4]
    assert rule.time_start.hour == 8 and rule.time_end.hour == 10


async def test_tag_media_creates_and_assigns(db_session):
    from app.models.tag import CmsMediaTagValue, CmsTagNamespace, CmsTagValue
    org = await _org(db_session)
    media = await _media(db_session, org, "Affiche.jpg", media_type="image")
    user = await _user_with_perm(db_session, org, [("media", "edit")])

    res = await resolve_action(db_session, org.id_org, "tag_media",
                               {"media": "Affiche", "value": "soldes", "namespace": "Promo"})
    assert "error" not in res and res["params"]["namespace_key"] == "promo"

    out = await execute_action(db_session, user, "tag_media", res["params"])
    assert out["ok"] is True

    ns = (await db_session.execute(select(CmsTagNamespace).where(
        CmsTagNamespace.id_org == org.id_org, CmsTagNamespace.namespace_key == "promo"
    ))).scalar_one()
    val = (await db_session.execute(select(CmsTagValue).where(
        CmsTagValue.id_namespace == ns.id_namespace, CmsTagValue.tag_value == "soldes"
    ))).scalar_one()
    link = (await db_session.execute(select(CmsMediaTagValue).where(
        CmsMediaTagValue.id_media == media.id_media, CmsMediaTagValue.id_tag == val.id_tag
    ))).scalar_one_or_none()
    assert link is not None


async def test_assign_and_unassign_basket(db_session, monkeypatch):
    from app.models.audio_basket import CmsAudioBasket, CmsPlayerBasketConfig
    from app.models.player import CmsPlayer
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    basket = CmsAudioBasket(id_org=org.id_org, basket_name="Ambiance", basket_type="music")
    db_session.add(basket)
    await db_session.flush()
    p1 = CmsPlayer(id_org=org.id_org, player_name="Hall", status="online")
    db_session.add(p1)
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("audio_baskets", "configure")])

    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(aa, "_dispatch_player_command", _noop)

    res = await resolve_action(db_session, org.id_org, "assign_basket_to_players",
                               {"basket": "Ambiance", "target": "Hall", "mix_ratio": 70})
    assert "error" not in res
    assert res["params"]["player_ids"] == [p1.id_player] and res["params"]["mix_ratio"] == 70
    out = await execute_action(db_session, user, "assign_basket_to_players", res["params"])
    assert out["ok"] is True
    cfg = (await db_session.execute(select(CmsPlayerBasketConfig).where(
        CmsPlayerBasketConfig.id_player == p1.id_player,
        CmsPlayerBasketConfig.id_basket == basket.id_basket,
    ))).scalar_one()
    assert cfg.mix_ratio == 70 and cfg.is_active is True

    res2 = await resolve_action(db_session, org.id_org, "unassign_basket_from_players",
                                {"basket": "Ambiance", "target": "Hall"})
    out2 = await execute_action(db_session, user, "unassign_basket_from_players", res2["params"])
    assert out2["ok"] is True
    gone = (await db_session.execute(select(CmsPlayerBasketConfig).where(
        CmsPlayerBasketConfig.id_player == p1.id_player,
        CmsPlayerBasketConfig.id_basket == basket.id_basket,
    ))).scalar_one_or_none()
    assert gone is None


async def test_set_playlist_item_rules_scoped_preserves_default(db_session):
    from app.models.player_group import CmsPlayerGroup
    from app.models.playlist import CmsPlaylistItemRule
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "Promo")
    media = await _media(db_session, org, "Clip.mp4")
    db_session.add(CmsPlaylistItem(
        id_playlist=pl.id_playlist, id_media=media.id_media, sort_order=0))
    grp = CmsPlayerGroup(id_org=org.id_org, group_name="Lyon")
    db_session.add(grp)
    await db_session.flush()
    user = await _user_with_playlist_edit(db_session, org)

    # 1) Règle par défaut (tous les players).
    res1 = await resolve_action(db_session, org.id_org, "set_playlist_item_rules", {
        "playlist": "Promo", "media": "Clip", "days": ["lun"],
        "time_start": "09:00", "time_end": "12:00"})
    assert res1["params"]["scope"] == "default"
    await execute_action(db_session, user, "set_playlist_item_rules", res1["params"])

    # 2) Override pour le groupe Lyon → doit COEXISTER avec la règle par défaut.
    res2 = await resolve_action(db_session, org.id_org, "set_playlist_item_rules", {
        "playlist": "Promo", "media": "Clip", "target": "Lyon",
        "days": ["sam"], "time_start": "10:00", "time_end": "14:00"})
    assert res2["params"]["scope"] == "player_group" and res2["params"]["target_id"] == grp.id_group
    out = await execute_action(db_session, user, "set_playlist_item_rules", res2["params"])
    assert out["ok"] is True

    item = (await db_session.execute(
        select(CmsPlaylistItem).where(CmsPlaylistItem.id_playlist == pl.id_playlist)
    )).scalar_one()
    rules = (await db_session.execute(
        select(CmsPlaylistItemRule).where(CmsPlaylistItemRule.id_item == item.id_item)
    )).scalars().all()
    scopes = {(r.scope, r.target_id) for r in rules}
    assert ("default", None) in scopes and ("player_group", grp.id_group) in scopes
    assert len(rules) == 2


async def test_set_maintenance_command(db_session, monkeypatch):
    from app.models.player import CmsPlayer
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    db_session.add(CmsPlayer(id_org=org.id_org, player_name="Ecran 1", status="online"))
    await db_session.flush()
    admin = await _org_admin(db_session, org)

    res = await resolve_action(db_session, org.id_org, "set_maintenance", {"target": "tous"})
    assert "error" not in res
    sent = []
    async def _fake(ids, cmd):
        sent.append(cmd)
    monkeypatch.setattr(aa, "_dispatch_player_command", _fake)
    out = await execute_action(db_session, admin, "set_maintenance", res["params"])
    assert out["ok"] is True and sent == ["maintenance"]


async def test_update_schedule_and_manage_targets(db_session, monkeypatch):
    from app.models.player_group import CmsPlayerGroup
    from app.models.schedule import CmsSchedule, CmsScheduleRule, CmsScheduleTarget
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    pl = await _playlist(db_session, org, "P")
    sched = CmsSchedule(id_org=org.id_org, schedule_name="Prog", schedule_kind="content",
                        id_playlist=pl.id_playlist, priority=0)
    grp = CmsPlayerGroup(id_org=org.id_org, group_name="Sud")
    db_session.add_all([sched, grp])
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("schedules", "edit")])

    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(aa, "_dispatch_player_command", _noop)

    # Modif : priorité + désactivation + nouvelle règle.
    res = await resolve_action(db_session, org.id_org, "update_schedule", {
        "schedule": "Prog", "priority": 9, "active": False,
        "days": ["ven"], "time_start": "18:00", "time_end": "20:00"})
    assert "error" not in res
    out = await execute_action(db_session, user, "update_schedule", res["params"])
    assert out["ok"] is True
    refreshed = (await db_session.execute(
        select(CmsSchedule).where(CmsSchedule.id_schedule == sched.id_schedule))).scalar_one()
    assert refreshed.priority == 9 and refreshed.is_active is False
    rule = (await db_session.execute(select(CmsScheduleRule).where(
        CmsScheduleRule.id_schedule == sched.id_schedule))).scalar_one()
    assert rule.rule_type == "weekly" and rule.days_of_week == [4]

    # Ajout de cible.
    res2 = await resolve_action(db_session, org.id_org, "add_schedule_target",
                                {"schedule": "Prog", "target": "Sud"})
    await execute_action(db_session, user, "add_schedule_target", res2["params"])
    tgt = (await db_session.execute(select(CmsScheduleTarget).where(
        CmsScheduleTarget.id_schedule == sched.id_schedule))).scalars().all()
    assert len(tgt) == 1 and tgt[0].target_type == "player_group"
    assert tgt[0].target_id == grp.id_group

    # Retrait de cible.
    res3 = await resolve_action(db_session, org.id_org, "remove_schedule_target",
                                {"schedule": "Prog", "target": "Sud"})
    await execute_action(db_session, user, "remove_schedule_target", res3["params"])
    gone = (await db_session.execute(select(CmsScheduleTarget).where(
        CmsScheduleTarget.id_schedule == sched.id_schedule))).scalars().all()
    assert gone == []


async def test_screen_power_only_equipped_players(db_session, monkeypatch):
    from app.models.player import CmsPlayer
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    p_eq = CmsPlayer(id_org=org.id_org, player_name="Ecran RS232", status="online",
                     display_config={"type": "rs232"})
    p_no = CmsPlayer(id_org=org.id_org, player_name="Sans ecran", status="online",
                     display_config={})
    db_session.add_all([p_eq, p_no])
    await db_session.flush()
    admin = await _org_admin(db_session, org)

    res = await resolve_action(db_session, org.id_org, "screen_power",
                               {"target": "tous", "state": "off"})
    assert "error" not in res
    assert res["params"]["player_ids"] == [p_eq.id_player]
    assert res["params"]["display_cmd"] == "power_off" and res["warnings"]  # exclusion signalée

    sent = []
    async def _fake(ids, cmd, extra):
        sent.append((sorted(ids), cmd, extra))
    monkeypatch.setattr(aa, "_dispatch_display_command", _fake)
    out = await execute_action(db_session, admin, "screen_power", res["params"])
    assert out["ok"] is True
    assert sent and sent[0][1] == "power_off" and sent[0][0] == [p_eq.id_player]


async def test_screen_command_no_equipped_player_errors(db_session):
    from app.models.player import CmsPlayer
    org = await _org(db_session)
    db_session.add(CmsPlayer(id_org=org.id_org, player_name="P", status="online",
                             display_config={}))
    await db_session.flush()
    res = await resolve_action(db_session, org.id_org, "screen_set_input",
                               {"target": "tous", "source": "hdmi2"})
    assert "error" in res and "screen" in res["error"].lower()


async def test_configure_audio_basket(db_session, monkeypatch):
    from app.models.audio_basket import CmsAudioBasket
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    basket = CmsAudioBasket(id_org=org.id_org, basket_name="Ambiance", basket_type="music")
    db_session.add(basket)
    await db_session.flush()
    user = await _user_with_perm(db_session, org, [("audio_baskets", "configure")])

    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(aa, "_dispatch_player_command", _noop)

    res = await resolve_action(db_session, org.id_org, "configure_audio_basket", {
        "basket": "Ambiance", "shuffle": True, "trigger_mode": "minutes",
        "trigger_value": 10, "ducking_pct": 15})
    assert "error" not in res
    out = await execute_action(db_session, user, "configure_audio_basket", res["params"])
    assert out["ok"] is True
    refreshed = (await db_session.execute(select(CmsAudioBasket).where(
        CmsAudioBasket.id_basket == basket.id_basket))).scalar_one()
    assert refreshed.shuffle is True and refreshed.trigger_mode == "minutes"
    assert refreshed.trigger_value == 10 and refreshed.ducking_config.get("target_pct") == 15


# ── HDMI switch / fuseau device / rangement médias (2026-07-08) ────────────────

async def test_resolve_hdmi_switch_filters_capable(db_session):
    """Le switch HDMI ne cible que les players à entrée HDMI physique (XT/4K/XD1230) ;
    les autres sont listés en warning. Mode invalide = erreur."""
    from app.models.player import CmsPlayer
    org = await _org(db_session)
    bs = CmsPlayer(id_org=org.id_org, player_name="xt-cap", player_os="brightsign",
                   capabilities={"bs_model": "XT1144"})
    hd = CmsPlayer(id_org=org.id_org, player_name="hd-nocap", player_os="brightsign",
                   capabilities={"bs_model": "HD1024"})
    nuc = CmsPlayer(id_org=org.id_org, player_name="nuc-nocap", player_os="linux")
    db_session.add_all([bs, hd, nuc])
    await db_session.flush()

    res = await resolve_action(db_session, org.id_org, "hdmi_switch",
                               {"target": "tous", "mode": "start"})
    assert "error" not in res, res
    assert res["params"]["player_ids"] == [bs.id_player]
    assert res["params"]["mode"] == "start"
    assert res["warnings"]  # hd + nuc ignorés

    nocap = await resolve_action(db_session, org.id_org, "hdmi_switch",
                                 {"target": "nuc-nocap", "mode": "start"})
    assert "error" in nocap
    badmode = await resolve_action(db_session, org.id_org, "hdmi_switch",
                                   {"target": "xt-cap", "mode": "sideways"})
    assert "error" in badmode


async def test_resolve_set_player_timezone(db_session):
    """Fuseau device : un player à la fois, chaîne validée (pas d'injection)."""
    from app.models.player import CmsPlayer
    org = await _org(db_session)
    p1 = CmsPlayer(id_org=org.id_org, player_name="tz-one", player_os="linux")
    p2 = CmsPlayer(id_org=org.id_org, player_name="tz-two", player_os="linux")
    db_session.add_all([p1, p2])
    await db_session.flush()

    ok = await resolve_action(db_session, org.id_org, "set_player_timezone",
                              {"target": "tz-one", "timezone": "Europe/Paris"})
    assert "error" not in ok, ok
    assert ok["params"]["player_ids"] == [p1.id_player]
    assert ok["params"]["timezone"] == "Europe/Paris"

    multi = await resolve_action(db_session, org.id_org, "set_player_timezone",
                                 {"target": "tous", "timezone": "Europe/Paris"})
    assert "error" in multi  # le fuseau se règle player PAR player
    inj = await resolve_action(db_session, org.id_org, "set_player_timezone",
                               {"target": "tz-one", "timezone": "Europe/Paris; rm -rf /"})
    assert "error" in inj


async def test_move_media_to_folder_resolve_and_execute(db_session):
    """Rangement : résolution (unfiled) puis exécution = id_folder posé, scopé org."""
    from app.models.media_folder import CmsMediaFolder
    org = await _org(db_session)
    folder = CmsMediaFolder(id_org=org.id_org, folder_name="Promos")
    db_session.add(folder)
    await db_session.flush()
    m1 = await _media(db_session, org, "promo-a.mp4")
    m2 = await _media(db_session, org, "logo.png", media_type="image")

    res = await resolve_action(db_session, org.id_org, "move_media_to_folder",
                               {"folder": "Promos", "unfiled": True})
    assert "error" not in res, res
    assert set(res["params"]["id_media"]) == {m1.id_media, m2.id_media}
    assert res["params"]["id_folder"] == folder.id_folder

    user = await _user_with_perm(db_session, org, [("media", "edit")])
    out = await execute_batch(db_session, user, [
        {"act": "move_media_to_folder", "params": res["params"]},
    ])
    assert out["ok"] is True, out
    moved = (await db_session.execute(
        select(CmsMedia).where(CmsMedia.id_media.in_([m1.id_media, m2.id_media]))
    )).scalars().all()
    assert all(m.id_folder == folder.id_folder for m in moved)

    # Plus rien à déplacer → erreur explicite (pas d'action vide)
    again = await resolve_action(db_session, org.id_org, "move_media_to_folder",
                                 {"folder": "Promos", "unfiled": True})
    assert "error" in again


async def test_create_event_keep_screens_on(db_session):
    """create_event porte keep_screens_on (écrans maintenus allumés pendant l'événement)."""
    org = await _org(db_session)
    await _media(db_session, org, "soiree.mp4")
    res = await resolve_action(db_session, org.id_org, "create_event",
                               {"name": "Soirée", "content": "soiree.mp4",
                                "keep_screens_on": True})
    assert "error" not in res, res
    assert res["params"]["keep_screens_on"] is True
    assert "KEPT ON" in res["description"]

    user = await _user_with_perm(db_session, org, [("events", "create")])
    out = await execute_batch(db_session, user, [
        {"act": "create_event", "params": res["params"]},
    ])
    assert out["ok"] is True, out
    from app.models.event import CmsEvent
    ev = (await db_session.execute(
        select(CmsEvent).where(CmsEvent.id_org == org.id_org,
                               CmsEvent.event_name == "Soirée")
    )).scalar_one()
    assert ev.keep_screens_on is True


# ── Mise à jour OTA par l'assistant (update_players, gatée org-admin + PIN) ──


async def _ota_parc(db, org):
    """3 players (2 plateformes) + catalogue : stable linux (NUC-Hall en retard,
    NUC-Stock à jour), BETA linux plus haute (à ignorer), stable brightsign (en retard).
    Versions à majeur 900+`_uid()` : le catalogue est GLOBAL (unicité track/version/famille)
    et `create_deployment` committe → chaque test doit dominer le catalogue sans collision."""
    from app.models.player import CmsPlayer
    from app.models.player_version import CmsPlayerUpdateVersion
    n = 900 + _uid()
    p_old = CmsPlayer(id_org=org.id_org, player_name="NUC-Hall", status="online",
                      player_os="linux", player_version=f"{n}.0.1")
    p_ok = CmsPlayer(id_org=org.id_org, player_name="NUC-Stock", status="online",
                     player_os="linux", player_version=f"{n}.0.2")
    p_bs = CmsPlayer(id_org=org.id_org, player_name="BS-Vitrine", status="online",
                     player_os="brightsign", player_version=f"{n}.5.6")
    db.add_all([p_old, p_ok, p_bs])
    v_lin = CmsPlayerUpdateVersion(track="player", version=f"{n}.0.2", channel="stable",
                                   model_family="linux", download_url="/u/lin.tgz")
    v_beta = CmsPlayerUpdateVersion(track="player", version=f"{n}.9.0", channel="beta",
                                    model_family="linux", download_url="/u/beta.tgz")
    v_bs = CmsPlayerUpdateVersion(track="player", version=f"{n}.5.7", channel="stable",
                                  model_family="brightsign", download_url="/u/bs.tgz")
    db.add_all([v_lin, v_beta, v_bs])
    await db.flush()
    return p_old, p_ok, p_bs, v_lin, v_bs


async def test_update_players_resolves_per_platform_stable_only(db_session):
    """Groupes par plateforme, dernière STABLE seulement (beta ignorée), à-jour ignorés."""
    org = await _org(db_session)
    p_old, p_ok, p_bs, v_lin, v_bs = await _ota_parc(db_session, org)

    res = await resolve_action(db_session, org.id_org, "update_players", {"target": "tous"})
    assert "error" not in res, res
    groups = {g["platform"]: g for g in res["params"]["groups"]}
    assert groups["linux"]["id_version"] == v_lin.id_version
    assert groups["linux"]["version"] == v_lin.version    # la stable, pas la beta plus haute
    assert groups["linux"]["player_ids"] == [p_old.id_player]
    assert groups["brightsign"]["id_version"] == v_bs.id_version
    assert groups["brightsign"]["player_ids"] == [p_bs.id_player]
    assert any("already up to date" in w and "NUC-Stock" in w for w in res["warnings"])
    assert "2 player(s)" in res["description"]


async def test_update_players_all_up_to_date_errors(db_session):
    from app.models.player import CmsPlayer
    from app.models.player_version import CmsPlayerUpdateVersion
    org = await _org(db_session)
    n = 900 + _uid()   # domine toute version générique committée par une autre suite
    db_session.add(CmsPlayer(id_org=org.id_org, player_name="Solo", status="online",
                             player_os="linux", player_version=f"{n}.0.0"))
    db_session.add(CmsPlayerUpdateVersion(track="player", version=f"{n}.0.0",
                                          channel="stable", model_family="linux"))
    await db_session.flush()
    res = await resolve_action(db_session, org.id_org, "update_players", {"target": "Solo"})
    assert "error" in res and "already up to date" in res["error"]


async def test_update_players_no_catalog_version_errors(db_session, monkeypatch):
    """Plateforme sans version au catalogue → erreur explicite (pas de proposition vide).
    latest_player_version est neutralisé : d'autres suites committent des versions
    GÉNÉRIQUES (model_family NULL) qui matcheraient n'importe quelle plateforme."""
    from app.models.player import CmsPlayer
    from app.services import ota
    org = await _org(db_session)
    db_session.add(CmsPlayer(id_org=org.id_org, player_name="Orphan", status="online",
                             player_os="chromeos", player_version="2.0.0"))
    await db_session.flush()

    async def _none(_db, _platform, channel="stable"):
        return None
    monkeypatch.setattr(ota, "latest_player_version", _none)
    res = await resolve_action(db_session, org.id_org, "update_players", {"target": "Orphan"})
    assert "error" in res and "catalog" in res["error"]


async def test_update_players_executes_deployments_and_audit(db_session, monkeypatch):
    """L'exécution crée UN déploiement approuvé par plateforme (targets pending +
    previous_version) et force_sync les players ciblés. Audit par déploiement."""
    from app.models.player_version import CmsUpdateDeployment, CmsUpdateDeploymentTarget
    from app.services import assistant_actions as aa
    org = await _org(db_session)
    p_old, _p_ok, p_bs, v_lin, v_bs = await _ota_parc(db_session, org)
    admin = await _org_admin(db_session, org)

    res = await resolve_action(db_session, org.id_org, "update_players", {"target": "tous"})
    assert "error" not in res, res

    synced = []
    async def _fake(ids, cmd):
        synced.append((sorted(ids), cmd))
    monkeypatch.setattr(aa, "_dispatch_player_command", _fake)

    out = await execute_action(db_session, admin, "update_players", res["params"])
    assert out["ok"] is True, out
    assert "OTA deployment" in out["message"]

    deps = (await db_session.execute(
        select(CmsUpdateDeployment).where(CmsUpdateDeployment.id_org == org.id_org)
    )).scalars().all()
    assert {d.id_version for d in deps} == {v_lin.id_version, v_bs.id_version}
    assert all(d.strategy == "immediate" and d.status == "pending" for d in deps)
    tgts = (await db_session.execute(
        select(CmsUpdateDeploymentTarget).where(
            CmsUpdateDeploymentTarget.id_deployment.in_([d.id_deployment for d in deps]))
    )).scalars().all()
    by_player = {t.id_player: t for t in tgts}
    assert set(by_player) == {p_old.id_player, p_bs.id_player}
    assert by_player[p_old.id_player].previous_version == p_old.player_version
    assert {tuple(ids) for ids, cmd in synced if cmd == "force_sync"} == {
        (p_old.id_player,), (p_bs.id_player,)}
    audit = (await db_session.execute(
        select(CmsAuditLog).where(CmsAuditLog.action == "assistant_update_players")
    )).scalars().all()
    assert len(audit) == 2 and all(a.source == "assistant" for a in audit)


async def test_update_players_denied_for_non_admin_and_pin_gated(db_session):
    from app.services.assistant_actions import PIN_REQUIRED_ACTIONS
    org = await _org(db_session)
    await _ota_parc(db_session, org)
    user = await _user_with_perm(db_session, org, [("players", "read")])  # PAS org-admin

    res = await resolve_action(db_session, org.id_org, "update_players", {"target": "tous"})
    assert "error" not in res
    out = await execute_action(db_session, user, "update_players", res["params"])
    assert out["ok"] is False and "Permission denied" in out["error"]
    # 2e facteur messagerie : l'OTA est d'office dans les actions à PIN (décision Ed).
    assert "update_players" in PIN_REQUIRED_ACTIONS
