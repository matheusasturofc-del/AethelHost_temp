"""Jogadores, arquivos, mundos e backups de um servidor deste PC."""
import json
import os
import re
import shutil
import time
import urllib.error
import uuid
import zipfile
from hashlib import md5
from pathlib import Path
from urllib.parse import quote

from config import BACKUPS_DIR, DATA, SERVERS_DIR
from content import ContentError
from net import get_json
from props import read_properties, set_properties

TMP_DIR = DATA / "tmp"
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")        # nome de jogador do Minecraft Java
WORLD_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
BACKUP_RE = re.compile(r"^\d{8}-\d{6}(?:-[A-Za-z0-9\-]+)?\.zip$")
BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')
TEXT_EXT = {".properties", ".json", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".conf", ".ini", ".xml",
            ".csv", ".log", ".mcmeta", ".md", ".lang", ".skript"}
MAX_TEXT = 1024 * 1024
MAX_EXTRACT = 4 * 1024 ** 3   # tamanho máximo depois de descompactar
MAX_ENTRIES = 200_000
KEEP_AUTO_BACKUPS = 15


def root(sid):
    return SERVERS_DIR / sid


def bad(message, status=400):
    return ContentError(status, message)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- caminhos seguros

def safe_path(sid, rel, must_exist=True):
    """Resolve `rel` dentro da pasta do servidor. Nada de `..`, disco de fora ou atalhos que escapem."""
    base = root(sid).resolve()
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or BAD_CHARS.search(p) for p in parts):
        raise bad("Caminho inválido.")
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise bad("Caminho inválido.")
    if must_exist and not target.exists():
        raise bad("Não encontrei esse arquivo ou pasta.", 404)
    return target


def clean_name(name):
    """Nome de UM arquivo ou pasta (sem barras)."""
    name = str(name or "").strip()
    if not name or len(name) > 100 or name in (".", "..") or "/" in name or "\\" in name or BAD_CHARS.search(name):
        raise bad("Nome inválido: não use barras nem caracteres especiais.")
    return name


def rel_of(sid, path):
    base = root(sid).resolve()
    return "" if path == base else path.relative_to(base).as_posix()


# ---------------------------------------------------------------- arquivos

def list_dir(sid, rel):
    root(sid).mkdir(parents=True, exist_ok=True)
    d = safe_path(sid, rel)
    if not d.is_dir():
        raise bad("Isso não é uma pasta.")
    items = []
    for p in d.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        is_dir = p.is_dir()
        items.append({"name": p.name, "dir": is_dir, "size": None if is_dir else st.st_size, "modified": int(st.st_mtime),
                      "text": not is_dir and p.suffix.lower() in TEXT_EXT and st.st_size <= MAX_TEXT})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return {"path": rel_of(sid, d), "items": items[:2000], "truncated": len(items) > 2000}


def read_text(sid, rel):
    f = safe_path(sid, rel)
    if not f.is_file() or f.suffix.lower() not in TEXT_EXT:
        raise bad("Só dá para editar arquivos de texto (.properties, .json, .txt, .yml…).")
    if f.stat().st_size > MAX_TEXT:
        raise bad("O arquivo passa de 1 MB: baixe para editar no seu computador.")
    try:
        return {"content": f.read_bytes().decode("utf-8"), "size": f.stat().st_size}
    except UnicodeDecodeError:
        raise bad("Esse arquivo não está em UTF-8, então não dá para editar aqui.")


def write_text(sid, rel, content):
    f = safe_path(sid, rel, must_exist=False)
    if f.suffix.lower() not in TEXT_EXT or f == root(sid).resolve():
        raise bad("Só dá para salvar arquivos de texto (.properties, .json, .txt, .yml…).")
    if not f.parent.is_dir():
        raise bad("A pasta não existe.", 404)
    data = str(content).encode("utf-8")
    if len(data) > MAX_TEXT:
        raise bad("O texto passa de 1 MB.")
    _atomic_write(f, data)


def save_upload(sid, dir_rel, name, data):
    d = safe_path(sid, dir_rel)
    if not d.is_dir():
        raise bad("A pasta de destino não existe.", 404)
    _atomic_write(d / clean_name(name), data)


def make_dir(sid, rel, name):
    parent = safe_path(sid, rel)
    if not parent.is_dir():
        raise bad("A pasta de destino não existe.", 404)
    target = parent / clean_name(name)
    if target.exists():
        raise bad("Já existe algo com esse nome.", 409)
    target.mkdir()


def delete_path(sid, rel):
    target = safe_path(sid, rel)
    if target == root(sid).resolve():
        raise bad("Não dá para apagar a pasta raiz do servidor.")
    shutil.rmtree(target) if target.is_dir() else target.unlink()


def rename_path(sid, rel, new_name):
    src = safe_path(sid, rel)
    if src == root(sid).resolve():
        raise bad("Não dá para renomear a pasta raiz do servidor.")
    dest = src.with_name(clean_name(new_name))
    if dest.exists():
        raise bad("Já existe algo com esse nome.", 409)
    src.rename(dest)


# ---------------------------------------------------------------- zip

def zip_paths(pairs, dest):
    """pairs = [(caminho, nome_no_zip)]. Pastas entram inteiras."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")  # só ganha o nome final quando terminar inteiro
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for src, arc in pairs:
                if src.is_file():
                    z.write(src, arc)
                    continue
                for f in src.rglob("*"):
                    # session.lock fica travado pelo Minecraft enquanto o servidor roda, e não faz falta no backup
                    if f.is_file() and not f.is_symlink() and f.name != "session.lock":
                        z.write(f, f"{arc}/{f.relative_to(src).as_posix()}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def safe_extract(zf, dest, strip=""):
    """Descompacta sem deixar nada escapar de `dest` (zip malicioso com ../ ou caminho absoluto)."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXTRACT:
        raise bad("O arquivo compactado é grande demais.")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise bad("O arquivo compactado tem um caminho inseguro.")
        if strip:
            if not name.startswith(strip):
                continue
            name = name[len(strip):]
        parts = [p for p in name.split("/") if p]
        if not parts or name.endswith("/"):
            continue
        if any(p == ".." or BAD_CHARS.search(p) for p in parts):
            raise bad("O arquivo compactado tem um caminho inseguro.")
        target = dest.joinpath(*parts).resolve()
        if dest not in target.parents:
            raise bad("O arquivo compactado tem um caminho inseguro.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def tmp_zip(pairs, label):
    """Zip temporário para baixar (o servidor apaga depois de enviar)."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    dest = TMP_DIR / f"{uuid.uuid4().hex}-{re.sub(r'[^A-Za-z0-9_-]', '_', label)}.zip"
    zip_paths(pairs, dest)
    return dest


def cleanup_tmp(max_age=3600):
    if not TMP_DIR.is_dir():
        return
    for f in TMP_DIR.iterdir():
        try:
            if f.is_file() and time.time() - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- mundos

def level_name(sid):
    return read_properties(root(sid) / "server.properties").get("level-name", "world").strip() or "world"


def world_names(sid):
    """Pastas com level.dat (mundos principais). `x_nether` e `x_the_end` são dimensões do mundo `x`."""
    base = root(sid)
    if not base.is_dir():
        return []
    found = {p.name for p in base.iterdir() if p.is_dir() and (p / "level.dat").is_file()}
    return sorted(n for n in found if not any(n == f"{o}{s}" for o in found for s in ("_nether", "_the_end")))


def world_dirs(sid, name):
    return [root(sid) / d for d in (name, f"{name}_nether", f"{name}_the_end") if (root(sid) / d).is_dir()]


def all_world_dirs(sid):
    return [d for n in world_names(sid) for d in world_dirs(sid, n)]


def folder_size(dirs):
    return sum(f.stat().st_size for d in dirs for f in d.rglob("*") if f.is_file())


def list_worlds(sid):
    active = level_name(sid)
    names = world_names(sid)
    out = []
    for n in names:
        dirs = world_dirs(sid, n)
        out.append({"name": n, "active": n == active, "size": folder_size(dirs),
                    "lastPlayed": int((root(sid) / n / "level.dat").stat().st_mtime), "pending": False})
    if active not in names:  # o Minecraft cria a pasta quando o servidor liga
        out.insert(0, {"name": active, "active": True, "size": 0, "lastPlayed": None, "pending": True})
    return out


def use_world(sid, name):
    if name not in world_names(sid):
        raise bad("Esse mundo não existe.", 404)
    set_properties(root(sid) / "server.properties", {"level-name": name})


def world_properties(name, seed, wtype, version_tuple):
    """As linhas do server.properties que criam um mundo novo (valida nome, tipo e semente)."""
    if not WORLD_RE.match(name or ""):
        raise bad("Nome do mundo: use de 1 a 32 letras, números, _ ou -.")
    types = {"normal": "normal", "flat": "flat", "large_biomes": "large_biomes", "amplified": "amplified"}
    if wtype not in types:
        raise bad("Tipo de mundo inválido.")
    seed = str(seed or "").strip()
    if len(seed) > 60 or "\n" in seed or "\r" in seed:
        raise bad("A semente (seed) aceita até 60 caracteres.")
    modern = version_tuple >= (1, 16)  # a partir da 1.16 o tipo vem com "minecraft:"
    level_type = f"minecraft\\:{types[wtype]}" if modern else {"normal": "DEFAULT", "flat": "FLAT", "large_biomes": "LARGEBIOMES", "amplified": "AMPLIFIED"}[wtype]
    return {"level-name": name, "level-seed": seed, "level-type": level_type}


def create_world(sid, name, seed, wtype, version_tuple):
    props = world_properties(name, seed, wtype, version_tuple)
    if (root(sid) / name).exists() or name in world_names(sid):
        raise bad("Já existe um mundo ou pasta com esse nome.", 409)
    root(sid).mkdir(parents=True, exist_ok=True)
    set_properties(root(sid) / "server.properties", props, raw=("level-type",))


def delete_world(sid, name):
    if name not in world_names(sid):
        raise bad("Esse mundo não existe.", 404)
    if name == level_name(sid):
        raise bad("Esse é o mundo em uso. Escolha outro mundo antes de apagar este.", 409)
    create_backup(sid, f"antes-de-apagar-{name}", names=[name])  # rede de segurança
    for d in world_dirs(sid, name):
        shutil.rmtree(d)


def world_zip(sid, name):
    if name not in world_names(sid):
        raise bad("Esse mundo não existe.", 404)
    return tmp_zip([(d, d.name) for d in world_dirs(sid, name)], name)


def upload_world(sid, name, zip_path):
    """Recebe um .zip de mundo: level.dat na raiz ou dentro de uma pasta (com _nether e _the_end ao lado, se houver)."""
    if not WORLD_RE.match(name or ""):
        raise bad("Nome do mundo: use de 1 a 32 letras, números, _ ou -.")
    if (root(sid) / name).exists():
        raise bad("Já existe um mundo ou pasta com esse nome.", 409)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise bad("Esse arquivo não é um .zip válido.")
    with zf:
        levels = [n.replace("\\", "/") for n in zf.namelist() if n.replace("\\", "/").endswith("level.dat")]
        levels = [n for n in levels if n.count("/") <= 1]
        if not levels:
            raise bad("Não achei o level.dat: o .zip precisa ter a pasta do mundo (com level.dat dentro).")
        level = min(levels, key=lambda n: n.count("/"))
        top = level[: -len("level.dat")]                     # "" ou "meumundo/"
        main = root(sid) / name
        try:
            safe_extract(zf, main, strip=top)
            if top:
                base = top.rstrip("/")
                for suffix in ("_nether", "_the_end"):
                    if any(n.startswith(f"{base}{suffix}/") for n in zf.namelist()):
                        safe_extract(zf, root(sid) / f"{name}{suffix}", strip=f"{base}{suffix}/")
        except Exception:
            for d in world_dirs(sid, name) or [main]:
                shutil.rmtree(d, ignore_errors=True)
            raise


# ---------------------------------------------------------------- backups

def backup_folder(sid):
    return BACKUPS_DIR / sid


def list_backups(sid):
    out = []
    folder = backup_folder(sid)
    if folder.is_dir():
        for f in folder.iterdir():
            if f.is_file() and BACKUP_RE.match(f.name):
                st = f.stat()
                tag = re.sub(r"^\d{8}-\d{6}-?", "", f.name[:-4]) or "manual"
                out.append({"name": f.name, "size": st.st_size, "created": int(st.st_mtime), "tag": tag})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def backup_path(sid, name):
    if not BACKUP_RE.match(str(name)):
        raise bad("Nome de backup inválido.")
    f = backup_folder(sid) / name
    if not f.is_file():
        raise bad("Esse backup não existe.", 404)
    return f


def create_backup(sid, tag="manual", names=None):
    """Guarda um .zip dos mundos (com nether e end). Devolve o nome do arquivo, ou None se ainda não há mundo."""
    dirs = [d for n in (names if names is not None else world_names(sid)) for d in world_dirs(sid, n)]
    if not dirs:
        return None
    folder = backup_folder(sid)
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
    name = f"{stamp}-{tag}.zip"
    while (folder / name).exists():  # duas em um segundo não podem se sobrescrever
        n += 1
        name = f"{stamp}-{tag}-{n}.zip"
    zip_paths([(d, d.name) for d in dirs], folder / name)
    prune_auto(sid)
    return name


def prune_auto(sid):
    """Os backups automáticos ("antes-de-...") não se acumulam para sempre: ficam os mais recentes."""
    auto = [b for b in list_backups(sid) if b["tag"].startswith("antes-de-")]
    for old in auto[KEEP_AUTO_BACKUPS:]:
        (backup_folder(sid) / old["name"]).unlink(missing_ok=True)


def restore_backup(sid, name):
    f = backup_path(sid, name)
    try:
        zf = zipfile.ZipFile(f)
    except zipfile.BadZipFile:
        raise bad("Esse backup está corrompido.")
    with zf:
        tops = {n.replace("\\", "/").split("/")[0] for n in zf.namelist() if n.strip("/")}
        if not any(f"{t}/level.dat" in [n.replace("\\", "/") for n in zf.namelist()] for t in tops):
            raise bad("Esse backup não tem nenhum mundo (level.dat).")
        safety = create_backup(sid, "antes-de-restaurar")
        for d in all_world_dirs(sid):
            shutil.rmtree(d)
        safe_extract(zf, root(sid))
    return {"restored": sorted(tops), "safety": safety}


def delete_backup(sid, name):
    backup_path(sid, name).unlink()


# ---------------------------------------------------------------- jogadores

def _load_list(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _save_list(path, data):
    _atomic_write(Path(path), json.dumps(data, ensure_ascii=False, indent=2))


def offline_uuid(name):
    """UUID que servidores em modo offline (online-mode=false) dão ao jogador, igual ao do Java."""
    h = bytearray(md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def resolve_player(sid, name):
    """(uuid, nome certo). Em modo online pergunta à Mojang; em modo offline calcula."""
    if read_properties(root(sid) / "server.properties").get("online-mode", "true").strip().lower() == "false":
        return offline_uuid(name), name
    try:
        data = get_json(f"https://api.mojang.com/users/profiles/minecraft/{quote(name)}")
        raw = data["id"]
    except urllib.error.HTTPError as e:
        if e.code in (404, 400):
            raise bad("Esse jogador não existe na Mojang. Confira o nome.", 404)
        raise bad("A Mojang não respondeu direito. Tente de novo em instantes.", 502)
    except (ValueError, KeyError):  # resposta vazia = jogador não encontrado
        raise bad("Esse jogador não existe na Mojang. Confira o nome.", 404)
    except (urllib.error.URLError, TimeoutError):
        raise bad("Não consegui consultar a Mojang. Confira a internet, ou ligue o servidor para ele mesmo resolver o nome.", 502)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}", data.get("name", name)


def get_players(sid):
    base = root(sid)
    props = read_properties(base / "server.properties")
    return {
        "ops": [{"name": p.get("name", "?"), "level": p.get("level", 4)} for p in _load_list(base / "ops.json")],
        "whitelist": {"enabled": props.get("white-list", "false").strip().lower() == "true",
                      "players": [p.get("name", "?") for p in _load_list(base / "whitelist.json")]},
        "banned": [{"name": p.get("name", "?"), "reason": p.get("reason", ""), "created": p.get("created", "")}
                   for p in _load_list(base / "banned-players.json")],
        "onlineMode": props.get("online-mode", "true").strip().lower() != "false",
    }


def offline_change(sid, action, name, reason=""):
    """Muda as listas com o servidor DESLIGADO, escrevendo nos mesmos arquivos que o Minecraft usa."""
    base = root(sid)
    base.mkdir(parents=True, exist_ok=True)
    files = {"op": "ops.json", "deop": "ops.json", "whitelist_add": "whitelist.json", "whitelist_remove": "whitelist.json",
             "ban": "banned-players.json", "pardon": "banned-players.json"}
    if action in ("whitelist_on", "whitelist_off"):
        set_properties(base / "server.properties", {"white-list": "true" if action == "whitelist_on" else "false"})
        return
    if action not in files:
        raise bad("Essa ação só funciona com o servidor ligado.", 409)
    path = base / files[action]
    entries = _load_list(path)
    keep = [e for e in entries if str(e.get("name", "")).lower() != name.lower()]
    if action in ("deop", "whitelist_remove", "pardon"):
        _save_list(path, keep)
        return
    uid, real = resolve_player(sid, name)
    keep = [e for e in keep if e.get("uuid") != uid]
    if action == "op":
        keep.append({"uuid": uid, "name": real, "level": 4, "bypassesPlayerLimit": False})
    elif action == "whitelist_add":
        keep.append({"uuid": uid, "name": real})
    else:
        keep.append({"uuid": uid, "name": real, "created": time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime()),
                     "source": "AethelHost", "expires": "forever", "reason": reason or "Banned by an operator."})
    _save_list(path, keep)


PLAYER_COMMANDS = {
    "op": "op {name}", "deop": "deop {name}", "whitelist_add": "whitelist add {name}",
    "whitelist_remove": "whitelist remove {name}", "whitelist_on": "whitelist on", "whitelist_off": "whitelist off",
    "ban": "ban {name} {reason}", "pardon": "pardon {name}", "kick": "kick {name} {reason}",
}
NEEDS_NAME = {"op", "deop", "whitelist_add", "whitelist_remove", "ban", "pardon", "kick"}


def check_player_action(action, name, reason):
    """Valida antes de virar comando: o nome é só letras/números/_ e o motivo não pode ter quebra de linha."""
    if action not in PLAYER_COMMANDS:
        raise bad("Ação inválida.")
    if action in NEEDS_NAME and not PLAYER_RE.match(str(name or "")):
        raise bad("Nome de jogador inválido: use de 3 a 16 letras, números ou _.")
    reason = re.sub(r"[\x00-\x1f]", " ", str(reason or "")).strip()[:100]
    return name, reason


def player_command(action, name, reason):
    return PLAYER_COMMANDS[action].format(name=name or "", reason=reason).strip()
