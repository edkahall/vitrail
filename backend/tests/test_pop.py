"""
Tests Sprint 1.6 — Proof of Play batch ingestion (§XXIV).

Couvre :
  - test_pop_batch_inserted_and_hashes
  - test_pop_batch_merkle_chain_continues
  - test_pop_batch_empty_logs_rejected
"""

import uuid
from datetime import datetime, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_player_token, hash_password
from app.models.audio_basket import CmsAudioBasket, CmsAudioBasketItem
from app.models.media import CmsMedia
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.player import CmsPlayer
from app.models.pop import CmsProofOfPlay
from app.models.pop_archive import CmsPopArchive
from app.models.role import CmsRole, CmsUserRoleAssignment
from app.models.user import CmsUser
from app.services import pop_archive

# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    slug = f"pop-org-{uuid.uuid4().hex[:8]}"
    o = CmsOrganization(org_name="PopOrg", org_slug=slug)
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


@pytest_asyncio.fixture
async def player(db_session: AsyncSession, org: CmsOrganization) -> CmsPlayer:
    p = CmsPlayer(
        id_org=org.id_org,
        player_name=f"player-{uuid.uuid4().hex[:8]}",
        player_os="linux",
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
def player_token(player: CmsPlayer) -> str:
    token, _ = create_player_token(player.id_player)
    return token


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, org: CmsOrganization) -> str:
    """Token CMS_ORG_ADMIN de l'org (pour /pop/verify et /pop/certificate)."""
    user = CmsUser(id_org=org.id_org, email=f"adm-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    await db_session.flush()
    await db_session.execute(
        pg_insert(CmsRole).values(role_key="CMS_ORG_ADMIN", label="Org Admin", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    await db_session.flush()
    role = (await db_session.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.commit()
    return create_access_token(
        user.id_user, extra={"org": org.id_org, "roles": ["CMS_ORG_ADMIN"], "locale": "en"})


# ── Tests ──────────────────────────────────────────────────────────────────────

async def test_pop_batch_inserted_and_hashes(
    client: AsyncClient, player_token: str
) -> None:
    resp = await client.post(
        "/api/v1/pop/batch",
        json={
            "logs": [
                {"id_media": 1, "id_playlist": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 5000},
                {"id_media": 2, "played_at": "2026-06-01T10:00:05Z", "duration_ms": 3000},
            ]
        },
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["inserted"] == 2
    assert len(data["first_hash"]) == 64
    assert len(data["last_hash"]) == 64
    assert data["first_hash"] != data["last_hash"]


async def test_pop_batch_merkle_chain_continues(
    client: AsyncClient, player_token: str
) -> None:
    resp1 = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 10, "played_at": "2026-06-01T11:00:00Z", "duration_ms": 1000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp1.status_code == 200
    last_hash_batch1 = resp1.json()["last_hash"]

    resp2 = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 11, "played_at": "2026-06-01T11:00:01Z", "duration_ms": 2000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp2.status_code == 200
    # Le premier hash du 2ème batch est calculé à partir du dernier hash du 1er batch
    assert resp2.json()["first_hash"] != last_hash_batch1
    # Mais les deux valeurs de hash sont différentes (chaînage effectif)
    assert resp2.json()["first_hash"] != resp1.json()["first_hash"]


async def _seed_two_plays(client: AsyncClient, player_token: str) -> None:
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [
            {"id_media": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 5000},
            {"id_media": 2, "played_at": "2026-06-01T10:00:05Z", "duration_ms": 3000},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 200


async def test_pop_plays_list_and_player_filter(
    client: AsyncClient, player: CmsPlayer, player_token: str, admin_token: str
) -> None:
    await _seed_two_plays(client, player_token)
    # Liste paginée
    resp = await client.get(
        "/api/v1/pop/plays?from=2026-06-01&until=2026-06-01",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["rows"]) == 2
    assert data["rows"][0]["signed"] is False           # player sans clé → non signé
    assert data["rows"][0]["player_name"] == player.player_name
    # Filtre par player présent → 2 ; par un autre player → 0
    r2 = await client.get(
        f"/api/v1/pop/plays?from=2026-06-01&until=2026-06-01&id_players={player.id_player}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.json()["total"] == 2
    r3 = await client.get(
        "/api/v1/pop/plays?from=2026-06-01&until=2026-06-01&id_players=999999",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r3.json()["total"] == 0


async def test_pop_plays_media_type_filter(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, player: CmsPlayer,
    admin_token: str,
) -> None:
    """Filtre `media_type` (S1 PoP audio) : audio = paniers §XIX ; visual = le reste,
    y compris une preuve dont le média a disparu (LEFT JOIN, jamais perdue)."""
    audio = CmsMedia(id_org=org.id_org, media_name="Track", media_type="audio",
                     storage_key=f"a/{uuid.uuid4().hex}.ogg", checksum_sha256="x", transcode_status="done")
    amb = CmsMedia(id_org=org.id_org, media_name="Oiseaux", media_type="audio",
                   storage_key=f"b/{uuid.uuid4().hex}.ogg", checksum_sha256="w", transcode_status="done")
    image = CmsMedia(id_org=org.id_org, media_name="Visuel", media_type="image",
                     storage_key=f"i/{uuid.uuid4().hex}.jpg", checksum_sha256="y", transcode_status="done")
    doc = CmsMedia(id_org=org.id_org, media_name="Doc.pdf", media_type="document",
                   storage_key=f"d/{uuid.uuid4().hex}.pdf", checksum_sha256="z", transcode_status="done")
    db_session.add_all([audio, amb, image, doc])
    await db_session.flush()
    # « Oiseaux » appartient à un panier AMBIANCE → sous-famille audio_ambiance.
    basket = CmsAudioBasket(id_org=org.id_org, basket_name="Ambiance test", basket_type="ambiance")
    db_session.add(basket)
    await db_session.flush()
    db_session.add(CmsAudioBasketItem(id_basket=basket.id_basket, id_media=amb.id_media))
    when = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    for id_media in (audio.id_media, amb.id_media, image.id_media, doc.id_media, 999999):
        db_session.add(CmsProofOfPlay(id_org=org.id_org, id_player=player.id_player,
                                      id_media=id_media, played_at=when, duration_ms=1000, merkle_hash="h"))
    await db_session.commit()

    async def fetch(extra: str = "") -> dict:
        resp = await client.get(
            f"/api/v1/pop/plays?from=2026-06-01&until=2026-06-01{extra}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        return resp.json()

    got_all = await fetch()
    assert got_all["total"] == 5
    # Flag is_document (regroupement des pages côté UI) : posé sur le doc, pas sur le reste.
    flags = {r["media_name"]: r["is_document"] for r in got_all["rows"]}
    assert flags["Doc.pdf"] is True and flags["Track"] is False and flags["Visuel"] is False
    got_audio = await fetch("&media_type=audio")
    assert got_audio["total"] == 2
    assert {r["media_name"] for r in got_audio["rows"]} == {"Track", "Oiseaux"}
    # Sous-familles par appartenance aux paniers : ambiance vs le reste de l'audio.
    got_amb = await fetch("&media_type=audio_ambiance")
    assert got_amb["total"] == 1 and got_amb["rows"][0]["media_name"] == "Oiseaux"
    got_std = await fetch("&media_type=audio_standard")
    assert got_std["total"] == 1 and got_std["rows"][0]["media_name"] == "Track"
    got_visual = await fetch("&media_type=visual")
    assert got_visual["total"] == 3  # image + document + média disparu
    # CSV : mêmes filtres
    csv_resp = await client.get(
        "/api/v1/pop/plays.csv?from=2026-06-01&until=2026-06-01&media_type=audio",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert csv_resp.status_code == 200
    assert "Track" in csv_resp.text and "Visuel" not in csv_resp.text


async def test_pop_purge_player(
    client: AsyncClient, player: CmsPlayer, player_token: str, admin_token: str
) -> None:
    await _seed_two_plays(client, player_token)
    # Purge → supprime toutes les preuves du player et renvoie le compte.
    resp = await client.delete(
        f"/api/v1/pop/players/{player.id_player}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    # Après purge : plus aucune preuve.
    after = await client.get(
        "/api/v1/pop/plays?from=2026-06-01&until=2026-06-01",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert after.json()["total"] == 0
    # Player inexistant / autre tenant → 404 (jamais 403).
    r404 = await client.delete(
        "/api/v1/pop/players/999999",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r404.status_code == 404


async def test_pop_purge_requires_admin(
    client: AsyncClient, player: CmsPlayer, player_token: str
) -> None:
    # Sans token admin (token player) → refusé.
    resp = await client.delete(
        f"/api/v1/pop/players/{player.id_player}",
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code in (401, 403)


async def test_pop_plays_csv(
    client: AsyncClient, player_token: str, admin_token: str
) -> None:
    await _seed_two_plays(client, player_token)
    resp = await client.get(
        "/api/v1/pop/plays.csv?from=2026-06-01&until=2026-06-01",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "Player" in resp.text


async def test_pop_report_pdf(
    client: AsyncClient, player_token: str, admin_token: str
) -> None:
    await _seed_two_plays(client, player_token)
    resp = await client.get(
        "/api/v1/pop/report.pdf?from=2026-06-01&until=2026-06-01",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:4] == b"%PDF"


async def test_pop_plays_requires_admin(client: AsyncClient, player_token: str) -> None:
    # Token player → pas un user admin → 401/403
    resp = await client.get(
        "/api/v1/pop/plays?from=2026-06-01&until=2026-06-01",
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code in (401, 403)


async def test_pop_batch_empty_logs_rejected(
    client: AsyncClient, player_token: str
) -> None:
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": []},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 422


async def test_pop_batch_requires_player_token(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 1000}]},
    )
    assert resp.status_code in (401, 403)


async def test_pop_top_by_type(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, player: CmsPlayer,
) -> None:
    """Top diffusés par type : regroupe les PoP par media_type, top N par groupe."""
    # Admin token de l'org
    user = CmsUser(id_org=org.id_org, email=f"a-{uuid.uuid4().hex[:8]}@t.com",
                   password_hash=hash_password("Password1!"), is_active=True)
    db_session.add(user)
    await db_session.flush()
    # CMS_ORG_ADMIN requis en base (require_org_admin lit les rôles via RBAC)
    await db_session.execute(
        pg_insert(CmsRole).values(role_key="CMS_ORG_ADMIN", label="Org Admin", is_system=True)
        .on_conflict_do_nothing(constraint="uq_cms_roles_role_key")
    )
    await db_session.flush()
    role = (await db_session.execute(select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN"))).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db_session.flush()
    token = create_access_token(user.id_user, extra={"org": org.id_org, "roles": ["CMS_ORG_ADMIN"], "locale": "en"})

    # 2 médias (audio + vidéo)
    audio = CmsMedia(id_org=org.id_org, media_name="Jingle", media_type="audio",
                     storage_key=f"a/{uuid.uuid4().hex}.ogg", checksum_sha256="x", transcode_status="done")
    video = CmsMedia(id_org=org.id_org, media_name="Spot", media_type="video",
                     storage_key=f"v/{uuid.uuid4().hex}.mp4", checksum_sha256="y", transcode_status="done")
    db_session.add_all([audio, video])
    await db_session.flush()

    now = datetime.now(timezone.utc)
    # 3 diffusions audio + 1 vidéo
    for _ in range(3):
        db_session.add(CmsProofOfPlay(id_org=org.id_org, id_player=player.id_player,
                                      id_media=audio.id_media, played_at=now, duration_ms=1000, merkle_hash="h"))
    db_session.add(CmsProofOfPlay(id_org=org.id_org, id_player=player.id_player,
                                  id_media=video.id_media, played_at=now, duration_ms=2000, merkle_hash="h"))
    await db_session.commit()

    resp = await client.get("/api/v1/pop/top-by-type?days=30&limit=3",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    groups = {g["media_type"]: g for g in resp.json()["groups"]}
    assert groups["audio"]["items"][0]["name"] == "Jingle"
    assert groups["audio"]["items"][0]["count"] == 3
    assert groups["video"]["items"][0]["name"] == "Spot"


# ── Durcissement token player (audit 2026-06-24) ─────────────────────────────────

async def test_player_token_revoked_on_mismatch(
    client: AsyncClient, player: CmsPlayer, player_token: str, db_session: AsyncSession
) -> None:
    """Révocation : si le token stocké sur le player diffère du token présenté
    (rotation/re-provision), l'ancien token est refusé (401)."""
    await db_session.execute(
        update(CmsPlayer)
        .where(CmsPlayer.id_player == player.id_player)
        .values(player_token="rotated-fresh-token-value")
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 1000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 401


async def test_player_token_prev_accepted_within_grace(
    client: AsyncClient, player: CmsPlayer, player_token: str, db_session: AsyncSession
) -> None:
    """Rotation « Option B » : après rotation, l'ANCIEN token reste accepté tant que la fenêtre
    de grâce (player_token_prev_until) n'est pas dépassée — couvre un push token:refresh raté."""
    from datetime import timedelta
    await db_session.execute(
        update(CmsPlayer)
        .where(CmsPlayer.id_player == player.id_player)
        .values(
            player_token="rotated-new-current-token",
            player_token_prev=player_token,
            player_token_prev_until=datetime.now(timezone.utc) + timedelta(days=1),
        )
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 1000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 200


async def test_player_token_prev_rejected_after_grace(
    client: AsyncClient, player: CmsPlayer, player_token: str, db_session: AsyncSession
) -> None:
    """La fenêtre de grâce expirée : l'ancien token n'est plus accepté (401)."""
    from datetime import timedelta
    await db_session.execute(
        update(CmsPlayer)
        .where(CmsPlayer.id_player == player.id_player)
        .values(
            player_token="rotated-new-current-token",
            player_token_prev=player_token,
            player_token_prev_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 1000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 401


async def test_player_blocked_when_org_suspended(
    client: AsyncClient, org: CmsOrganization, player: CmsPlayer, player_token: str,
    db_session: AsyncSession,
) -> None:
    """Un player d'une organisation suspendue ne peut plus s'authentifier (401)."""
    await db_session.execute(
        update(CmsOrganization).where(CmsOrganization.id_org == org.id_org).values(is_active=False)
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 1, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 1000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 401


# ── P2 signature côté player + P1 vérification / certificat (§XXIV) ───────────────

def _ed25519():
    """(clé privée, PEM public) pour signer des preuves dans les tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pem


def _sign(priv, id_player, id_media, id_playlist, played_at_iso, duration_ms):
    import base64

    from app.services.pop_proof import canonical_message, played_at_ms
    dt = datetime.fromisoformat(played_at_iso.replace("Z", "+00:00"))
    msg = canonical_message(id_player, id_media, id_playlist, played_at_ms(dt), duration_ms)
    return base64.b64encode(priv.sign(msg)).decode()


async def test_pop_signed_batch_registers_key_and_verifies(
    client: AsyncClient, player: CmsPlayer, player_token: str, admin_token: str,
    db_session: AsyncSession,
) -> None:
    """Un lot SIGNÉ : enregistre la clé publique (TOFU), stocke la signature ; /pop/verify confirme."""
    priv, pem = _ed25519()
    played = "2026-06-01T10:00:00Z"
    sig = _sign(priv, player.id_player, 5, 7, played, 4000)
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"pop_public_key": pem, "logs": [
            {"id_media": 5, "id_playlist": 7, "played_at": played, "duration_ms": 4000, "signature": sig},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signed"] == 1
    # Clé publique enregistrée sur le player (TOFU)
    await db_session.refresh(player)
    assert player.pop_public_key == pem
    # /pop/verify : chaîne valide + 1 preuve signée
    v = await client.get("/api/v1/pop/verify?days=365",
                         headers={"Authorization": f"Bearer {admin_token}"})
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["chain_valid"] is True
    assert body["signed"] == 1 and body["signature_invalid"] == 0
    assert body["merkle_root"] is not None


async def test_pop_invalid_signature_rejected(
    client: AsyncClient, player: CmsPlayer, player_token: str,
) -> None:
    """Une signature qui ne correspond pas au message → lot refusé (400, anti-altération)."""
    _, pem = _ed25519()
    other_priv, _ = _ed25519()  # signe avec une AUTRE clé que celle déclarée
    played = "2026-06-01T10:00:00Z"
    bad_sig = _sign(other_priv, player.id_player, 5, 7, played, 4000)
    resp = await client.post(
        "/api/v1/pop/batch",
        json={"pop_public_key": pem, "logs": [
            {"id_media": 5, "id_playlist": 7, "played_at": played, "duration_ms": 4000, "signature": bad_sig},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert resp.status_code == 400


async def test_pop_key_rotation_self_heals(
    client: AsyncClient, player: CmsPlayer, player_token: str, admin_token: str,
    db_session: AsyncSession,
) -> None:
    """P0.1 self-heal : un wipe storage device régénère le seed → nouvelle clé. Un lot signé par
    la NOUVELLE clé (qui prouve la possession de la privée) fait tourner la clé enregistrée au
    lieu de rejeter silencieusement le PoP (avant : 400 jusqu'au reset manuel)."""
    from app.models.audit_log import CmsAuditLog
    # 1er enrôlement TOFU avec la clé A.
    priv_a, pem_a = _ed25519()
    played_a = "2026-06-01T10:00:00Z"
    sig_a = _sign(priv_a, player.id_player, 5, 7, played_a, 4000)
    r1 = await client.post(
        "/api/v1/pop/batch",
        json={"pop_public_key": pem_a, "logs": [
            {"id_media": 5, "id_playlist": 7, "played_at": played_a, "duration_ms": 4000, "signature": sig_a},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert r1.status_code == 200, r1.text

    # Le seed change (wipe) → clé B. Le lot est signé par B et joint B.
    priv_b, pem_b = _ed25519()
    played_b = "2026-06-01T10:05:00Z"
    sig_b = _sign(priv_b, player.id_player, 8, 7, played_b, 3000)
    r2 = await client.post(
        "/api/v1/pop/batch",
        json={"pop_public_key": pem_b, "logs": [
            {"id_media": 8, "id_playlist": 7, "played_at": played_b, "duration_ms": 3000, "signature": sig_b},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert r2.status_code == 200, r2.text          # accepté (avant : 400)
    assert r2.json()["signed"] == 1
    # La clé enregistrée a tourné vers B + horodatage posé.
    await db_session.refresh(player)
    assert player.pop_public_key == pem_b
    assert player.pop_key_rotated_at is not None
    # Trace d'audit de la rotation.
    audit = (await db_session.execute(
        select(CmsAuditLog).where(
            CmsAuditLog.resource_type == "player",
            CmsAuditLog.action == "pop_key_rotated",
            CmsAuditLog.resource_id == str(player.id_player),
        )
    )).scalars().all()
    assert len(audit) == 1
    # /pop/verify : les deux preuves restent valides (chaîne + signatures contre la clé courante
    # pour la 2e ; la 1re, signée par A désormais déréférencée, compte comme signature invalide).
    v = await client.get("/api/v1/pop/verify?days=365",
                         headers={"Authorization": f"Bearer {admin_token}"})
    assert v.status_code == 200, v.text
    assert v.json()["chain_valid"] is True


async def test_pop_key_rotation_rejected_without_proof_of_possession(
    client: AsyncClient, player: CmsPlayer, player_token: str, db_session: AsyncSession,
) -> None:
    """Une clé entrante DIFFÉRENTE qui ne signe PAS le lot (signatures faites par une 3e clé) ne
    fait PAS tourner la clé : pas de preuve de possession → lot rejeté (400), clé inchangée. Ferme
    le contournement « je déclare une nouvelle clé pour faire passer des preuves forgées »."""
    # Enrôle A.
    priv_a, pem_a = _ed25519()
    sig_a = _sign(priv_a, player.id_player, 5, 7, "2026-06-01T10:00:00Z", 4000)
    r1 = await client.post(
        "/api/v1/pop/batch",
        json={"pop_public_key": pem_a, "logs": [
            {"id_media": 5, "id_playlist": 7, "played_at": "2026-06-01T10:00:00Z", "duration_ms": 4000, "signature": sig_a},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert r1.status_code == 200
    # Déclare B mais signe avec une 3e clé C (≠ B) → B ne prouve rien.
    _, pem_b = _ed25519()
    priv_c, _ = _ed25519()
    sig_c = _sign(priv_c, player.id_player, 8, 7, "2026-06-01T10:05:00Z", 3000)
    r2 = await client.post(
        "/api/v1/pop/batch",
        json={"pop_public_key": pem_b, "logs": [
            {"id_media": 8, "id_playlist": 7, "played_at": "2026-06-01T10:05:00Z", "duration_ms": 3000, "signature": sig_c},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert r2.status_code == 400
    await db_session.refresh(player)
    assert player.pop_public_key == pem_a          # clé inchangée
    assert player.pop_key_rotated_at is None


async def test_pop_chain_stays_valid_with_out_of_order_played_at(
    client: AsyncClient, player: CmsPlayer, player_token: str, admin_token: str,
) -> None:
    """Régression : multi-zones / offline flush → la ligne au played_at max n'est PAS la ligne au
    id_pop max. La chaîne est semée et vérifiée par id_pop (ordre d'insertion), jamais par
    played_at, sinon le 2e lot se chaîne sur le mauvais prédécesseur → faux « chaîne altérée »."""
    # Lot 1 : 2 preuves dans le MÊME lot, la 2e (id_pop plus grand) a un played_at ANTÉRIEUR.
    r1 = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [
            {"id_media": 5, "played_at": "2026-06-01T10:00:02Z", "duration_ms": 2000},
            {"id_media": 6, "played_at": "2026-06-01T10:00:01Z", "duration_ms": 2000},
        ]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert r1.status_code == 200, r1.text
    # Lot 2 : une preuve postérieure → doit se chaîner sur le DERNIER inséré (id_pop), pas sur le
    # played_at max du lot 1.
    r2 = await client.post(
        "/api/v1/pop/batch",
        json={"logs": [{"id_media": 7, "played_at": "2026-06-01T10:00:03Z", "duration_ms": 2000}]},
        headers={"Authorization": f"Bearer {player_token}"},
    )
    assert r2.status_code == 200, r2.text
    v = await client.get("/api/v1/pop/verify?days=365",
                         headers={"Authorization": f"Bearer {admin_token}"})
    assert v.status_code == 200, v.text
    assert v.json()["chain_valid"] is True


async def test_pop_certificate(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
    player: CmsPlayer, admin_token: str,
) -> None:
    """Attestation : Merkle root + total sur une période."""
    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(CmsProofOfPlay(
            id_org=org.id_org, id_player=player.id_player, id_media=1,
            played_at=now, duration_ms=1000, merkle_hash=f"{i:064x}"))
    await db_session.commit()
    frm = now.date().isoformat()
    until = now.date().isoformat()
    resp = await client.get(
        f"/api/v1/pop/certificate?from={frm}&until={until}",
        headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_plays"] == 3
    assert body["merkle_root"] is not None and len(body["merkle_root"]) == 64
    assert body["players"][0]["plays"] == 3


def test_merkle_root_unit():
    from app.services.pop_proof import merkle_root
    assert merkle_root([]) is None
    one = merkle_root(["aa" * 32])
    assert one is not None and len(one) == 64
    # déterministe + sensible à l'ordre/au contenu
    assert merkle_root(["11" * 32, "22" * 32]) != merkle_root(["22" * 32, "11" * 32])


# ── Archives (froid MinIO) ──────────────────────────────────────────────────────

async def _add_archive(db_session, id_org, day, *, row_count=2, merkle_root="ab" * 32,
                       csv_key=None, pdf_key="k.pdf", players=None):
    from datetime import date, timedelta
    d = date.fromisoformat(day)
    a = CmsPopArchive(
        id_org=id_org, period_start=d, period_end=d + timedelta(days=1),
        row_count=row_count, merkle_root=merkle_root,
        csv_key=csv_key or f"pop-archives/{id_org}/{day}.csv.gz",
        pdf_key=pdf_key, bytes_csv=1234, players=players or [{"player_name": "NUC", "plays": row_count}],
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


async def test_archives_list_and_isolation(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, admin_token: str,
) -> None:
    await _add_archive(db_session, org.id_org, "2026-06-25")
    await _add_archive(db_session, org.id_org, "2026-06-26")
    # archive d'une AUTRE org → ne doit pas apparaître
    other = CmsOrganization(org_name="Other", org_slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    await _add_archive(db_session, other.id_org, "2026-06-25")

    resp = await client.get("/api/v1/pop/archives", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    # trié par période décroissante
    assert body["rows"][0]["period_start"] == "2026-06-26"
    assert body["rows"][0]["has_pdf"] is True


async def test_archive_download_csv_and_pdf_404(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, admin_token: str,
) -> None:
    a = await _add_archive(db_session, org.id_org, "2026-06-25")
    r = await client.get(f"/api/v1/pop/archives/{a.id_archive}/download?kind=csv",
                         headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["url"].startswith("http")

    a2 = await _add_archive(db_session, org.id_org, "2026-06-26", pdf_key=None)
    r2 = await client.get(f"/api/v1/pop/archives/{a2.id_archive}/download?kind=pdf",
                          headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.status_code == 404  # PDF non archivé

    # archive inexistante / autre org → 404
    r3 = await client.get("/api/v1/pop/archives/999999/download",
                          headers={"Authorization": f"Bearer {admin_token}"})
    assert r3.status_code == 404


async def test_archive_verify(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, admin_token: str,
    monkeypatch,
) -> None:
    from datetime import datetime as _dtcls
    # Les lignes portent TOUTES les colonnes de `pop_archive.ARCHIVE_COLUMNS`, comme le
    # worker les lit. Les omettre reproduisait le défaut qu'on vient de corriger : le CSV
    # archivait alors « non recouvert » sans que rien ne le signale.
    rows = [
        {"id_pop": 1, "id_player": 5, "id_playlist": None, "id_media": 10,
         "played_at": _dtcls(2026, 6, 25, 8, tzinfo=timezone.utc), "duration_ms": 1000,
         "merkle_hash": "a" * 64, "signature": None, "screen_off": False, "covered": False},
        {"id_pop": 2, "id_player": 5, "id_playlist": None, "id_media": 11,
         "played_at": _dtcls(2026, 6, 25, 9, tzinfo=timezone.utc), "duration_ms": 1000,
         "merkle_hash": "b" * 64, "signature": None, "screen_off": False, "covered": False},
    ]
    gz = pop_archive.build_csv_gz(rows)
    good_root = pop_archive.merkle_root_from_csv_gz(gz)[0]

    import app.api.v1.endpoints.pop as pop_ep

    async def _fake_download(key):
        return gz
    monkeypatch.setattr(pop_ep, "download_file", _fake_download)

    ok_arc = await _add_archive(db_session, org.id_org, "2026-06-25",
                                row_count=2, merkle_root=good_root)
    r = await client.post(f"/api/v1/pop/archives/{ok_arc.id_archive}/verify",
                          headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    bad_arc = await _add_archive(db_session, org.id_org, "2026-06-26",
                                 row_count=2, merkle_root="deadbeef" * 8)
    r2 = await client.post(f"/api/v1/pop/archives/{bad_arc.id_archive}/verify",
                           headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.json()["ok"] is False


async def test_verify_reports_chain_from_when_archived(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization,
    player: CmsPlayer, player_token: str, admin_token: str,
) -> None:
    await _seed_two_plays(client, player_token)
    await _add_archive(db_session, org.id_org, "2026-06-25")

    resp = await client.get("/api/v1/pop/verify?days=365",
                            headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    # period_end de l'archive = début de la fenêtre chaude vérifiable
    assert resp.json()["chain_from"] == "2026-06-26"


# ── Langue des documents (profil du demandeur, défaut anglais) ─────────────────

def _uncompressed_pdf(monkeypatch) -> None:
    """Désactive la compression des flux reportlab : le texte devient lisible dans les octets."""
    from reportlab import rl_config
    monkeypatch.setattr(rl_config, "pageCompression", 0)


_PDF_REPORT = {
    "org_name": "Org", "from_date": "2026-06-01", "until_date": "2026-06-01",
    "generated_at": "2026-06-02 10:00 UTC", "total_plays": 3, "total_screen_off": 1,
    "total_covered": 1, "merkle_root": "ab" * 32, "scope_label": "S",
    "players": [{"player_name": "P1", "plays": 3, "duration_sec": 30, "screen_off": 1,
                 "covered": 1, "signed": 2, "unsigned": 1, "signature_invalid": 0,
                 "chain_valid": True, "chain_head": "cd" * 32, "merkle_root": "ef" * 32}],
}


def test_pop_report_pdf_locale_default_english_and_german(monkeypatch) -> None:
    from app.services.pop_report import build_pop_report_pdf
    _uncompressed_pdf(monkeypatch)
    en = build_pop_report_pdf(_PDF_REPORT)
    assert b"Total plays" in en and b"Chain valid" in en and b"Total diffusions" not in en
    de = build_pop_report_pdf(_PDF_REPORT, locale="de")
    assert b"Wiedergaben gesamt" in de and b"Kette g" in de and b"Total plays" not in de
    fr = build_pop_report_pdf(_PDF_REPORT, locale="fr-FR")
    assert b"Total diffusions" in fr


def test_pop_doc_texts_parity_and_archive_label() -> None:
    from app.services import doc_i18n
    from app.services.pop_report import DOC_TEXTS, csv_header
    keys = set(DOC_TEXTS["en"])
    for loc in doc_i18n.SUPPORTED_LOCALES:
        assert set(DOC_TEXTS[loc]) == keys, loc
        header = csv_header(loc)
        assert len(header) == 8
        assert all(h.isascii() and " " not in h for h in header), loc
    # Archive journalière (worker, aucun utilisateur) → anglais.
    assert doc_i18n.tr(DOC_TEXTS, None, "scope_daily_archive") == "Proof of Play archive (daily)"


async def _set_org_users_locale(db_session: AsyncSession, id_org: int, locale: str) -> None:
    await db_session.execute(update(CmsUser).where(CmsUser.id_org == id_org).values(locale=locale))
    await db_session.commit()


async def test_pop_plays_csv_follows_profile_locale(
    client: AsyncClient, player_token: str, admin_token: str,
    db_session: AsyncSession, org: CmsOrganization,
) -> None:
    await _seed_two_plays(client, player_token)
    url = "/api/v1/pop/plays.csv?from=2026-06-01&until=2026-06-01"
    h = {"Authorization": f"Bearer {admin_token}"}
    en = await client.get(url, headers=h)
    assert en.status_code == 200
    first = en.text.splitlines()[0]
    assert "Played_at_UTC" in first and "Diffuse_le_UTC" not in first
    assert ",no," in en.text or ",yes," in en.text

    await _set_org_users_locale(db_session, org.id_org, "fr")
    fr = await client.get(url, headers=h)
    assert "Diffuse_le_UTC" in fr.text.splitlines()[0]
    assert ",non," in fr.text or ",oui," in fr.text


async def test_pop_report_pdf_follows_profile_locale(
    client: AsyncClient, player_token: str, admin_token: str,
    db_session: AsyncSession, org: CmsOrganization, monkeypatch,
) -> None:
    await _seed_two_plays(client, player_token)
    _uncompressed_pdf(monkeypatch)
    url = "/api/v1/pop/report.pdf?from=2026-06-01&until=2026-06-01"
    h = {"Authorization": f"Bearer {admin_token}"}
    en = await client.get(url, headers=h)
    assert en.status_code == 200
    assert b"All players of the organization" in en.content
    await _set_org_users_locale(db_session, org.id_org, "de")
    de = await client.get(url, headers=h)
    assert b"Alle Player der Organisation" in de.content
    assert b"All players of the organization" not in de.content
