"""Rapport exécutif PDF white-label (§24.3).

reportlab importé PARESSEUSEMENT (comme pop_report) → le module reste importable même si
reportlab n'est pas installé. Le rapport reprend les KPIs de l'Executive Dashboard + la
flotte + le top contenus + l'estimation ROI, brandé aux couleurs/logo de l'org.

Langue : celle du profil de la personne à qui le document est destiné (`locale`, défaut
anglais) — cf. services/doc_i18n.py.
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

from app.services.doc_i18n import norm_locale, tr

DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "default_title": "Executive report",
        "period_line": "{org} — period from {from_d} to {until_d}",
        "generated_on": "Generated on {when}",
        "h_kpis": "Key indicators",
        "kpi_health": "Network Health Score",
        "kpi_uptime": "Network uptime",
        "kpi_conformity": "Playback compliance",
        "kpi_players": "Active players",
        "kpi_alerts": "Active alerts (24 h)",
        "kpi_content": "Content played",
        "plays_line": "{plays} plays · {duration}",
        "h_fleet": "Fleet status",
        "fleet_online": "Online",
        "fleet_warning": "Warning",
        "fleet_offline": "Offline",
        "fleet_maintenance": "Maintenance",
        "h_top": "Top content played",
        "col_content": "Content",
        "col_plays": "Plays",
        "col_duration": "Duration",
        "h_roi": "Estimated network value (ROI)",
        "roi_contacts": "Estimated contacts over the period",
        "roi_value": "Network value (CPM)",
        "roi_paper": "Estimated paper savings",
        "roi_total": "Estimated cumulative ROI",
        "roi_assumptions": (
            "Assumptions: {screens} screens, {hours} h/day, {contacts} contacts/h/screen, "
            "CPM {cpm} EUR, poster avoided {paper} EUR/month/screen, uptime {uptime}. "
            "Indicative values."),
        "confidential": "Confidential — {org}",
    },
    "fr": {
        "default_title": "Rapport exécutif",
        "period_line": "{org} — période du {from_d} au {until_d}",
        "generated_on": "Généré le {when}",
        "h_kpis": "Indicateurs clés",
        "kpi_health": "Network Health Score",
        "kpi_uptime": "Uptime réseau",
        "kpi_conformity": "Conformité de diffusion",
        "kpi_players": "Players actifs",
        "kpi_alerts": "Alertes actives (24 h)",
        "kpi_content": "Contenu diffusé",
        "plays_line": "{plays} diffusions · {duration}",
        "h_fleet": "Statut de la flotte",
        "fleet_online": "En ligne",
        "fleet_warning": "Attention",
        "fleet_offline": "Hors ligne",
        "fleet_maintenance": "Maintenance",
        "h_top": "Top contenus diffusés",
        "col_content": "Contenu",
        "col_plays": "Diffusions",
        "col_duration": "Durée",
        "h_roi": "Estimation de la valeur réseau (ROI)",
        "roi_contacts": "Contacts estimés sur la période",
        "roi_value": "Valeur réseau (CPM)",
        "roi_paper": "Économie papier estimée",
        "roi_total": "ROI cumulé estimé",
        "roi_assumptions": (
            "Hypothèses : {screens} écrans, {hours} h/j, {contacts} contacts/h/écran, "
            "CPM {cpm} EUR, affiche évitée {paper} EUR/mois/écran, uptime {uptime}. "
            "Valeurs indicatives."),
        "confidential": "Confidentiel — {org}",
    },
    "es": {
        "default_title": "Informe ejecutivo",
        "period_line": "{org} — período del {from_d} al {until_d}",
        "generated_on": "Generado el {when}",
        "h_kpis": "Indicadores clave",
        "kpi_health": "Network Health Score",
        "kpi_uptime": "Disponibilidad de la red",
        "kpi_conformity": "Conformidad de difusión",
        "kpi_players": "Players activos",
        "kpi_alerts": "Alertas activas (24 h)",
        "kpi_content": "Contenido difundido",
        "plays_line": "{plays} difusiones · {duration}",
        "h_fleet": "Estado de la flota",
        "fleet_online": "En línea",
        "fleet_warning": "Atención",
        "fleet_offline": "Fuera de línea",
        "fleet_maintenance": "Mantenimiento",
        "h_top": "Contenidos más difundidos",
        "col_content": "Contenido",
        "col_plays": "Difusiones",
        "col_duration": "Duración",
        "h_roi": "Estimación del valor de la red (ROI)",
        "roi_contacts": "Contactos estimados en el período",
        "roi_value": "Valor de la red (CPM)",
        "roi_paper": "Ahorro de papel estimado",
        "roi_total": "ROI acumulado estimado",
        "roi_assumptions": (
            "Hipótesis: {screens} pantallas, {hours} h/día, {contacts} contactos/h/pantalla, "
            "CPM {cpm} EUR, cartel evitado {paper} EUR/mes/pantalla, disponibilidad {uptime}. "
            "Valores indicativos."),
        "confidential": "Confidencial — {org}",
    },
    "pt": {
        "default_title": "Relatório executivo",
        "period_line": "{org} — período de {from_d} a {until_d}",
        "generated_on": "Gerado em {when}",
        "h_kpis": "Indicadores-chave",
        "kpi_health": "Network Health Score",
        "kpi_uptime": "Disponibilidade da rede",
        "kpi_conformity": "Conformidade de difusão",
        "kpi_players": "Players ativos",
        "kpi_alerts": "Alertas ativos (24 h)",
        "kpi_content": "Conteúdo difundido",
        "plays_line": "{plays} difusões · {duration}",
        "h_fleet": "Estado da frota",
        "fleet_online": "Online",
        "fleet_warning": "Atenção",
        "fleet_offline": "Offline",
        "fleet_maintenance": "Manutenção",
        "h_top": "Conteúdos mais difundidos",
        "col_content": "Conteúdo",
        "col_plays": "Difusões",
        "col_duration": "Duração",
        "h_roi": "Estimativa do valor da rede (ROI)",
        "roi_contacts": "Contactos estimados no período",
        "roi_value": "Valor da rede (CPM)",
        "roi_paper": "Poupança de papel estimada",
        "roi_total": "ROI acumulado estimado",
        "roi_assumptions": (
            "Pressupostos: {screens} ecrãs, {hours} h/dia, {contacts} contactos/h/ecrã, "
            "CPM {cpm} EUR, cartaz evitado {paper} EUR/mês/ecrã, disponibilidade {uptime}. "
            "Valores indicativos."),
        "confidential": "Confidencial — {org}",
    },
    "de": {
        "default_title": "Management-Bericht",
        "period_line": "{org} — Zeitraum vom {from_d} bis {until_d}",
        "generated_on": "Erstellt am {when}",
        "h_kpis": "Kennzahlen",
        "kpi_health": "Network Health Score",
        "kpi_uptime": "Netzwerk-Verfügbarkeit",
        "kpi_conformity": "Ausspielungstreue",
        "kpi_players": "Aktive Player",
        "kpi_alerts": "Aktive Warnungen (24 h)",
        "kpi_content": "Ausgespielte Inhalte",
        "plays_line": "{plays} Ausspielungen · {duration}",
        "h_fleet": "Flottenstatus",
        "fleet_online": "Online",
        "fleet_warning": "Warnung",
        "fleet_offline": "Offline",
        "fleet_maintenance": "Wartung",
        "h_top": "Meistgespielte Inhalte",
        "col_content": "Inhalt",
        "col_plays": "Ausspielungen",
        "col_duration": "Dauer",
        "h_roi": "Geschätzter Netzwerkwert (ROI)",
        "roi_contacts": "Geschätzte Kontakte im Zeitraum",
        "roi_value": "Netzwerkwert (CPM)",
        "roi_paper": "Geschätzte Papiereinsparung",
        "roi_total": "Geschätzter kumulierter ROI",
        "roi_assumptions": (
            "Annahmen: {screens} Bildschirme, {hours} h/Tag, {contacts} Kontakte/h/Bildschirm, "
            "CPM {cpm} EUR, eingespartes Plakat {paper} EUR/Monat/Bildschirm, "
            "Verfügbarkeit {uptime}. Richtwerte."),
        "confidential": "Vertraulich — {org}",
    },
}


# ── Formats (nombres / dates) selon la langue ────────────────────────────────

def _int(n: float, locale: str | None) -> str:
    """Entier groupé : en 1,234 · fr/pt 1 234 · de/es 1.234."""
    loc = norm_locale(locale)
    s = f"{int(round(float(n))):,}"
    if loc == "en":
        return s
    return s.replace(",", "." if loc in ("de", "es") else " ")


def _dec(v, locale: str | None) -> str:
    """Nombre tel quel (pas d'arrondi), séparateur décimal de la langue."""
    s = str(v)
    return s if norm_locale(locale) == "en" else s.replace(".", ",")


def _pct(v, locale: str | None) -> str:
    return f"{_dec(v, locale)}%" if norm_locale(locale) == "en" else f"{_dec(v, locale)} %"


def fmt_date(d: date | str, locale: str | None) -> str:
    """Date du document : en ISO, de 31.12.2026, fr/es/pt 31/12/2026."""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    loc = norm_locale(locale)
    if loc == "en":
        return d.isoformat()
    return d.strftime("%d.%m.%Y" if loc == "de" else "%d/%m/%Y")


def fmt_datetime(dt: datetime, locale: str | None) -> str:
    return f"{fmt_date(dt.date(), locale)} {dt.strftime('%H:%M')}"


def _fmt_duration(total_sec: int) -> str:
    h, rem = divmod(int(total_sec), 3600)
    m = rem // 60
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m"
    return f"{total_sec}s"


def _eur(v: float, locale: str | None = None) -> str:
    return f"{_int(v, locale)} EUR"


def build_executive_pdf(
    *,
    org_name: str,
    payload: dict,
    branding: dict,
    roi: dict | None,
    logo_bytes: bytes | None = None,
    generated_at: str = "",
    locale: str | None = None,
) -> bytes:
    """Construit le rapport exécutif PDF. `payload` = sortie de build_executive_payload.
    `generated_at` = horodatage déjà formaté (cf. `fmt_datetime`)."""
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

    def _t(key: str, **kw) -> str:
        return tr(DOC_TEXTS, locale, key, **kw)

    try:
        accent = colors.HexColor(branding.get("primary_color") or "#4875B4")
    except (ValueError, TypeError):
        accent = colors.HexColor("#4875B4")
    ink = colors.HexColor("#1C1E2A")
    muted = colors.HexColor("#6B7280")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=branding.get("report_title") or _t("default_title"),
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=ink, fontSize=18, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=accent, fontSize=12,
                        spaceBefore=12, spaceAfter=4)
    normal = styles["Normal"]
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=muted)

    story: list = []

    # ── En-tête (logo + titre + sous-titre + période) ─────────────────────────
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

    story.append(Paragraph(branding.get("report_title") or _t("default_title"), h1))
    if branding.get("report_subtitle"):
        story.append(Paragraph(branding["report_subtitle"], normal))
    story.append(Paragraph(
        _t("period_line", org=org_name, from_d=fmt_date(payload["from_date"], locale),
          until_d=fmt_date(payload["until_date"], locale)), small))
    if generated_at:
        story.append(Paragraph(_t("generated_on", when=generated_at), small))
    story.append(Spacer(1, 6 * mm))

    # ── KPIs ──────────────────────────────────────────────────────────────────
    story.append(Paragraph(_t("h_kpis"), h2))
    fleet = payload["fleet"]
    hs = payload["health_score"]
    kpi_rows = [
        [_t("kpi_health"), "—" if hs is None else f"{hs} / 100"],
        [_t("kpi_uptime"),
         "—" if payload["uptime_pct"] is None else _pct(payload["uptime_pct"], locale)],
        [_t("kpi_conformity"),
         "—" if payload["conformity_pct"] is None else _pct(payload["conformity_pct"], locale)],
        [_t("kpi_players"), f"{fleet['online']} / {fleet['total']}"],
        [_t("kpi_alerts"), str(payload["alerts_active"])],
        [_t("kpi_content"),
         _t("plays_line", plays=_int(payload["plays_count"], locale),
           duration=_fmt_duration(payload["play_seconds"]))],
    ]
    t = Table(kpi_rows, colWidths=[75 * mm, 99 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), muted),
        ("TEXTCOLOR", (1, 0), (1, -1), ink),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    # ── Flotte ────────────────────────────────────────────────────────────────
    story.append(Paragraph(_t("h_fleet"), h2))
    fl = Table(
        [[_t("fleet_online"), _t("fleet_warning"), _t("fleet_offline"),
          _t("fleet_maintenance")],
         [str(fleet["online"]), str(fleet["warning"]), str(fleet["offline"]),
          str(fleet["maintenance"])]],
        colWidths=[43.5 * mm] * 4,
    )
    fl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TEXTCOLOR", (0, 0), (-1, 0), muted),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 1), (0, 1), colors.HexColor("#2E7D32")),
        ("TEXTCOLOR", (1, 1), (1, 1), colors.HexColor("#E65100")),
        ("TEXTCOLOR", (2, 1), (2, 1), colors.HexColor("#C62828")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
    ]))
    story.append(fl)

    # ── Top contenus ──────────────────────────────────────────────────────────
    top = payload.get("top_content") or []
    if top:
        story.append(Paragraph(_t("h_top"), h2))
        rows = [["#", _t("col_content"), _t("col_plays"), _t("col_duration")]]
        for i, c in enumerate(top, 1):
            rows.append([str(i), (c.get("media_name") or f"#{c.get('id_media')}")[:60],
                         _int(c["plays"], locale), _fmt_duration(c["seconds"])])
        tc = Table(rows, colWidths=[10 * mm, 104 * mm, 30 * mm, 30 * mm])
        tc.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, 0), muted),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#E5E7EB")),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(tc)

    # ── ROI ───────────────────────────────────────────────────────────────────
    if roi:
        story.append(Paragraph(_t("h_roi"), h2))
        roi_rows = [
            [_t("roi_contacts"), _int(roi["impressions"], locale)],
            [_t("roi_value"), _eur(roi["network_value_eur"], locale)],
            [_t("roi_paper"), _eur(roi["paper_savings_eur"], locale)],
            [_t("roi_total"), _eur(roi["total_roi_eur"], locale)],
        ]
        rt = Table(roi_rows, colWidths=[95 * mm, 79 * mm])
        rt.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("TEXTCOLOR", (0, 0), (0, -1), muted),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (1, -1), (1, -1), accent),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E5E7EB")),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(rt)
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(_t(
            "roi_assumptions", screens=roi["screens"],
            hours=_dec(roi["hours_per_day"], locale),
            contacts=_dec(roi["contacts_per_hour"], locale),
            cpm=_dec(roi["cpm_eur"], locale), paper=_dec(roi["paper_eur_per_month"], locale),
            uptime=_pct(roi["uptime_pct"], locale)), small))

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(_t("confidential", org=org_name), small))

    doc.build(story)
    return buf.getvalue()
