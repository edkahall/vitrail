"""Retail Media S4 — rapport annonceur PDF white-label (§2.8, plan §4).

Même moteur/gabarit que le rapport exécutif (report_pdf.py) : reportlab importé
paresseusement, branding org (couleur/logo/titre). `payload` = sortie de
services/campaign_report.build_campaign_report. Rédigé dans la langue du profil de
l'utilisateur qui le télécharge (`locale`, défaut anglais) — cf. services/doc_i18n.py.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from app.services.doc_i18n import norm_locale, tr
from app.services.report_pdf import _fmt_duration

DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Campaign report — {name}",
        "advertiser": "Advertiser: {name}",
        "subtitle": "{org} — campaign from {start} to {end} · reported period: {pfrom} → {puntil}",
        "generated": "Generated on {when}",
        "sec_delivery": "Delivery",
        "kpi_plays": "Plays delivered",
        "kpi_signed": "Signed proofs (Ed25519)",
        "kpi_airtime": "Airtime",
        "kpi_screens": "Screens reached",
        "kpi_screens_val": "{reached} / {targeted} targeted",
        "kpi_sov": "Share of voice served",
        "kpi_sov_val": "{served}  (sold: {sold} %)",
        "sec_amount": "Amount",
        "sales_model": "Sales model",
        "model_sov": "Share of voice",
        "model_flat": "Flat fee",
        "amount_campaign": "Campaign amount",
        "cpm": "Indicative CPM equivalent ({rate} {currency}/1000)",
        "dist_site": "Breakdown by site",
        "col_site": "Site",
        "no_site": "No site",
        "screens_plays": "{n} screen(s) · {plays}",
        "dist_site_group": "Breakdown by site group",
        "col_site_group": "Site group",
        "sites_plays": "{n} site(s) · {plays}",
        "other_sites": "Other sites",
        "dist_screen": "Breakdown by screen",
        "col_screen": "Screen",
        "col_plays": "Plays",
        "col_airtime": "Airtime",
        "col_sov": "SOV served",
        "dist_creative": "Breakdown by creative",
        "col_creative": "Creative",
        "sec_daily": "Daily delivery",
        "col_date": "Date",
        "footer_proof": (
            "Every play is backed by a timestamped proof of play, chained (Merkle tree) "
            "and signed by the screen (Ed25519) — verifiable after the fact."),
        "confidential": "Confidential — {org}",
        "filename": "campaign_report_{id}_{pfrom}_{puntil}.pdf",
    },
    "fr": {
        "title": "Rapport de campagne — {name}",
        "advertiser": "Annonceur : {name}",
        "subtitle": "{org} — campagne du {start} au {end} · période rapportée : {pfrom} → {puntil}",
        "generated": "Généré le {when}",
        "sec_delivery": "Diffusion",
        "kpi_plays": "Passages diffusés",
        "kpi_signed": "Preuves signées (Ed25519)",
        "kpi_airtime": "Temps d'antenne",
        "kpi_screens": "Écrans touchés",
        "kpi_screens_val": "{reached} / {targeted} ciblés",
        "kpi_sov": "Part de voix servie",
        "kpi_sov_val": "{served}  (vendue : {sold} %)",
        "sec_amount": "Montant",
        "sales_model": "Modèle de vente",
        "model_sov": "Part de voix",
        "model_flat": "Forfait",
        "amount_campaign": "Montant de la campagne",
        "cpm": "Équivalent CPM indicatif ({rate} {currency}/1000)",
        "dist_site": "Répartition par site",
        "col_site": "Site",
        "no_site": "Sans site",
        "screens_plays": "{n} écran(s) · {plays}",
        "dist_site_group": "Répartition par groupe de sites",
        "col_site_group": "Groupe de sites",
        "sites_plays": "{n} site(s) · {plays}",
        "other_sites": "Autres sites",
        "dist_screen": "Répartition par écran",
        "col_screen": "Écran",
        "col_plays": "Passages",
        "col_airtime": "Temps d'antenne",
        "col_sov": "SOV servi",
        "dist_creative": "Répartition par créatif",
        "col_creative": "Créatif",
        "sec_daily": "Diffusion quotidienne",
        "col_date": "Date",
        "footer_proof": (
            "Chaque passage est adossé à une preuve de diffusion (Proof of Play) horodatée, "
            "chaînée (arbre de Merkle) et signée par l'écran (Ed25519) — vérifiable a posteriori."),
        "confidential": "Confidentiel — {org}",
        "filename": "rapport_campagne_{id}_{pfrom}_{puntil}.pdf",
    },
    "es": {
        "title": "Informe de campaña — {name}",
        "advertiser": "Anunciante: {name}",
        "subtitle": "{org} — campaña del {start} al {end} · periodo del informe: {pfrom} → {puntil}",
        "generated": "Generado el {when}",
        "sec_delivery": "Difusión",
        "kpi_plays": "Pases emitidos",
        "kpi_signed": "Pruebas firmadas (Ed25519)",
        "kpi_airtime": "Tiempo en antena",
        "kpi_screens": "Pantallas alcanzadas",
        "kpi_screens_val": "{reached} / {targeted} previstas",
        "kpi_sov": "Cuota de voz servida",
        "kpi_sov_val": "{served}  (vendida: {sold} %)",
        "sec_amount": "Importe",
        "sales_model": "Modelo de venta",
        "model_sov": "Cuota de voz",
        "model_flat": "Tarifa plana",
        "amount_campaign": "Importe de la campaña",
        "cpm": "CPM equivalente orientativo ({rate} {currency}/1000)",
        "dist_site": "Desglose por sitio",
        "col_site": "Sitio",
        "no_site": "Sin sitio",
        "screens_plays": "{n} pantalla(s) · {plays}",
        "dist_site_group": "Desglose por grupo de sitios",
        "col_site_group": "Grupo de sitios",
        "sites_plays": "{n} sitio(s) · {plays}",
        "other_sites": "Otros sitios",
        "dist_screen": "Desglose por pantalla",
        "col_screen": "Pantalla",
        "col_plays": "Pases",
        "col_airtime": "Tiempo en antena",
        "col_sov": "SOV servida",
        "dist_creative": "Desglose por creatividad",
        "col_creative": "Creatividad",
        "sec_daily": "Difusión diaria",
        "col_date": "Fecha",
        "footer_proof": (
            "Cada pase está respaldado por una prueba de emisión (Proof of Play) con marca de "
            "tiempo, encadenada (árbol de Merkle) y firmada por la pantalla (Ed25519), "
            "verificable a posteriori."),
        "confidential": "Confidencial — {org}",
        "filename": "informe_campana_{id}_{pfrom}_{puntil}.pdf",
    },
    "pt": {
        "title": "Relatório de campanha — {name}",
        "advertiser": "Anunciante: {name}",
        "subtitle": "{org} — campanha de {start} a {end} · período do relatório: {pfrom} → {puntil}",
        "generated": "Gerado em {when}",
        "sec_delivery": "Difusão",
        "kpi_plays": "Passagens difundidas",
        "kpi_signed": "Provas assinadas (Ed25519)",
        "kpi_airtime": "Tempo de antena",
        "kpi_screens": "Ecrãs alcançados",
        "kpi_screens_val": "{reached} / {targeted} visados",
        "kpi_sov": "Quota de voz servida",
        "kpi_sov_val": "{served}  (vendida: {sold} %)",
        "sec_amount": "Montante",
        "sales_model": "Modelo de venda",
        "model_sov": "Quota de voz",
        "model_flat": "Valor fixo",
        "amount_campaign": "Montante da campanha",
        "cpm": "CPM equivalente indicativo ({rate} {currency}/1000)",
        "dist_site": "Repartição por local",
        "col_site": "Local",
        "no_site": "Sem local",
        "screens_plays": "{n} ecrã(s) · {plays}",
        "dist_site_group": "Repartição por grupo de locais",
        "col_site_group": "Grupo de locais",
        "sites_plays": "{n} local(is) · {plays}",
        "other_sites": "Outros locais",
        "dist_screen": "Repartição por ecrã",
        "col_screen": "Ecrã",
        "col_plays": "Passagens",
        "col_airtime": "Tempo de antena",
        "col_sov": "SOV servida",
        "dist_creative": "Repartição por criativo",
        "col_creative": "Criativo",
        "sec_daily": "Difusão diária",
        "col_date": "Data",
        "footer_proof": (
            "Cada passagem é suportada por uma prova de difusão (Proof of Play) com carimbo "
            "temporal, encadeada (árvore de Merkle) e assinada pelo ecrã (Ed25519), "
            "verificável a posteriori."),
        "confidential": "Confidencial — {org}",
        "filename": "relatorio_campanha_{id}_{pfrom}_{puntil}.pdf",
    },
    "de": {
        "title": "Kampagnenbericht — {name}",
        "advertiser": "Werbetreibender: {name}",
        "subtitle": "{org} — Kampagne vom {start} bis {end} · Berichtszeitraum: {pfrom} → {puntil}",
        "generated": "Erstellt am {when}",
        "sec_delivery": "Ausspielung",
        "kpi_plays": "Ausgespielte Durchläufe",
        "kpi_signed": "Signierte Nachweise (Ed25519)",
        "kpi_airtime": "Sendezeit",
        "kpi_screens": "Erreichte Bildschirme",
        "kpi_screens_val": "{reached} / {targeted} angezielt",
        "kpi_sov": "Ausgelieferter Share of Voice",
        "kpi_sov_val": "{served}  (verkauft: {sold} %)",
        "sec_amount": "Betrag",
        "sales_model": "Verkaufsmodell",
        "model_sov": "Share of Voice",
        "model_flat": "Pauschale",
        "amount_campaign": "Kampagnenbetrag",
        "cpm": "Indikativer TKP-Gegenwert ({rate} {currency}/1000)",
        "dist_site": "Aufteilung nach Standort",
        "col_site": "Standort",
        "no_site": "Ohne Standort",
        "screens_plays": "{n} Bildschirm(e) · {plays}",
        "dist_site_group": "Aufteilung nach Standortgruppe",
        "col_site_group": "Standortgruppe",
        "sites_plays": "{n} Standort(e) · {plays}",
        "other_sites": "Andere Standorte",
        "dist_screen": "Aufteilung nach Bildschirm",
        "col_screen": "Bildschirm",
        "col_plays": "Durchläufe",
        "col_airtime": "Sendezeit",
        "col_sov": "SOV ausgeliefert",
        "dist_creative": "Aufteilung nach Motiv",
        "col_creative": "Motiv",
        "sec_daily": "Tägliche Ausspielung",
        "col_date": "Datum",
        "footer_proof": (
            "Jeder Durchlauf ist durch einen zeitgestempelten Ausspielnachweis (Proof of Play) "
            "belegt, verkettet (Merkle-Baum) und vom Bildschirm signiert (Ed25519) — "
            "nachträglich überprüfbar."),
        "confidential": "Vertraulich — {org}",
        "filename": "kampagnenbericht_{id}_{pfrom}_{puntil}.pdf",
    },
}

# (séparateur de milliers, séparateur décimal) par langue
_SEPARATORS = {"en": (",", "."), "fr": (" ", ","), "es": (".", ","), "pt": (".", ","),
               "de": (".", ",")}


def _num(v: float, decimals: int = 0, locale: str | None = None) -> str:
    """Nombre à la mode de la langue : en 1,234.56 · fr 1 234,56 · de/es/pt 1.234,56."""
    thousands, dec = _SEPARATORS[norm_locale(locale)]
    raw = f"{v:,.{decimals}f}"
    return raw.replace(",", "\x00").replace(".", dec).replace("\x00", thousands)


def _n(v: int, locale: str | None = None) -> str:
    return _num(v, 0, locale)


def _pct(v, locale: str | None = None) -> str:
    """Valeur telle que calculée, avec le séparateur décimal de la langue."""
    return str(v).replace(".", _SEPARATORS[norm_locale(locale)][1])


def fmt_generated_at(dt: datetime, locale: str | None = None) -> str:
    """Horodatage « généré le » : en ISO, fr/es/pt jj/mm/aaaa, de jj.mm.aaaa."""
    loc = norm_locale(locale)
    if loc == "en":
        return dt.strftime("%Y-%m-%d %H:%M")
    if loc == "de":
        return dt.strftime("%d.%m.%Y %H:%M")
    return dt.strftime("%d/%m/%Y %H:%M")


def report_filename(id_campaign: int, period_from, period_until,
                    locale: str | None = None) -> str:
    """Nom du fichier téléchargé (ASCII), dans la langue du demandeur."""
    return tr(DOC_TEXTS, locale, "filename", id=id_campaign, pfrom=period_from,
              puntil=period_until)


def build_campaign_pdf(
    *,
    org_name: str,
    payload: dict,
    branding: dict,
    logo_bytes: bytes | None = None,
    generated_at: str = "",
    level: str = "player",
    locale: str | None = None,
) -> bytes:
    """`level` (retour Ed #3) : granularité de la répartition — 'player' (défaut),
    'site' ou 'site_group' (agrégats du service, SOV servi moyen par niveau).
    `locale` : langue du document (défaut anglais)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    def T(key: str, **kw) -> str:  # noqa: N802
        return tr(DOC_TEXTS, locale, key, **kw)

    def N(v) -> str:  # noqa: N802
        return _n(v, locale)

    def P(v) -> str:  # noqa: N802
        return _pct(v, locale)

    try:
        accent = colors.HexColor(branding.get("primary_color") or "#4875B4")
    except (ValueError, TypeError):
        accent = colors.HexColor("#4875B4")
    ink = colors.HexColor("#1C1E2A")
    muted = colors.HexColor("#6B7280")
    line = colors.HexColor("#E5E7EB")

    camp = payload["campaign"]
    totals = payload["totals"]
    sov = payload["sov"]
    amount = payload["amount"]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=T("title", name=camp["campaign_name"]),
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=ink, fontSize=18, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=accent, fontSize=12,
                        spaceBefore=12, spaceAfter=4)
    normal = styles["Normal"]
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=muted)

    story: list = []

    # ── En-tête ───────────────────────────────────────────────────────────────
    if logo_bytes:
        try:
            img = Image(BytesIO(logo_bytes))
            ratio = img.imageWidth / img.imageHeight if img.imageHeight else 1
            img.drawHeight = 16 * mm
            img.drawWidth = min(50 * mm, 16 * mm * ratio)
            img.hAlign = "LEFT"
            story.append(img)
            story.append(Spacer(1, 4 * mm))
        except Exception:
            pass

    story.append(Paragraph(T("title", name=camp["campaign_name"]), h1))
    if camp.get("advertiser_name"):
        story.append(Paragraph(T("advertiser", name=camp["advertiser_name"]), normal))
    story.append(Paragraph(
        T("subtitle", org=org_name, start=camp["starts_on"], end=camp["ends_on"],
          pfrom=payload["period"]["from"], puntil=payload["period"]["until"]),
        small))
    if generated_at:
        story.append(Paragraph(T("generated", when=generated_at), small))
    story.append(Spacer(1, 6 * mm))

    # ── Indicateurs de diffusion ──────────────────────────────────────────────
    story.append(Paragraph(T("sec_delivery"), h2))
    signed_pct = (round(100.0 * totals["signed_plays"] / totals["plays"], 1)
                  if totals["plays"] else 0.0)
    served = "—" if sov["served_pct"] is None else f"{P(sov['served_pct'])} %"
    kpi_rows = [
        [T("kpi_plays"), N(totals["plays"])],
        [T("kpi_signed"), f"{N(totals['signed_plays'])} ({P(signed_pct)} %)"],
        [T("kpi_airtime"), _fmt_duration(totals["airtime_ms"] // 1000)],
        [T("kpi_screens"), T("kpi_screens_val", reached=totals["players_reached"],
                             targeted=totals["players_targeted"])],
        [T("kpi_sov"), T("kpi_sov_val", served=served, sold=P(sov["sold_pct"]))],
    ]
    t = Table(kpi_rows, colWidths=[75 * mm, 99 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), muted),
        ("TEXTCOLOR", (1, 0), (1, -1), ink),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, line),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    # ── Montant ───────────────────────────────────────────────────────────────
    story.append(Paragraph(T("sec_amount"), h2))
    model_label = {"sov": T("model_sov"), "flat": T("model_flat")}.get(amount["model"],
                                                                       amount["model"])
    amount_rows = [[T("sales_model"), model_label]]
    if amount["amount"] is not None:
        amount_rows.append([
            T("amount_campaign"),
            f"{_num(amount['amount'], 2, locale)} {amount['currency']}"])
    if amount["cpm_indicative"] is not None:
        amount_rows.append([
            T("cpm", rate=P(amount["cpm_rate"]), currency=amount["currency"]),
            f"{_num(amount['cpm_indicative'], 2, locale)} {amount['currency']}"])
    at = Table(amount_rows, colWidths=[95 * mm, 79 * mm])
    at.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), muted),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, -1), (1, -1), accent),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, line),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(at)

    # ── Répartition (granularité choisie : écran / site / groupe de sites) ───
    per_site = payload.get("per_site") or []
    per_site_group = payload.get("per_site_group") or []
    if level == "site" and per_site:
        title, first_col = T("dist_site"), T("col_site")
        dist_rows = [(s["site_name"] or T("no_site"),
                      T("screens_plays", n=s["players_reached"], plays=N(s["plays"])),
                      s) for s in per_site[:40]]
    elif level == "site_group" and per_site_group:
        title, first_col = T("dist_site_group"), T("col_site_group")
        dist_rows = [(g["group_name"],
                      T("sites_plays", n=len(g["site_ids"]), plays=N(g["plays"])),
                      g) for g in per_site_group[:40]]
        # Sites hors groupe → ligne agrégée « Autres sites »
        grouped = {sid for g in per_site_group for sid in g["site_ids"]}
        rest = [s for s in per_site if s["id_site"] not in grouped]
        if rest:
            other = {
                "plays": sum(s["plays"] for s in rest),
                "airtime_ms": sum(s["airtime_ms"] for s in rest),
                "sov_served_pct": None,
            }
            dist_rows.append((T("other_sites"),
                              T("sites_plays", n=len(rest), plays=N(other["plays"])), other))
    else:
        title, first_col = T("dist_screen"), T("col_screen")
        dist_rows = [(p["player_name"][:48], N(p["plays"]), p)
                     for p in (payload.get("per_player") or [])[:40]]
    if dist_rows:
        story.append(Paragraph(title, h2))
        rows = [[first_col, T("col_plays"), T("col_airtime"), T("col_sov")]]
        for label, plays_label, obj in dist_rows:
            rows.append([
                str(label)[:48], plays_label,
                _fmt_duration(obj["airtime_ms"] // 1000),
                "—" if obj["sov_served_pct"] is None else f"{P(obj['sov_served_pct'])} %",
            ])
        pt = Table(rows, colWidths=[74 * mm, 36 * mm, 34 * mm, 30 * mm])
        pt.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, 0), muted),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.4, line),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(pt)

    # ── Par créatif ───────────────────────────────────────────────────────────
    per_creative = payload.get("per_creative") or []
    if len(per_creative) > 1:
        story.append(Paragraph(T("dist_creative"), h2))
        rows = [[T("col_creative"), T("col_plays"), T("col_airtime")]]
        for c in per_creative:
            rows.append([c["media_name"][:60], N(c["plays"]),
                         _fmt_duration(c["airtime_ms"] // 1000)])
        ct = Table(rows, colWidths=[110 * mm, 30 * mm, 34 * mm])
        ct.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, 0), muted),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.4, line),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(ct)

    # ── Par jour ──────────────────────────────────────────────────────────────
    per_day = payload.get("per_day") or []
    if per_day:
        story.append(Paragraph(T("sec_daily"), h2))
        rows = [[T("col_date"), T("col_plays"), T("col_airtime")]]
        for d in per_day[-31:]:
            rows.append([d["date"], N(d["plays"]), _fmt_duration(d["airtime_ms"] // 1000)])
        dt = Table(rows, colWidths=[60 * mm, 40 * mm, 74 * mm])
        dt.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, 0), muted),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (2, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.4, line),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(dt)

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(T("footer_proof"), small))
    story.append(Paragraph(T("confidential", org=org_name), small))

    doc.build(story)
    return buf.getvalue()
