"""Mods e plugins: arquivos .jar na pasta do servidor, mais busca e instalação pelo Modrinth."""
import io
import json
import os
import re
import threading
import zipfile
from urllib.parse import quote, urlencode

from config import SERVERS_DIR
from net import download, get_json
from software import SOFTWARE

MODRINTH = "https://api.modrinth.com/v2"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+() \-]{0,120}\.jar$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MAX_JAR = 64 * 1024 * 1024
LOCK = threading.Lock()


class ContentError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def kind_of(server):
    """'mods', 'plugins' ou None (Vanilla não aceita nenhum dos dois)."""
    return SOFTWARE.get(server.get("software", "vanilla"), {}).get("kind")


def folder_of(server):
    return SERVERS_DIR / server["id"] / kind_of(server)


def _meta_file(server):
    return SERVERS_DIR / server["id"] / "aethelhost-content.json"


def _read_meta(server):
    try:
        return json.loads(_meta_file(server).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(server, meta):
    f = _meta_file(server)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def safe_name(filename):
    name = re.sub(r"[^A-Za-z0-9._+() \-]", "_", os.path.basename(str(filename))).lstrip(". ")
    if not name.lower().endswith(".jar"):
        name += ".jar"
    if not NAME_RE.match(name):
        raise ContentError(400, "Nome de arquivo inválido.")
    return name


def _paths(server, name):
    """(ativado, desativado): os dois caminhos possíveis do mesmo arquivo."""
    if not NAME_RE.match(name):
        raise ContentError(400, "Nome de arquivo inválido.")
    folder = folder_of(server)
    return folder / name, folder / (name + ".disabled")


def list_items(server):
    folder = folder_of(server)
    meta = _read_meta(server)
    items = []
    if folder.is_dir():
        for p in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if not p.is_file():
                continue
            if p.name.endswith(".jar.disabled"):
                base, enabled = p.name[:-len(".disabled")], False
            elif p.name.endswith(".jar"):
                base, enabled = p.name, True
            else:
                continue
            info = meta.get(base, {})
            items.append({"name": base, "size": p.stat().st_size, "enabled": enabled,
                          "title": info.get("title"), "version": info.get("version")})
    return items


def toggle(server, name):
    on, off = _paths(server, name)
    if on.exists():
        on.rename(off)
    elif off.exists():
        off.rename(on)
    else:
        raise ContentError(404, "Arquivo não encontrado.")


def delete(server, name):
    on, off = _paths(server, name)
    found = False
    for p in (on, off):
        if p.exists():
            p.unlink()
            found = True
    if not found:
        raise ContentError(404, "Arquivo não encontrado.")
    with LOCK:
        meta = _read_meta(server)
        if meta.pop(name, None) is not None:
            _write_meta(server, meta)


def save_upload(server, filename, data):
    if len(data) > MAX_JAR:
        raise ContentError(413, "O arquivo passa de 64 MB.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ContentError(400, "Isso não parece um arquivo .jar válido.")
    name = safe_name(filename)
    on, off = _paths(server, name)
    on.parent.mkdir(parents=True, exist_ok=True)
    off.unlink(missing_ok=True)  # o novo substitui uma cópia desativada
    tmp = on.with_name(name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, on)
    return name


# ---------------------------------------------------------------- Modrinth

def loaders_for(server):
    return {"fabric": ["fabric"], "quilt": ["quilt", "fabric"], "forge": ["forge"], "neoforge": ["neoforge"],
            "paper": ["paper", "spigot", "bukkit"],
            "purpur": ["purpur", "paper", "spigot", "bukkit"]}.get(server.get("software", ""), [])


def search(server, query, offset=0):
    """Só mostra o que é compatível com o software e a versão deste servidor."""
    kind = kind_of(server)
    facets = [
        ["project_type:mod"] if kind == "mods" else ["project_type:mod", "project_type:plugin"],
        [f"categories:{loader}" for loader in loaders_for(server)],
        [f"versions:{server['version']}"],
    ]
    if kind == "mods":  # mod só de cliente não serve num servidor
        facets.append(["server_side:required", "server_side:optional"])
    params = {"query": query, "limit": 20, "offset": max(0, offset),
              "index": "relevance" if query else "downloads", "facets": json.dumps(facets)}
    data = get_json(f"{MODRINTH}/search?{urlencode(params)}")
    hits = [{"id": h["project_id"], "title": h["title"], "description": h["description"],
             "downloads": h["downloads"], "icon": h.get("icon_url"), "author": h.get("author")}
            for h in data["hits"]]
    return {"hits": hits, "total": data["total_hits"], "offset": data["offset"]}


def _pick_version(project, server):
    params = urlencode({"loaders": json.dumps(loaders_for(server)),
                        "game_versions": json.dumps([server["version"]])})
    versions = get_json(f"{MODRINTH}/project/{quote(project)}/version?{params}")
    rank = {"release": 0, "beta": 1, "alpha": 2}
    versions.sort(key=lambda v: v["date_published"], reverse=True)
    versions.sort(key=lambda v: rank.get(v["version_type"], 3))  # estável primeiro, e o mais novo dentro dela
    return versions[0] if versions else None


def install(server, project):
    """Instala o projeto e as dependências obrigatórias. Devolve {installed, skipped, warnings}."""
    if not PROJECT_RE.match(project):
        raise ContentError(400, "Projeto inválido.")
    result = {"installed": [], "skipped": [], "warnings": []}
    with LOCK:
        meta = _read_meta(server)
        _install_one(server, project, meta, result, seen=set(), depth=0)
        _write_meta(server, meta)
    return result


def _install_one(server, project, meta, result, seen, depth):
    info = get_json(f"{MODRINTH}/project/{quote(project)}")
    pid, title = info["id"], info["title"]
    if pid in seen:
        return
    seen.add(pid)
    folder = folder_of(server)
    if any(m.get("project") == pid and ((folder / n).exists() or (folder / (n + ".disabled")).exists())
           for n, m in meta.items()):
        result["skipped"].append(title)
        return
    version = _pick_version(pid, server)
    if not version:
        result["warnings"].append(
            f"{title}: não tem versão para {SOFTWARE[server['software']]['label']} {server['version']}.")
        return
    file = next((f for f in version["files"] if f.get("primary")), version["files"][0])
    name = safe_name(file["filename"])
    hashes = file["hashes"]
    digest = ("sha512", hashes["sha512"]) if "sha512" in hashes else ("sha1", hashes["sha1"])
    on, off = _paths(server, name)
    download(file["url"], on, digest, max_bytes=MAX_JAR)
    off.unlink(missing_ok=True)
    meta[name] = {"project": pid, "title": title, "version": version["version_number"]}
    result["installed"].append(title)
    if depth < 3:
        for dep in version.get("dependencies", []):
            if dep.get("dependency_type") == "required" and dep.get("project_id"):
                _install_one(server, dep["project_id"], meta, result, seen, depth + 1)
