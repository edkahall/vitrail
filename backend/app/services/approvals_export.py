"""Exports de la file d'approbation §XXI (retour Ed 2026-07-12) — CSV + PDF.

Même scope que GET /approvals (reviewer = org entière ; soumetteur = SES demandes).
`rows` = liste de dicts préparés par l'endpoint : resource_name, type, soumetteur,
dates, statut, valideur, dernier motif de décision. Rédigés dans la langue du profil
de l'utilisateur qui exporte (`locale`, défaut anglais) — cf. services/doc_i18n.py.
reportlab importé paresseusement (comme report_pdf.py).
"""

from __future__ import annotations

import csv
from io import BytesIO, StringIO

from app.services.doc_i18n import norm_locale, tr

DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "status_pending_review": "Pending review",
        "status_changes_requested": "Changes requested",
        "status_approved": "Approved",
        "status_rejected": "Rejected",
        "status_published": "Published",
        "status_cancelled": "Cancelled",
        "h_id": "ID",
        "h_resource": "Resource",
        "h_type": "Type",
        "h_submitted_by": "Submitted by",
        "h_submitted_at": "Submitted on",
        "h_due": "Due date",
        "h_status": "Status",
        "h_decided_by": "Decided by",
        "h_decided_at": "Decided on",
        "h_published_at": "Published on",
        "h_comment": "Reason / comment",
        "pdf_meta_title": "Approvals — {org}",
        "pdf_title": "Approval requests — {org}",
        "generated": "Generated on {when}",
        "status_filter": " · status: {status}",
        "count": " · {n} request(s)",
        "filename_csv": "approvals_{stamp}.csv",
        "filename_pdf": "approvals_{stamp}.pdf",
    },
    "fr": {
        "status_pending_review": "En attente",
        "status_changes_requested": "Modifications demandées",
        "status_approved": "Approuvée",
        "status_rejected": "Rejetée",
        "status_published": "Publiée",
        "status_cancelled": "Annulée",
        "h_id": "ID",
        "h_resource": "Ressource",
        "h_type": "Type",
        "h_submitted_by": "Soumis par",
        "h_submitted_at": "Soumis le",
        "h_due": "Échéance",
        "h_status": "Statut",
        "h_decided_by": "Décidé par",
        "h_decided_at": "Décidé le",
        "h_published_at": "Publié le",
        "h_comment": "Motif / commentaire",
        "pdf_meta_title": "Approbations — {org}",
        "pdf_title": "Demandes d'approbation — {org}",
        "generated": "Généré le {when}",
        "status_filter": " · statut : {status}",
        "count": " · {n} demande(s)",
        "filename_csv": "approbations_{stamp}.csv",
        "filename_pdf": "approbations_{stamp}.pdf",
    },
    "es": {
        "status_pending_review": "Pendiente",
        "status_changes_requested": "Cambios solicitados",
        "status_approved": "Aprobada",
        "status_rejected": "Rechazada",
        "status_published": "Publicada",
        "status_cancelled": "Cancelada",
        "h_id": "ID",
        "h_resource": "Recurso",
        "h_type": "Tipo",
        "h_submitted_by": "Enviada por",
        "h_submitted_at": "Enviada el",
        "h_due": "Fecha límite",
        "h_status": "Estado",
        "h_decided_by": "Decidida por",
        "h_decided_at": "Decidida el",
        "h_published_at": "Publicada el",
        "h_comment": "Motivo / comentario",
        "pdf_meta_title": "Aprobaciones — {org}",
        "pdf_title": "Solicitudes de aprobación — {org}",
        "generated": "Generado el {when}",
        "status_filter": " · estado: {status}",
        "count": " · {n} solicitud(es)",
        "filename_csv": "aprobaciones_{stamp}.csv",
        "filename_pdf": "aprobaciones_{stamp}.pdf",
    },
    "pt": {
        "status_pending_review": "Pendente",
        "status_changes_requested": "Alterações pedidas",
        "status_approved": "Aprovado",
        "status_rejected": "Rejeitado",
        "status_published": "Publicado",
        "status_cancelled": "Cancelado",
        "h_id": "ID",
        "h_resource": "Recurso",
        "h_type": "Tipo",
        "h_submitted_by": "Submetido por",
        "h_submitted_at": "Submetido em",
        "h_due": "Prazo",
        "h_status": "Estado",
        "h_decided_by": "Decidido por",
        "h_decided_at": "Decidido em",
        "h_published_at": "Publicado em",
        "h_comment": "Motivo / comentário",
        "pdf_meta_title": "Aprovações — {org}",
        "pdf_title": "Pedidos de aprovação — {org}",
        "generated": "Gerado em {when}",
        "status_filter": " · estado: {status}",
        "count": " · {n} pedido(s)",
        "filename_csv": "aprovacoes_{stamp}.csv",
        "filename_pdf": "aprovacoes_{stamp}.pdf",
    },
    "de": {
        "status_pending_review": "Ausstehend",
        "status_changes_requested": "Änderungen angefordert",
        "status_approved": "Genehmigt",
        "status_rejected": "Abgelehnt",
        "status_published": "Veröffentlicht",
        "status_cancelled": "Storniert",
        "h_id": "ID",
        "h_resource": "Ressource",
        "h_type": "Typ",
        "h_submitted_by": "Eingereicht von",
        "h_submitted_at": "Eingereicht am",
        "h_due": "Fälligkeit",
        "h_status": "Status",
        "h_decided_by": "Entschieden von",
        "h_decided_at": "Entschieden am",
        "h_published_at": "Veröffentlicht am",
        "h_comment": "Begründung / Kommentar",
        "pdf_meta_title": "Freigaben — {org}",
        "pdf_title": "Freigabeanfragen — {org}",
        "generated": "Erstellt am {when}",
        "status_filter": " · Status: {status}",
        "count": " · {n} Anfrage(n)",
        "filename_csv": "freigaben_{stamp}.csv",
        "filename_pdf": "freigaben_{stamp}.pdf",
    },
}

_STATUSES = ("pending_review", "changes_requested", "approved", "rejected", "published",
             "cancelled")
_HEADER_KEYS = ("h_id", "h_resource", "h_type", "h_submitted_by", "h_submitted_at", "h_due",
                "h_status", "h_decided_by", "h_decided_at", "h_published_at", "h_comment")


def status_label(status: str, locale: str | None = None) -> str:
    if status in _STATUSES:
        return tr(DOC_TEXTS, locale, f"status_{status}")
    return status


def _headers(locale: str | None) -> list[str]:
    return [tr(DOC_TEXTS, locale, k) for k in _HEADER_KEYS]


def export_filename(kind: str, stamp: str, locale: str | None = None) -> str:
    """Nom du fichier téléchargé (ASCII) — `kind` = 'csv' ou 'pdf'."""
    return tr(DOC_TEXTS, locale, f"filename_{kind}", stamp=stamp)


def _fmt_dt(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if v else ""


def _row_values(r: dict, locale: str | None = None) -> list[str]:
    return [
        str(r["id_request"]),
        r.get("resource_name") or f"#{r['resource_id']}",
        r.get("resource_type") or "",
        r.get("submitted_by_name") or "",
        _fmt_dt(r.get("submitted_at")),
        _fmt_dt(r.get("due_date")),
        status_label(r["status"], locale),
        r.get("reviewed_by_name") or "",
        _fmt_dt(r.get("reviewed_at")),
        _fmt_dt(r.get("published_at")),
        (r.get("decision_comment") or "").replace("\n", " ").strip(),
    ]


def approvals_csv(rows: list[dict], *, locale: str | None = None) -> str:
    buf = StringIO()
    # Séparateur attendu par Excel selon la langue : « , » en anglais, « ; » ailleurs
    # (décimale à virgule).
    delimiter = "," if norm_locale(locale) == "en" else ";"
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(_headers(locale))
    for r in rows:
        w.writerow(_row_values(r, locale))
    return buf.getvalue()


def build_approvals_pdf(*, org_name: str, rows: list[dict], generated_at: str = "",
                        status_filter: str | None = None,
                        locale: str | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    accent = colors.HexColor("#4875B4")
    ink = colors.HexColor("#1C1E2A")
    muted = colors.HexColor("#6B7280")
    line = colors.HexColor("#E5E7EB")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=tr(DOC_TEXTS, locale, "pdf_meta_title", org=org_name),
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=ink, fontSize=16,
                        spaceAfter=2)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=muted)
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, textColor=ink,
                          leading=10)

    story: list = [Paragraph(tr(DOC_TEXTS, locale, "pdf_title", org=org_name), h1)]
    sub = tr(DOC_TEXTS, locale, "generated", when=generated_at) if generated_at else ""
    if status_filter:
        sub += tr(DOC_TEXTS, locale, "status_filter",
                  status=status_label(status_filter, locale))
    sub += tr(DOC_TEXTS, locale, "count", n=len(rows))
    story.append(Paragraph(sub, small))
    story.append(Spacer(1, 6 * mm))

    data = [_headers(locale)]
    for r in rows:
        vals = _row_values(r, locale)
        # Paragraph pour les colonnes texte longues (wrap) — les autres restent str
        vals[1] = Paragraph(vals[1], cell)
        vals[10] = Paragraph(vals[10], cell)
        data.append(vals)
    widths = [10, 42, 18, 30, 24, 22, 30, 30, 24, 24, 60]
    table = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, line),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    doc.build(story)
    return buf.getvalue()
