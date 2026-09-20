#!/usr/bin/env python3
"""Gera o ícone do BlockHost: duas linhas curvas opostas, uma cinza e uma verde, que juntas formam um bloco.

Uso:  python tools/make_icon.py [--preview caminho.png]
Saída: assets/icon.svg (site e aba do navegador) e assets/default-icon.png (64x64, ícone do servidor no Minecraft).
O fundo é transparente. Só usa a biblioteca padrão.
"""
import math
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets"
SIZE = 64
C = SIZE / 2          # centro
HALF = 21.0           # meia-lateral do bloco (linha central do traço)
R = 11.0              # raio dos cantos
GAP = 7.5             # distância do meio do canto até a ponta de cada linha
W_MAX, W_MIN = 8.0, 2.6   # espessura no meio e nas pontas: traço afinando, mais "tecnológico"
SHIFT = 1.4           # cada linha desliza um pouco para fora, dando profundidade de bloco

GRAY = ((205, 212, 220), (108, 117, 128))   # gradiente ao longo da linha cinza
GREEN = ((72, 226, 138), (30, 125, 70))     # gradiente ao longo da linha verde (verde um pouco escuro)


# ---------------------------------------------------------------- geometria do quadrado arredondado

def rounded_square(step=0.5):
    """Pontos do contorno (sentido horário, começando no meio do lado de cima) e a posição de cada canto."""
    L = HALF - R                     # metade da parte reta de cada lado
    pts, corners = [], {}
    def line(x0, y0, x1, y1):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for i in range(n):
            pts.append((x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n))
    def arc(cx, cy, a0, a1, name):
        n = max(2, int(R * abs(a1 - a0) / step))
        corners[name] = len(pts) + n // 2
        for i in range(n):
            a = a0 + (a1 - a0) * i / n
            pts.append((cx + R * math.cos(a), cy + R * math.sin(a)))
    line(C, C - HALF, C + L, C - HALF)
    arc(C + L, C - L, -math.pi / 2, 0, "TR")
    line(C + HALF, C - L, C + HALF, C + L)
    arc(C + L, C + L, 0, math.pi / 2, "BR")
    line(C + L, C + HALF, C - L, C + HALF)
    arc(C - L, C + L, math.pi / 2, math.pi, "BL")
    line(C - HALF, C + L, C - HALF, C - L)
    arc(C - L, C - L, math.pi, 3 * math.pi / 2, "TL")
    line(C - L, C - HALF, C, C - HALF)
    return pts, corners


def take(pts, start, end):
    """Trecho do contorno de `start` até `end` (dá a volta no fim da lista se preciso)."""
    n = len(pts)
    return [pts[i % n] for i in range(start, end + (n if end < start else 0))]


def stroke_polygon(path, shift):
    """Transforma uma linha em um polígono com espessura variável e pontas arredondadas."""
    dx, dy = shift
    m = len(path)
    left, right, halfw = [], [], []
    for i, (x, y) in enumerate(path):
        ax, ay = path[max(i - 1, 0)]
        bx, by = path[min(i + 1, m - 1)]
        tx, ty = bx - ax, by - ay
        norm = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / norm, tx / norm
        u = i / (m - 1)
        w = (W_MIN + (W_MAX - W_MIN) * math.sin(math.pi * u) ** 0.7) / 2
        halfw.append(w)
        left.append((x + nx * w + dx, y + ny * w + dy))
        right.append((x - nx * w + dx, y - ny * w + dy))

    def cap(i, end):
        """Meia-volta que fecha a ponta: no fim passa pela frente da linha, no começo por trás."""
        x, y = path[i]
        ax, ay = path[max(i - 1, 0)]
        bx, by = path[min(i + 1, m - 1)]
        t = math.atan2(by - ay, bx - ax)
        a0 = t + math.pi / 2 if end else t - math.pi / 2
        return [(x + dx + halfw[i] * math.cos(a0 - math.pi * k / 8),
                 y + dy + halfw[i] * math.sin(a0 - math.pi * k / 8)) for k in range(1, 8)]

    return left + cap(m - 1, True) + right[::-1] + cap(0, False)


def build():
    pts, c = rounded_square()
    n = len(pts)
    gap = int(GAP / 0.5)
    gray = take(pts, c["BL"] + gap, c["TR"] - gap)      # lado esquerdo + canto de cima + lado de cima
    green = take(pts, c["TR"] + gap, c["BL"] - gap)     # lado direito + canto de baixo + lado de baixo
    return [(stroke_polygon(gray, (-SHIFT, -SHIFT)), GRAY, (-1, -1)),
            (stroke_polygon(green, (SHIFT, SHIFT)), GREEN, (1, 1))]


# ---------------------------------------------------------------- desenho (PNG)

def lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def rasterize(shapes, scale=8):
    """Preenche os polígonos em alta resolução e reduz: bordas suaves, fundo transparente."""
    big = SIZE * scale
    acc = [[[0.0, 0.0, 0.0, 0.0] for _ in range(big)] for _ in range(big)]
    for poly, (c0, c1), _ in shapes:
        pts = [(x * scale, y * scale) for x, y in poly]
        ys = [p[1] for p in pts]
        y_lo, y_hi = max(0, int(min(ys))), min(big - 1, int(max(ys)))
        # o degradê corre na diagonal da caixa da linha, igual ao do SVG
        bx0, by0, bx1, by1 = min(p[0] for p in pts), min(ys), max(p[0] for p in pts), max(ys)
        vx, vy = bx1 - bx0, by1 - by0
        norm = vx * vx + vy * vy
        for y in range(y_lo, y_hi + 1):
            yc = y + 0.5
            xs = []
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                if (y1 > yc) != (y2 > yc):
                    xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                for x in range(max(0, math.ceil(xs[k] - 0.5)), min(big, math.floor(xs[k + 1] - 0.5) + 1)):
                    t = ((x - bx0) * vx + (y - by0) * vy) / norm
                    r, g, b = lerp(c0, c1, min(1, max(0, t)))
                    px = acc[y][x]
                    px[0], px[1], px[2], px[3] = r, g, b, 1.0
    rows = []
    for oy in range(SIZE):
        row = []
        for ox in range(SIZE):
            r = g = b = a = 0.0
            for y in range(oy * scale, (oy + 1) * scale):
                for x in range(ox * scale, (ox + 1) * scale):
                    p = acc[y][x]
                    r += p[0] * p[3]; g += p[1] * p[3]; b += p[2] * p[3]; a += p[3]
            n = scale * scale
            row.append((round(r / a), round(g / a), round(b / a), round(255 * a / n)) if a else (0, 0, 0, 0))
        rows.append(row)
    return rows


def write_png(rows, path):
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for px in row:
            raw += bytes(px)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                     + chunk(b"IEND", b""))


# ---------------------------------------------------------------- desenho (SVG)

def write_svg(shapes, path):
    defs, paths = [], []
    for i, (poly, (c0, c1), (sx, sy)) in enumerate(shapes):
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        # mesma diagonal do PNG: começa no canto do início e termina no canto do fim
        ga, gb = ((x0, y0), (x1, y1)) if i == 0 else ((x0, y0), (x1, y1))
        defs.append(f'<linearGradient id="g{i}" gradientUnits="userSpaceOnUse" x1="{ga[0]:.1f}" y1="{ga[1]:.1f}" x2="{gb[0]:.1f}" y2="{gb[1]:.1f}">'
                    f'<stop offset="0" stop-color="#{c0[0]:02x}{c0[1]:02x}{c0[2]:02x}"/>'
                    f'<stop offset="1" stop-color="#{c1[0]:02x}{c1[1]:02x}{c1[2]:02x}"/></linearGradient>')
        d = "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in poly) + " Z"
        paths.append(f'<path d="{d}" fill="url(#g{i})"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="64" height="64">\n'
           f'<defs>{"".join(defs)}</defs>\n' + "\n".join(paths) + "\n</svg>\n")
    path.write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    shapes = build()
    rows = rasterize(shapes)
    write_png(rows, OUT / "default-icon.png")
    write_svg(shapes, OUT / "icon.svg")
    print("ok:", OUT / "default-icon.png", "e", OUT / "icon.svg")
    if "--preview" in sys.argv:  # versão grande só para conferir o desenho
        big_shapes = [([(x * 4, y * 4) for x, y in poly], col, sh) for poly, col, sh in shapes]
        SIZE_BACKUP = SIZE
        globals()["SIZE"] = SIZE * 4
        write_png(rasterize(big_shapes, 2), Path(sys.argv[sys.argv.index("--preview") + 1]))
        globals()["SIZE"] = SIZE_BACKUP
