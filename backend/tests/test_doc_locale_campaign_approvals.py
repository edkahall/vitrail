"""Langue des documents serveur — rapport de campagne PDF + exports d'approbation.

Témoins : chaque test compare deux langues sur la même entrée, il échoue si `locale`
était ignoré (le texte attendu d'une langue n'apparaît pas dans l'autre).
"""

from datetime import datetime

import pytest
from reportlab import rl_config

from app.services import approvals_export, campaign_report_pdf


@pytest.fixture
def plain_pdf(monkeypatch):
    """PDF non compressé : le texte des pages devient lisible dans les octets."""
    monkeypatch.setattr(rl_config, "pageCompression", 0)


def _payload() -> dict:
    return {
        "campaign": {"campaign_name": "Spring", "advertiser_name": "Acme",
                     "starts_on": "2026-03-01", "ends_on": "2026-03-31"},
        "period": {"from": "2026-03-01", "until": "2026-03-31"},
        "totals": {"plays": 12345, "signed_plays": 12000, "airtime_ms": 3_600_000,
                   "players_reached": 3, "players_targeted": 4},
        "sov": {"served_pct": 9.5, "sold_pct": 10},
        "amount": {"model": "flat", "amount": 1234.5, "currency": "EUR",
                   "cpm_indicative": None, "cpm_rate": None},
        "per_player": [{"player_name": "Screen A", "plays": 12345,
                        "airtime_ms": 3_600_000, "sov_served_pct": 9.5}],
        "per_creative": [], "per_day": [],
    }


def test_campaign_pdf_english_default_and_german(plain_pdf):
    kw = dict(org_name="Org", payload=_payload(), branding={})
    en = campaign_report_pdf.build_campaign_pdf(**kw)
    de = campaign_report_pdf.build_campaign_pdf(**kw, locale="de")
    fr = campaign_report_pdf.build_campaign_pdf(**kw, locale="fr")
    assert en[:4] == de[:4] == b"%PDF"
    # anglais par défaut
    assert b"Plays delivered" in en and b"Flat fee" in en and b"12,345" in en
    assert b"1,234.50 EUR" in en
    # allemand : textes + séparateurs
    assert b"Sendezeit" in de and b"Pauschale" in de and b"12.345" in de
    assert b"1.234,50 EUR" in de
    assert b"Plays delivered" not in de and b"Sendezeit" not in en
    # français inchangé
    assert b"Forfait" in fr and b"12 345" in fr and b"1 234,50 EUR" in fr


def test_campaign_filename_and_date_helpers():
    assert campaign_report_pdf.report_filename(7, "2026-03-01", "2026-03-31") == \
        "campaign_report_7_2026-03-01_2026-03-31.pdf"
    assert campaign_report_pdf.report_filename(7, "a", "b", "fr") == "rapport_campagne_7_a_b.pdf"
    for loc in ("en", "fr", "es", "pt", "de"):
        assert campaign_report_pdf.report_filename(1, "a", "b", loc).isascii()
    dt = datetime(2026, 3, 5, 14, 7)
    assert campaign_report_pdf.fmt_generated_at(dt) == "2026-03-05 14:07"
    assert campaign_report_pdf.fmt_generated_at(dt, "fr") == "05/03/2026 14:07"
    assert campaign_report_pdf.fmt_generated_at(dt, "de") == "05.03.2026 14:07"


def _rows() -> list[dict]:
    return [{"id_request": 1, "resource_name": "Spring", "resource_id": 9,
             "resource_type": "campaign", "submitted_by_name": "Ann",
             "submitted_at": datetime(2026, 3, 1, 9, 0), "due_date": None,
             "status": "rejected", "reviewed_by_name": "Bob",
             "reviewed_at": datetime(2026, 3, 2, 9, 0), "published_at": None,
             "decision_comment": "No"}]


def test_approvals_csv_english_default_and_spanish():
    en = approvals_export.approvals_csv(_rows())
    es = approvals_export.approvals_csv(_rows(), locale="es")
    assert en.splitlines()[0].startswith("ID,Resource,Type,Submitted by")
    assert "Rejected" in en and "Rechazada" not in en
    assert es.splitlines()[0].startswith("ID;Recurso;Tipo;Enviada por")
    assert "Rechazada" in es and "Rejected" not in es
    assert "Rejetée" in approvals_export.approvals_csv(_rows(), locale="fr")


def test_approvals_pdf_english_default_and_portuguese(plain_pdf):
    en = approvals_export.build_approvals_pdf(org_name="Org", rows=_rows(),
                                              generated_at="x", status_filter="rejected")
    pt = approvals_export.build_approvals_pdf(org_name="Org", rows=_rows(),
                                              generated_at="x", status_filter="rejected",
                                              locale="pt")
    assert b"Approval requests" in en and b"Rejected" in en
    assert b"Pedidos de aprova" in pt and b"Rejeitado" in pt
    assert b"Approval requests" not in pt and b"Rejeitado" not in en
    assert approvals_export.export_filename("pdf", "20260301") == "approvals_20260301.pdf"
    assert approvals_export.export_filename("csv", "20260301", "de") == "freigaben_20260301.csv"
