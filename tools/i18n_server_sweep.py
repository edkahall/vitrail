"""Find French strings hard-coded in the backend (comments and docstrings excluded).

    python tools/i18n_server_sweep.py backend/app out.json frontend/messages

A string counts as French if it has a French accent, or more words that only appear in
fr.json than words that only appear in en.json. Blind spot: accent-free French made only
of words absent from both files (e.g. "Frais / Alimentaire") -- review those by hand.
Each hit is tagged with its context (kw:detail = API error, dict:label, log, ...).
"""
import ast, re, sys, json, collections
from pathlib import Path

ROOT = Path(sys.argv[1])
ACCENT = re.compile(r"[éèêàùçôîâûëïÉÈÀÇ]")
WORDS = re.compile(r"\b(le|la|les|des|du|une|est|pas|introuvable|invalide|requis|doit|déjà|aucun|aucune|impossible|accès|refusé|non|autorisé|veuillez|pour|avec|sans|dans|cette|votre|vos)\b", re.I)
EN_HINT = re.compile(r"\b(the|is|not|found|invalid|must|already|cannot|required|access)\b", re.I)

import json as _j
def _words(o, acc):
    if isinstance(o, dict):
        for v in o.values(): _words(v, acc)
    elif isinstance(o, str):
        acc.update(w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", re.sub(r"\{[^}]*\}", " ", o)))
    return acc
_FRONT = Path(sys.argv[3])
_FRW = _words(_j.load(open(_FRONT / "fr.json", encoding="utf-8")), set())
_ENW = _words(_j.load(open(_FRONT / "en.json", encoding="utf-8")), set())
FR_ONLY = _FRW - _ENW
EN_ONLY = _ENW - _FRW
def is_french(s):
    if len(s) < 4 or not re.search(r"[A-Za-zÀ-ÿ]{3}", s):
        return False
    if re.fullmatch(r"[a-z0-9_.:/-]+", s):      # identifiers, keys, paths
        return False
    if ACCENT.search(s):
        return True
    ws = [w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", s)]
    fr = sum(w in FR_ONLY for w in ws); en = sum(w in EN_ONLY for w in ws)
    return fr >= 1 and fr > en

def docstring_ids(tree):
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = getattr(n, "body", [])
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                ids.add(id(b[0].value))
    return ids

def context(parents, node):
    """Walk up the parents to tell how the string is used."""
    p = node
    for _ in range(6):
        p = parents.get(id(p))
        if p is None:
            break
        if isinstance(p, ast.keyword) and p.arg in ("detail", "message", "label", "description", "title", "subject", "reason", "error", "msg", "help", "name", "folder_name", "text", "body"):
            return f"kw:{p.arg}"
        if isinstance(p, ast.Call):
            f = p.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if name in ("info", "warning", "error", "debug", "exception", "critical", "print"):
                return "log"
            if name.endswith("Exception") or name.endswith("Error") or name in ("_err", "_fail", "HTTPException"):
                return f"raise:{name}"
            return f"call:{name}"
        if isinstance(p, ast.Dict):
            for k, v in zip(p.keys, p.values):
                if v is node or (isinstance(v, ast.JoinedStr) and node in ast.walk(v)):
                    if isinstance(k, ast.Constant):
                        return f"dict:{k.value}"
            return "dict"
        if isinstance(p, ast.Assign):
            t = p.targets[0]
            return "assign:" + (t.id if isinstance(t, ast.Name) else type(t).__name__)
        if isinstance(p, ast.Return):
            return "return"
    return "other"

out = []
for f in sorted(ROOT.rglob("*.py")):
    if "/tests/" in f.as_posix() or "alembic" in f.as_posix():
        continue
    src = f.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    docs = docstring_ids(tree)
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[id(c)] = n
    seen_joined = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            txt = "".join(v.value if isinstance(v, ast.Constant) else "{…}" for v in n.values)
            for v in n.values:
                seen_joined.add(id(v))
            if is_french(txt):
                out.append((f.relative_to(ROOT).as_posix(), n.lineno, context(parents, n), txt))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and id(n) not in seen_joined:
            if is_french(n.value):
                out.append((f.relative_to(ROOT).as_posix(), n.lineno, context(parents, n), n.value))

json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
cats = collections.Counter(c.split(":")[0] for _, _, c, _ in out)
files = collections.Counter(f for f, *_ in out)
print("total", len(out)); print("by context", dict(cats))
print("top files"); [print(f"  {n:4} {f}") for f, n in files.most_common(25)]
