"""Plafonds de dépense BYOK — réserver avant d'appeler, corriger après (P3.2, migr 0126).

Le CMS enregistrait le coût de chaque job mais **rien ne bloquait**. En BYOK c'est l'argent
du CLIENT : une boucle ou un import massif se voit sur SA facture, pas sur la nôtre.

⛔ NE PAS reconstruire ce calcul sur `cms_generation_jobs`. Mesuré le 2026-08-07 : sur les
CINQ chemins qui appellent réellement un fournisseur payant, QUATRE y sont invisibles ou faux
(image synchrone sans job, promo à `cost_usd = 0` sur de vrais appels fal, voix de Jeanine
absente). Un plafond bâti là-dessus aurait eu l'air de fonctionner. Tout passe par ici.

🔑 RÉSERVATION, PAS CONSTAT — c'est le point non évident.
Deux générations lancées ensemble lisent la même somme avant que l'une ou l'autre n'ait
écrit, et **passent toutes les deux** sous un plafond qui n'en tient qu'une. On écrit donc la
ligne AVANT l'appel, sur l'estimation, et le contrôle + l'écriture sont rendus atomiques par
un verrou consultatif de transaction porté par l'organisation.

Trois issues pour une réservation :
  · `settle(id, actual)` — l'appel a rendu : on inscrit le montant réel ;
  · `release(id)`        — l'appel n'a PAS eu lieu : aucun argent dépensé, la ligne cesse de
                           compter (une réservation abandonnée qui resterait finirait par
                           bloquer une organisation sans qu'aucune facture n'existe) ;
  · rien                 — un processus tué laisse la réservation en place. C'est le bon
                           sens du risque : on refuse trop tôt plutôt que trop tard, et la
                           période calendaire la fait tomber d'elle-même.

⚠️ Un appel qui ÉCHOUE côté fournisseur n'est pas forcément gratuit. On ne relâche que
lorsqu'on sait que rien n'est parti (refus local, clé absente, validation). En cas de doute,
on `settle` : mieux vaut compter une dépense qui n'a pas eu lieu que d'ignorer celle qui a
eu lieu — c'est l'argent du client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation import CmsAiSpend
from app.models.organization import CmsOrganization

#: Clé de verrou consultatif — constante arbitraire mais STABLE : deux valeurs différentes
#: dans le produit ne se verrouilleraient pas l'une l'autre.
_LOCK_NAMESPACE = 0x5F1D  # "SFID"

PERIODS = ("day", "week", "month")


class SpendCapExceeded(Exception):
    """Refus de plafond. Porte de quoi DISTINGUER un plafond d'une panne à l'écran.

    C'est tout l'intérêt : « génération impossible » laisse chercher une panne réseau,
    « il reste 2,10 $ sur les 50 $ du mois » se comprend et se règle.
    """

    def __init__(self, *, scope: str, period: str, cap_usd: float,
                 spent_usd: float, estimated_usd: float) -> None:
        self.scope = scope                  # org | user
        self.period = period
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd
        self.estimated_usd = estimated_usd
        super().__init__(
            f"{scope} spending cap reached: ${spent_usd:.2f} spent out of "
            f"${cap_usd:.2f} ({period}), this generation would cost "
            f"${estimated_usd:.2f} more."
        )

    def as_detail(self) -> dict:
        return {
            "code": "spend_cap_exceeded",
            "scope": self.scope,
            "period": self.period,
            "cap_usd": round(self.cap_usd, 2),
            "spent_usd": round(self.spent_usd, 2),
            "estimated_usd": round(self.estimated_usd, 4),
            "remaining_usd": round(max(0.0, self.cap_usd - self.spent_usd), 2),
            "message": str(self),
        }


@dataclass
class Caps:
    org_cap_usd: float | None
    user_cap_usd: float | None
    period: str


def period_start(period: str, now: datetime) -> datetime:
    """Début de la période CALENDAIRE courante, en UTC.

    ⚠️ Volontairement distinct d'`assistant_quota.period_start` : celui-là borne les TOKENS
    de l'assistant (weekly|monthly, sur des dates locales), celui-ci borne de l'ARGENT chez
    des fournisseurs qui facturent en UTC. Les faire partager une fonction les forcerait à
    s'accorder sur une sémantique qu'ils ne partagent pas — et le jour où l'un change, il
    ferait bouger l'autre en silence.
    """
    now = now.astimezone(timezone.utc)
    day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if period == "day":
        return day
    if period == "week":                      # semaine ISO : lundi
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)                 # month (défaut)


def _cap(value) -> float | None:
    """NULL ou ≤ 0 → pas de plafond (même convention que `ai_quota_tokens`)."""
    if value is None:
        return None
    v = float(value)
    return v if v > 0 else None


async def load_caps(db: AsyncSession, id_org: int) -> Caps:
    row = (await db.execute(
        select(CmsOrganization.ai_spend_cap_usd,
               CmsOrganization.ai_user_spend_cap_usd,
               CmsOrganization.ai_spend_cap_period)
        .where(CmsOrganization.id_org == id_org)
    )).one_or_none()
    if row is None:
        return Caps(None, None, "month")
    period = row[2] if row[2] in PERIODS else "month"
    return Caps(_cap(row[0]), _cap(row[1]), period)


async def spent_since(
    db: AsyncSession, id_org: int, since: datetime, *, id_user: int | None = None
) -> float:
    """Dépense engagée depuis `since` : réel s'il est connu, estimation sinon.

    `COALESCE(actual_usd, estimated_usd)` — une réservation encore ouverte compte pour son
    estimation, c'est précisément ce qui tient le plafond entre deux appels simultanés.
    Les lignes `released` sont exclues : aucun argent n'est parti.
    """
    q = select(func.coalesce(func.sum(
        func.coalesce(CmsAiSpend.actual_usd, CmsAiSpend.estimated_usd)), 0)
    ).where(
        CmsAiSpend.id_org == id_org,
        CmsAiSpend.created_at >= since,
        CmsAiSpend.status != "released",
    )
    if id_user is not None:
        q = q.where(CmsAiSpend.id_user == id_user)
    return float((await db.execute(q)).scalar() or 0.0)


async def reserve(
    db: AsyncSession, *, id_org: int, id_user: int | None, provider: str,
    kind: str, model: str | None, estimated_usd: float,
) -> int:
    """Contrôle les plafonds PUIS réserve la dépense, atomiquement. Renvoie `id_spend`.

    Lève `SpendCapExceeded` AVANT tout appel fournisseur — c'est la seule position où un
    refus ne coûte rien au client.

    🔑 Le verrou consultatif de transaction sérialise contrôle + écriture PAR ORGANISATION.
    Sans lui, deux requêtes simultanées lisent la même somme, se trouvent toutes deux sous le
    plafond, et le dépassent ensemble. Il est relâché au commit (rien à nettoyer) et ne
    bloque que les générations de LA MÊME organisation.

    ⚠️ La ligne est écrite même sans aucun plafond : le registre sert aussi à AFFICHER la
    dépense. Un registre qui ne se remplirait qu'en présence d'un plafond serait vide le jour
    où on veut en poser un.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :org)"),
        {"ns": _LOCK_NAMESPACE, "org": id_org},
    )
    caps = await load_caps(db, id_org)
    est = max(0.0, float(estimated_usd or 0.0))

    if caps.org_cap_usd is not None or caps.user_cap_usd is not None:
        since = period_start(caps.period, datetime.now(timezone.utc))
        if caps.org_cap_usd is not None:
            spent = await spent_since(db, id_org, since)
            if spent + est > caps.org_cap_usd:
                raise SpendCapExceeded(
                    scope="org", period=caps.period, cap_usd=caps.org_cap_usd,
                    spent_usd=spent, estimated_usd=est)
        # Le plafond par utilisateur n'a de sens que pour un geste ATTRIBUÉ : une génération
        # déclenchée par un worker sans compte ne peut pas être imputée à quelqu'un.
        if caps.user_cap_usd is not None and id_user is not None:
            uspent = await spent_since(db, id_org, since, id_user=id_user)
            if uspent + est > caps.user_cap_usd:
                raise SpendCapExceeded(
                    scope="user", period=caps.period, cap_usd=caps.user_cap_usd,
                    spent_usd=uspent, estimated_usd=est)

    row = CmsAiSpend(
        id_org=id_org, id_user=id_user, provider=provider, kind=kind, model=model,
        estimated_usd=round(est, 4), status="reserved",
    )
    db.add(row)
    await db.flush()
    return row.id_spend


#: Mêmes instructions en SQL brut, pour le WORKER (psycopg2 synchrone, il ne peut pas
#: utiliser le service async). ⚠️ Une seule écriture du solde dans le produit : deux versions
#: divergeraient, et c'est le montant facturé au client qui dériverait.
SETTLE_SQL = (
    "UPDATE cms_ai_spend SET status='settled', settled_at=now(), "
    "actual_usd=COALESCE(%s, actual_usd) WHERE id_spend=%s"
)
RELEASE_SQL = (
    "UPDATE cms_ai_spend SET status='released', settled_at=now() WHERE id_spend=%s"
)


async def settle(db: AsyncSession, id_spend: int, actual_usd: float | None) -> None:
    """L'appel a rendu : on inscrit le montant réel (l'estimation reste si on ne l'a pas)."""
    values: dict = {"status": "settled", "settled_at": datetime.now(timezone.utc)}
    if actual_usd is not None:
        values["actual_usd"] = round(max(0.0, float(actual_usd)), 4)
    await db.execute(
        update(CmsAiSpend).where(CmsAiSpend.id_spend == id_spend).values(**values))


async def release(db: AsyncSession, id_spend: int) -> None:
    """L'appel n'a PAS eu lieu : la réservation cesse de compter.

    ⛔ À n'appeler que lorsqu'on SAIT que rien n'est parti chez le fournisseur. Dans le
    doute, `settle` : mieux vaut compter une dépense qui n'a pas eu lieu que d'ignorer celle
    qui a eu lieu — c'est l'argent du client.
    """
    await db.execute(
        update(CmsAiSpend).where(CmsAiSpend.id_spend == id_spend)
        .values(status="released", settled_at=datetime.now(timezone.utc)))


async def usage(db: AsyncSession, id_org: int, *, id_user: int | None = None) -> dict:
    """Dépense de la période courante + plafonds, pour l'écran."""
    caps = await load_caps(db, id_org)
    since = period_start(caps.period, datetime.now(timezone.utc))
    org_spent = await spent_since(db, id_org, since)
    out: dict = {
        "period": caps.period,
        "period_start": since.isoformat(),
        "org_cap_usd": caps.org_cap_usd,
        "org_spent_usd": round(org_spent, 4),
        "user_cap_usd": caps.user_cap_usd,
        "user_spent_usd": None,
    }
    if id_user is not None:
        out["user_spent_usd"] = round(
            await spent_since(db, id_org, since, id_user=id_user), 4)
    return out
