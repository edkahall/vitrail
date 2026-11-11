"""Fournisseurs de génération : dire ce qu'ils répondent, n'offrir que ce qu'ils accepteront.

Origine : la comparaison avec le projet jumeau de génération de contenu IA (2026-08-04,
`docs/RAPPORT_REPRISES_IA_GENERATION.md` §1). Trois défauts en production, qui se cumulent
sur le même geste — un client BYOK qui demande une voix off :

  ① l'erreur du fournisseur était affichée comme une PANNE RÉSEAU. `except httpx.HTTPError`
     attrape aussi `HTTPStatusError`, donc un 402 « Free users cannot use library voices via
     the API » devenait « erreur réseau ElevenLabs : HTTPStatusError » — la mauvaise cause,
     et la seule phrase qui disait quoi faire, jetée ;
  ② les voix proposées étaient une liste ÉCRITE EN DUR, juste pour le compte contre lequel
     elle a été écrite. Pire : le 2026-07-17 on a mesuré que les anciennes premade
     (Bella/Elli/Antoni/Josh/Adam) n'existent plus, et on n'a corrigé que la liste fal ;
  ③ rien ne lisait le quota AVANT l'appel, alors que `/v1/user` est déjà interrogé pour
     valider la clé.

Les tests pures n'ouvrent aucune socket : `httpx.Client` est remplacé par un double qui rend
la réponse voulue.
"""

import uuid
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import CmsOrganization, CmsOrgAuthSettings
from app.models.role import (
    CmsPermission,
    CmsRole,
    CmsRolePermission,
    CmsUserRoleAssignment,
)
from app.models.user import CmsUser
from app.services.generation import GenerationError, catalog, elevenlabs

# La phrase exacte renvoyée par ElevenLabs, et qui n'atteignait jamais l'écran.
_LIBRARY_VOICE_REFUSAL = "Free users cannot use library voices via the API"


# ── Doubles httpx (aucune socket) ─────────────────────────────────────────────

class _FakeClient:
    """Remplace `httpx.Client` : rend une réponse préparée, quelle que soit la requête."""

    def __init__(self, response: httpx.Response):
        self._response = response

    def __call__(self, *args, **kwargs):   # httpx.Client(timeout=...) → self
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return self._response

    def get(self, *args, **kwargs):
        return self._response


def _response(status_code: int, *, json_body=None, content: bytes = b"") -> httpx.Response:
    return httpx.Response(
        status_code,
        json=json_body,
        request=httpx.Request("GET", "https://api.elevenlabs.io/v1/x"),
    ) if json_body is not None else httpx.Response(
        status_code,
        content=content,
        request=httpx.Request("GET", "https://api.elevenlabs.io/v1/x"),
    )


# ── ① L'erreur du fournisseur arrive telle quelle ─────────────────────────────

def test_provider_refusal_carries_the_status_and_the_providers_own_sentence():
    from app.services.generation import raise_for_provider

    resp = _response(402, json_body={"detail": {"message": _LIBRARY_VOICE_REFUSAL}})
    with pytest.raises(GenerationError) as exc:
        raise_for_provider("ElevenLabs", resp)
    message = str(exc.value)
    assert "402" in message
    assert _LIBRARY_VOICE_REFUSAL in message


def test_provider_refusal_on_401_names_the_key_without_losing_the_explanation():
    from app.services.generation import raise_for_provider

    resp = _response(401, json_body={"detail": "Invalid API key"})
    with pytest.raises(GenerationError) as exc:
        raise_for_provider("ElevenLabs", resp)
    assert "key rejected" in str(exc.value) and "Invalid API key" in str(exc.value)


def test_provider_refusal_says_nothing_on_success():
    from app.services.generation import raise_for_provider

    raise_for_provider("ElevenLabs", _response(200, content=b"ok"))  # ne lève pas


def test_provider_refusal_falls_back_to_the_body_when_the_shape_is_unknown():
    from app.services.generation import raise_for_provider

    resp = _response(500, content=b"<html>Bad gateway</html>")
    with pytest.raises(GenerationError) as exc:
        raise_for_provider("ElevenLabs", resp)
    assert "500" in str(exc.value) and "Bad gateway" in str(exc.value)


def test_network_error_names_only_the_transport():
    from app.services.generation import network_error

    err = network_error("ElevenLabs", httpx.ConnectTimeout("timed out"))
    assert "network" in str(err) and "ConnectTimeout" in str(err)


# ── Le défaut tel qu'un client le vivait ──────────────────────────────────────

def test_a_402_on_a_voice_is_not_reported_as_a_network_failure(monkeypatch):
    """Le test qui compte : c'est ce chemin exact qui mentait en production."""
    resp = _response(402, json_body={"detail": {"message": _LIBRARY_VOICE_REFUSAL}})
    monkeypatch.setattr(elevenlabs.httpx, "Client", _FakeClient(resp))

    with pytest.raises(GenerationError) as exc:
        elevenlabs.generate_voice(text="Bonjour", api_key="sk-test")

    message = str(exc.value)
    assert _LIBRARY_VOICE_REFUSAL in message
    assert "402" in message
    assert "network" not in message.lower()          # ⛔ la mauvaise cause
    assert "HTTPStatusError" not in message         # ⛔ le nom de la classe au lieu du motif


def test_a_refusal_on_music_is_not_reported_as_a_network_failure_either(monkeypatch):
    resp = _response(422, json_body={"detail": {"message": "prompt too long"}})
    monkeypatch.setattr(elevenlabs.httpx, "Client", _FakeClient(resp))

    with pytest.raises(GenerationError) as exc:
        elevenlabs.generate_music(prompt="x" * 10, api_key="sk-test")
    assert "prompt too long" in str(exc.value) and "network" not in str(exc.value).lower()


def test_a_real_transport_failure_is_still_called_a_network_error(monkeypatch):
    class _Boom(_FakeClient):
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(elevenlabs.httpx, "Client", _Boom(_response(200)))
    with pytest.raises(GenerationError) as exc:
        elevenlabs.generate_voice(text="Bonjour", api_key="sk-test")
    assert "network" in str(exc.value)


def test_music_does_not_forward_empty_parameters(monkeypatch):
    """Un champ vide envoyé au fournisseur, c'est un refus qu'on s'inflige."""
    seen: dict = {}

    class _Capture(_FakeClient):
        def post(self, url, headers=None, json=None, **kwargs):
            seen.update(json or {})
            return self._response

    monkeypatch.setattr(elevenlabs.httpx, "Client", _Capture(_response(200, content=b"AUDIO")))
    elevenlabs.generate_music(
        prompt="jazz", api_key="sk-test", params={"lyrics": "", "music_length_ms": 30000}
    )
    assert "lyrics" not in seen
    assert seen["music_length_ms"] == 30000


# ── ② Les voix du COMPTE, pas une liste écrite en dur ─────────────────────────

def test_account_voices_replace_the_catalogue_options_and_the_default():
    models = catalog.models_for(kind="voice", providers={"elevenlabs"})
    voices = [
        {"voice_id": "acc-1", "name": "Chloé", "category": "cloned"},
        {"voice_id": "acc-2", "name": "Marc", "category": ""},
    ]
    out = catalog.with_account_voices(models, voices)
    param = _voice_param(out, "elevenlabs/tts")
    assert [o["value"] for o in param["options"]] == ["acc-1", "acc-2"]
    assert param["default"] == "acc-1"          # le défaut du catalogue peut ne pas exister ici
    assert param["options"][0]["label_literal"] == "Chloé (cloned)"
    assert param["options"][1]["label_literal"] == "Marc"


def test_an_unreadable_voice_list_leaves_the_catalogue_alone():
    """Une panne du fournisseur ne doit jamais coûter son formulaire à l'opérateur."""
    models = catalog.models_for(kind="voice", providers={"elevenlabs"})
    before = _voice_param(models, "elevenlabs/tts")["options"]
    after = _voice_param(catalog.with_account_voices(models, []), "elevenlabs/tts")["options"]
    assert after == before


def test_account_voices_do_not_touch_the_other_providers():
    models = catalog.models_for(kind="voice", providers={"fal", "elevenlabs"})
    fal_before = _voice_param(models, "fal-ai/elevenlabs/tts/eleven-v3")["options"]
    out = catalog.with_account_voices(models, [{"voice_id": "acc-1", "name": "Chloé"}])
    assert _voice_param(out, "fal-ai/elevenlabs/tts/eleven-v3")["options"] == fal_before


def test_the_static_fallback_no_longer_ships_voices_measured_dead():
    """Le 2026-07-17 on a mesuré que ces premade n'existent plus — et on n'avait corrigé
    que la liste fal. Les proposer, c'est promettre un refus au moment de la génération."""
    dead = {
        "EXAVITQu4vr4xnSDxMaL",   # Bella
        "MF3mGyEYCl7XYWbV9V6O",   # Elli
        "ErXwobaYiN019PkySvjV",   # Antoni
        "TxGEqnHWrfWFTfGW9XjX",   # Josh
        "pNInz6obpgDQGcFmaJgB",   # Adam
    }
    models = catalog.models_for(kind="voice", providers={"elevenlabs"})
    offered = {o["value"] for o in _voice_param(models, "elevenlabs/tts")["options"]}
    assert not (offered & dead)
    assert elevenlabs.DEFAULT_VOICE_ID in offered   # il reste au moins un repli utilisable


def test_the_default_voice_comes_from_the_account(monkeypatch):
    """Mesuré en production le 2026-08-04 : `DEFAULT_VOICE_ID` n'est pas dans les 21 voix
    du compte, et l'offre y est `free` — donc tout appel sans voix explicite partait sur
    une voix de bibliothèque refusée. C'est ce qui mettait la réponse vocale de Jeanine en
    panne silencieuse (elle n'en précise aucune)."""
    monkeypatch.setattr(
        elevenlabs, "list_voices",
        lambda **kw: [{"voice_id": "acc-1", "name": "Sarah", "category": "premade"}],
    )
    assert elevenlabs.resolve_default_voice("sk-test") == "acc-1"


@pytest.mark.parametrize("outcome", ["empty", "boom"])
def test_an_unreadable_account_falls_back_rather_than_staying_silent(monkeypatch, outcome):
    """Mieux vaut tenter une voix qui peut être refusée que ne pas parler du tout."""
    def _voices(**kw):
        if outcome == "boom":
            raise GenerationError("ElevenLabs a répondu 500")
        return []

    monkeypatch.setattr(elevenlabs, "list_voices", _voices)
    assert elevenlabs.resolve_default_voice("sk-test") == elevenlabs.DEFAULT_VOICE_ID


def test_an_explicit_voice_is_never_second_guessed(monkeypatch):
    """Une voix choisie à l'écran ne doit pas déclencher d'aller-retour supplémentaire."""
    def _boom(**kw):
        raise AssertionError("la liste du compte ne doit pas être consultée")

    monkeypatch.setattr(elevenlabs, "list_voices", _boom)
    monkeypatch.setattr(elevenlabs.httpx, "Client", _FakeClient(_response(200, content=b"AUDIO")))
    content, _ctype, _cost = elevenlabs.generate_voice(
        text="Bonjour", api_key="sk-test", voice_id="chosen-1"
    )
    assert content == b"AUDIO"


def test_list_voices_keeps_only_usable_entries(monkeypatch):
    body = {"voices": [
        {"voice_id": "v1", "name": "Chloé", "category": "premade"},
        {"name": "sans identifiant"},          # inexploitable → écartée
        {"voice_id": "v2"},                    # sans nom → le nom retombe sur l'identifiant
    ]}
    monkeypatch.setattr(elevenlabs.httpx, "Client", _FakeClient(_response(200, json_body=body)))
    voices = elevenlabs.list_voices(api_key="sk-test")
    assert [v["voice_id"] for v in voices] == ["v1", "v2"]
    assert voices[1]["name"] == "v2"


# ── ③ Le quota, lu en direct, jamais stocké ───────────────────────────────────

def test_quota_reads_the_plan_and_what_is_left():
    quota = elevenlabs.read_quota({"subscription": {
        "tier": "starter", "character_count": 9_000, "character_limit": 30_000,
        "next_character_count_reset_unix": 1_760_000_000,
    }})
    assert quota is not None
    assert (quota.tier, quota.used, quota.limit, quota.remaining) == ("starter", 9000, 30000, 21000)
    assert quota.resets_at == datetime.fromtimestamp(1_760_000_000, tz=timezone.utc)


@pytest.mark.parametrize("body", [
    {},                                                   # pas d'abonnement
    {"subscription": {}},                                 # abonnement muet
    {"subscription": {"character_limit": 0, "character_count": 0}},   # plafond absurde
    {"subscription": {"character_limit": "30000", "character_count": 0}},  # types inattendus
])
def test_an_unreadable_quota_is_unknown_and_never_a_refusal(body):
    """`None` veut dire « je ne sais pas », jamais « il ne reste rien ». Seul un nombre
    qu'on tient réellement peut justifier un refus."""
    assert elevenlabs.read_quota(body) is None


def test_remaining_never_goes_negative():
    quota = elevenlabs.read_quota({"subscription": {
        "tier": "free", "character_count": 40_000, "character_limit": 10_000}})
    assert quota is not None and quota.remaining == 0


# ── Le refus se prend AVANT l'appel au fournisseur ────────────────────────────

@pytest_asyncio.fixture
async def org(db_session: AsyncSession) -> CmsOrganization:
    o = CmsOrganization(org_name="Gen", org_slug=f"gen-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    await db_session.flush()
    db_session.add(CmsOrgAuthSettings(id_org=o.id_org))
    await db_session.commit()
    await db_session.refresh(o)
    return o


async def _mk_user(db: AsyncSession, org: CmsOrganization) -> CmsUser:
    uid = uuid.uuid4().hex[:8]
    user = CmsUser(
        id_org=org.id_org, email=f"g-{uid}@test.com", full_name="Gen User",
        password_hash=hash_password("Password1!"), is_active=True,
    )
    db.add(user)
    await db.flush()
    role = CmsRole(id_org=org.id_org, role_key=f"CMS_G_{uid}", label="G", is_system=False)
    db.add(role)
    await db.flush()
    await db.execute(
        pg_insert(CmsPermission)
        .values(module="generation", operation="create", label="generation.create")
        .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
    )
    perm = (await db.execute(
        pg_insert(CmsPermission)
        .values(module="generation", operation="create", label="generation.create")
        .on_conflict_do_nothing(constraint="uq_cms_permissions_module_operation")
        .returning(CmsPermission.id_permission)
    )).scalar_one_or_none()
    if perm is None:
        from sqlalchemy import select as _select
        perm = (await db.execute(
            _select(CmsPermission.id_permission).where(
                CmsPermission.module == "generation", CmsPermission.operation == "create")
        )).scalar_one()
    db.add(CmsRolePermission(id_role=role.id_role, id_permission=perm))
    db.add(CmsUserRoleAssignment(id_user=user.id_user, id_role=role.id_role))
    await db.commit()
    await db.refresh(user)
    return user


def _auth(user: CmsUser) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id_user)}"}


def _fake_key(monkeypatch):
    from app.services.ai_credentials import ResolvedKey

    async def _resolved(db, org, provider):
        return ResolvedKey(api_key="sk-test", byok=True, source="org")

    monkeypatch.setattr(
        "app.api.v1.endpoints.generation.resolve_ai_credentials", _resolved
    )


@pytest.mark.asyncio
async def test_a_voice_over_beyond_the_plan_is_refused_before_the_provider_is_called(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, monkeypatch
):
    user = await _mk_user(db_session, org)
    _fake_key(monkeypatch)
    monkeypatch.setattr(
        elevenlabs, "get_quota",
        lambda **kw: elevenlabs.Quota(tier="free", used=9_900, limit=10_000, resets_at=None),
    )
    resp = await client.post(
        "/api/v1/generation/audio",
        json={"kind": "voice", "provider": "elevenlabs", "prompt": "x" * 500},
        headers=_auth(user),
    )
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert "500" in detail and "100" in detail        # demandé / restant, chiffrés
    assert "free" in detail.lower()


@pytest.mark.asyncio
async def test_an_unreadable_quota_never_blocks_the_work(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, monkeypatch
):
    """Une hoquet du fournisseur ne doit pas arrêter un travail qui serait passé."""
    user = await _mk_user(db_session, org)
    _fake_key(monkeypatch)
    monkeypatch.setattr(elevenlabs, "get_quota", lambda **kw: None)
    monkeypatch.setattr(
        "app.workers.generation_worker.generate_content.delay", lambda *a, **k: None
    )
    resp = await client.post(
        "/api/v1/generation/audio",
        json={"kind": "voice", "provider": "elevenlabs", "prompt": "Bonjour"},
        headers=_auth(user),
    )
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_music_is_not_measured_against_a_character_quota(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization, monkeypatch
):
    """ElevenLabs facture la voix en caractères du texte — la musique dans une autre unité.
    Deviner la conversion produirait des refus sur lesquels personne ne peut agir."""
    user = await _mk_user(db_session, org)
    _fake_key(monkeypatch)

    def _boom(**kw):
        raise AssertionError("le quota ne doit pas être consulté pour de la musique")

    monkeypatch.setattr(elevenlabs, "get_quota", _boom)
    monkeypatch.setattr(
        "app.workers.generation_worker.generate_content.delay", lambda *a, **k: None
    )
    resp = await client.post(
        "/api/v1/generation/audio",
        json={"kind": "music", "provider": "elevenlabs", "prompt": "jazz doux"},
        headers=_auth(user),
    )
    assert resp.status_code == 202


# ── L'écran des clés doit pouvoir montrer l'offre ─────────────────────────────

@pytest.mark.asyncio
async def test_the_plan_route_says_so_when_a_provider_has_nothing_to_show(
    client: AsyncClient, db_session: AsyncSession, org: CmsOrganization
):
    """fal est à l'usage, Anthropic n'expose aucun quota : le dire vaut mieux que feindre
    d'être générique."""
    from sqlalchemy import select

    admin = CmsUser(
        id_org=org.id_org, email=f"a-{uuid.uuid4().hex[:8]}@test.com", full_name="Admin",
        password_hash=hash_password("Password1!"), is_active=True,
    )
    db_session.add(admin)
    await db_session.flush()
    await db_session.execute(
        pg_insert(CmsRole)
        .values(id_org=org.id_org, role_key="CMS_ORG_ADMIN", label="Org Admin", is_system=True)
        .on_conflict_do_nothing(index_elements=["role_key"])
    )
    await db_session.flush()
    role = (await db_session.execute(
        select(CmsRole).where(CmsRole.role_key == "CMS_ORG_ADMIN")
    )).scalar_one()
    db_session.add(CmsUserRoleAssignment(id_user=admin.id_user, id_role=role.id_role))
    await db_session.commit()
    await db_session.refresh(admin)

    resp = await client.get(
        f"/api/v1/orgs/{org.id_org}/ai-keys/fal/plan", headers=_auth(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["supported"] is False and body["detail"]


def _voice_param(models: list[dict], model_id: str) -> dict:
    model = next(m for m in models if m["id"] == model_id)
    key = "voice_id" if model["provider"] == "elevenlabs" else "voice"
    return next(p for p in model["params"] if p["key"] == key)
