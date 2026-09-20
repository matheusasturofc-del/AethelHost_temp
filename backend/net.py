"""Downloads seguros: só sites oficiais conhecidos, redirecionamentos conferidos e hash verificado."""
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "launchermeta.mojang.com", "piston-meta.mojang.com", "piston-data.mojang.com", "launcher.mojang.com",
    "fill.papermc.io", "fill-data.papermc.io",
    "api.purpurmc.org",
    "meta.fabricmc.net",
    "api.modrinth.com", "cdn.modrinth.com",
    "api.mojang.com",  # descobre o UUID de um jogador pelo nome
    "meta.quiltmc.org", "maven.quiltmc.org",
    "files.minecraftforge.net", "maven.minecraftforge.net",
    "maven.neoforged.net",
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",  # programa do playit (túnel)
}
USER_AGENT = "AethelHost/0.2 (projeto pessoal)"


def check_url(url):
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"Endereço não permitido: {url}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # um redirecionamento também não pode sair da lista
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def open_url(url, timeout=30):
    check_url(url)
    return _opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def get_json(url, timeout=30):
    with open_url(url, timeout) as r:
        return json.load(r)


def get_text(url, timeout=30, limit=4096):
    with open_url(url, timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def download(url, dest, hash=None, max_bytes=300_000_000, timeout=60):
    """Baixa para `dest`. `hash` é ("sha256", "abc…") ou None. Só grava o arquivo final se estiver íntegro."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.new(hash[0]) if hash else None
    total = 0
    try:
        with open_url(url, timeout) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 16):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("O arquivo é grande demais.")
                f.write(chunk)
                if digest:
                    digest.update(chunk)
        if digest and digest.hexdigest().lower() != hash[1].lower():
            raise RuntimeError("O arquivo baixado está corrompido (o hash não confere).")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
