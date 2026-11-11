"""Répartition du budget de connexions entre les processus backend (2026-08-08).

Le test qui compte est le premier : à UN processus, la dérivation doit rendre EXACTEMENT
les valeurs historiques. Sans lui, on livrerait un rétrécissement silencieux des pools à
toutes les installations mono-processus — une régression qui ne se voit qu'un jour de pointe.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.core.pooling import (
    _MIN_PER_PROCESS,
    derive_pool_sizes,
    max_workers_for_budget,
    split_per_process,
)
from app.db.session import assert_pool_budget_fits, connections_per_process

HISTORIQUE = (10, 20, 5, 10)
BUDGET = 60


def test_un_processus_rend_exactement_les_valeurs_historiques():
    assert derive_pool_sizes(1, BUDGET) == HISTORIQUE


@pytest.mark.parametrize("workers", range(1, max_workers_for_budget(BUDGET) + 1))
def test_la_repartition_tient_toujours_dans_le_budget(workers):
    """L'invariant qui évite le pire mode de panne : une config qui paraît saine, va bien
    au repos, et épuise la réserve PENDANT la rafale — le moment où l'on comptait dessus.

    Borne haute = le plafond que le budget peut réellement doter, celui-là même que
    l'entrypoint applique avant de forker.
    """
    sizes = derive_pool_sizes(workers, BUDGET)
    assert_pool_budget_fits(workers, connections_per_process(*sizes), BUDGET)


def test_au_dela_du_budget_la_garde_REFUSE_au_lieu_de_bricoler():
    """Passé le plafond, le budget ne peut plus donner à chaque processus des pools au
    plancher.

    On ne descend PAS sous le plancher pour faire rentrer le calcul : c'est précisément ce
    que faisait l'ancienne version, et elle produisait un pool BYPASSRLS de 2 connexions
    qui faisait rater des diffusions. On refuse de démarrer, en nommant les leviers.
    ⚠️ Inatteignable sans y avoir mis la main : l'entrypoint plafonne désormais lui-même.
    """
    trop = max_workers_for_budget(BUDGET) + 1
    sizes = derive_pool_sizes(trop, BUDGET)
    with pytest.raises(RuntimeError, match="Connection budget exceeded"):
        assert_pool_budget_fits(trop, connections_per_process(*sizes), BUDGET)


@pytest.mark.parametrize("workers", [1, 2, 3, 4, 8, 16])
def test_chaque_pool_reste_utilisable(workers):
    """⚠️ Ce test FIGEAIT le défaut jusqu'au 2026-08-12 : il exigeait `wpool + wover >= 2`,
    c'est-à-dire exactement le dimensionnement qui a cassé en production. Un test peut
    certifier un mode de panne s'il se contente de la valeur que le code produit."""
    pool, over, wpool, wover = derive_pool_sizes(workers, BUDGET)
    assert min(pool, over, wpool, wover) >= 1, "un pool à zéro bloquerait le processus"
    # Le pool BYPASSRLS n'est pas accessoire : c'est celui des WebSockets de players et de
    # force_sync. `2` le laissait rater des diffusions ; `2 + 4` est la configuration
    # mesurée saine en production, on n'invente pas un autre nombre.
    assert wpool >= 2 and wover >= 4, (
        f"pool BYPASSRLS {wpool}+{wover} : sous ce plancher, un processus s'épuise dès "
        "que trois écrans se synchronisent ensemble (incident du 2026-08-11)"
    )
    assert pool >= 2 and over >= 2, "un processus doit servir 2 requêtes d'interface"


def test_plus_de_processus_ne_veut_pas_dire_plus_de_connexions():
    """C'est tout le raisonnement : le budget est GLOBAL, pas par processus."""
    totaux = [
        w * connections_per_process(*derive_pool_sizes(w, BUDGET))
        for w in range(1, max_workers_for_budget(BUDGET) + 1)
    ]
    assert max(totaux) <= BUDGET
    # …et la capacité totale ne s'effondre pas non plus quand on répartit.
    assert min(totaux) >= BUDGET // 2


def test_le_budget_ne_descend_jamais_sous_un_plancher_utilisable():
    """Un budget absurdement bas ne doit pas produire des pools à zéro : on rend un
    plancher, et c'est la GARDE qui refuse le démarrage en nommant les leviers."""
    sizes = derive_pool_sizes(8, 8)
    assert min(sizes) >= 1
    with pytest.raises(RuntimeError, match="Connection budget exceeded"):
        assert_pool_budget_fits(8, connections_per_process(*sizes), 8)


def test_un_budget_genereux_ne_depasse_pas_les_valeurs_historiques():
    """Au-delà, ce sont des connexions dont la mesure dit qu'elles ne servent pas — et
    elles seraient prises sur la réserve de Celery et de la maintenance."""
    assert derive_pool_sizes(1, 10_000) == HISTORIQUE


def test_split_repartit_tout_le_budget():
    # Sous `_MIN_PER_PROCESS`, la fonction remonte AU plancher par contrat : elle ne peut
    # donc pas rendre la somme demandée, et c'est la garde qui refusera le démarrage.
    for per_process in range(_MIN_PER_PROCESS, 46):
        assert sum(split_per_process(per_process)) == per_process


# ── L'incident du 2026-08-11, figé pour qu'il ne puisse pas revenir ───────────────


def test_le_pool_des_ECRANS_ne_s_effondre_plus_quand_on_multiplie_les_processus():
    """Le défaut exact qui a fait rater une diffusion à 4 écrans sur 8.

    Avant : `derive_pool_sizes(8, 60)` rendait `BYPASSRLS 1 + 1` → `QueuePool limit of
    size 1 overflow 1`, 502 échecs de force_sync. Le pool BYPASSRLS porte TOUT le trafic
    des écrans ; le prorata historique le traitait comme un pool de maintenance.
    """
    for workers in (2, 3, 4, 6, 8, 12):
        _, _, wpool, wover = derive_pool_sizes(workers, BUDGET)
        assert wpool + wover >= 6, (
            f"{workers} processus : BYPASSRLS retombé à {wpool}+{wover}"
        )


def test_la_configuration_en_service_en_production_est_INCHANGEE():
    """Non-régression qui compte autant que le correctif : la prod tourne aujourd'hui à
    3 processus avec (4, 10, 2, 4) et 0 échec. Le plancher ne doit pas la déplacer —
    sinon on « corrige » en changeant ce qui marche."""
    assert derive_pool_sizes(3, BUDGET) == (4, 10, 2, 4)


def test_le_budget_plafonne_le_nombre_de_processus_avant_le_fork():
    """Sans ce plafond, une machine à 16 cœurs demanderait 8 processus, le budget ne
    pourrait pas leur donner leur plancher, et la garde refuserait le démarrage : un
    serveur client qui ne redémarre plus après une mise à jour. On rend moins de
    processus plutôt qu'un service mort."""
    cap = max_workers_for_budget(BUDGET)
    assert cap == 6, cap
    # …et à ce plafond, la répartition tient DANS le budget, planchers compris.
    sizes = derive_pool_sizes(cap, BUDGET)
    assert_pool_budget_fits(cap, connections_per_process(*sizes), BUDGET)
    assert sizes == (2, 2, 2, 4)


def test_relever_le_budget_rend_les_processus_perdus():
    """Le levier reste à l'exploitant : c'est ce que nomme déjà le message de la garde."""
    assert max_workers_for_budget(120) == 12
    assert max_workers_for_budget(10) == 1
    assert max_workers_for_budget(0) == 1, "jamais zéro processus"


# ── La reprise en main par l'exploitant ──────────────────────────────────────────
# Tout le mécanisme repose sur une hypothèse : pydantic sait distinguer « il a écrit 10 »
# de « ça vaut 10 parce que personne n'a rien dit ». On la MESURE au lieu de la supposer.


def test_pydantic_distingue_une_valeur_fournie_d_un_defaut(monkeypatch):
    from app.core.config import Settings

    assert not Settings().pool_sizes_are_explicit
    monkeypatch.setenv("CMS_DB_POOL_SIZE", "10")  # la MÊME valeur que le défaut
    assert Settings().pool_sizes_are_explicit, (
        "si l'égalité au défaut suffisait à masquer un choix explicite, un exploitant "
        "qui écrit 10 se ferait recalculer ses pools dans son dos"
    )


def test_un_reglage_explicite_est_respecte_tel_quel(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("CMS_DB_POOL_SIZE", "7")
    s = Settings()
    assert s.effective_pool_sizes == (7, 20, 5, 10), (
        "on respecte les quatre valeurs telles quelles : recalculer la moitié des pools "
        "d'un exploitant qui a repris la main serait pire que de ne rien faire"
    )


def test_sans_reglage_explicite_les_pools_suivent_le_nombre_de_processus(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    s = Settings()
    assert s.effective_pool_sizes == derive_pool_sizes(4, s.CMS_DB_CONNECTION_BUDGET)
    assert sum(s.effective_pool_sizes) * 4 <= s.CMS_DB_CONNECTION_BUDGET


# ── L'entrypoint de l'image : c'est lui qui décide du nombre de processus ─────────
ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


def _exec_entrypoint(
    tmp_path,
    env_value: str | None,
    cmd: str = "uvicorn",
    budget: str | None = None,
    with_python: bool = True,
):
    """Exécute le VRAI script (jamais un portage) et rend le processus terminé.

    La commande finale est un leurre posé dans le PATH : on lit ainsi la valeur telle que
    l'application la recevrait, au lieu de se fier au message affiché par le script.

    `with_python` ajoute l'interpréteur au PATH et le paquet `app` au PYTHONPATH : le
    script consulte alors le VRAI `max_workers_for_budget`, pas une valeur simulée. Sans
    lui, on exerce le chemin de repli (plafond illisible).
    """
    if not ENTRYPOINT.exists():  # pragma: no cover
        pytest.skip("entrypoint absent")
    shim = tmp_path / cmd
    shim.write_text('#!/bin/sh\necho "RESULT=[$WEB_CONCURRENCY]"\n')
    shim.chmod(0o755)
    path = f"{tmp_path}:/usr/bin:/bin"
    env = {"PATH": path}
    if not with_python:
        # Un `python` système peut exister dans /usr/bin (runner GitHub) : sans ce leurre
        # qui ÉCHOUE, le script le trouverait et lirait le plafond, et le cas « plafond
        # illisible » ne serait exercé que sur certaines machines.
        fake = tmp_path / "python"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
    if with_python:
        # ⚠️ Le leurre reste EN TÊTE : l'interpréteur vit dans le même répertoire que les
        # vrais `uvicorn` et `celery`, qui masqueraient le leurre et feraient échouer les
        # tests sur une commande réellement exécutée.
        env["PATH"] = f"{path}:{Path(sys.executable).parent}"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    if env_value is not None:
        env["WEB_CONCURRENCY"] = env_value
    if budget is not None:
        env["CMS_DB_CONNECTION_BUDGET"] = budget
    # cwd NEUTRE : `python -c` met le répertoire courant dans sys.path. Lancé depuis
    # backend/, un `python` système (/usr/bin, présent sur le runner GitHub) importait
    # `app` et lisait le plafond — le cas « plafond illisible » n'était alors plus exercé.
    return subprocess.run(
        ["sh", str(ENTRYPOINT), cmd], capture_output=True, text=True, env=env,
        cwd=tmp_path,
    )


def _run_entrypoint(tmp_path, env_value: str | None, cmd: str = "uvicorn", **kw) -> str:
    out = _exec_entrypoint(tmp_path, env_value, cmd, **kw)
    assert "RESULT=[" in out.stdout, f"le leurre n'a pas été exécuté : {out.stderr}"
    return out.stdout.split("RESULT=[")[1].split("]")[0]


@pytest.mark.skipif(sys.platform == "win32", reason="script sh, exécuté dans l'image Linux")
def test_entrypoint_respecte_une_valeur_posee_par_l_exploitant(tmp_path):
    assert _run_entrypoint(tmp_path, "3") == "3"


@pytest.mark.skipif(sys.platform == "win32", reason="script sh, exécuté dans l'image Linux")
@pytest.mark.parametrize("valeur", ["auto", "", None])
def test_entrypoint_calcule_un_ENTIER_quand_la_valeur_est_auto_ou_absente(tmp_path, valeur):
    # « auto » ne doit JAMAIS atteindre l'application : elle attend un entier.
    obtenu = _run_entrypoint(tmp_path, valeur)
    assert obtenu.isdigit() and 1 <= int(obtenu) <= 8, obtenu


@pytest.mark.skipif(sys.platform == "win32", reason="script sh, exécuté dans l'image Linux")
def test_entrypoint_plafonne_les_processus_selon_le_budget(tmp_path):
    """Le plafond est appliqué AVANT le fork, avec le vrai `max_workers_for_budget`.

    Sans lui, une machine à 16 cœurs demanderait 8 processus que le budget ne peut pas
    doter, et la garde refuserait de démarrer — un serveur client mort après une MAJ.
    """
    genereux = int(_run_entrypoint(tmp_path, "auto", budget="1000"))
    serre = int(_run_entrypoint(tmp_path, "auto", budget="10"))  # plafond = 1 processus
    assert serre == 1, serre
    if genereux <= 1:
        pytest.skip(
            "machine à 1 ou 2 cœurs : les deux cas rendent 1, ce test ne peut RIEN "
            "distinguer ici — il vaut mieux le dire que passer pour la mauvaise raison"
        )
    assert genereux > serre, f"le budget n'a rien plafonné ({genereux} vs {serre})"


@pytest.mark.skipif(sys.platform == "win32", reason="script sh, exécuté dans l'image Linux")
def test_entrypoint_degrade_BRUYAMMENT_si_le_plafond_est_illisible(tmp_path):
    """Un plafond qu'on ne sait pas lire ne doit pas se traduire par un silence : on garde
    la valeur des cœurs, on le DIT, et la garde de session.py refusera si ça ne tient pas.
    Un repli muet nous ramènerait au défaut d'origine."""
    out = _exec_entrypoint(tmp_path, "auto", with_python=False)
    assert "RESULT=[" in out.stdout
    valeur = out.stdout.split("RESULT=[")[1].split("]")[0]
    assert valeur.isdigit() and int(valeur) >= 1, valeur
    assert "AVERTISSEMENT" in out.stdout, out.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="script sh, exécuté dans l'image Linux")
def test_entrypoint_ne_donne_pas_de_parallelisme_web_a_celery(tmp_path):
    """celery-beat partage l'image du backend : il ne doit ni hériter d'un parallélisme
    web, ni recevoir « auto », qui ferait échouer la lecture de configuration."""
    assert _run_entrypoint(tmp_path, "auto", cmd="celery") == "1"
