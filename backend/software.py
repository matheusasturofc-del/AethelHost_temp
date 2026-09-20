"""Versões do Minecraft e programas de servidor (Vanilla, Paper, Purpur, Fabric).

Tudo vem ao vivo dos sites oficiais, então versões novas aparecem sozinhas.
"""
import json
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import CACHE_DIR, JARS_DIR
from net import check_url, download, get_json, get_text

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

SOFTWARE = {
    "vanilla": {"label": "Vanilla", "kind": None,
                "desc": "O servidor oficial da Mojang. Não aceita mods nem plugins."},
    "paper": {"label": "Paper", "kind": "plugins",
              "desc": "Rápido e otimizado. Aceita plugins (Bukkit/Spigot/Paper)."},
    "purpur": {"label": "Purpur", "kind": "plugins",
               "desc": "Baseado no Paper, com muitas opções extras. Aceita plugins."},
    "fabric": {"label": "Fabric", "kind": "mods",
               "desc": "Leve e moderno. Aceita mods do Fabric."},
    "quilt": {"label": "Quilt", "kind": "mods",
              "desc": "Feito a partir do Fabric e compatível com os mods dele. Aceita mods do Quilt e do Fabric."},
    "forge": {"label": "Forge", "kind": "mods",
              "desc": "O mais tradicional para mods grandes e modpacks. Aceita mods do Forge. Precisa instalar na primeira vez."},
    "neoforge": {"label": "NeoForge", "kind": "mods",
                 "desc": "O sucessor moderno do Forge (a partir da 1.20.2). Aceita mods do NeoForge. Precisa instalar na primeira vez."},
}

# Estes três vêm com um instalador que roda uma vez dentro da pasta do servidor.
INSTALLERS = {"quilt", "forge", "neoforge"}

VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.\-]*")
TOKEN_RE = re.compile(r"[0-9A-Za-z.\-+]+")


def vkey(version):
    """'1.21.11' -> (1, 21, 11). Serve para comparar versões (26.x > 1.x)."""
    return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))


# ---------------------------------------------------------------- versões do Minecraft

_manifest = {"at": 0.0, "data": None}


def manifest():
    if _manifest["data"] and time.time() - _manifest["at"] < 3600:
        return _manifest["data"]
    cache = CACHE_DIR / "manifest.json"
    try:
        data = get_json(MANIFEST_URL)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        if _manifest["data"]:
            return _manifest["data"]
        try:  # sem internet: usa a última cópia salva
            data = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            raise RuntimeError("Não consegui consultar a lista de versões da Mojang e não há cópia salva.")
    _manifest.update(at=time.time(), data=data)
    return data


def mc_releases():
    """Versões estáveis, da mais nova até a 1.8: [{'id', 'url'}]."""
    out = []
    for v in manifest()["versions"]:
        if v["type"] == "release":
            out.append({"id": v["id"], "url": v["url"]})
            if v["id"] == "1.8":
                break
    return out


# ---------------------------------------------------------------- Java exigido por versão

_jr_lock = threading.Lock()
_JR_FILE = CACHE_DIR / "java_req.json"


def guess_java(version):
    """Plano B, se a Mojang não responder."""
    k = vkey(version)
    if k >= (26,):
        return 25
    if k >= (1, 20, 5):
        return 21
    if k >= (1, 18):
        return 17
    if k >= (1, 17):
        return 16
    return 8


def _read_jr():
    try:
        return json.loads(_JR_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_jr(data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _JR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, _JR_FILE)


def _fetch_jr(url):
    return int(get_json(url).get("javaVersion", {}).get("majorVersion", 8))


def prefetch_java(releases):
    """Descobre em paralelo o Java de cada versão que ainda não está no cache (o dado nunca muda)."""
    with _jr_lock:
        missing = [r for r in releases if r["id"] not in _read_jr()]
    if not missing:
        return

    def one(r):
        try:
            return r["id"], _fetch_jr(r["url"])
        except Exception:
            return r["id"], None

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(one, missing))
    with _jr_lock:
        cache = _read_jr()
        cache.update({k: v for k, v in results if v})
        _write_jr(cache)


def required_java(version):
    with _jr_lock:
        cache = _read_jr()
    if version in cache:
        return cache[version]
    try:
        url = next(r["url"] for r in mc_releases() if r["id"] == version)
        major = _fetch_jr(url)
    except Exception:
        return guess_java(version)
    with _jr_lock:
        cache = _read_jr()
        cache[version] = major
        _write_jr(cache)
    return major


# ---------------------------------------------------------------- versões por software

_sw_cache = {}


def _supported(sw):
    hit = _sw_cache.get(sw)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    if sw == "vanilla":
        ids = {r["id"] for r in mc_releases()}
    elif sw == "paper":
        ids = {v for group in get_json("https://fill.papermc.io/v3/projects/paper")["versions"].values() for v in group}
    elif sw == "purpur":
        ids = set(get_json("https://api.purpurmc.org/v2/purpur")["versions"])
    elif sw == "fabric":
        ids = {g["version"] for g in get_json("https://meta.fabricmc.net/v2/versions/game") if g["stable"]}
    elif sw == "quilt":
        ids = {g["version"] for g in get_json("https://meta.quiltmc.org/v3/versions/game") if g["stable"]}
    elif sw == "forge":
        ids = {v for v in forge_versions() if vkey(v) >= (1, 12)}  # antes da 1.12 o Forge usa outro formato
    elif sw == "neoforge":
        ids = set(neoforge_by_mc())
    else:
        raise RuntimeError("Software desconhecido.")
    _sw_cache[sw] = (time.time(), ids)
    return ids


def software_versions(sw):
    """Versões estáveis do Minecraft que este software suporta, da mais nova para a mais antiga."""
    ids = _supported(sw)
    return [r["id"] for r in mc_releases() if r["id"] in ids]


# ---------------------------------------------------------------- de onde baixar cada jar

def _vanilla(version):
    rel = next((r for r in mc_releases() if r["id"] == version), None)
    if not rel:
        raise RuntimeError(f"Versão {version} não encontrada na Mojang.")
    info = get_json(rel["url"])["downloads"]["server"]
    return {"url": info["url"], "name": f"{version}.jar", "hash": ("sha1", info["sha1"]), "size": info["size"]}


def _paper(version):
    builds = get_json(f"https://fill.papermc.io/v3/projects/paper/versions/{version}/builds")
    if not builds:
        raise RuntimeError(f"O Paper não tem build para a versão {version}.")
    build = next((b for b in builds if b["channel"] == "STABLE"), builds[0])  # prefere build estável
    dl = build["downloads"]["server:default"]
    note = None
    if build["channel"] != "STABLE":
        note = f"Atenção: para a {version} o Paper só tem builds em fase {build['channel'].lower()} (podem ter falhas)."
    return {"url": dl["url"], "name": f"paper-{version}-{build['id']}.jar",
            "hash": ("sha256", dl["checksums"]["sha256"]), "size": dl["size"], "note": note}


def _purpur(version):
    latest = get_json(f"https://api.purpurmc.org/v2/purpur/{version}/latest")
    build = str(latest["build"])
    if not build.isdigit():
        raise RuntimeError("Resposta inesperada do Purpur.")
    return {"url": f"https://api.purpurmc.org/v2/purpur/{version}/{build}/download",
            "name": f"purpur-{version}-{build}.jar", "hash": ("md5", latest["md5"]), "size": None}


def _fabric(version):
    loader = next(x["version"] for x in get_json("https://meta.fabricmc.net/v2/versions/loader") if x["stable"])
    installer = next(x["version"] for x in get_json("https://meta.fabricmc.net/v2/versions/installer") if x["stable"])
    if not TOKEN_RE.fullmatch(loader) or not TOKEN_RE.fullmatch(installer):
        raise RuntimeError("Resposta inesperada do Fabric.")
    return {"url": f"https://meta.fabricmc.net/v2/versions/loader/{version}/{loader}/{installer}/server/jar",
            "name": f"fabric-{version}-{loader}-{installer}.jar", "hash": None, "size": None}


def _quilt(version):
    installer = get_json("https://meta.quiltmc.org/v3/versions/installer")[0]
    if not TOKEN_RE.fullmatch(installer["version"]):
        raise RuntimeError("Resposta inesperada do Quilt.")
    return {"url": installer["url"], "name": f"quilt-installer-{installer['version']}.jar",
            "hash": ("sha256", installer["hashes"]["sha256"]), "size": installer.get("file_size"), "installer": True}


_promos = {"at": 0.0, "data": {}}


def forge_promos():
    if time.time() - _promos["at"] > 1800:
        _promos.update(at=time.time(),
                       data=get_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")["promos"])
    return _promos["data"]


def forge_versions():
    """Versões do Minecraft que têm Forge."""
    return {k.rsplit("-", 1)[0] for k in forge_promos() if k.endswith(("-latest", "-recommended"))}


def _maven_installer(url):
    """Hash do instalador: os repositórios Maven publicam um .sha1 ao lado de cada arquivo."""
    sha1 = get_text(url + ".sha1", limit=200).strip().split()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", sha1):
        raise RuntimeError("Não consegui obter o hash oficial do instalador.")
    return ("sha1", sha1)


def _forge(version):
    promos = forge_promos()
    build = promos.get(f"{version}-recommended") or promos.get(f"{version}-latest")
    if not build or not TOKEN_RE.fullmatch(build):
        raise RuntimeError(f"O Forge não tem build para a versão {version}.")
    name = f"forge-{version}-{build}-installer.jar"
    url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}-{build}/{name}"
    return {"url": url, "name": name, "hash": _maven_installer(url), "size": None, "installer": True}


_neo = {"at": 0.0, "map": {}}


def neo_to_mc(nv):
    """21.4.147 -> 1.21.4  |  21.0.7 -> 1.21  |  26.3.0.7-beta -> 26.3  (a versão do NeoForge diz a do Minecraft)."""
    core = nv.split("-")[0].split(".")
    if not all(p.isdigit() for p in core) or len(core) < 3:
        return None
    a, b, c = int(core[0]), int(core[1]), int(core[2])
    if a >= 26:                      # esquema novo: 26.<versão>.<patch>.<build>
        return f"{a}.{b}" if c == 0 else f"{a}.{b}.{c}"
    return f"1.{a}" if b == 0 else f"1.{a}.{b}"   # esquema antigo: <MC menor>.<MC patch>.<build>


def neoforge_by_mc():
    """{versão do Minecraft: melhor versão do NeoForge} (a mais nova estável; se só há beta, a beta mais nova)."""
    if time.time() - _neo["at"] > 1800:
        versions = get_json("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")["versions"]
        best = {}
        for nv in versions:
            mc = neo_to_mc(nv)
            if not mc:
                continue
            rank = (0 if "-" in nv else 1, vkey(nv.split("-")[0]))  # estável vale mais que beta
            if mc not in best or rank > best[mc][0]:
                best[mc] = (rank, nv)
        _neo.update(at=time.time(), map={mc: nv for mc, (_, nv) in best.items()})
    return _neo["map"]


def _neoforge(version):
    nv = neoforge_by_mc().get(version)
    if not nv or not TOKEN_RE.fullmatch(nv):
        raise RuntimeError(f"O NeoForge não tem build para a versão {version}.")
    name = f"neoforge-{nv}-installer.jar"
    url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{nv}/{name}"
    note = "Atenção: para esta versão o NeoForge só tem builds beta (podem ter falhas)." if "-" in nv else None
    return {"url": url, "name": name, "hash": _maven_installer(url), "size": None, "installer": True, "note": note}


_PROVIDERS = {"vanilla": _vanilla, "paper": _paper, "purpur": _purpur, "fabric": _fabric,
              "quilt": _quilt, "forge": _forge, "neoforge": _neoforge}


def spec_for(sw, version):
    """O que baixar: {url, name, hash, size, note}. O endereço sempre é conferido na lista oficial."""
    if sw not in _PROVIDERS:
        raise RuntimeError("Software desconhecido.")
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError("Versão inválida.")
    spec = _PROVIDERS[sw](version)
    check_url(spec["url"])
    return spec


def ensure_jar(sw, version, log):
    """Devolve o jar do software/versão, baixando (e conferindo o hash) se ainda não estiver salvo."""
    try:
        spec = spec_for(sw, version)
    except Exception as e:
        pattern = f"{version}.jar" if sw == "vanilla" else f"{sw}-{version}-*.jar"
        cached = sorted(JARS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not cached:
            raise
        log(f"Sem acesso ao site de downloads ({e}). Usando a cópia salva {cached[0].name}.")
        return cached[0]
    jar = JARS_DIR / spec["name"]
    if jar.exists():
        return jar
    if spec.get("note"):
        log(spec["note"])
    size = f" ({spec['size'] // 1_000_000} MB)" if spec.get("size") else ""
    log(f"Baixando {spec['name']}{size}. Só acontece na primeira vez.")
    download(spec["url"], jar, spec["hash"])
    if not zipfile.is_zipfile(jar):
        jar.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado não é um .jar válido.")
    log("Download concluído e verificado." if spec["hash"] else "Download concluído.")
    return jar


# ---------------------------------------------------------------- instaladores (Quilt, Forge, NeoForge)

def find_launch(sw, sdir):
    """Argumentos do Java (depois das opções de memória) que ligam o servidor já instalado, ou None."""
    sdir = Path(sdir)
    if sw == "quilt":
        return ["-jar", "quilt-server-launch.jar", "nogui"] if (sdir / "quilt-server-launch.jar").is_file() else None
    args_name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
    vendor = "net/neoforged/neoforge" if sw == "neoforge" else "net/minecraftforge/forge"
    found = sorted((sdir / "libraries" / vendor).glob(f"*/{args_name}")) if (sdir / "libraries").is_dir() else []
    if found:
        return [f"@{found[-1].relative_to(sdir).as_posix()}", "nogui"]  # Forge/NeoForge modernos usam um arquivo de argumentos
    if sw == "forge":  # Forge antigo (até a 1.16): um jar que se liga sozinho
        jars = sorted(p.name for p in sdir.glob("forge-*.jar") if "installer" not in p.name)
        return ["-jar", jars[-1], "nogui"] if jars else None
    return None


def run_installer(sw, version, jar, sdir, java_exe, log):
    """Roda o instalador uma vez, dentro da pasta do servidor, mostrando a saída no console."""
    import subprocess
    if sw == "quilt":
        cmd = [java_exe, "-jar", str(jar), "install", "server", version, "--download-server", f"--install-dir={sdir}"]
    else:
        cmd = [java_exe, "-jar", str(jar), "--installServer"]
    log(f"Instalando {SOFTWARE[sw]['label']} {version} (só na primeira vez; pode levar alguns minutos)…")
    proc = subprocess.Popen(cmd, cwd=sdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    timer = threading.Timer(1500, proc.kill)  # um instalador travado não pode prender o servidor para sempre
    timer.start()
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log(line)
        code = proc.wait()
    finally:
        timer.cancel()
    if code != 0:
        raise RuntimeError("O instalador terminou com erro. Veja as mensagens acima.")


def prepare_launch(sw, version, jar, sdir, java_exe, log):
    """O que vem depois das opções de memória na linha de comando do Java."""
    if sw not in INSTALLERS:
        return ["-jar", str(jar), "nogui"]
    sdir = Path(sdir)
    marker = sdir / "blockhost-install.json"
    want = {"software": sw, "version": version, "installer": Path(jar).name}
    try:
        have = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        have = None
    launch = find_launch(sw, sdir)
    if have != want or not launch:
        run_installer(sw, version, jar, sdir, java_exe, log)
        launch = find_launch(sw, sdir)
        if not launch:
            raise RuntimeError("A instalação terminou, mas não achei o arquivo para ligar o servidor.")
        marker.write_text(json.dumps(want), encoding="utf-8")
    return launch


# ---------------------------------------------------------------- memória do PC

def system_ram_mb():
    try:
        if os.name == "nt":
            import ctypes

            class Mem(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("totalPage", ctypes.c_ulonglong), ("availPage", ctypes.c_ulonglong),
                            ("totalVirt", ctypes.c_ulonglong), ("availVirt", ctypes.c_ulonglong),
                            ("availExt", ctypes.c_ulonglong)]

            m = Mem()
            m.length = ctypes.sizeof(Mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.total // 2**20)
        with open("/proc/meminfo") as f:
            return int(next(line for line in f if line.startswith("MemTotal")).split()[1]) // 1024
    except Exception:
        return 4096


def ram_options():
    """Quantidades de RAM oferecidas: até 75% da memória do PC, para o Windows não sufocar."""
    limit = system_ram_mb() * 0.75
    return [m for m in (1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384) if m <= limit] or [1024]
