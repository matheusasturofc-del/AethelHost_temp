"""Mods e plugins: arquivos .jar na pasta do servidor, mais busca e instalação pelo Modrinth."""
import io
import json
import os
import re
import threading
import zipfile
from urllib.parse import quote, urlencode

from config import SERVERS_DIR
from errors import ContentError
from net import check_url, download, get_json
from software import SOFTWARE

MODRINTH = "https://api.modrinth.com/v2"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+() \-]{0,120}\.jar$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MAX_JAR = 64 * 1024 * 1024
LOCK = threading.Lock()


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


def _remote(server):
    return server.get("plan") == "vps" or bool(server.get("node"))


def _rem():
    import remote  # só aqui: remote usa manage, que usa este módulo
    return remote


def list_items(server):
    folder = folder_of(server)
    meta = _read_meta(server)
    items = []
    if _remote(server):
        for fname, size in sorted(_rem().content_list(server, kind_of(server)), key=lambda x: x[0].lower()):
            if fname.endswith(".jar.disabled"):
                base, enabled = fname[:-len(".disabled")], False
            elif fname.endswith(".jar"):
                base, enabled = fname, True
            else:
                continue
            info = meta.get(base, {})
            items.append({"name": base, "size": size, "enabled": enabled, "title": info.get("title"), "version": info.get("version")})
    elif folder.is_dir():
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
    if _remote(server):
        kind = kind_of(server)
        if _rem().content_exists(server, kind, name):
            _rem().content_rename(server, kind, name, name + ".disabled")
        elif _rem().content_exists(server, kind, name + ".disabled"):
            _rem().content_rename(server, kind, name + ".disabled", name)
        else:
            raise ContentError(404, "Arquivo não encontrado.")
        return
    if on.exists():
        on.rename(off)
    elif off.exists():
        off.rename(on)
    else:
        raise ContentError(404, "Arquivo não encontrado.")


def delete(server, name):
    on, off = _paths(server, name)
    if _remote(server):
        _rem().content_delete(server, kind_of(server), [name, name + ".disabled"])
    else:
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
    if _remote(server):
        _rem().content_write(server, kind_of(server), name, data)
        return name
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


def install(server, project, version_id=None):
    """Instala o projeto e as dependências obrigatórias. Sem `version_id` escolhe sozinho a versão compatível com o servidor;
    com `version_id` instala aquela (mesmo de outra versão do Minecraft: avisa que pode não funcionar). Devolve {installed, skipped, warnings}."""
    if not PROJECT_RE.match(project):
        raise ContentError(400, "Projeto inválido.")
    if version_id is not None and not PROJECT_RE.match(str(version_id)):
        raise ContentError(400, "Versão inválida.")
    result = {"installed": [], "skipped": [], "warnings": []}
    with LOCK:
        meta = _read_meta(server)
        _install_one(server, project, meta, result, seen=set(), depth=0, version_id=version_id)
        _write_meta(server, meta)
    return result


def _is_installed(server, pid, meta):
    """O projeto já está na pasta do servidor (ligado ou desativado)?"""
    if _remote(server):
        present = {n for n, _ in _rem().content_list(server, kind_of(server))}
        return any(m.get("project") == pid and (n in present or n + ".disabled" in present) for n, m in meta.items())
    folder = folder_of(server)
    return any(m.get("project") == pid and ((folder / n).exists() or (folder / (n + ".disabled")).exists())
               for n, m in meta.items())


DEP_ORDER = {"required": 0, "optional": 1, "incompatible": 2, "embedded": 3}


def project_info(server, project):
    """A página de um mod/plugin: descrição, a versão que será instalada neste servidor e as dependências
    (obrigatórias, opcionais, incompatíveis e embutidas), com o que precisa estar também no Minecraft dos jogadores."""
    if not PROJECT_RE.match(project or ""):
        raise ContentError(400, "Projeto inválido.")
    info = get_json(f"{MODRINTH}/project/{quote(project)}")
    pid = info["id"]
    version = _pick_version(pid, server)
    meta = _read_meta(server)
    deps, ids = [], []
    if version:
        for d in version.get("dependencies", [])[:30]:
            if d.get("dependency_type") not in DEP_ORDER:
                continue
            dp = d.get("project_id")
            if not dp and d.get("version_id") and PROJECT_RE.match(d["version_id"]):
                try:  # dependência dada só pela versão: descobre de que projeto ela é
                    dp = get_json(f"{MODRINTH}/version/{quote(d['version_id'])}").get("project_id")
                except Exception:
                    dp = None
            if dp and PROJECT_RE.match(dp) and dp != pid and all(x[0] != dp for x in ids):
                ids.append((dp, d["dependency_type"]))
    if ids:
        listed = get_json(f"{MODRINTH}/projects?ids={quote(json.dumps([i for i, _ in ids]))}")
        by_id = {p["id"]: p for p in listed}
        for dp, typ in ids:
            p = by_id.get(dp)
            if not p:
                continue
            deps.append({"id": dp, "title": p["title"], "description": p.get("description", ""), "icon": p.get("icon_url"), "type": typ,
                         "clientSide": p.get("client_side"), "serverSide": p.get("server_side"), "slug": p.get("slug"),
                         "installed": _is_installed(server, dp, meta)})
        deps.sort(key=lambda x: (DEP_ORDER[x["type"]], x["title"].lower()))
    return {
        "id": pid, "title": info["title"], "description": info.get("description", ""), "icon": info.get("icon_url"),
        "downloads": info.get("downloads", 0), "followers": info.get("followers", 0), "categories": info.get("categories", []),
        "body": (info.get("body") or "")[:30000],
        "gallery": [{"url": g["url"], "title": g.get("title") or "", "description": g.get("description") or "", "featured": bool(g.get("featured"))}
                    for g in sorted(info.get("gallery") or [], key=lambda g: (not g.get("featured"), g.get("ordering", 0)))
                    if str(g.get("url", "")).startswith("https://cdn.modrinth.com/")][:24],
        "clientSide": info.get("client_side"), "serverSide": info.get("server_side"), "projectType": info.get("project_type"),
        "url": f"https://modrinth.com/{info.get('project_type', 'mod')}/{info.get('slug', pid)}",
        "kind": kind_of(server), "software": SOFTWARE[server["software"]]["label"], "mcVersion": server["version"],
        "installed": _is_installed(server, pid, meta),
        "version": {"number": version["version_number"], "name": version.get("name", ""), "type": version["version_type"], "date": version["date_published"]} if version else None,
        "dependencies": deps,
    }


def versions(server, project):
    """Todas as versões do projeto para o software deste servidor (de qualquer versão do Minecraft), marcando as compatíveis.
    Versões de outros softwares (ex.: Forge num servidor Fabric) não entram: não se mistura mods e plugins de outros softwares."""
    if not PROJECT_RE.match(project or ""):
        raise ContentError(400, "Projeto inválido.")
    params = urlencode({"loaders": json.dumps(loaders_for(server))})
    raw = get_json(f"{MODRINTH}/project/{quote(project)}/version?{params}")
    raw.sort(key=lambda v: v["date_published"], reverse=True)
    out = []
    for v in raw[:150]:
        files = v.get("files") or []
        f = next((x for x in files if x.get("primary")), files[0] if files else None)
        games = v.get("game_versions", [])
        out.append({"id": v["id"], "number": v["version_number"], "name": v.get("name") or "", "type": v["version_type"], "date": v["date_published"],
                    "gameVersions": games, "loaders": v.get("loaders", []), "downloads": v.get("downloads", 0),
                    "changelog": (v.get("changelog") or "")[:6000], "compatible": server["version"] in games,
                    "size": (f or {}).get("size", 0), "file": (f or {}).get("filename", "")})
    return {"versions": out, "total": len(raw), "mcVersion": server["version"], "loaders": loaders_for(server)}


def _remove_project_files(server, meta, pid):
    """Tira do servidor os arquivos deste projeto (para trocar por outra versão). Chamado com LOCK já em mãos."""
    for name in [n for n, m in meta.items() if m.get("project") == pid]:
        on, off = _paths(server, name)
        if _remote(server):
            _rem().content_delete(server, kind_of(server), [name, name + ".disabled"])
        else:
            on.unlink(missing_ok=True)
            off.unlink(missing_ok=True)
        meta.pop(name, None)


def _install_one(server, project, meta, result, seen, depth, version_id=None):
    info = get_json(f"{MODRINTH}/project/{quote(project)}")
    pid, title = info["id"], info["title"]
    if pid in seen:
        return
    seen.add(pid)
    folder = folder_of(server)
    if version_id:  # uma versão escolhida na página do mod
        if _is_installed(server, pid, meta):
            _remove_project_files(server, meta, pid)  # troca a versão que já estava instalada
        version = get_json(f"{MODRINTH}/version/{quote(version_id)}")
        if version.get("project_id") != pid:
            raise ContentError(400, "Essa versão não é deste projeto.")
        if not set(version.get("loaders", [])) & set(loaders_for(server)):
            raise ContentError(400, f"Essa versão é para outro software ({', '.join(version.get('loaders', [])) or '?'}): não dá para usar num servidor {SOFTWARE[server['software']]['label']}.")
        if server["version"] not in version.get("game_versions", []):
            result["warnings"].append(f"{title}: a versão {version['version_number']} não é para o Minecraft {server['version']}. Ela foi instalada, mas pode não funcionar.")
    elif _is_installed(server, pid, meta):
        result["skipped"].append(title)
        return
    else:
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
    if _remote(server):
        check_url(file["url"])
        _rem().content_fetch(server, kind_of(server), name, file["url"], digest, MAX_JAR)
    else:
        download(file["url"], on, digest, max_bytes=MAX_JAR)
        off.unlink(missing_ok=True)
    meta[name] = {"project": pid, "title": title, "version": version["version_number"]}
    result["installed"].append(title)
    if depth < 3:
        for dep in version.get("dependencies", []):
            if dep.get("dependency_type") == "required" and dep.get("project_id"):
                _install_one(server, dep["project_id"], meta, result, seen, depth + 1)
