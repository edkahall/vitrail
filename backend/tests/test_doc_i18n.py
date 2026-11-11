"""Server-made documents in the profile language (2026-11-11).

Every generator keeps its texts in one module-level `DOC_TEXTS` table. This test finds
them all by scanning the code, so a new generator is checked without being listed here:
same keys in the 5 locales, same `{placeholders}` per key, no empty text.
"""

import importlib
import re
import string
from pathlib import Path

import pytest

from app.services.doc_i18n import SUPPORTED_LOCALES, norm_locale, tr

APP = Path(__file__).resolve().parents[1] / "app"


def _modules_with_doc_texts() -> list[str]:
    found = []
    for py in APP.rglob("*.py"):
        if re.search(r"^DOC_TEXTS\b", py.read_text(encoding="utf-8"), re.M):
            rel = py.relative_to(APP.parent).with_suffix("")
            found.append(".".join(rel.parts))
    return sorted(found)


MODULES = _modules_with_doc_texts()


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_generators_are_found():
    # Witness: the sweep must find the generators converted on 2026-11-11, otherwise the
    # parity test below would pass on nothing.
    assert len(MODULES) >= 5, MODULES


@pytest.mark.parametrize("module", MODULES)
def test_doc_texts_parity(module):
    table = importlib.import_module(module).DOC_TEXTS
    assert set(table) == set(SUPPORTED_LOCALES), f"{module}: locales {sorted(table)}"
    ref = table["en"]
    for loc in SUPPORTED_LOCALES:
        assert set(table[loc]) == set(ref), (
            f"{module} [{loc}]: missing {sorted(set(ref) - set(table[loc]))}, "
            f"extra {sorted(set(table[loc]) - set(ref))}")
        for key, text in table[loc].items():
            assert text.strip(), f"{module} [{loc}] {key}: empty"
            assert _fields(text) == _fields(ref[key]), f"{module} [{loc}] {key}: placeholders"


def test_norm_locale():
    assert norm_locale(None) == "en"
    assert norm_locale("") == "en"
    assert norm_locale("fr-FR") == "fr"
    assert norm_locale("DE") == "de"
    assert norm_locale("pt_BR") == "pt"
    assert norm_locale("it") == "en"


def test_tr_falls_back_to_english_then_key():
    table = {"en": {"a": "Hello {name}", "b": "Only English"}, "fr": {"a": "Bonjour {name}"}}
    assert tr(table, "fr", "a", name="Ed") == "Bonjour Ed"
    assert tr(table, "fr", "b") == "Only English"
    assert tr(table, "xx", "a", name="Ed") == "Hello Ed"
    assert tr(table, "fr", "missing") == "missing"
