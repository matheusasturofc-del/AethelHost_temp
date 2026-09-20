#!/usr/bin/env python3
"""Gera o ícone padrão do BlockHost: um bloco isométrico cinza com topo de grama verde-escuro.

Uso:  python tools/make_icon.py
Saída: assets/icon.svg (site e aba do navegador) e assets/default-icon.png (64x64, ícone do servidor no Minecraft).
O fundo é transparente. Só usa a biblioteca padrão.
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets"
N = 32  # grade de 32x32 "pixels grandes"; o PNG final dobra para 64x64

# O cubo visto de cima, em coordenadas da grade.
TOP = [(16, 1), (30, 8), (16, 15), (2, 8)]
LEFT = [(2, 8), (16, 15), (16, 30), (2, 23)]
RIGHT = [(16, 15), (30, 8), (30, 23), (16, 30)]

OUTLINE = (20, 23, 26)
GREEN = {"top": [(53, 133, 63), (46, 119, 56), (63, 154, 74)],
         "left": [(49, 128, 59), (43, 115, 52)],
         "right": [(40, 105, 50), (35, 94, 44)]}
GRAY = {"left": [(91, 97, 106), (83, 89, 98), (99, 105, 114)],
        "right": [(69, 74, 82), (62, 67, 74), (75, 80, 88)]}
FRINGE = [0, 1, 0, 2, 1, 0]  # a "grama" pinga em alturas diferentes


def inside(poly, px, py):
    """Ponto dentro de um polígono (regra par-ímpar)."""
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def face_at(x, y):
    px, py = x + 0.5, y + 0.5
    for name, poly in (("top", TOP), ("left", LEFT), ("right", RIGHT)):
        if inside(poly, px, py):
            return name
    return None


def pick(options, x, y):
    return options[(x * 7 + y * 13 + x * y) % len(options)]  # "ruído" fixo, sempre igual


def build():
    grid = [[None] * N for _ in range(N)]
    for y in range(N):
        for x in range(N):
            face = face_at(x, y)
            if face == "top":
                grid[y][x] = pick(GREEN["top"], x, y)
            elif face in ("left", "right"):
                edge = 8 + (x + 0.5 - 2) * 0.5 if face == "left" else 15 - (x + 0.5 - 16) * 0.5
                depth = 2 + FRINGE[x % len(FRINGE)]
                colors = GREEN[face] if (y + 0.5) - edge < depth else GRAY[face]
                grid[y][x] = pick(colors, x, y)
    # contorno escuro em volta de tudo, para o bloco "destacar"
    filled = [[c is not None for c in row] for row in grid]
    for y in range(N):
        for x in range(N):
            if not filled[y][x]:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < N and 0 <= ny < N) or not filled[ny][nx]:
                    grid[y][x] = OUTLINE
                    break
    return grid


def write_png(grid, path, scale=2):
    size = N * scale
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filtro "nenhum" desta linha
        for x in range(size):
            c = grid[y // scale][x // scale]
            raw += bytes((*c, 255)) if c else b"\x00\x00\x00\x00"

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def write_svg(grid, path):
    rects = []
    for y, row in enumerate(grid):
        x = 0
        while x < N:
            c = row[x]
            if c is None:
                x += 1
                continue
            run = x
            while run < N and row[run] == c:
                run += 1
            rects.append(f'<rect x="{x}" y="{y}" width="{run - x}" height="1" fill="#{c[0]:02x}{c[1]:02x}{c[2]:02x}"/>')
            x = run
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {N} {N}" width="64" height="64" '
           f'shape-rendering="crispEdges">\n' + "\n".join(rects) + "\n</svg>\n")
    path.write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    grid = build()
    write_png(grid, OUT / "default-icon.png")
    write_svg(grid, OUT / "icon.svg")
    print("ok:", OUT / "default-icon.png", "e", OUT / "icon.svg")
