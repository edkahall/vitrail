"""Géométrie de mur d'écrans (video wall) + compensation de bezel automatique.

Modèle « canvas virtuel continu » : la source (vidéo/image) est projetée sur un grand
canvas virtuel qui couvre TOUT le mur, **bezels (joints inter-écrans) compris**. Chaque écran
n'affiche que la portion du canvas qui tombe sur sa dalle VISIBLE ; les bandes qui tombent
« derrière » les bezels ne sont pas affichées → un objet en mouvement passe derrière le joint
et réapparaît = compensation de bezel correcte, sur les **deux axes** (gère le 2×2).

Fonction pure (aucune I/O) : `compute_wall_layout(cols, rows, cells, gap_scale)` rend, pour
chaque cellule `(row, col)`, le **sous-rectangle normalisé** `{x, y, w, h}` (fractions 0..1 de
la source) que l'écran de cette cellule doit afficher en plein cadre.

Hypothèse v1 (grille régulière, cas validé HW = mur homogène) : tous les écrans d'une même
COLONNE partagent largeur de dalle + bezels gauche/droit ; tous ceux d'une même LIGNE partagent
hauteur + bezels haut/bas. Les bezels par cellule sont acceptés mais la position des colonnes
/lignes est calculée sur un représentant (1re cellule trouvée). Murs irréguliers = v2.

Données terrain (bureau riri/fifi, 2026-06-11) : dalle 528 mm, bezel 4,5 mm/côté → joint
intérieur 9 mm → fraction cachée ≈ 0.845 % par couture. Le `gap_scale` (slider de fine-tune,
défaut 1.0) élargit/réduit les joints (Ed a préféré une légère sur-compensation à l'œil).
Cf. EVOLUTIONS.md « Video Wall ».
"""

from __future__ import annotations

from typing import Any, Optional

# Clés de bezel attendues sur une cellule (millimètres, défaut 0 si absent).
_BEZEL_KEYS = ("bezel_top_mm", "bezel_bottom_mm", "bezel_left_mm", "bezel_right_mm")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f >= 0 else default
    except (TypeError, ValueError):
        return default


def _cell_key(c: dict) -> Optional[tuple[int, int]]:
    try:
        return int(c["row"]), int(c["col"])
    except (KeyError, TypeError, ValueError):
        return None


def compute_wall_layout(
    cols: int,
    rows: int,
    cells: list[dict],
    gap_scale: float = 1.0,
) -> dict[tuple[int, int], dict]:
    """Calcule le sous-rectangle source `{x,y,w,h}` (0..1) de chaque cellule du mur.

    Args:
        cols, rows : dimensions de la grille (≥1).
        cells : liste de dicts, chacun avec `row`, `col`, `panel_w_mm`, `panel_h_mm` et
            optionnellement `bezel_{top,bottom,left,right}_mm` (défaut 0).
        gap_scale : multiplicateur des joints intérieurs (fine-tune ; 1.0 = géométrie exacte ;
            >1 = légère sur-compensation, le joint masqué s'élargit).

    Returns:
        `{(row, col): {"x", "y", "w", "h"}}` — fractions normalisées de la source. Somme des
        largeurs visibles + joints sur une ligne = 1.0. Cellules absentes de `cells` omises.

    Raises:
        ValueError : grille invalide, ou aucune cellule exploitable.
    """
    if cols < 1 or rows < 1:
        raise ValueError("cols and rows must be ≥ 1")

    by_cell: dict[tuple[int, int], dict] = {}
    for c in cells:
        key = _cell_key(c)
        if key is None:
            continue
        r, col = key
        if 0 <= r < rows and 0 <= col < cols:
            by_cell[key] = c
    if not by_cell:
        raise ValueError("no valid cell in the grid")

    # Représentant par colonne (largeur + bezels L/R) et par ligne (hauteur + bezels T/B) :
    # 1re cellule trouvée pour cette colonne / cette ligne (grille régulière, cf. docstring).
    def _col_rep(col: int) -> Optional[dict]:
        for r in range(rows):
            if (r, col) in by_cell:
                return by_cell[(r, col)]
        return None

    def _row_rep(row: int) -> Optional[dict]:
        for col in range(cols):
            if (row, col) in by_cell:
                return by_cell[(row, col)]
        return None

    # Largeurs de colonne (dalle visible) — défaut 0 si colonne absente (sera ignorée).
    col_w = [_f(_col_rep(j).get("panel_w_mm")) if _col_rep(j) else 0.0 for j in range(cols)]
    row_h = [_f(_row_rep(i).get("panel_h_mm")) if _row_rep(i) else 0.0 for i in range(rows)]

    # Joints intérieurs : entre col j et j+1 = bezel droit(col j) + bezel gauche(col j+1).
    gap_x = []
    for j in range(cols - 1):
        a, b = _col_rep(j), _col_rep(j + 1)
        g = (_f(a.get("bezel_right_mm")) if a else 0.0) + (_f(b.get("bezel_left_mm")) if b else 0.0)
        gap_x.append(g * gap_scale)
    gap_y = []
    for i in range(rows - 1):
        a, b = _row_rep(i), _row_rep(i + 1)
        g = (_f(a.get("bezel_bottom_mm")) if a else 0.0) + (_f(b.get("bezel_top_mm")) if b else 0.0)
        gap_y.append(g * gap_scale)

    canvas_w = sum(col_w) + sum(gap_x)
    canvas_h = sum(row_h) + sum(gap_y)
    if canvas_w <= 0 or canvas_h <= 0:
        raise ValueError("panel dimensions missing or zero (panel_w_mm/panel_h_mm)")

    # Position de départ (mm) de chaque colonne / ligne sur le canvas.
    x_start = [0.0] * cols
    for j in range(1, cols):
        x_start[j] = x_start[j - 1] + col_w[j - 1] + gap_x[j - 1]
    y_start = [0.0] * rows
    for i in range(1, rows):
        y_start[i] = y_start[i - 1] + row_h[i - 1] + gap_y[i - 1]

    out: dict[tuple[int, int], dict] = {}
    for (r, col) in by_cell:
        out[(r, col)] = {
            "x": round(x_start[col] / canvas_w, 6),
            "y": round(y_start[r] / canvas_h, 6),
            "w": round(col_w[col] / canvas_w, 6),
            "h": round(row_h[r] / canvas_h, 6),
        }
    return out


def bezel_overscan(
    rows: int,
    cols: int,
    row: int,
    col: int,
    panel_w_mm: float,
    panel_h_mm: float,
    bezel_left_mm: float = 0.0,
    bezel_right_mm: float = 0.0,
    bezel_top_mm: float = 0.0,
    bezel_bottom_mm: float = 0.0,
    gap_scale: float = 1.0,
) -> dict:
    """Compensation de bezel **mode B** (contenu pré-découpé : chaque écran a SA tuile pleine).

    Chaque écran « sur-balaye » (overscan) sa propre image pour pousser hors-champ, sur ses
    bords INTÉRIEURS (ceux qui ont un voisin), une bande = son propre bezel intérieur → le
    contenu qui serait avalé par le joint réapparaît. Le voisin compense son propre bezel →
    ensemble ils masquent le joint complet. C'est le geste validé HW au bureau (`video-zoom`
    + `video-align`, compatible hwdec VAAPI — contrairement à un filtre crop).

    Sortie **renderer-agnostique** :
      scale_x/scale_y : facteur d'agrandissement (≥1) par axe.
      align_x/align_y : bord d'ancrage ∈ {-1 (gauche/haut), 0 (centre), +1 (droite/bas)} —
        l'agrandissement pousse le contenu vers l'OPPOSÉ de l'ancrage.
    Application : mpv `video-zoom=log2(scale)` + `video-align-x/y` ; CSS `transform: scale()`
    + `transform-origin` (align -1→0%, +1→100%, 0→50%).

    Formule (validée) : overscan d'un axe = somme des bezels intérieurs de cet axe / dalle.
    Réf. bureau : bezel 4,5 mm, dalle 528 mm → scale 1.0085 → `video-zoom` 0.0122.
    """
    pw = _f(panel_w_mm)
    ph = _f(panel_h_mm)
    has_left = col > 0
    has_right = col < cols - 1
    has_top = row > 0
    has_bottom = row < rows - 1

    interior_x = (_f(bezel_left_mm) if has_left else 0.0) + (_f(bezel_right_mm) if has_right else 0.0)
    interior_y = (_f(bezel_top_mm) if has_top else 0.0) + (_f(bezel_bottom_mm) if has_bottom else 0.0)

    scale_x = 1.0 + (interior_x / pw) * gap_scale if pw > 0 else 1.0
    scale_y = 1.0 + (interior_y / ph) * gap_scale if ph > 0 else 1.0

    # Ancrage : si un seul bord d'un axe est intérieur, on ancre le bord OPPOSÉ (l'overscan
    # part vers l'intérieur) ; si les deux (écran central) → centré (overscan symétrique).
    align_x = -1 if (has_right and not has_left) else (1 if (has_left and not has_right) else 0)
    align_y = -1 if (has_bottom and not has_top) else (1 if (has_top and not has_bottom) else 0)

    return {
        "mode": "overscan",
        "scale_x": round(scale_x, 6),
        "scale_y": round(scale_y, 6),
        "align_x": align_x,
        "align_y": align_y,
    }


def player_wall_transform(wall: Optional[dict]) -> Optional[dict]:
    """Transform à pousser au player depuis son bloc `display_config.wall`.

    `wall` porte : `mode` ('overscan' [mode B, défaut] ou 'crop' [mode A source unique]),
    `rows`, `cols`, `cell` {row,col}, `panel_w_mm`, `panel_h_mm`, bezels, `gap_scale`, et —
    pour le mode crop — `cells` (tous les membres). Repli gracieux : None si non applicable
    (le renderer affiche alors plein cadre). Ne lève jamais (ne casse pas le sync)."""
    if not isinstance(wall, dict):
        return None
    cell = wall.get("cell")
    if not isinstance(cell, dict):
        return None
    try:
        rows = int(wall["rows"])
        cols = int(wall["cols"])
        r, c = int(cell["row"]), int(cell["col"])
    except (KeyError, TypeError, ValueError):
        return None
    if rows < 1 or cols < 1 or not (0 <= r < rows and 0 <= c < cols):
        return None
    gap_scale = _f(wall.get("gap_scale"), 1.0) or 1.0

    if wall.get("mode") == "crop":
        # Mode A (source unique tuilée) : sous-rectangle de CE player. On TAGUE `mode:"crop"` —
        # les renderers (CSS / ExoPlayer / mpv) dispatchent sur ce champ ; sans lui, le rect
        # {x,y,w,h} serait pris pour de l'overscan (no-op) et le crop ne s'appliquerait jamais.
        rect = player_wall_rect(wall)
        return {"mode": "crop", **rect} if rect else None

    # Mode B (overscan, défaut) : besoin de la dalle + bezels de CE player.
    pw = _f(wall.get("panel_w_mm"))
    ph = _f(wall.get("panel_h_mm"))
    if pw <= 0 or ph <= 0:
        return None
    return bezel_overscan(
        rows, cols, r, c, pw, ph,
        _f(wall.get("bezel_left_mm")), _f(wall.get("bezel_right_mm")),
        _f(wall.get("bezel_top_mm")), _f(wall.get("bezel_bottom_mm")),
        gap_scale,
    )


def player_wall_rect(
    wall: Optional[dict],
) -> Optional[dict]:
    """Helper haut-niveau : sous-rectangle d'UN player à partir de son bloc `wall`.

    `wall` (issu de `display_config.wall` du player + agrégat groupe) doit porter la grille
    complète : `cols`, `rows`, `cells` (tous les membres), `cell` (la cellule de CE player
    `{row, col}`), `gap_scale` optionnel. Retourne `{x,y,w,h}` de ce player, ou None si non
    applicable (pas de mur, cellule absente). Ne lève pas : un mur mal configuré ne doit pas
    casser le sync (repli = plein cadre côté renderer).
    """
    if not isinstance(wall, dict):
        return None
    cell = wall.get("cell")
    cells = wall.get("cells")
    if not isinstance(cell, dict) or not isinstance(cells, list):
        return None
    try:
        cols = int(wall["cols"])
        rows = int(wall["rows"])
        r, c = int(cell["row"]), int(cell["col"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        layout = compute_wall_layout(cols, rows, cells, _f(wall.get("gap_scale"), 1.0) or 1.0)
    except ValueError:
        return None
    return layout.get((r, c))
