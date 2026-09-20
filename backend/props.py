"""Leitura e escrita do server.properties."""
import re
from pathlib import Path

COLOR_CODE_RE = re.compile(r"&([0-9a-fk-or])")  # só minúsculas: "R&D" continua sendo texto normal


def prop_escape(value):
    """Texto -> valor de .properties: acentos e emojis viram \\uXXXX, quebras de linha viram espaço."""
    s = str(value).replace("\\", "\\\\").replace("\r", " ").replace("\n", " ")
    b = s.encode("utf-16-le")
    units = [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]
    return "".join(chr(u) if u < 128 else f"\\u{u:04x}" for u in units)


def motd_to_properties(text):
    """'&aOlá\\n&lLinha 2' -> valor pronto para o server.properties (§ e quebra de linha já escapados)."""
    lines = text.replace("\r", "").split("\n")[:2]
    return "\\n".join(prop_escape(COLOR_CODE_RE.sub("§\\1", line)) for line in lines)


def read_properties(path):
    """{chave: valor} do arquivo (valores como estão escritos). Arquivo ausente vira {}."""
    out = {}
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.lstrip().startswith(("#", "!")) and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value
    except FileNotFoundError:
        pass
    return out


def set_properties(path, values, raw=()):
    """Grava chaves no server.properties. As chaves em `raw` já vêm escapadas."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], set()

    def fmt(k, v):
        return f"{k}={v if k in raw else prop_escape(v)}"

    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            out.append(fmt(key, values[key]))
            done.add(key)
        else:
            out.append(line)
    out += [fmt(k, v) for k, v in values.items() if k not in done]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
