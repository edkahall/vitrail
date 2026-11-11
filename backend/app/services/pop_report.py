"""Génération du rapport/certificat Proof of Play en PDF (§XXIV).

Document unique et organisé couvrant un ou plusieurs players (sélection libre, par groupe ou par
site, résolue côté endpoint) : résumé global + une section par player (intégrité de chaîne,
compteurs signés/non-signés, Merkle root, tête de chaîne). Sert de preuve vérifiable à fournir à
un annonceur / pour une déclaration.

reportlab est importé PARESSEUSEMENT (dans la fonction) : le module reste importable même si la
dépendance n'est pas encore installée (fenêtre avant rebuild d'image) — seul l'appel PDF échoue.
"""

from __future__ import annotations

from io import BytesIO

from app.services.doc_i18n import tr

# Textes du rapport PoP (PDF) ET des sorties voisines qui en dépendent (CSV /pop/plays.csv,
# libellés de périmètre de pop.py, archive journalière du worker). Langue = celle du profil
# du demandeur ; archive journalière (aucun utilisateur) → anglais.
# CSV : en-têtes ASCII sans espace (lus aussi par des tableurs/outils), séparés par des virgules.
DOC_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Proof of Play report",
        "org_period": "Organization: <b>{org}</b> &nbsp;·&nbsp; Period: "
                      "<b>{from_date}</b> → <b>{until_date}</b>",
        "scope": "Scope: {scope}",
        "generated": "Generated on {at}",
        "total_plays": "Total plays",
        "of_screen_off": "Of which screen off (not counted)",
        "of_covered": "Of which zone covered (not counted)",
        "players_covered": "Players covered",
        "merkle_global": "Global Merkle root",
        "chain_valid": "Chain valid",
        "chain_broken": "CHAIN TAMPERED",
        "plays": "Plays",
        "total_duration": "Total duration",
        "signed": "Signed ✓",
        "unsigned": "Unsigned",
        "sig_invalid": "Invalid signatures",
        "integrity": "Integrity",
        "covered_zone": "Zone covered",
        "screen_off": "Screen off",
        "not_counted": "not counted",
        "merkle_root": "Merkle root: <font face='Courier'>{value}</font>",
        "chain_head": "Chain head: <font face='Courier'>{value}</font>",
        "footer": "Each play is chained by SHA-256 hash (tamper-evidence) and, when the player "
                  "supports it, signed with an Ed25519 key unique to the device. An invalid "
                  "signature or a broken chain is reported above.",
        "scope_group": "Group #{id}",
        "scope_site": "Site #{id}",
        "scope_selected": "{n} selected player(s)",
        "scope_all": "All players of the organization",
        "scope_daily_archive": "Proof of Play archive (daily)",
        "csv_header": "Player,Media,Played_at_UTC,Duration_sec,Signed,Screen_off,"
                      "Zone_covered,Chain_hash",
        "yes": "yes",
        "no": "no",
    },
    "fr": {
        "title": "Rapport Proof of Play",
        "org_period": "Organisation : <b>{org}</b> &nbsp;·&nbsp; Période : "
                      "<b>{from_date}</b> → <b>{until_date}</b>",
        "scope": "Périmètre : {scope}",
        "generated": "Généré le {at}",
        "total_plays": "Total diffusions",
        "of_screen_off": "Dont écran coupé (non comptées)",
        "of_covered": "Dont zone recouverte (non comptées)",
        "players_covered": "Players couverts",
        "merkle_global": "Merkle root global",
        "chain_valid": "Chaîne valide",
        "chain_broken": "CHAÎNE ALTÉRÉE",
        "plays": "Diffusions",
        "total_duration": "Durée totale",
        "signed": "Signées ✓",
        "unsigned": "Non signées",
        "sig_invalid": "Signatures invalides",
        "integrity": "Intégrité",
        "covered_zone": "Zone recouverte",
        "screen_off": "Écran coupé",
        "not_counted": "non comptabilisées",
        "merkle_root": "Merkle root : <font face='Courier'>{value}</font>",
        "chain_head": "Tête de chaîne : <font face='Courier'>{value}</font>",
        "footer": "Chaque diffusion est chaînée par hash SHA-256 (anti-altération) et, lorsque le "
                  "player le supporte, signée par une clé Ed25519 propre au device. Une signature "
                  "invalide ou une rupture de chaîne est signalée ci-dessus.",
        "scope_group": "Groupe #{id}",
        "scope_site": "Site #{id}",
        "scope_selected": "{n} player(s) sélectionné(s)",
        "scope_all": "Tous les players de l'organisation",
        "scope_daily_archive": "Archive Proof of Play (journalière)",
        "csv_header": "Player,Media,Diffuse_le_UTC,Duree_sec,Signe,Ecran_coupe,"
                      "Zone_recouverte,Hash_chaine",
        "yes": "oui",
        "no": "non",
    },
    "es": {
        "title": "Informe Proof of Play",
        "org_period": "Organización: <b>{org}</b> &nbsp;·&nbsp; Periodo: "
                      "<b>{from_date}</b> → <b>{until_date}</b>",
        "scope": "Alcance: {scope}",
        "generated": "Generado el {at}",
        "total_plays": "Total de emisiones",
        "of_screen_off": "De ellas, pantalla apagada (no contadas)",
        "of_covered": "De ellas, zona cubierta (no contadas)",
        "players_covered": "Players incluidos",
        "merkle_global": "Merkle root global",
        "chain_valid": "Cadena válida",
        "chain_broken": "CADENA ALTERADA",
        "plays": "Emisiones",
        "total_duration": "Duración total",
        "signed": "Firmadas ✓",
        "unsigned": "No firmadas",
        "sig_invalid": "Firmas no válidas",
        "integrity": "Integridad",
        "covered_zone": "Zona cubierta",
        "screen_off": "Pantalla apagada",
        "not_counted": "no contabilizadas",
        "merkle_root": "Merkle root: <font face='Courier'>{value}</font>",
        "chain_head": "Cabeza de cadena: <font face='Courier'>{value}</font>",
        "footer": "Cada emisión se encadena mediante hash SHA-256 (antimanipulación) y, cuando el "
                  "player lo admite, se firma con una clave Ed25519 propia del dispositivo. Una "
                  "firma no válida o una ruptura de cadena se indica más arriba.",
        "scope_group": "Grupo #{id}",
        "scope_site": "Sitio #{id}",
        "scope_selected": "{n} player(s) seleccionado(s)",
        "scope_all": "Todos los players de la organización",
        "scope_daily_archive": "Archivo Proof of Play (diario)",
        "csv_header": "Player,Medio,Emitido_UTC,Duracion_seg,Firmado,Pantalla_apagada,"
                      "Zona_cubierta,Hash_cadena",
        "yes": "si",
        "no": "no",
    },
    "pt": {
        "title": "Relatório Proof of Play",
        "org_period": "Organização: <b>{org}</b> &nbsp;·&nbsp; Período: "
                      "<b>{from_date}</b> → <b>{until_date}</b>",
        "scope": "Âmbito: {scope}",
        "generated": "Gerado em {at}",
        "total_plays": "Total de difusões",
        "of_screen_off": "Das quais com ecrã desligado (não contadas)",
        "of_covered": "Das quais com zona coberta (não contadas)",
        "players_covered": "Players abrangidos",
        "merkle_global": "Merkle root global",
        "chain_valid": "Cadeia válida",
        "chain_broken": "CADEIA ALTERADA",
        "plays": "Difusões",
        "total_duration": "Duração total",
        "signed": "Assinadas ✓",
        "unsigned": "Não assinadas",
        "sig_invalid": "Assinaturas inválidas",
        "integrity": "Integridade",
        "covered_zone": "Zona coberta",
        "screen_off": "Ecrã desligado",
        "not_counted": "não contabilizadas",
        "merkle_root": "Merkle root: <font face='Courier'>{value}</font>",
        "chain_head": "Cabeça da cadeia: <font face='Courier'>{value}</font>",
        "footer": "Cada difusão é encadeada por hash SHA-256 (anti-adulteração) e, quando o "
                  "player o suporta, assinada com uma chave Ed25519 própria do dispositivo. Uma "
                  "assinatura inválida ou uma quebra de cadeia é indicada acima.",
        "scope_group": "Grupo #{id}",
        "scope_site": "Local #{id}",
        "scope_selected": "{n} player(s) selecionado(s)",
        "scope_all": "Todos os players da organização",
        "scope_daily_archive": "Arquivo Proof of Play (diário)",
        "csv_header": "Player,Media,Difundido_UTC,Duracao_seg,Assinado,Ecra_desligado,"
                      "Zona_coberta,Hash_cadeia",
        "yes": "sim",
        "no": "nao",
    },
    "de": {
        "title": "Proof-of-Play-Bericht",
        "org_period": "Organisation: <b>{org}</b> &nbsp;·&nbsp; Zeitraum: "
                      "<b>{from_date}</b> → <b>{until_date}</b>",
        "scope": "Umfang: {scope}",
        "generated": "Erstellt am {at}",
        "total_plays": "Wiedergaben gesamt",
        "of_screen_off": "Davon Bildschirm aus (nicht gezählt)",
        "of_covered": "Davon Zone verdeckt (nicht gezählt)",
        "players_covered": "Erfasste Player",
        "merkle_global": "Globaler Merkle-Root",
        "chain_valid": "Kette gültig",
        "chain_broken": "KETTE MANIPULIERT",
        "plays": "Wiedergaben",
        "total_duration": "Gesamtdauer",
        "signed": "Signiert ✓",
        "unsigned": "Nicht signiert",
        "sig_invalid": "Ungültige Signaturen",
        "integrity": "Integrität",
        "covered_zone": "Zone verdeckt",
        "screen_off": "Bildschirm aus",
        "not_counted": "nicht gezählt",
        "merkle_root": "Merkle-Root: <font face='Courier'>{value}</font>",
        "chain_head": "Kettenkopf: <font face='Courier'>{value}</font>",
        "footer": "Jede Wiedergabe ist per SHA-256-Hash verkettet (Manipulationsschutz) und, "
                  "sofern der Player es unterstützt, mit einem gerätespezifischen Ed25519-"
                  "Schlüssel signiert. Eine ungültige Signatur oder ein Kettenbruch wird oben "
                  "gemeldet.",
        "scope_group": "Gruppe #{id}",
        "scope_site": "Standort #{id}",
        "scope_selected": "{n} ausgewählte(r) Player",
        "scope_all": "Alle Player der Organisation",
        "scope_daily_archive": "Proof-of-Play-Archiv (täglich)",
        "csv_header": "Player,Medium,Gespielt_UTC,Dauer_Sek,Signiert,Bildschirm_aus,"
                      "Zone_verdeckt,Ketten_Hash",
        "yes": "ja",
        "no": "nein",
    },
}


def csv_header(locale: str | None) -> list[str]:
    """En-tête du CSV /pop/plays.csv dans la langue demandée (ASCII, sans espace)."""
    return tr(DOC_TEXTS, locale, "csv_header").split(",")


def _fmt_duration(total_sec: int) -> str:
    h, rem = divmod(int(total_sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def build_pop_report_pdf(report: dict, *, locale: str | None = None) -> bytes:
    """Construit le PDF à partir d'un dict structuré (voir endpoint pop.report_pdf).

    report = {
        org_name, from_date, until_date, generated_at,
        total_plays, total_screen_off, total_covered, merkle_root, scope_label,
        players: [{player_name, plays, duration_sec, screen_off, covered, signed, unsigned,
                   signature_invalid, chain_valid, chain_head, merkle_root}]
    }

    `total_plays` / `plays` = diffusions COMPTABILISÉES. Deux causes les en excluent, toutes
    deux affichées séparément et jamais ajoutées à l'antenne :
      · `screen_off` — sortie vidéo coupée (migr 0102) ;
      · `covered`    — zone recouverte par un overlay plein écran, spot de campagne P3.6 ou
        takeover VOD (migr 0125).
    Elles restent dans la chaîne et le merkle_root, qui portent sur toutes les preuves —
    c'est ce qui rend l'attestation recalculable par un tiers.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    def T(key: str, **kw) -> str:
        return tr(DOC_TEXTS, locale, key, **kw)

    accent = colors.HexColor("#4875B4")
    ink = colors.HexColor("#1D2B3A")
    ok = colors.HexColor("#2F855A")
    bad = colors.HexColor("#C53030")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=T("title"),
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=ink, fontSize=18, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=accent, fontSize=13,
                        spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8,
                           textColor=colors.HexColor("#5A6473"))

    story: list = []
    story.append(Paragraph(T("title"), h1))
    story.append(Paragraph(
        T("org_period", org=report["org_name"], from_date=report["from_date"],
          until_date=report["until_date"]), styles["Normal"]))
    if report.get("scope_label"):
        story.append(Paragraph(T("scope", scope=report["scope_label"]), styles["Normal"]))
    story.append(Paragraph(T("generated", at=report["generated_at"]), small))
    story.append(Spacer(1, 8))

    # Résumé global. Les lignes « non comptées » ne s'affichent que s'il y a matière :
    # ne pas polluer un rapport nominal avec des zéros.
    summary = [[T("total_plays"), str(report["total_plays"])]]
    if report.get("total_screen_off"):
        summary.append([T("of_screen_off"), str(report["total_screen_off"])])
    if report.get("total_covered"):
        summary.append([T("of_covered"), str(report["total_covered"])])
    summary.append([T("players_covered"), str(len(report["players"]))])
    summary.append([T("merkle_global"), report.get("merkle_root") or "—"])
    # Le Merkle root est la DERNIÈRE ligne : on cible son index calculé, jamais un index
    # écrit en dur — les lignes conditionnelles ci-dessus le décalent (la police Courier
    # atterrissait sur « Players couverts » dès qu'une preuve était marquée).
    _merkle_row = len(summary) - 1
    t = Table(summary, colWidths=[45 * mm, 125 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (1, _merkle_row), (1, _merkle_row), "Courier", 7),
        ("TEXTCOLOR", (0, 0), (0, -1), ink),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F4F8")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE7")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE7")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    # Sections par player
    for p in report["players"]:
        story.append(Paragraph(p["player_name"], h2))
        integ = T("chain_valid") if p["chain_valid"] else T("chain_broken")
        integ_color = ok if p["chain_valid"] else bad
        rows = [
            [T("plays"), str(p["plays"]), T("total_duration"), _fmt_duration(p["duration_sec"])],
            [T("signed"), str(p["signed"]), T("unsigned"), str(p["unsigned"])],
            [T("sig_invalid"), str(p["signature_invalid"]), T("integrity"), integ],
        ]
        if p.get("covered"):
            rows.insert(1, [T("covered_zone"), str(p["covered"]), "", T("not_counted")])
        if p.get("screen_off"):
            rows.insert(1, [T("screen_off"), str(p["screen_off"]), "", T("not_counted")])
        pt = Table(rows, colWidths=[38 * mm, 25 * mm, 38 * mm, 69 * mm])
        pt.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
            ("TEXTCOLOR", (0, 0), (0, -1), ink), ("TEXTCOLOR", (2, 0), (2, -1), ink),
            ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
            ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9),
            ("TEXTCOLOR", (3, 2), (3, 2), integ_color),
            ("TEXTCOLOR", (1, 2), (1, 2), bad if p["signature_invalid"] else ink),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE7")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE7")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(pt)
        story.append(Spacer(1, 2))
        _mr = p.get("merkle_root") or "—"
        _ch = p.get("chain_head") or "—"
        story.append(Paragraph(T("merkle_root", value=_mr), small))
        story.append(Paragraph(T("chain_head", value=_ch), small))

    story.append(Spacer(1, 10))
    story.append(Paragraph(T("footer"), small))

    doc.build(story)
    return buf.getvalue()
