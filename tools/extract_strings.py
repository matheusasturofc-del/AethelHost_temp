#!/usr/bin/env python3
"""Lista os textos em português da interface e do backend (base para o dicionário PT->EN de js/i18n.js).

Uso:  python tools/extract_strings.py            -> imprime todos
      python tools/extract_strings.py --missing  -> só os que ainda não estão no dicionário
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PT_HINT = re.compile(r"[áàâãéêíóôõúçÁÉÍÓÚÇ]|\b(de|do|da|dos|das|para|com|sem|não|nao|um|uma|o|a|os|as|em|no|na|seu|sua|você|servidor|mundo|jogador|arquivo|pasta|erro|criar|ligar|desligar|escolha|aceita)\b", re.I)


class Texts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ("placeholder", "title", "alt", "aria-label") and v:
                self.out.append(v.strip())

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            for piece in re.split(r"\s{2,}|\n", data):
                if piece.strip():
                    self.out.append(re.sub(r"\s+", " ", piece.strip()))


def html_texts(path):
    p = Texts()
    p.feed(path.read_text(encoding="utf-8"))
    return p.out


LITERAL = re.compile(r'''(?<![\w$])(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`)''')


def literals(text):
    # Linha por linha: um template aninhado (crase dentro de crase) só atrapalha a linha dele, não o arquivo todo.
    for line in text.splitlines():
        for m in LITERAL.finditer(line):
            s = next(g for g in m.groups() if g is not None)
            yield s.replace("\\n", "\n")
        for m in re.finditer(r"`([^`\n]*)`", line):  # templates com crase por dentro: pega o texto entre as crases
            yield m.group(1)


def strip_js_comments(text):
    """Tira comentários (// e /* */) sem mexer em // dentro de textos como "http://…"."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        lines.append(re.sub(r"(?<=[;{},)])\s+//\s.*$", "", line))
    return "\n".join(lines)


def js_texts(text):
    out = []
    for s in literals(strip_js_comments(text)):
        s = s.strip()
        if len(s) < 3 or s.startswith(("http", "/", ".", "#", "&")) or re.fullmatch(r"[\w\-./:]+", s):
            continue
        if re.search(r"=>|[{};]\s*$", s) and "${" not in s:
            continue
        if PT_HINT.search(s):
            out.append(s)
    return out


def py_texts(text):
    out = []
    for m in re.finditer(r'''(?:ApiError\(\s*\d+\s*,\s*|ContentError\(\s*\d+\s*,\s*|bad\(\s*|RuntimeError\(\s*|self\.log\(\s*|log\(\s*|raise ValueError\(\s*)(f?)"((?:[^"\\\n]|\\.)*)"''', text):
        out.append(m.group(2))
    for m in re.finditer(r'''(?:say|echo)\s+"([^"\n]*)"''', text):  # mensagens dos scripts que rodam na VPS
        out.append(m.group(1))
    for m in re.finditer(r'''"(?:desc|note)":\s*(?:f)?"((?:[^"\\\n]|\\.)*)"''', text):
        out.append(m.group(1))
    return [s for s in out if PT_HINT.search(s)]


def collect():
    found = {}
    def add(kind, where, s):
        s = s.strip()
        if s and not re.fullmatch(r"[\W\d_]+", s):
            found.setdefault(s, set()).add(f"{kind}:{where}")
    for f in sorted(ROOT.glob("*.html")):
        for s in html_texts(f):
            add("html", f.name, s)
        for block in re.findall(r"<script>(.*?)</script>", f.read_text(encoding="utf-8"), re.S):
            for s in js_texts(block):
                add("js", f.name, s)
    for f in sorted((ROOT / "js").glob("*.js")):
        if f.name not in ("i18n.js", "i18n-en.js") and not f.name.startswith("_"):
            for s in js_texts(f.read_text(encoding="utf-8")):
                add("js", f.name, s)
    for f in sorted((ROOT / "backend").glob("*.py")):
        for s in py_texts(f.read_text(encoding="utf-8")):
            add("py", f.name, s)
    return found


if __name__ == "__main__":
    found = collect()
    for s in sorted(found):
        print(s.replace("\n", "\\n"))
    print(f"\n# {len(found)} textos", file=sys.stderr)
