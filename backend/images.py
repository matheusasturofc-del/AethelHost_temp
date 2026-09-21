"""Fotos de perfil e banners: só PNG ou JPEG de verdade (conferidos pelo conteúdo, não pelo nome), com limite de tamanho."""
import struct

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"


class ImageError(Exception):
    pass


def _png_size(data):
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ImageError("O PNG está corrompido.")
    return struct.unpack(">II", data[16:24])


def _jpeg_size(data):
    """Procura o marcador SOF (0xFFC0 a 0xFFCF, menos 0xFFC4/C8/CC), que guarda a altura e a largura."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise ImageError("O JPEG está corrompido.")


def check(data, max_bytes, max_side):
    """Confere e devolve o tipo ("png" ou "jpeg"). Levanta ImageError com a mensagem certa para a pessoa."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImageError("Nenhuma imagem foi enviada.")
    if len(data) > max_bytes:
        raise ImageError(f"A imagem passa de {max_bytes // 1000} KB. Escolha uma menor.")
    if data.startswith(PNG):
        kind, (w, h) = "png", _png_size(data)
    elif data.startswith(JPEG):
        kind, (w, h) = "jpeg", _jpeg_size(data)
    else:
        raise ImageError("Use uma imagem PNG ou JPEG.")
    if not (0 < w <= max_side and 0 < h <= max_side):
        raise ImageError(f"A imagem é grande demais (no máximo {max_side} pixels de lado).")
    return kind
