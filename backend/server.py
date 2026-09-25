#!/usr/bin/env python3
"""AethelHost — backend local.

Serve o site e a API, e liga/desliga servidores de Minecraft de verdade neste PC.
Usa só a biblioteca padrão do Python (nada para instalar).

Rodar:  python backend/server.py   ->   http://127.0.0.1:8080
"""
import base64
import glob
import json
import mimetypes
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # deixa importar config, net, software, content

import content  # noqa: E402
import drive  # noqa: E402
import images  # noqa: E402
import accounts  # noqa: E402
import auth  # noqa: E402
import captcha  # noqa: E402
import mail  # noqa: E402
import mailtemplate  # noqa: E402
import notify  # noqa: E402
import manage  # noqa: E402
import options  # noqa: E402
import remote  # noqa: E402
import sshx  # noqa: E402
import tunnel  # noqa: E402
from sshx import ensure_key, ssh_cmd, ssh_permanent, ssh_script, ssh_stream  # noqa: E402
from config import BACKUPS_DIR, DATA, JARS_DIR, ROOT, SERVERS_DIR  # noqa: E402
from props import COLOR_CODE_RE, motd_to_properties, prop_escape, read_properties, set_properties  # noqa: E402
from software import (INSTALLERS, SOFTWARE, ensure_jar, mc_releases, prefetch_java, prepare_launch,  # noqa: E402
                      ram_options, required_java, software_versions, spec_for, system_ram_mb, vkey)

DB_FILE = DATA / "servers.json"

HOST = "127.0.0.1"  # só este PC acessa a API
PORT = int(os.environ.get("AETHELHOST_PORT") or os.environ.get("BLOCKHOST_PORT") or 8080)  # outra porta só para testes
DOMAIN = "aethelhost.net"  # nome provisório
FIRST_MC_PORT = 25565
MAX_PLAYERS = 20
DEFAULT_RAM_MB = 1024
MAX_VPS_SERVERS = 10  # servidores em VPS por conta
MAX_VPS_HOSTS = 3  # VPS diferentes por conta
IDLE_LIMIT = 5 * 3600  # plano grátis fecha após 5h sem jogadores
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

IP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$")
HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$", re.I)
JOIN_RE = re.compile(r"\]: (\w{1,16}) joined the game$")
LEAVE_RE = re.compile(r"\]: (\w{1,16}) left the game$")
LIST_RE = re.compile(r"\]: There are \d+ of a max of \d+ players online:\s*(.*)$")  # resposta do comando "list"


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


# ---------------------------------------------------------------- Java

_java_cache = {"at": 0.0, "list": []}


def java_major(exe):
    try:
        out = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW
        ).stderr
    except Exception:
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major  # "1.8.0" -> 8


def find_javas():
    """Lista [(versão_major, caminho)] dos Javas instalados. Guarda por 30s."""
    if time.time() - _java_cache["at"] < 30:
        return _java_cache["list"]
    exe_name = "java.exe" if os.name == "nt" else "java"
    cands = []
    if os.environ.get("JAVA_HOME"):
        cands.append(Path(os.environ["JAVA_HOME"]) / "bin" / exe_name)
    if shutil.which("java"):
        cands.append(Path(shutil.which("java")))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "Amazon Corretto", "BellSoft", "Semeru"):
            cands += [Path(p) for p in glob.glob(str(Path(base) / vendor / "*" / "bin" / exe_name))]
    seen, found = set(), []
    for c in cands:
        try:
            key = str(c.resolve()).lower()
        except OSError:
            continue
        if key in seen or not c.exists():
            continue
        seen.add(key)
        major = java_major(str(c))
        if major:
            found.append((major, str(c)))
    found.sort()
    _java_cache.update(at=time.time(), list=found)
    return found


def pick_java(need):
    """O menor Java instalado que atende à versão exigida."""
    for major, exe in find_javas():
        if major >= need:
            return major, exe
    return None


# ---------------------------------------------------------------- Banco (arquivo JSON)

DB_LOCK = threading.RLock()


def _normalize(server):
    """Servidores criados antes de existir Software/RAM/dono ganham os valores padrão."""
    server.setdefault("software", "vanilla")
    server.setdefault("ramMb", DEFAULT_RAM_MB)
    server.setdefault("owner", None)  # sem dono até a primeira conta ser criada; aí passam a ser dela
    server.setdefault("public", False)  # endereço público pelo playit.gg (só plano Grátis)
    server.setdefault("shares", [])  # compartilhamento: [{user, level, files, status}]
    return server


def db_load():
    try:
        return [_normalize(s) for s in json.loads(DB_FILE.read_text(encoding="utf-8"))]
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        DB_FILE.replace(DB_FILE.with_suffix(".json.bad"))  # não perde o que estava lá
        return []


def db_save(servers):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(servers, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DB_FILE)


CTX = threading.local()  # quem está fazendo o pedido (o Handler preenche)


def current_user():
    return getattr(CTX, "user", None)


LEVEL_RANK = {"basic": 1, "full": 2, "owner": 3}
SHARE_LEVELS = ("full", "basic")
SHARE_FILES = ("none", "read", "write")
MAX_SHARES = 20


def _role(server, user=None):
    """(nível, arquivos) da conta neste servidor, ou None se ela não tem acesso.
    Nível: owner (dono), full (completo) ou basic (básico). Arquivos: write, read ou none."""
    user = user or current_user()
    if not user:
        return None
    if server.get("owner") == user["id"]:
        return ("owner", "write")
    for sh in server.get("shares", []):
        if sh["user"] == user["id"] and sh["status"] == "accepted":
            return (sh["level"], sh["files"])
    return None


def _allowed(role, need):
    level, files = role
    if need in LEVEL_RANK:
        return LEVEL_RANK[level] >= LEVEL_RANK[need]
    if need == "files_read":
        return level == "owner" or files in ("read", "write")
    if need == "files_write":
        return level == "owner" or files == "write"
    return False


def _pick(servers, sid):
    """O servidor `sid`, se a conta logada tem acesso a ele com o nível que a rota pede (CTX.need; o padrão é só o dono).
    Quem não tem acesso nenhum recebe 404: é como se o servidor não existisse."""
    server = next((s for s in servers if s["id"] == sid), None)
    role = _role(server) if server else None
    if not role:
        raise ApiError(404, "Servidor não encontrado.")
    if not _allowed(role, getattr(CTX, "need", "owner")):
        raise ApiError(403, "Você não tem permissão para fazer isso neste servidor.")
    return server


def find_server(sid):
    return _pick(db_load(), sid)


# ---------------------------------------------------------------- Processo do Minecraft

def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False




# ---------------------------------------------------------------- Ícone do servidor

DEFAULT_ICON = ROOT / "assets" / "default-icon.png"
MAX_ICON_BYTES = 256 * 1024


def custom_icon(sid):
    return SERVERS_DIR / sid / "aethelhost-icon.png"  # a "capa" escolhida por você


def icon_bytes(sid):
    f = custom_icon(sid)
    return f.read_bytes() if f.is_file() else DEFAULT_ICON.read_bytes()


def check_icon(data):
    """O Minecraft só aceita PNG de exatamente 64x64."""
    if len(data) > MAX_ICON_BYTES:
        raise ApiError(413, "A imagem é grande demais (máximo 256 KB depois de reduzida).")
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ApiError(400, "O ícone precisa ser um arquivo PNG.")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (64, 64):
        raise ApiError(400, f"O ícone precisa ter 64x64 pixels (esse tem {width}x{height}).")


def icon_version(sid):
    """Muda quando a capa muda: o navegador guarda a imagem em cache e só baixa de novo se isto mudar."""
    f = custom_icon(sid)
    return f.stat().st_mtime_ns // 1_000_000 if f.is_file() else 0


class Reply:
    """Resposta JSON que também define cookies (login e logout)."""

    def __init__(self, data, cookies=()):
        self.data, self.cookies = data, list(cookies)


class Raw:
    """Resposta que não é JSON: uma imagem (body) ou um arquivo grande enviado aos poucos (path)."""

    def __init__(self, body=b"", ctype="application/octet-stream", cache="no-store", path=None, filename=None,
                 delete_after=False):
        self.body, self.ctype, self.cache = body, ctype, cache
        self.path, self.filename, self.delete_after = path, filename, delete_after


class Runtime:
    """Estado de um servidor ligado (ou desligado) neste PC."""

    def __init__(self, sid):
        self.sid = sid
        self.plan = "free"
        self.state = "offline"  # offline | starting | online | stopping
        self.proc = None
        self.players = set()
        self.idle_since = None
        self.gamerules = {}  # regras definidas no painel: aplicadas toda vez que o servidor liga
        self.lines = []
        self.base = 0  # índice absoluto da primeira linha guardada
        self.lock = threading.RLock()

    def log(self, text, kind=""):
        with self.lock:
            self.lines.append({"t": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
            if len(self.lines) > 1000:
                drop = len(self.lines) - 1000
                del self.lines[:drop]
                self.base += drop

    def read(self, since):
        with self.lock:
            return self.lines[max(since - self.base, 0):], self.base + len(self.lines)

    def snapshot(self):
        with self.lock:
            idle_left = None
            if self.plan == "free" and self.state == "online" and self.idle_since:
                idle_left = max(0, int(IDLE_LIMIT - (time.time() - self.idle_since)))
            return {"state": self.state, "players": sorted(self.players), "idleLeft": idle_left}

    def start(self, server):
        with self.lock:
            if self.state != "offline":
                raise ApiError(409, "O servidor já está ligado ou iniciando.")
            self.state = "starting"
            self.plan = server["plan"]
            self.gamerules = dict(server.get("gamerules") or {})
            self.players.clear()
        if server.get("public") and server["plan"] == "free":
            tunnel.want(server["id"], server["port"])  # o endereço fica pronto enquanto o servidor liga
        if self.lines:
            self.log("──────── Nova execução ────────", "sep")
        threading.Thread(target=self._run, args=(server,), daemon=True).start()

    def _run(self, server):
        try:
            need = required_java(server["version"])
            java = pick_java(need)
            if not java:
                raise RuntimeError(f"Este PC não tem Java {need} ou superior instalado.")
            jar = ensure_jar(server["software"], server["version"], self.log)
            sdir = SERVERS_DIR / server["id"]
            sdir.mkdir(parents=True, exist_ok=True)
            if not port_free(server["port"]):
                raise RuntimeError(f"A porta {server['port']} já está em uso neste PC.")
            (sdir / "eula.txt").write_text("eula=true\n", encoding="utf-8")  # aceito na criação
            (sdir / "server-icon.png").write_bytes(icon_bytes(server["id"]))  # a capa que o Minecraft mostra na lista
            values = {"server-port": server["port"], "motd": motd_to_properties(server["subtitle"] or server["name"])}
            if "max-players" not in read_properties(sdir / "server.properties"):  # depois disso quem manda é a aba Opções
                values["max-players"] = MAX_PLAYERS
            set_properties(sdir / "server.properties", values, raw=("motd",))
            launch = prepare_launch(server["software"], server["version"], jar, sdir, java[1], self.log)
            ram = server["ramMb"]
            self.log(f"Iniciando {SOFTWARE[server['software']]['label']} {server['version']} com Java {java[0]} e {ram} MB de RAM…")
            self.proc = subprocess.Popen(
                [java[1], f"-Xms{min(512, ram)}M", f"-Xmx{ram}M",
                 "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",  # acentos no console
                 *launch],
                cwd=sdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW,
            )
        except Exception as e:
            self.log(f"Erro: {e}", "error")
            with self.lock:
                self.state = "offline"
            tunnel.unwant(self.sid)
            return
        self._watch()

    def _parse(self, line):
        """Guarda a linha no console e atualiza estado e jogadores a partir dela."""
        self.log(line)
        with self.lock:
            if self.state == "starting" and re.search(r"\bDone \(", line):
                self.state = "online"
                self.idle_since = time.time()
                if self.gamerules:
                    threading.Thread(target=self._apply_gamerules, daemon=True).start()
            if m := JOIN_RE.search(line):
                self.players.add(m.group(1))
                self.idle_since = None
            elif m := LEAVE_RE.search(line):
                self.players.discard(m.group(1))
                if not self.players:
                    self.idle_since = time.time()
            elif m := LIST_RE.search(line):
                self.players = {n.strip() for n in m.group(1).split(",") if n.strip()}
                self.idle_since = None if self.players else (self.idle_since or time.time())

    def _watch(self):
        for raw in self.proc.stdout:
            self._parse(raw.rstrip())
        code = self.proc.wait()
        self.log(f"Servidor encerrado (código {code}).")
        with self.lock:
            self.state = "offline"
            self.players.clear()
            self.idle_since = None
            self.proc = None
        tunnel.unwant(self.sid)

    def stop(self):
        with self.lock:
            proc = self.proc
            if not proc or self.state not in ("starting", "online"):
                raise ApiError(409, "Ainda não dá para parar: espere terminar de preparar o servidor.")
            self.state = "stopping"
        self.log("Parando o servidor…")
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
        except OSError:
            proc.kill()
        threading.Thread(target=self._kill_later, args=(proc,), daemon=True).start()

    @staticmethod
    def _kill_later(proc):
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()

    def command(self, text, echo=True):
        text = text.replace("\r", " ").replace("\n", " ").strip()[:200]
        with self.lock:
            if not text:
                raise ApiError(400, "Comando vazio.")
            if self.state != "online" or not self.proc:
                raise ApiError(409, "O servidor precisa estar online para receber comandos.")
            if echo:
                self.log(f"> {text}", "cmd")
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()

    def _apply_gamerules(self):
        """As gamerules escolhidas no painel valem de novo a cada início (o mundo guarda o último valor)."""
        time.sleep(1.5)  # deixa o servidor terminar de aceitar comandos
        for name, value in self.gamerules.items():
            try:
                self.command(f"gamerule {name} {value}", echo=False)
            except ApiError:
                return
            time.sleep(0.05)
        self.log(f"Regras do jogo aplicadas ({len(self.gamerules)}).")


# ---------------------------------------------------------------- VPS (SSH)
# As funções de SSH ficam em sshx.py; aqui ficam os scripts que rodam na VPS e o servidor remoto.

JAVA_MAJOR_SH = r'''java_major() {
  command -v java >/dev/null 2>&1 || { echo 0; return; }
  v=$(java -version 2>&1 | head -1 | sed -E 's/.*"([0-9]+)(\.([0-9]+))?.*/\1 \3/')
  set -- $v
  if [ "$1" = 1 ]; then echo "${2:-0}"; else echo "${1:-0}"; fi
}
'''

CHECK_SCRIPT = JAVA_MAJOR_SH + r'''
. /etc/os-release 2>/dev/null
echo "OS=${PRETTY_NAME:-desconhecido}"
echo "ARCH=$(uname -m)"
echo "MEM=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
echo "JAVA=$(java_major)"
if [ "$(id -u)" = 0 ] || sudo -n true 2>/dev/null; then echo "SUDO=yes"; else echo "SUDO=no"; fi
if command -v tmux >/dev/null 2>&1; then echo "TMUX=yes"; else echo "TMUX=no"; fi
'''

SETUP_BODY = r'''
say() { echo "[VPS] $*"; }
DIR="$HOME/aethelhost/$ID"
SESSION="bh-$ID"
if [ "$(id -u)" = 0 ]; then SUDO=""; else SUDO="sudo -n"; fi

if ! command -v tmux >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || [ "$(java_major)" -lt "$NEED" ]; then
  if [ -n "$SUDO" ] && ! $SUDO true 2>/dev/null; then
    echo "ERRO: faltam programas na VPS (Java $NEED, tmux, curl) e o usuário não tem sudo sem senha para instalá-los."
    exit 1
  fi
  say "Instalando Java $NEED, tmux e curl (pode levar alguns minutos)..."
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -q
    $SUDO apt-get install -y -q tmux curl "openjdk-$NEED-jre-headless"
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q tmux curl "java-$NEED-openjdk-headless"
  else
    echo "ERRO: esta VPS não usa apt nem dnf. Instale Java $NEED, tmux e curl manualmente."
    exit 1
  fi
fi
if [ "$(java_major)" -lt "$NEED" ]; then
  echo "ERRO: o Java $NEED não ficou disponível nesta VPS."
  exit 1
fi

mkdir -p "$DIR/logs"
cd "$DIR"
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "BH_ALREADY_RUNNING"
  exit 0
fi

if [ ! -f "$JAR_NAME" ]; then
  say "Baixando $JAR_NAME..."
  curl -fsSL --retry 3 -o "$JAR_NAME.part" "$JAR_URL"
  if [ -n "$JAR_HASH" ] && ! echo "$JAR_HASH  $JAR_NAME.part" | "${JAR_ALGO}sum" -c - >/dev/null 2>&1; then
    rm -f "$JAR_NAME.part"
    echo "ERRO: o arquivo baixado está corrompido (o hash não confere)."
    exit 1
  fi
  mv "$JAR_NAME.part" "$JAR_NAME"
fi

echo "eula=true" > eula.txt
printf '%s' "$ICON_B64" | base64 -d > server-icon.png
setprop() {
  f=server.properties; touch "$f"
  if grep -q "^$1=" "$f"; then
    K="$1" V="$2" awk 'BEGIN{FS="="} $1==ENVIRON["K"]{print ENVIRON["K"] "=" ENVIRON["V"]; next} {print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    printf '%s=%s\n' "$1" "$2" >> "$f"
  fi
}
setprop server-port "$PORT"
setprop motd "$MOTD"
grep -q '^max-players=' server.properties || setprop max-players "$MAXP"  # depois disso a aba Opções manda

open_port() {
  if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
    $SUDO ufw allow "$PORT/tcp" >/dev/null 2>&1 && say "Firewall (ufw): porta $PORT liberada." || say "Não consegui liberar a porta no ufw."
  fi
  if command -v iptables >/dev/null 2>&1 && $SUDO iptables -S INPUT 2>/dev/null | grep -q REJECT; then
    if ! $SUDO iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
      $SUDO iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null \
        && say "Firewall (iptables): porta $PORT liberada até a VPS reiniciar." \
        || say "Não consegui liberar a porta no iptables."
    fi
  fi
  say "Lembre: o painel do provedor (ex.: Oracle Cloud) também precisa liberar a porta $PORT/TCP."
}
open_port || true

MEM=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
XMX=$RAMMB
LIMIT=$((MEM * 85 / 100))
if [ "$XMX" -gt "$LIMIT" ]; then
  XMX=$LIMIT
  say "Aviso: a VPS tem só ${MEM} MB; usando ${XMX} MB (o pedido era ${RAMMB} MB)."
fi
if [ "$XMX" -lt 384 ]; then XMX=384; fi

LAUNCH="-jar '$JAR_NAME' nogui"
if [ "$INSTALLER" = 1 ]; then
  WANT="$SW $VERSION $JAR_NAME"
  find_launch() {
    if [ "$SW" = quilt ]; then
      [ -f quilt-server-launch.jar ] && echo "-jar quilt-server-launch.jar nogui"
      return 0
    fi
    A=$(ls -1 libraries/net/neoforged/neoforge/*/unix_args.txt libraries/net/minecraftforge/forge/*/unix_args.txt 2>/dev/null | tail -n 1)
    if [ -n "$A" ]; then echo "@$A nogui"; return 0; fi
    F=$(ls -1 forge-*.jar 2>/dev/null | grep -v installer | tail -n 1)
    [ -n "$F" ] && echo "-jar $F nogui"
    return 0
  }
  L=$(find_launch)
  if [ "$(cat aethelhost-install.json 2>/dev/null)" != "$WANT" ] || [ -z "$L" ]; then
    say "Instalando $SW $VERSION (só na primeira vez; pode levar alguns minutos)..."
    if [ "$SW" = quilt ]; then
      java -jar "$JAR_NAME" install server "$VERSION" --download-server --install-dir="$DIR" </dev/null 2>&1 || { echo "ERRO: o instalador terminou com erro."; exit 1; }
    else
      java -jar "$JAR_NAME" --installServer </dev/null 2>&1 || { echo "ERRO: o instalador terminou com erro."; exit 1; }
    fi
    L=$(find_launch)
    if [ -z "$L" ]; then echo "ERRO: a instalação terminou, mas não achei o arquivo para ligar o servidor."; exit 1; fi
    printf '%s' "$WANT" > aethelhost-install.json
  fi
  LAUNCH="$L"
fi

rm -f logs/latest.log
say "Iniciando o servidor (Java $(java_major), ${XMX} MB)..."
tmux new-session -d -s "$SESSION" -c "$DIR" "exec java -Xms128M -Xmx${XMX}M $LAUNCH"
echo "BH_STARTED"
'''


def remote_setup_script(server, need, jar):
    q = shlex.quote
    motd = motd_to_properties(server["subtitle"] or server["name"])
    icon = base64.b64encode(icon_bytes(server["id"])).decode("ascii")
    head = (
        "set -e\n"
        f"ID={q(server['id'])}\nNEED={need}\nPORT={server['port']}\nMAXP={MAX_PLAYERS}\n"
        f"JAR_URL={q(jar['url'])}\nJAR_NAME={q(jar['name'])}\n"
        f"JAR_ALGO={q(jar['hash'][0] if jar['hash'] else '')}\nJAR_HASH={q(jar['hash'][1] if jar['hash'] else '')}\n"
        f"MOTD={q(motd)}\nICON_B64={q(icon)}\n"
        f"RAMMB={int(server.get('ramMb') or DEFAULT_RAM_MB)}\nSW={q(server['software'])}\nVERSION={q(server['version'])}\n"
        f"INSTALLER={1 if jar.get('installer') else 0}\n"
    )
    return head + JAVA_MAJOR_SH + SETUP_BODY


class RemoteRuntime(Runtime):
    """Servidor que roda numa VPS: SSH + tmux para controlar, `tail -F` do log para o console."""

    def __init__(self, sid):
        super().__init__(sid)
        self.vps = None
        self.session_up = False  # a sessão tmux já foi criada na VPS
        self.gone = False  # a sessão tmux acabou
        self._adopted = False

    def _setup_line(self, line):
        if line == "BH_ALREADY_RUNNING":
            self._adopted = True
        elif line != "BH_STARTED":
            self.log(line)

    def _run(self, server):
        self.vps = {**server["vps"], "owner": server["owner"]}  # o dono decide qual chave SSH é usada
        self.session_up = self.gone = self._adopted = False
        try:
            need = required_java(server["version"])
            self.log(f"Conectando à VPS {self.vps['host']}…")
            jar = spec_for(server["software"], server["version"])
            if jar.get("note"):
                self.log(jar["note"])
            script = remote_setup_script(server, need, jar)
            for attempt in (1, 2, 3):
                try:
                    code = ssh_stream(self.vps, script, self._setup_line)
                    break
                except RuntimeError as e:
                    if attempt == 3 or ssh_permanent(str(e)):
                        raise
                    self.log(f"{e} Tentando de novo…", "warn")
                    time.sleep(5)
            if code != 0:
                raise RuntimeError("A preparação da VPS falhou. Veja as mensagens acima.")
        except Exception as e:
            self.log(f"Erro: {e}", "error")
            with self.lock:
                self.state = "offline"
            return
        if self._adopted:
            self.log("O servidor já estava rodando na VPS. Reconectado ao console.")
        self.session_up = True
        self._tail("500" if self._adopted else "+1")

    def adopt(self, server):
        """Depois de reiniciar o AethelHost: se o servidor ainda está rodando na VPS (tmux), volta a acompanhar o console."""
        with self.lock:
            if self.state != "offline":
                return
            self.state = "online"
            self.plan = server["plan"]
            self.gamerules = dict(server.get("gamerules") or {})
            self.players.clear()
        self.vps = {**server["vps"], "owner": server["owner"]}
        self.session_up, self.gone, self._adopted = True, False, True
        self.log("Reconectado a um servidor que já estava rodando na VPS.")
        threading.Thread(target=self._adopt_run, daemon=True).start()

    def _adopt_run(self):
        threading.Timer(6, self._ask_players).start()
        self._tail("500")

    def _ask_players(self):
        try:
            if self.state == "online":
                self.command("list", echo=False)
        except (ApiError, RuntimeError):
            pass

    def _tail(self, start_at):
        threading.Thread(target=self._poll_session, daemon=True).start()
        while not self.gone:
            cmd = f'tail -n {start_at} -F "$HOME/aethelhost/{self.sid}/logs/latest.log" 2>&1'
            self.proc = subprocess.Popen(
                ssh_cmd(self.vps, cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            )
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line.startswith("tail: "):  # avisos do tail enquanto o log ainda não existe
                    self._parse(line)
            self.proc.wait()
            if self.gone:
                break
            self.log("Conexão com a VPS interrompida. Tentando reconectar…", "warn")
            start_at = "0"
            time.sleep(5)
        self.log("Servidor encerrado.")
        with self.lock:
            self.state = "offline"
            self.players.clear()
            self.idle_since = None
            self.proc = None
            self.session_up = False

    def _poll_session(self):
        script = f'tmux has-session -t "bh-{self.sid}" 2>/dev/null && echo yes || echo no'
        while not self.gone:
            time.sleep(5)
            try:
                _, out = ssh_script(self.vps, script, 25)
            except RuntimeError:
                continue  # sem rede agora, tenta de novo
            if "yes" not in out:
                time.sleep(1)  # deixa chegarem as últimas linhas do log
                self.gone = True
                if self.proc:
                    self.proc.terminate()
                return

    def stop(self):
        with self.lock:
            if self.state not in ("starting", "online") or not self.session_up:
                raise ApiError(409, "Ainda não dá para parar: espere terminar de preparar a VPS.")
            self.state = "stopping"
        self.log("Parando o servidor…")
        threading.Thread(target=self._stop_remote, daemon=True).start()

    def _stop_remote(self):
        sid = self.sid
        try:
            ssh_script(self.vps, f'tmux send-keys -t "bh-{sid}" -l stop; tmux send-keys -t "bh-{sid}" Enter', 30)
            deadline = time.time() + 90
            while not self.gone and time.time() < deadline:
                time.sleep(1)
            if not self.gone:
                self.log("O servidor demorou para parar; forçando o encerramento.", "warn")
                ssh_script(self.vps, f'tmux kill-session -t "bh-{sid}"', 30)
        except RuntimeError as e:
            self.log(f"Erro ao parar: {e}", "error")
            with self.lock:
                if self.state == "stopping":
                    self.state = "online"  # dá para tentar de novo

    def command(self, text, echo=True):
        text = text.replace("\r", " ").replace("\n", " ").strip()[:200]
        with self.lock:
            if not text:
                raise ApiError(400, "Comando vazio.")
            if self.state != "online" or not self.session_up:
                raise ApiError(409, "O servidor precisa estar online para receber comandos.")
        self.log(f"> {text}", "cmd")
        sid = self.sid
        try:
            ssh_script(
                self.vps,
                f'tmux send-keys -t "bh-{sid}" -l -- {shlex.quote(text)}; tmux send-keys -t "bh-{sid}" Enter', 20,
            )
        except RuntimeError as e:
            raise ApiError(502, str(e))


RUNTIMES = {}
RUNTIMES_LOCK = threading.Lock()


def runtime(server):
    with RUNTIMES_LOCK:
        rt = RUNTIMES.get(server["id"])
        if rt is None:
            rt = (RemoteRuntime if server["plan"] == "vps" else Runtime)(server["id"])
            RUNTIMES[server["id"]] = rt
        return rt


def monitor():
    """Fecha servidores do plano grátis após 5h sem jogadores."""
    while True:
        time.sleep(5)
        for rt in list(RUNTIMES.values()):
            try:
                if rt.plan == "free" and rt.state == "online" and rt.idle_since \
                        and time.time() - rt.idle_since >= IDLE_LIMIT:
                    rt.log("Fechando o servidor: 5 horas sem jogadores.", "warn")
                    rt.stop()
            except Exception:
                traceback.print_exc()


def adopt_vps_servers():
    """Servidores em VPS continuam rodando lá quando o AethelHost reinicia: vê quais ainda estão no ar e reconecta."""
    seen = {}
    for server in db_load():
        if server["plan"] != "vps":
            continue
        key = (server["owner"], server["vps"]["host"], server["vps"]["port"], server["vps"]["user"])
        try:
            if key not in seen:
                _, out = ssh_script({**server["vps"], "owner": server["owner"]}, 'tmux ls 2>/dev/null | cut -d: -f1', 25)
                seen[key] = set(out.split())
            if f"bh-{server['id']}" in seen[key]:
                runtime(server).adopt(server)
        except (RuntimeError, content.ContentError, OSError):
            seen[key] = seen.get(key, set())  # VPS fora do ar agora: o servidor aparece desligado até alguém abrir o painel


def shutdown_all():
    """Desliga os servidores deste PC. Os da VPS continuam rodando lá, no tmux."""
    local = [rt for rt in RUNTIMES.values() if not isinstance(rt, RemoteRuntime)]
    for rt in local:
        try:
            rt.stop()
        except ApiError:
            pass
    deadline = time.time() + 60
    while time.time() < deadline and any(rt.state != "offline" for rt in local):
        time.sleep(0.5)
    tunnel.shutdown()


# ---------------------------------------------------------------- API

def _max_players(server):
    """As vagas do servidor: o que está no server.properties (editável na aba Opções)."""
    if server["plan"] == "free":
        try:
            return int(read_properties(SERVERS_DIR / server["id"] / "server.properties")["max-players"])
        except (KeyError, ValueError):
            pass
    return MAX_PLAYERS


def view(server):
    rt = RUNTIMES.get(server["id"])
    role = _role(server)
    shared = bool(role and role[0] != "owner")
    extra = {}
    if shared:  # quem recebeu o servidor não vê dados da VPS do dono nem a lista de convites
        owner = auth.get_user(server["owner"]) or {}
        extra = {"ownerName": owner.get("name", ""), "ownerUsername": owner.get("username", "")}
        if server.get("vps"):
            extra["vps"] = {"provider": server["vps"].get("provider", ""), "host": server["vps"]["host"]}
    return {
        **{k: v for k, v in server.items() if k != "shares"},
        **extra,
        "role": role[0] if role else None, "files": role[1] if role else None, "shared": shared,
        "publicName": f"{server['ip']}.{DOMAIN}",
        "address": f"{server['vps']['host']}:{server['port']}" if server["plan"] == "vps" else f"localhost:{server['port']}",
        "maxPlayers": _max_players(server),
        "vpsRamOptions": vps_ram_options(server) if server["plan"] == "vps" else None,
        "softwareLabel": SOFTWARE[server["software"]]["label"],
        "contentKind": content.kind_of(server),
        "iconVersion": icon_version(server["id"]),
        "tunnel": tunnel.info(server["id"]) if server.get("public") else {"state": "off"},
        "runtime": rt.snapshot() if rt else {"state": "offline", "players": [], "idleLeft": None},
    }


def clean_motd(body):
    """Subtítulo = MOTD do Minecraft: até 2 linhas, com códigos de cor (&a, &l…). Espaços no começo ficam (alinhamento)."""
    text = re.sub(r"[\x00-\x09\x0b-\x1f]", "", str(body.get("subtitle", "")).replace("\r", "")).rstrip()
    lines = text.split("\n")
    if len(lines) > 2:
        raise ApiError(400, "O subtítulo aceita no máximo 2 linhas.")
    if len(text) > 160:
        raise ApiError(400, "O subtítulo é longo demais (máximo 160 caracteres, contando os códigos de cor).")
    if any(len(COLOR_CODE_RE.sub("", line).strip()) > 60 for line in lines):
        raise ApiError(400, "Cada linha do subtítulo aceita até 60 letras.")
    return text


def clean_text(body, key, minimum, maximum, label):
    value = str(body.get(key, "")).strip()
    if not minimum <= len(value) <= maximum:
        raise ApiError(400, f"{label}: use de {minimum} a {maximum} caracteres." if minimum else
                       f"{label}: no máximo {maximum} caracteres.")
    return value


def api_meta(query, body):
    return 200, {
        "domain": DOMAIN,
        "maxPlayers": MAX_PLAYERS,
        "software": [{"id": k, "label": v["label"], "kind": v["kind"], "desc": v["desc"]} for k, v in SOFTWARE.items()],
        "ramOptions": ram_options(),
        "systemRamMb": system_ram_mb(),
    }


def api_software_versions(query, body, sw):
    """Versões que o software suporta, e quais este PC consegue rodar com o Java instalado."""
    if sw not in SOFTWARE:
        raise ApiError(404, "Software desconhecido.")
    prefetch_java(mc_releases())
    versions = []
    for v in software_versions(sw):
        need = required_java(v)
        versions.append({"version": v, "java": need, "available": pick_java(need) is not None})
    missing = sorted({v["java"] for v in versions if not v["available"]})
    return 200, {"versions": versions, "missingJava": missing}


def valid_version(sw, version):
    return isinstance(version, str) and version in software_versions(sw)


def api_ip_check(query, body):
    ip = query.get("ip", [""])[0]
    valid = bool(IP_RE.match(ip))
    taken = any(s["ip"] == ip for s in db_load())
    return 200, {"valid": valid, "available": valid and not taken}


def api_list(query, body):
    return 200, [view(s) for s in db_load() if _role(s)]


def api_create(query, body):
    captcha.require(body.get("captcha"))
    name = clean_text(body, "name", 3, 30, "Nome")
    subtitle = clean_motd(body)
    ip = str(body.get("ip", ""))
    version, plan = body.get("version"), body.get("plan")
    software = body.get("software", "vanilla")
    if not IP_RE.match(ip):
        raise ApiError(400, "Endereço inválido: use de 3 a 24 caracteres (letras minúsculas, números e hífen).")
    if body.get("eula") is not True:
        raise ApiError(400, "É preciso aceitar o EULA do Minecraft.")
    if software not in SOFTWARE:
        raise ApiError(400, "Software inválido.")
    if not valid_version(software, version):
        raise ApiError(400, f"O {SOFTWARE[software]['label']} não tem a versão {version}.")
    if plan not in ("free", "vps"):
        raise ApiError(400, "Plano inválido.")

    vps = None
    if plan == "free":
        need = required_java(version)
        if not pick_java(need):
            raise ApiError(400, f"A versão {version} precisa do Java {need}, que não está instalado neste PC.")
    else:
        v = body.get("vps") or {}
        host, user = str(v.get("host", "")).strip(), str(v.get("user", "")).strip()
        port = v.get("port")
        if not HOST_RE.match(host) or not USER_RE.match(user) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ApiError(400, "Dados da VPS inválidos.")
        sshx.check_host(host)  # nada de localhost nem rede interna
        vps = {"provider": str(v.get("provider", "Outro"))[:40], "host": host, "port": port, "user": user}

    with DB_LOCK:
        servers = db_load()
        if any(s["ip"] == ip for s in servers):
            raise ApiError(409, "Este endereço já está em uso. Escolha outro.")
        if plan == "vps":
            mine = [s for s in servers if s["plan"] == "vps" and s["owner"] == current_user()["id"]]
            if len(mine) >= MAX_VPS_SERVERS:
                raise ApiError(429, f"Limite de {MAX_VPS_SERVERS} servidores em VPS por conta.")
            if vps["host"] not in {s["vps"]["host"] for s in mine} and len({s["vps"]["host"] for s in mine}) >= MAX_VPS_HOSTS:
                raise ApiError(429, f"Limite de {MAX_VPS_HOSTS} VPS diferentes por conta.")
        # Neste PC a porta também precisa estar livre; na VPS basta não repetir a de outro servidor da mesma VPS.
        if plan == "vps":
            used = {s["port"] for s in servers if s["plan"] == "vps" and s["vps"]["host"] == vps["host"]}
        else:
            used = {s["port"] for s in servers if s["plan"] == "free"}
        port = FIRST_MC_PORT
        while port in used or (plan == "free" and not port_free(port)):
            port += 1
        server = {
            "id": uuid.uuid4().hex[:12], "name": name, "subtitle": subtitle, "ip": ip,
            "edition": "java", "software": software, "version": version, "ramMb": DEFAULT_RAM_MB,
            "plan": plan, "port": port, "eula": True, "owner": current_user()["id"], "public": False,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if vps:
            server["vps"] = vps
        servers.append(server)
        db_save(servers)
    return 201, view(server)


def api_get(query, body, sid):
    return 200, view(find_server(sid))


def api_patch(query, body, sid):
    name = clean_text(body, "name", 3, 30, "Nome")
    subtitle = clean_motd(body)
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        server.update(name=name, subtitle=subtitle)
        db_save(servers)
    return 200, view(server)


def _require_offline(sid, what):
    rt = RUNTIMES.get(sid)
    if rt and rt.state != "offline":
        raise ApiError(409, f"Desligue o servidor antes de {what}.")


VPS_RAM_STEPS = (1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768)


def vps_ram_options(server):
    """RAM oferecida numa VPS: até 80% da memória dela (medida em "Testar conexão"); antes disso, só 1 e 2 GB."""
    mem = (server.get("vps") or {}).get("memMb")
    if not mem:
        return [1024, 2048]
    return [m for m in VPS_RAM_STEPS if m <= mem * 0.8] or [512 if mem < 1024 else 1024]


def api_software_set(query, body, sid):
    """Troca software, versão e/ou RAM. Só com o servidor desligado; guarda um backup do mundo antes."""
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        _require_offline(sid, "mudar o software")
        sw = body.get("software", server["software"])
        version = body.get("version", server["version"])
        ram = body.get("ramMb", server["ramMb"])
        if sw not in SOFTWARE:
            raise ApiError(400, "Software inválido.")
        if not valid_version(sw, version):
            raise ApiError(400, f"O {SOFTWARE[sw]['label']} não tem a versão {version}.")
        allowed_ram = vps_ram_options(server) if server["plan"] == "vps" else ram_options()
        if not isinstance(ram, int) or (ram not in allowed_ram and ram != server["ramMb"]):
            raise ApiError(400, "Quantidade de RAM inválida para " + ("esta VPS." if server["plan"] == "vps" else "este PC."))

        changed = (sw, version) != (server["software"], server["version"])
        backup = None
        if changed and server["plan"] == "free":
            need = required_java(version)
            if not pick_java(need):
                raise ApiError(400, f"A versão {version} precisa do Java {need}, que não está instalado neste PC.")
            if manage.world_names(sid) and vkey(version) < vkey(server["version"]) and body.get("confirm") is not True:
                raise ApiError(409, "Voltar para uma versão mais antiga pode corromper o mundo.",
                               {"needConfirm": True})
            backup = manage.create_backup(sid, "antes-de-mudar-software")
        elif changed and server["plan"] == "vps":
            try:
                backup = remote.create_backup(server, "antes-de-mudar-software")
            except (RuntimeError, content.ContentError):
                backup = None  # sem conexão agora: a troca continua (o mundo na VPS não é mexido)
        if server["plan"] == "vps":
            try:
                had_content = changed and any(remote.content_list(server, kind) for kind in ("mods", "plugins"))
            except (RuntimeError, content.ContentError):
                had_content = False
        else:
            had_content = changed and any(
                f.name.endswith((".jar", ".jar.disabled"))
                for kind in ("mods", "plugins") for f in (SERVERS_DIR / sid / kind).glob("*") if f.is_file()
            )
        server.update(software=sw, version=version, ramMb=ram)
        db_save(servers)
    return 200, {**view(server), "backup": backup, "warnContent": had_content}


def _content_server(sid, mutate=False):
    server = find_server(sid)
    if not content.kind_of(server):
        raise ApiError(400, "O Vanilla não aceita mods nem plugins. Em Software, escolha Paper, Purpur ou Fabric.")
    if mutate:
        _require_offline(sid, "mexer em mods e plugins")
    return server


def api_content_list(query, body, sid):
    server = find_server(sid)
    kind = content.kind_of(server)
    if not kind:
        return 200, {"kind": kind, "items": [], "supported": False}
    return 200, {"kind": kind, "items": content.list_items(server), "supported": True}


def api_content_search(query, body, sid):
    server = _content_server(sid)
    try:
        offset = max(0, int(query.get("offset", ["0"])[0]))
    except ValueError:
        offset = 0
    return 200, content.search(server, query.get("q", [""])[0][:100], offset)


def api_content_install(query, body, sid):
    server = _content_server(sid, mutate=True)
    return 200, content.install(server, str(body.get("project", "")))


def api_content_upload(query, data, sid):
    server = _content_server(sid, mutate=True)
    name = content.save_upload(server, query.get("name", [""])[0], data)
    return 200, {"name": name}


def api_content_toggle(query, body, sid):
    content.toggle(_content_server(sid, mutate=True), str(body.get("name", "")))
    return 200, {"ok": True}


def api_content_delete(query, body, sid):
    content.delete(_content_server(sid, mutate=True), str(body.get("name", "")))
    return 200, {"ok": True}


def api_icon_get(query, body, sid):
    find_server(sid)
    return 200, Raw(icon_bytes(sid), "image/png", "private, max-age=3600")


def api_icon_set(query, data, sid):
    find_server(sid)
    check_icon(data)
    f = custom_icon(sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, f)
    return 200, {"ok": True, "iconVersion": icon_version(sid)}


def api_icon_reset(query, body, sid):
    find_server(sid)
    custom_icon(sid).unlink(missing_ok=True)
    return 200, {"ok": True, "iconVersion": 0}


def api_delete(query, body, sid):  # noqa: D401 - só o dono
    with DB_LOCK:
        servers = db_load()
        _pick(servers, sid)
        rt = RUNTIMES.get(sid)
        if rt and rt.state != "offline":
            raise ApiError(409, "Desligue o servidor antes de excluir.")
        db_save([s for s in servers if s["id"] != sid])
    folder = (SERVERS_DIR / sid).resolve()
    if folder.parent == SERVERS_DIR.resolve() and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    RUNTIMES.pop(sid, None)
    tunnel.forget(sid)
    notify.remove_where(lambda n: n["type"] == "share_invite" and n["data"].get("server") == sid)  # convites de um servidor que não existe mais
    return 200, {"ok": True}


def api_start(query, body, sid):
    server = find_server(sid)
    if server["plan"] == "free":  # na VPS, quem instala o Java é o próprio script de preparação
        need = required_java(server["version"])
        if not pick_java(need):
            raise ApiError(400, f"Este PC não tem Java {need} instalado (a versão {server['version']} precisa dele).")
    runtime(server).start(server)
    return 202, view(server)


def api_stop(query, body, sid):
    server = find_server(sid)
    runtime(server).stop()
    return 202, view(server)


def api_command(query, body, sid):
    server = find_server(sid)
    runtime(server).command(str(body.get("command", "")))
    return 200, {"ok": True}


def api_console(query, body, sid):
    server = find_server(sid)
    try:
        since = max(0, int(query.get("since", ["0"])[0]))
    except ValueError:
        since = 0
    lines, nxt = runtime(server).read(since)
    return 200, {"lines": lines, "next": nxt}


def api_vps_key(query, body):
    return 200, {"publicKey": ensure_key(current_user()["id"])}


def api_vps_check(query, body, sid):
    server = find_server(sid)
    if server["plan"] != "vps":
        raise ApiError(400, "Este servidor não usa VPS.")
    try:
        _, out = ssh_script({**server["vps"], "owner": server["owner"]}, CHECK_SCRIPT, 40)
    except RuntimeError as e:
        raise ApiError(502, str(e))
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        mem = int(info.get("MEM", "0"))
    except ValueError:
        mem = 0
    if mem > 0:
        with DB_LOCK:
            servers = db_load()
            for s in servers:
                if s["plan"] == "vps" and s["owner"] == server["owner"] and s["vps"]["host"] == server["vps"]["host"]:
                    s["vps"]["memMb"] = mem
            db_save(servers)
        server = find_server(sid)
    return 200, {"info": info, "needJava": required_java(server["version"]), "server": view(server)}


def api_vps_port(query, body, sid):
    """Tenta abrir a porta do servidor de fora (daqui): se falhar, o firewall da VPS ou do provedor está fechado."""
    server = find_server(sid)
    if server["plan"] != "vps":
        raise ApiError(400, "Este servidor não usa VPS.")
    sshx.check_host(server["vps"]["host"])
    try:
        with socket.create_connection((server["vps"]["host"], server["port"]), timeout=6):
            return 200, {"open": True, "state": _state(sid)}
    except OSError:
        return 200, {"open": False, "state": _state(sid)}


# ---------------------------------------------------------------- Jogadores, arquivos, mundos e backups
# No plano Grátis tudo acontece na pasta deste PC (manage.py, options.py). Na VPS as mesmas telas usam remote.py (SSH).

def _manage_server(sid):
    return find_server(sid)


def _remote(server):
    return server["plan"] == "vps"


def _state(sid):
    rt = RUNTIMES.get(sid)
    return rt.state if rt else "offline"


def _first(query, key):
    return query.get(key, [""])[0]


def api_players(query, body, sid):
    server = find_server(sid)
    rt = RUNTIMES.get(sid)
    snap = rt.snapshot() if rt else {"state": "offline", "players": []}
    lists = remote.get_players(server) if _remote(server) else manage.get_players(sid)
    return 200, {"supported": True, "state": snap["state"], "online": snap["players"], **lists}


def api_players_action(query, body, sid):
    server = _manage_server(sid)
    action = str(body.get("action", ""))
    name, reason = manage.check_player_action(action, body.get("name"), body.get("reason"))
    state = _state(sid)
    if state == "online":  # o próprio servidor resolve o nome e aplica na hora
        RUNTIMES[sid].command(manage.player_command(action, name, reason))
        return 200, {"mode": "command"}
    if state != "offline":
        raise ApiError(409, "Espere o servidor terminar de iniciar ou de parar.")
    if action == "kick":
        raise ApiError(409, "Só dá para expulsar quem está online: ligue o servidor.")
    if _remote(server):
        remote.offline_change(server, action, name, reason)
    else:
        manage.offline_change(sid, action, name, reason)
    return 200, {"mode": "file"}


def api_files_list(query, body, sid):
    server = find_server(sid)
    data = remote.list_dir(server, _first(query, "path")) if _remote(server) else manage.list_dir(sid, _first(query, "path"))
    return 200, {"supported": True, **data}


def api_files_read(query, body, sid):
    server = _manage_server(sid)
    return 200, (remote.read_text(server, _first(query, "path")) if _remote(server) else manage.read_text(sid, _first(query, "path")))


def api_files_write(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "editar arquivos")
    (remote.write_text(server, str(body.get("path", "")), body.get("content", "")) if _remote(server)
     else manage.write_text(sid, str(body.get("path", "")), body.get("content", "")))
    return 200, {"ok": True}


def api_files_upload(query, data, sid):
    server = _manage_server(sid)
    _require_offline(sid, "enviar arquivos")
    if _remote(server):
        remote.save_upload(server, _first(query, "dir"), _first(query, "name"), data)
    else:
        manage.save_upload(sid, _first(query, "dir"), _first(query, "name"), data)
    return 200, {"ok": True}


def api_files_mkdir(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "criar pastas")
    (remote.make_dir(server, str(body.get("path", "")), str(body.get("name", ""))) if _remote(server)
     else manage.make_dir(sid, str(body.get("path", "")), str(body.get("name", ""))))
    return 200, {"ok": True}


def api_files_delete(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "apagar arquivos")
    (remote.delete_path(server, str(body.get("path", ""))) if _remote(server) else manage.delete_path(sid, str(body.get("path", ""))))
    return 200, {"ok": True}


def api_files_rename(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "renomear arquivos")
    (remote.rename_path(server, str(body.get("path", "")), str(body.get("name", ""))) if _remote(server)
     else manage.rename_path(sid, str(body.get("path", "")), str(body.get("name", ""))))
    return 200, {"ok": True}


def api_files_download(query, body, sid):
    server = _manage_server(sid)
    if _remote(server):
        dest, filename, ctype = remote.download(server, _first(query, "path"))
        return 200, Raw(path=dest, ctype=ctype or "application/octet-stream", filename=filename, delete_after=True)
    target = manage.safe_path(sid, _first(query, "path"))
    if target.is_dir():
        label = target.name or "servidor"
        return 200, Raw(path=manage.tmp_zip([(target, label)], label), ctype="application/zip",
                        filename=label + ".zip", delete_after=True)
    return 200, Raw(path=target, filename=target.name)


def api_worlds(query, body, sid):
    server = find_server(sid)
    return 200, {"supported": True, "worlds": remote.list_worlds(server) if _remote(server) else manage.list_worlds(sid)}


def api_world_use(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "trocar de mundo")
    (remote.use_world(server, str(body.get("name", ""))) if _remote(server) else manage.use_world(sid, str(body.get("name", ""))))
    return 200, {"ok": True}


def api_world_create(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "criar um mundo")
    fn = remote.create_world if _remote(server) else manage.create_world
    fn(server if _remote(server) else sid, str(body.get("name", "")), body.get("seed", ""), str(body.get("type", "normal")),
       vkey(server["version"]))
    return 200, {"ok": True}


def api_world_delete(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "apagar um mundo")
    (remote.delete_world(server, str(body.get("name", ""))) if _remote(server) else manage.delete_world(sid, str(body.get("name", ""))))
    return 200, {"ok": True}


def api_world_download(query, body, sid):
    server = _manage_server(sid)
    name = _first(query, "name")
    if _remote(server):
        return 200, Raw(path=remote.world_download(server, name), ctype="application/gzip", filename=f"{name}.tar.gz", delete_after=True)
    return 200, Raw(path=manage.world_zip(sid, name), ctype="application/zip", filename=f"{name}.zip", delete_after=True)


def api_world_upload(query, zip_path, sid):
    server = _manage_server(sid)
    _require_offline(sid, "enviar um mundo")
    (remote.upload_world(server, _first(query, "name"), zip_path) if _remote(server) else manage.upload_world(sid, _first(query, "name"), zip_path))
    return 200, {"ok": True}


def api_backups(query, body, sid):
    server = find_server(sid)
    return 200, {"supported": True, "backups": remote.list_backups(server) if _remote(server) else manage.list_backups(sid),
                 "state": _state(sid)}


def _drive_name(server, backup):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", server["name"]).strip("-") or "servidor"
    return f"{slug}-{backup}"


def _drive_send(server, backup):
    """Começa a enviar um backup ao Drive da conta que está logada (em segundo plano)."""
    me = current_user()
    if not drive.linked(me["id"]):
        raise ApiError(409, "Vincule o Google Drive primeiro.")
    if _remote(server):
        remote_name = remote._backup_name(backup)
        get = lambda: (remote.download_backup(server, remote_name)[0], True)
    else:
        path = manage.backup_path(server["id"], backup)
        get = lambda: (path, False)
    drive.start_upload(me["id"], f"{server['id']}/{backup}", get, _drive_name(server, backup))


def api_backup_drive_get(query, body, sid):
    """Estado dos envios deste servidor e o que já está no Drive (só se a conta tem Drive vinculado)."""
    server = _manage_server(sid)
    me = current_user()
    out = {"linked": drive.linked(me["id"]), "uploads": {}, "remote": {}}
    prefix = f"{sid}/"
    for (uid, key), j in list(drive.JOBS.items()):
        if uid == me["id"] and key.startswith(prefix):
            out["uploads"][key[len(prefix):]] = j
    if out["linked"]:
        try:
            files = drive.list_files(me["id"])
            out["remote"] = files
            out["prefix"] = _drive_name(server, "")
        except content.ContentError as e:
            out["error"] = e.message
            out["linked"] = drive.linked(me["id"])
    return 200, out


def api_backup_drive_send(query, body, sid):
    server = _manage_server(sid)
    _drive_send(server, str(body.get("name", "")))
    return 202, {"started": True}


def api_drive_get(query, body):
    return 200, drive.status(current_user()["id"], BASE_URL if (current_user() or {}).get("admin") else None)


def api_drive_unlink(query, body):
    drive.unlink(current_user()["id"])
    return 200, drive.status(current_user()["id"])


def api_drive_settings(query, body):
    drive.set_auto(current_user()["id"], body.get("auto") is True)
    return 200, drive.status(current_user()["id"])


def api_backup_create(query, body, sid):
    server = _manage_server(sid)
    make = (lambda: remote.create_backup(server, "manual")) if _remote(server) else (lambda: manage.create_backup(sid, "manual"))
    state = _state(sid)
    if state in ("starting", "stopping"):
        raise ApiError(409, "Espere o servidor terminar de iniciar ou de parar.")
    if state == "online":
        # Com o servidor ligado: manda salvar tudo e parar de gravar, copia, e volta a gravar.
        rt = RUNTIMES[sid]
        _, mark = rt.read(0)
        rt.command("save-off")
        rt.command("save-all flush")
        try:
            deadline = time.time() + 45
            while not any("Saved the game" in ln["text"] for ln in rt.read(mark)[0]):
                if time.time() > deadline:
                    raise ApiError(504, "O servidor demorou para salvar o mundo. Tente de novo.")
                time.sleep(0.5)
            name = make()
        finally:
            try:
                rt.command("save-on")
            except ApiError:
                pass
    else:
        name = make()
    if not name:
        raise ApiError(400, "Ainda não há mundo para guardar: ligue o servidor uma vez.")
    sent = False
    entry = drive.entry(current_user()["id"])
    if entry and entry.get("auto"):  # "enviar backups automaticamente" ligado: já manda para o Drive
        try:
            _drive_send(server, name)
            sent = True
        except (ApiError, content.ContentError):
            pass
    return 200, {"name": name, "drive": sent}


def api_backup_restore(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "restaurar um backup")
    return 200, (remote.restore_backup(server, str(body.get("name", ""))) if _remote(server) else manage.restore_backup(sid, str(body.get("name", ""))))


def api_backup_delete(query, body, sid):
    server = _manage_server(sid)
    (remote.delete_backup(server, str(body.get("name", ""))) if _remote(server) else manage.delete_backup(sid, str(body.get("name", ""))))
    return 200, {"ok": True}


def api_backup_download(query, body, sid):
    server = _manage_server(sid)
    if _remote(server):
        dest, name = remote.download_backup(server, _first(query, "name"))
        return 200, Raw(path=dest, ctype="application/gzip", filename=name, delete_after=True)
    f = manage.backup_path(sid, _first(query, "name"))
    return 200, Raw(path=f, ctype="application/zip", filename=f.name)


def api_settings(query, body, sid):
    server = find_server(sid)
    if _remote(server):
        remote.pull_for_settings(server)
    return 200, {"supported": True, "state": _state(sid), **options.get_settings(server)}


def api_settings_properties(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "editar as configurações do jogo")
    if _remote(server):
        remote.pull(server, ("server.properties",))
    options.set_property_changes(sid, body.get("changes"))
    if _remote(server):
        remote.push_properties(server)
    return 200, {"ok": True}


def api_settings_gamerules(query, body, sid):
    """Guarda as regras no painel (valem a cada início) e, com o servidor ligado, aplica na hora."""
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        clean = options.check_rule_changes(server, body.get("changes"))
        server["gamerules"] = {**(server.get("gamerules") or {}), **clean}
        db_save(servers)
    rt = RUNTIMES.get(sid)
    if rt and rt.state == "online":
        for name, value in clean.items():
            rt.command(f"gamerule {name} {value}", echo=False)
    return 200, {"ok": True, "applied": bool(rt and rt.state == "online")}


# ---------------------------------------------------------------- Contas

BASE_URL = f"http://127.0.0.1:{PORT}"  # o login sempre volta para este endereço (é o que se cadastra no provedor)
mail.PUBLIC_URL = BASE_URL  # o link "bloquear este endereço" dos e-mails aponta para o site
PROTECTED_PAGES = {"servers.html", "create.html", "panel.html", "admin.html", "profile.html", "settings.html"}


def _claim_legacy(user):
    """A primeira conta fica com os servidores (e a chave SSH) que já existiam antes de haver contas."""
    with DB_LOCK:
        servers = db_load()
        changed = False
        for s in servers:
            if s.get("owner") is None:
                s["owner"] = user["id"]
                changed = True
        if changed:
            db_save(servers)
    old = DATA / "keys"
    if (old / "blockhost_ed25519").exists():
        dest = old / user["id"]
        dest.mkdir(parents=True, exist_ok=True)
        for name, new_name in (("blockhost_ed25519", "aethelhost_ed25519"), ("blockhost_ed25519.pub", "aethelhost_ed25519.pub"), ("known_hosts", "known_hosts")):
            if (old / name).exists():  # o projeto se chamava BlockHost: a chave antiga já vem com o nome novo
                shutil.move(str(old / name), str(dest / new_name))


auth.on_first_user = _claim_legacy


def _require_setup():
    if not auth.setup_allowed(current_user()):
        raise ApiError(403, "Só o administrador pode configurar os logins.")


def api_auth_providers(query, body):
    return 200, {"providers": auth.list_providers(), "canSetup": auth.setup_allowed(current_user()),
                 "password": {"enabled": mail.is_configured()}}  # e-mail e senha só funcionam com o envio de e-mail configurado


def api_auth_me(query, body):
    user = current_user()
    out = {"user": auth.self_user(user) if user else None}
    if user and not user.get("username"):
        out["suggestedUsername"] = accounts.suggest_username(user)
        out["needsPassword"] = accounts.needs_password(user)  # a tela do perfil também pede uma senha do AethelHost
    return 200, out


# ---------------------------------------------------------------- editar o perfil (a própria conta)

def api_account_name(query, body):
    user = current_user()
    auth.update_user(user, name=accounts.check_name(body.get("name")))
    return 200, {"user": auth.self_user(user)}


def _image(data, kind, max_bytes, max_side):
    try:
        return images.check(data, max_bytes, max_side)
    except images.ImageError as e:
        raise ApiError(400, str(e))


def api_account_avatar_set(query, data):
    user = current_user()
    auth.save_image("avatar", user, data, _image(data, "avatar", 300_000, 1024))
    return 200, {"user": auth.self_user(user)}


def api_account_avatar_reset(query, body):
    user = current_user()
    auth.delete_image("avatar", user)
    return 200, {"user": auth.self_user(user)}


def api_account_banner_set(query, body):
    """Escolhe um dos banners prontos (e tira a imagem própria, se tinha)."""
    user = current_user()
    preset = body.get("preset")
    if preset not in auth.BANNER_PRESETS:
        raise ApiError(400, "Banner desconhecido.")
    auth.delete_image("banner", user)
    auth.update_user(user, bannerPreset=preset)
    return 200, {"user": auth.self_user(user)}


def api_account_banner_image(query, data):
    user = current_user()
    auth.save_image("banner", user, data, _image(data, "banner", 2_000_000, 4000))
    return 200, {"user": auth.self_user(user)}


def api_account_unlink(query, body):
    user = current_user()
    auth.unlink_provider(user, str(body.get("provider", "")))
    return 200, {"user": auth.self_user(user)}


def api_account_prefs(query, body):
    user = current_user()
    prefs = dict(user.get("prefs") or {})
    if body.get("theme") in ("dark", "light"):
        prefs["theme"] = body["theme"]
    auth.update_user(user, prefs=prefs)
    return 200, {"user": auth.self_user(user)}


def api_account_password_start(query, body):
    return 200, accounts.start_password_change(current_user(), body)


def api_account_email_start(query, body):
    return 200, accounts.start_email_change(current_user(), body)


def api_account_verify(query, body):
    user = accounts.confirm_change(current_user(), body, getattr(CTX, "cookie", None))
    return 200, {"ok": True, "user": auth.self_user(user)}


def api_account_resend(query, body):
    return 200, accounts.resend_change(current_user(), body)


def _user_image(kind, uid):
    path = auth.image_file(kind, uid) if auth.get_user(uid) else None
    if not path:
        raise ApiError(404, "Imagem não encontrada.")
    return 200, Raw(body=path.read_bytes(), ctype="image/png" if path.suffix == ".png" else "image/jpeg", cache="private, max-age=3600")


def api_user_avatar(query, body, uid):
    return _user_image("avatar", uid)


def api_user_banner(query, body, uid):
    return _user_image("banner", uid)


def api_auth_profile(query, body):
    captcha.require(body.get("captcha"))
    user = current_user()
    pending = accounts.start_profile(user, body)
    if pending:  # com senha: falta o código do e-mail (POST /api/account/verify)
        return 200, pending
    return 200, {"user": auth.public_user(user)}


def api_auth_logout(query, body):
    auth.logout(getattr(CTX, "cookie", None))
    return 200, Reply({"ok": True}, [auth.CLEAR_SESSION])


def api_auth_config(query, body):
    _require_setup()
    return 200, {"redirectBase": BASE_URL, "providers": auth.config_view(BASE_URL)}


def api_auth_config_save(query, body):
    _require_setup()
    auth.save_provider(str(body.get("provider", "")), body)
    return 200, {"providers": auth.list_providers()}


def _require_admin(message="Só o administrador pode ligar ou desligar o playit.gg."):
    user = current_user()
    if not user or not user.get("admin"):
        raise ApiError(403, message)


def merge_accounts(keep_id, drop_id, name=None, username=None):
    """Junta duas contas: os logins, os servidores e a chave SSH da conta `drop` passam para `keep`."""
    with DB_LOCK:
        keep = auth.merge_users(keep_id, drop_id, name, username)
        servers = db_load()
        for s in servers:
            if s.get("owner") == drop_id:
                s["owner"] = keep_id
            fixed, seen = [], {s.get("owner")}
            for sh in s.get("shares", []):
                if sh["user"] == drop_id:
                    sh["user"] = keep_id
                if sh["user"] not in seen:  # sem repetir a mesma pessoa (nem o dono como convidado)
                    seen.add(sh["user"])
                    fixed.append(sh)
            s["shares"] = fixed
        db_save(servers)
        notify.reassign(drop_id, keep_id)
        old_keys, new_keys = DATA / "keys" / drop_id, DATA / "keys" / keep_id
        if old_keys.is_dir() and not new_keys.exists():
            old_keys.rename(new_keys)  # se as duas já tinham chave, a antiga fica onde está (a VPS de cada uma é separada)
    return keep


def api_admin_merge(query, body):
    _require_admin("Só o administrador pode mesclar contas.")
    keep = merge_accounts(str(body.get("keep", "")), str(body.get("drop", "")))
    return 200, {"ok": True, "user": auth.public_user(keep)}


# ---------------------------------------------------------------- compartilhamento

_invite_hits = {}  # dono -> horários dos últimos convites (limite: 15 por hora)


def _card(uid):
    u = auth.get_user(uid)
    return {"id": u["id"], "name": u["name"], "username": u.get("username") or "", "avatar": auth.avatar_url(u)} if u else None


def _share_levels(body, current=None):
    level = body.get("level", current["level"] if current else None)
    files = body.get("files", current["files"] if current else None)
    if level not in SHARE_LEVELS:
        raise ApiError(400, "Escolha o nível de acesso: Completo ou Básico.")
    if files not in SHARE_FILES:
        raise ApiError(400, "Escolha o acesso aos arquivos: Nenhum, Somente leitura ou Leitura e escrita.")
    return level, files


def _shares_view(server):
    owner_role = _role(server)
    return {"owner": _card(server["owner"]), "canEdit": bool(owner_role and owner_role[0] == "owner"),
            "shares": [{"user": _card(s["user"]), "level": s["level"], "files": s["files"], "status": s["status"], "invitedAt": s.get("invitedAt")}
                       for s in server.get("shares", []) if auth.get_user(s["user"])]}


def api_shares_list(query, body, sid):
    return 200, _shares_view(find_server(sid))


def api_share_add(query, body, sid):
    me = current_user()
    username = str(body.get("username", "")).strip().lstrip("@")
    level, files = _share_levels(body)
    if not username:
        raise ApiError(400, "Escreva o nome de usuário de quem vai receber o convite.")
    now = time.time()
    recent = [t for t in _invite_hits.get(me["id"], []) if now - t < 3600]
    if len(recent) >= 15:
        raise ApiError(429, "Você mandou muitos convites em pouco tempo. Tente de novo mais tarde.")
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        target = auth.find_by_username(username)
        if not target:
            raise ApiError(404, "Não achei ninguém com esse nome de usuário. Confira se está certo.")
        if target["id"] == server["owner"]:
            raise ApiError(400, "Esse usuário já é o dono do servidor.")
        if any(s["user"] == target["id"] for s in server["shares"]):
            raise ApiError(409, "Esse usuário já tem acesso (ou um convite pendente) neste servidor.")
        if len(server["shares"]) >= MAX_SHARES:
            raise ApiError(409, f"Um servidor pode ser compartilhado com até {MAX_SHARES} pessoas.")
        server["shares"].append({"user": target["id"], "level": level, "files": files, "status": "pending", "invitedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
        db_save(servers)
    _invite_hits[me["id"]] = recent + [now]
    notify.add(target["id"], "share_invite", {"server": sid, "serverName": server["name"], "from": me["id"], "fromName": me["name"],
                                              "fromUsername": me.get("username") or "", "level": level, "files": files})
    return 201, _shares_view(server)


def api_share_update(query, body, sid, uid):
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        share = next((s for s in server["shares"] if s["user"] == uid), None)
        if not share:
            raise ApiError(404, "Essa pessoa não está na lista.")
        share["level"], share["files"] = _share_levels(body, share)
        db_save(servers)
    if share["status"] == "accepted":
        me = current_user()
        notify.add(uid, "share_changed", {"server": sid, "serverName": server["name"], "from": me["id"], "fromName": me["name"],
                                         "fromUsername": me.get("username") or "", "level": share["level"], "files": share["files"]})
    else:  # convite ainda pendente: o texto do convite passa a mostrar o nível novo
        notify.remove_where(lambda n: n["user"] == uid and n["type"] == "share_invite" and n["data"].get("server") == sid)
        me = current_user()
        notify.add(uid, "share_invite", {"server": sid, "serverName": server["name"], "from": me["id"], "fromName": me["name"],
                                         "fromUsername": me.get("username") or "", "level": share["level"], "files": share["files"]})
    return 200, _shares_view(server)


def api_share_remove(query, body, sid, uid):
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        share = next((s for s in server["shares"] if s["user"] == uid), None)
        if not share:
            raise ApiError(404, "Essa pessoa não está na lista.")
        server["shares"] = [s for s in server["shares"] if s["user"] != uid]
        db_save(servers)
    notify.remove_where(lambda n: n["user"] == uid and n["type"] == "share_invite" and n["data"].get("server") == sid)
    if share["status"] == "accepted":
        me = current_user()
        notify.add(uid, "share_removed", {"server": sid, "serverName": server["name"], "from": me["id"], "fromName": me["name"], "fromUsername": me.get("username") or ""})
    return 200, _shares_view(server)


def api_share_leave(query, body, sid):
    """Quem recebeu o servidor sai dele."""
    me = current_user()
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        if server["owner"] == me["id"]:
            raise ApiError(400, "O dono não pode sair do próprio servidor. Exclua o servidor se não quiser mais.")
        server["shares"] = [s for s in server["shares"] if s["user"] != me["id"]]
        db_save(servers)
    notify.add(server["owner"], "share_left", {"server": sid, "serverName": server["name"], "from": me["id"], "fromName": me["name"], "fromUsername": me.get("username") or ""})
    return 200, {"ok": True}


def api_notifications(query, body):
    items, unread = notify.for_user(current_user()["id"])
    return 200, {"items": items, "unread": unread}


def api_notifications_read(query, body):
    ids = body.get("ids")
    notify.mark_read(current_user()["id"], set(map(str, ids)) if isinstance(ids, list) else None)
    return 200, {"ok": True}


def api_notification_delete(query, body, nid):
    n = notify.get(current_user()["id"], nid)
    if n and n["type"] == "share_invite":
        raise ApiError(409, "Aceite ou recuse o convite.")
    notify.remove(current_user()["id"], nid)
    return 200, {"ok": True}


def _answer_invite(nid, accept):
    me = current_user()
    n = notify.get(me["id"], nid)
    if not n or n["type"] != "share_invite":
        raise ApiError(404, "Convite não encontrado.")
    sid = n["data"]["server"]
    with DB_LOCK:
        servers = db_load()
        server = next((s for s in servers if s["id"] == sid), None)
        share = next((s for s in (server or {}).get("shares", []) if s["user"] == me["id"] and s["status"] == "pending"), None)
        if not share:  # o dono cancelou ou apagou o servidor
            notify.remove(me["id"], nid)
            raise ApiError(410, "Este convite não vale mais.")
        if accept:
            share["status"], share["acceptedAt"] = "accepted", time.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            server["shares"] = [s for s in server["shares"] if s is not share]
        db_save(servers)
    notify.remove(me["id"], nid)
    notify.add(server["owner"], "share_accepted" if accept else "share_declined",
               {"server": sid, "serverName": server["name"], "from": me["id"], "fromName": me["name"], "fromUsername": me.get("username") or "",
                "level": share["level"], "files": share["files"]})
    return 200, {"ok": True, "server": view(server) if accept else None}


def api_notification_accept(query, body, nid):
    return _answer_invite(nid, True)


def api_notification_decline(query, body, nid):
    return _answer_invite(nid, False)


def api_admin_overview(query, body):
    """Painel do administrador: todas as contas e os servidores de cada uma (só leitura)."""
    _require_admin("Só o administrador pode ver o painel de administração.")
    users = auth.all_users()
    known = {u["id"] for u in users}
    by_owner = {}
    online_players = ram_in_use = running = 0
    for s in db_load():
        rt = RUNTIMES.get(s["id"])
        snap = rt.snapshot() if rt else {"state": "offline", "players": []}
        item = {"id": s["id"], "name": s["name"], "ip": s["ip"], "plan": s["plan"], "software": SOFTWARE[s["software"]]["label"],
                "version": s["version"], "ramMb": s["ramMb"], "port": s["port"], "public": bool(s.get("public")),
                "createdAt": s.get("createdAt"), "state": snap["state"], "players": len(snap["players"])}
        by_owner.setdefault(s.get("owner") if s.get("owner") in known else None, []).append(item)
        if snap["state"] != "offline":
            running += 1
            online_players += item["players"]
            if s["plan"] == "free":
                ram_in_use += s["ramMb"]  # a VPS usa a memória da VPS, não a deste PC
    for u in users:
        u["servers"] = by_owner.get(u["id"], [])
    return 200, {
        "users": users,
        "orphans": by_owner.get(None, []),  # servidores de uma conta que não existe mais (ou sem dono)
        "totals": {"users": len(users), "servers": sum(len(v) for v in by_owner.values()), "running": running,
                   "players": online_players, "ramInUseMb": ram_in_use},
    }


def api_tunnel(query, body):
    st = tunnel.status()
    if not (current_user() or {}).get("admin"):
        st["claim"] = None  # o link de aprovação só serve para quem administra
    st["admin"] = bool((current_user() or {}).get("admin"))
    return 200, st


def api_tunnel_link(query, body):
    _require_admin()
    if not tunnel.supported():
        raise ApiError(400, "O playit ainda não tem programa para o sistema deste PC.")
    return 200, tunnel.begin_claim()


def api_tunnel_unlink(query, body):
    _require_admin()
    with DB_LOCK:
        servers = db_load()
        for s in servers:
            s["public"] = False  # sem conta ligada, ninguém tem endereço público
        db_save(servers)
    tunnel.unlink()
    return 200, {"ok": True}


def api_public_set(query, body, sid):
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "Informe enabled: true ou false.")
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        if server["plan"] != "free":
            raise ApiError(400, "Na VPS o servidor já tem o endereço da própria VPS.")
        if enabled and not tunnel.secret():
            raise ApiError(409, "O administrador precisa ligar o AethelHost ao playit.gg antes.")
        server["public"] = enabled
        db_save(servers)
    rt = RUNTIMES.get(sid)
    if rt and rt.state in ("starting", "online"):
        tunnel.want(sid, server["port"]) if enabled else tunnel.unwant(sid)
    return 200, view(server)


def api_pw_register(query, body):
    captcha.require(body.get("captcha"))
    return 200, accounts.register(body)


def api_pw_login(query, body):
    return 200, accounts.login(body)


def api_pw_resend(query, body):
    return 200, accounts.resend(body)


def api_pw_verify(query, body):
    user, token = accounts.verify(body)
    return 200, Reply({"ok": True, "user": auth.public_user(user)}, [auth.session_cookie(token)])


def api_mail_get(query, body):
    _require_setup()
    return 200, mail.config_view()


def api_mail_save(query, body):
    _require_setup()
    mail.save(body)
    return 200, mail.config_view()


def api_mail_test(query, body):
    _require_setup()
    to = mail.normalize_email(body.get("to"))
    if not to:
        raise ApiError(400, "Informe um e-mail válido para receber o teste.")
    if not mail.send(to, f"{mail.SITE_NAME}: e-mail de teste", "Deu certo! O envio de e-mail do AethelHost está funcionando."):
        raise ApiError(409, "Esse endereço pediu para não receber e-mails do AethelHost.")
    return 200, {"ok": True}


def api_captcha_get(query, body):
    _require_setup()
    return 200, captcha.config_view()


def api_captcha_save(query, body):
    _require_setup()
    return 200, captcha.save(body)


def api_captcha_config(query, body):
    """Chave pública do captcha, para qualquer visitante montar o widget (nada, se não estiver configurado)."""
    return 200, captcha.public_config()


ID = r"([a-z0-9]{1,32})"
ROUTES = [
    ("POST", r"^/api/account/name$", api_account_name),
    ("POST", r"^/api/account/avatar$", api_account_avatar_set),
    ("DELETE", r"^/api/account/avatar$", api_account_avatar_reset),
    ("POST", r"^/api/account/banner$", api_account_banner_set),
    ("POST", r"^/api/account/banner/image$", api_account_banner_image),
    ("POST", r"^/api/account/prefs$", api_account_prefs),
    ("POST", r"^/api/account/unlink$", api_account_unlink),
    ("POST", r"^/api/account/password/start$", api_account_password_start),
    ("POST", r"^/api/account/email/start$", api_account_email_start),
    ("POST", r"^/api/account/verify$", api_account_verify),
    ("POST", r"^/api/account/resend$", api_account_resend),
    ("GET", rf"^/api/users/{ID}/avatar$", api_user_avatar),
    ("GET", rf"^/api/users/{ID}/banner$", api_user_banner),
    ("GET", r"^/api/notifications$", api_notifications),
    ("POST", r"^/api/notifications/read$", api_notifications_read),
    ("DELETE", rf"^/api/notifications/{ID}$", api_notification_delete),
    ("POST", rf"^/api/notifications/{ID}/accept$", api_notification_accept),
    ("POST", rf"^/api/notifications/{ID}/decline$", api_notification_decline),
    ("GET", rf"^/api/servers/{ID}/shares$", api_shares_list),
    ("POST", rf"^/api/servers/{ID}/shares$", api_share_add),
    ("POST", rf"^/api/servers/{ID}/shares/{ID}$", api_share_update),
    ("DELETE", rf"^/api/servers/{ID}/shares/{ID}$", api_share_remove),
    ("POST", rf"^/api/servers/{ID}/leave$", api_share_leave),
    ("POST", r"^/api/auth/password/register$", api_pw_register),
    ("POST", r"^/api/auth/password/login$", api_pw_login),
    ("POST", r"^/api/auth/password/resend$", api_pw_resend),
    ("POST", r"^/api/auth/password/verify$", api_pw_verify),
    ("GET", r"^/api/auth/mail$", api_mail_get),
    ("POST", r"^/api/auth/mail$", api_mail_save),
    ("POST", r"^/api/auth/mail/test$", api_mail_test),
    ("GET", r"^/api/captcha/config$", api_captcha_config),
    ("GET", r"^/api/auth/captcha$", api_captcha_get),
    ("POST", r"^/api/auth/captcha$", api_captcha_save),
    ("GET", r"^/api/admin/overview$", api_admin_overview),
    ("POST", r"^/api/admin/merge$", api_admin_merge),
    ("GET", r"^/api/tunnel$", api_tunnel),
    ("POST", r"^/api/tunnel/link$", api_tunnel_link),
    ("POST", r"^/api/tunnel/unlink$", api_tunnel_unlink),
    ("POST", rf"^/api/servers/{ID}/public$", api_public_set),
    ("GET", r"^/api/auth/providers$", api_auth_providers),
    ("GET", r"^/api/auth/me$", api_auth_me),
    ("POST", r"^/api/auth/profile$", api_auth_profile),
    ("POST", r"^/api/auth/logout$", api_auth_logout),
    ("GET", r"^/api/auth/config$", api_auth_config),
    ("POST", r"^/api/auth/config$", api_auth_config_save),
    ("GET", r"^/api/meta$", api_meta),
    ("GET", r"^/api/ip-check$", api_ip_check),
    ("GET", r"^/api/servers$", api_list),
    ("POST", r"^/api/servers$", api_create),
    ("GET", rf"^/api/servers/{ID}$", api_get),
    ("PATCH", rf"^/api/servers/{ID}$", api_patch),
    ("DELETE", rf"^/api/servers/{ID}$", api_delete),
    ("POST", rf"^/api/servers/{ID}/start$", api_start),
    ("POST", rf"^/api/servers/{ID}/stop$", api_stop),
    ("POST", rf"^/api/servers/{ID}/command$", api_command),
    ("GET", rf"^/api/servers/{ID}/console$", api_console),
    ("GET", r"^/api/vps/key$", api_vps_key),
    ("POST", rf"^/api/servers/{ID}/vps/check$", api_vps_check),
    ("POST", rf"^/api/servers/{ID}/vps/port$", api_vps_port),
    ("GET", r"^/api/software/([a-z]{1,20})/versions$", api_software_versions),
    ("POST", rf"^/api/servers/{ID}/software$", api_software_set),
    ("GET", rf"^/api/servers/{ID}/content$", api_content_list),
    ("GET", rf"^/api/servers/{ID}/content/search$", api_content_search),
    ("POST", rf"^/api/servers/{ID}/content/install$", api_content_install),
    ("POST", rf"^/api/servers/{ID}/content/upload$", api_content_upload),
    ("POST", rf"^/api/servers/{ID}/content/toggle$", api_content_toggle),
    ("POST", rf"^/api/servers/{ID}/content/delete$", api_content_delete),
    ("GET", rf"^/api/servers/{ID}/icon$", api_icon_get),
    ("POST", rf"^/api/servers/{ID}/icon$", api_icon_set),
    ("POST", rf"^/api/servers/{ID}/icon/reset$", api_icon_reset),
    ("GET", rf"^/api/servers/{ID}/players$", api_players),
    ("POST", rf"^/api/servers/{ID}/players/action$", api_players_action),
    ("GET", rf"^/api/servers/{ID}/files$", api_files_list),
    ("GET", rf"^/api/servers/{ID}/files/read$", api_files_read),
    ("POST", rf"^/api/servers/{ID}/files/write$", api_files_write),
    ("POST", rf"^/api/servers/{ID}/files/upload$", api_files_upload),
    ("POST", rf"^/api/servers/{ID}/files/mkdir$", api_files_mkdir),
    ("POST", rf"^/api/servers/{ID}/files/delete$", api_files_delete),
    ("POST", rf"^/api/servers/{ID}/files/rename$", api_files_rename),
    ("GET", rf"^/api/servers/{ID}/files/download$", api_files_download),
    ("GET", rf"^/api/servers/{ID}/worlds$", api_worlds),
    ("POST", rf"^/api/servers/{ID}/worlds/use$", api_world_use),
    ("POST", rf"^/api/servers/{ID}/worlds/create$", api_world_create),
    ("POST", rf"^/api/servers/{ID}/worlds/delete$", api_world_delete),
    ("GET", rf"^/api/servers/{ID}/worlds/download$", api_world_download),
    ("POST", rf"^/api/servers/{ID}/worlds/upload$", api_world_upload),
    ("GET", rf"^/api/servers/{ID}/backups$", api_backups),
    ("GET", rf"^/api/servers/{ID}/backups/drive$", api_backup_drive_get),
    ("POST", rf"^/api/servers/{ID}/backups/drive$", api_backup_drive_send),
    ("GET", r"^/api/drive$", api_drive_get),
    ("POST", r"^/api/drive/unlink$", api_drive_unlink),
    ("POST", r"^/api/drive/settings$", api_drive_settings),
    ("POST", rf"^/api/servers/{ID}/backups/create$", api_backup_create),
    ("POST", rf"^/api/servers/{ID}/backups/restore$", api_backup_restore),
    ("POST", rf"^/api/servers/{ID}/backups/delete$", api_backup_delete),
    ("GET", rf"^/api/servers/{ID}/backups/download$", api_backup_download),
    ("GET", rf"^/api/servers/{ID}/settings$", api_settings),
    ("POST", rf"^/api/servers/{ID}/settings/properties$", api_settings_properties),
    ("POST", rf"^/api/servers/{ID}/settings/gamerules$", api_settings_gamerules),
]
# O que cada rota de servidor exige de quem não é o dono. Tudo que não está aqui (excluir o servidor, dados da VPS,
# mudar o compartilhamento…) é só do dono. Arquivos têm o seu próprio nível (leitura ou escrita).
NEED = {
    **{f: "basic" for f in (api_get, api_start, api_stop, api_console, api_icon_get, api_players, api_shares_list, api_share_leave)},
    **{f: "full" for f in (api_command, api_players_action, api_patch, api_icon_set, api_icon_reset, api_software_set, api_public_set,
                           api_content_list, api_content_search, api_content_install, api_content_upload, api_content_toggle, api_content_delete,
                           api_worlds, api_world_use, api_world_create, api_world_delete, api_world_download, api_world_upload,
                           api_backups, api_backup_create, api_backup_restore, api_backup_delete, api_backup_download,
                           api_backup_drive_get, api_backup_drive_send,
                           api_settings, api_settings_properties, api_settings_gamerules)},
    **{f: "files_read" for f in (api_files_list, api_files_read, api_files_download)},
    **{f: "files_write" for f in (api_files_write, api_files_upload, api_files_mkdir, api_files_delete, api_files_rename)},
}
PUBLIC = {api_auth_providers, api_auth_me, api_auth_logout, api_auth_config, api_auth_config_save,
          api_pw_register, api_pw_login, api_pw_resend, api_pw_verify, api_mail_get, api_mail_save, api_mail_test,
          api_captcha_config, api_captcha_get, api_captcha_save}  # não exigem login
RAW_UPLOAD = {api_content_upload, api_icon_set, api_files_upload, api_account_avatar_set, api_account_banner_image}  # recebem o arquivo cru (octet-stream) em vez de JSON
STREAM_UPLOAD = {api_world_upload}  # arquivos grandes: vão direto para o disco, sem ocupar a memória
MAX_UPLOAD = 64 * 1024 * 1024
RAW_LIMIT = {api_account_avatar_set: 400_000, api_account_banner_image: 2_500_000}  # fotos e banners são pequenos
MAX_STREAM = 2 * 1024 * 1024 * 1024

# Só estes arquivos do site são entregues (a pasta data/ e o .git nunca saem daqui).
STATIC_RE = re.compile(r"^(?:[a-z]+\.html|css/[\w.-]+\.css|js/[\w.-]+\.js|assets/[\w.-]+\.(?:svg|png))$")
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "AethelHost"

    def log_message(self, *args):  # o polling do painel encheria o terminal
        pass

    def _send(self, status, payload, ctype, cache="no-store", cookies=()):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, data, cookies=()):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", cookies=cookies)

    def _redirect(self, location, cookies=()):
        try:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth_route(self, method, parsed):
        """/auth/login/<provedor> leva ao provedor; /auth/callback/<provedor> é para onde ele devolve o login."""
        if method != "GET":
            raise ApiError(405, "Método não permitido.")
        if self.headers.get("Host", "").lower() != f"127.0.0.1:{PORT}":
            return self._redirect(BASE_URL + self.path)  # o login acontece sempre em 127.0.0.1
        md = re.match(r"^/auth/drive/(start|callback)$", parsed.path)
        if md:
            return self._drive_route(md.group(1), parsed)
        m = re.match(r"^/auth/(login|callback|link)/([a-z]{1,20})$", parsed.path)
        if not m:
            raise ApiError(404, "Página não encontrada.")
        action, pid = m.groups()
        query = parse_qs(parsed.query)
        me = current_user()
        try:
            if action == "login":
                url, bind = auth.begin(pid, BASE_URL, _first(query, "next"))
                return self._redirect(url, [auth.bind_cookie(bind)])
            if action == "link":  # conectar mais um login à conta que já está logada (aba Conectar-se)
                if not me:
                    return self._redirect(f"{BASE_URL}/login.html?next=/settings.html")
                url, bind = auth.begin(pid, BASE_URL, "/settings.html", link_user=me["id"])
                return self._redirect(url, [auth.bind_cookie(bind)])
            if _first(query, "error"):
                raise auth.LoginFailed("denied")
            _, token, next_url = auth.finish(pid, _first(query, "code"), _first(query, "state"),
                                             auth.bind_from_cookie(self.headers.get("Cookie")), BASE_URL, me["id"] if me else None)
            if token is None:  # conexão feita: volta para a aba Conectar-se, sem trocar a sessão
                return self._redirect(f"{BASE_URL}/settings.html?linked={pid}#connect", [auth.CLEAR_BIND])
            return self._redirect(BASE_URL + next_url, [auth.session_cookie(token), auth.CLEAR_BIND])
        except auth.LoginFailed as e:
            if me:  # quem já está logado estava conectando um login: o erro aparece na aba Conectar-se
                return self._redirect(f"{BASE_URL}/settings.html?link_error={e.code}#connect", [auth.CLEAR_BIND])
            return self._redirect(f"{BASE_URL}/login.html?error={e.code}", [auth.CLEAR_BIND])

    def _drive_route(self, action, parsed):
        """/auth/drive/start vai ao Google pedir acesso ao Drive; /auth/drive/callback é para onde ele devolve."""
        query = parse_qs(parsed.query)
        me = current_user()
        if not me:
            return self._redirect(f"{BASE_URL}/login.html")

        def back(server_id, **params):  # volta para a aba Backups do servidor de onde a pessoa saiu
            if re.fullmatch(r"[0-9a-f]{12}", server_id or ""):
                return f"{BASE_URL}/panel.html?" + urlencode({"id": server_id, "tab": "backups", **params})
            return f"{BASE_URL}/servers.html?" + urlencode(params)

        try:
            if action == "start":
                url, bind = drive.begin(me["id"], BASE_URL, _first(query, "server"))
                return self._redirect(url, [auth.bind_cookie(bind)])
            if _first(query, "error"):
                raise drive.DriveLinkFailed("denied", drive.server_of(_first(query, "state")))
            sid = drive.finish(_first(query, "code"), _first(query, "state"), auth.bind_from_cookie(self.headers.get("Cookie")),
                               BASE_URL, me["id"])
            return self._redirect(back(sid, drive="linked"), [auth.CLEAR_BIND])
        except drive.DriveLinkFailed as e:
            return self._redirect(back(e.server, drive_error=e.code), [auth.CLEAR_BIND])
        except content.ContentError:
            return self._redirect(back(_first(query, "server"), drive_error="not_configured"), [auth.CLEAR_BIND])

    def _send_file(self, status, raw):
        """Envia um arquivo do disco aos poucos (mundos e backups podem ter centenas de MB)."""
        path = Path(raw.path)
        try:
            self.send_response(status)
            self.send_header("Content-Type", raw.ctype)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if raw.filename:
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % re.sub(r"[^A-Za-z0-9._-]", "_", raw.filename))
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if raw.delete_after:
                path.unlink(missing_ok=True)

    def _save_stream(self, length):
        """Grava o corpo da requisição direto em um arquivo temporário."""
        manage.TMP_DIR.mkdir(parents=True, exist_ok=True)
        temp = manage.TMP_DIR / f"{uuid.uuid4().hex}.upload"
        left = length
        with open(temp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        return temp

    def _handle(self, method):
        CTX.user, CTX.cookie = None, self.headers.get("Cookie")
        try:
            # Barra outros sites que tentem usar esta API pelo seu navegador.
            if self.headers.get("Host", "").lower() not in {o.split("//")[1] for o in ORIGINS}:
                raise ApiError(403, "Host não permitido.")
            CTX.user = auth.user_from_cookie(CTX.cookie)  # quem está logado (ou ninguém)
            parsed = urlparse(self.path)
            if parsed.path.startswith("/auth/"):
                self._auth_route(method, parsed)
            elif parsed.path == "/mail/block":
                self._mail_block(method, parsed)
            elif parsed.path.startswith("/api/"):
                self._api(method, parsed)
            elif method == "GET":
                self._static(parsed.path, parsed.query)
            else:
                raise ApiError(405, "Método não permitido.")
        except ApiError as e:
            self._json(e.status, {"error": e.message, **e.extra})
        except content.ContentError as e:
            self._json(e.status, {"error": e.message})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._json(404, {"error": "Não encontrei isso no serviço externo (Modrinth, Paper, Fabric…)."})
            else:
                self._json(502, {"error": f"O serviço externo respondeu com erro {e.code}."})
        except (urllib.error.URLError, TimeoutError) as e:
            self._json(502, {"error": f"Não consegui acessar o serviço externo: {getattr(e, 'reason', e)}"})
        except RuntimeError as e:  # falhas previstas nos downloads (hash errado, endereço barrado…)
            self._json(502, {"error": str(e)})
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Erro interno do servidor."})

    def _mail_block(self, method, parsed):
        """Página do link "Bloquear este endereço" dos e-mails. O link só mostra a página; quem bloqueia é o botão (POST),
        para que programas que abrem links de e-mail sozinhos não bloqueiem ninguém sem querer."""
        if method not in ("GET", "POST"):
            raise ApiError(405, "Método não permitido.")
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ApiError(413, "Requisição grande demais.")
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        else:
            form = parse_qs(parsed.query)
        field = lambda k: (form.get(k) or [""])[0]
        lang = "pt" if field("l") == "pt" else "en"
        email, token = mail.normalize_email(field("e")), field("t")
        if not mail.check_token(email, token):
            page = mailtemplate.block_page(lang, "invalid")
        elif method == "POST" and field("action") == "block":
            mail.set_blocked(email, True)
            page = mailtemplate.block_page(lang, "blocked", email, token)
        elif method == "POST" and field("action") == "unblock":
            mail.set_blocked(email, False)
            page = mailtemplate.block_page(lang, "unblocked", email, token)
        else:
            page = mailtemplate.block_page(lang, "already" if mail.is_blocked(email) else "ask", email, token)
        self._send(200 if mail.check_token(email, token) else 400, page.encode("utf-8"), "text/html; charset=utf-8")

    def _api(self, method, parsed):
        route = None
        for m, pattern, fn in ROUTES:
            match = re.match(pattern, parsed.path)
            if m == method and match:
                route = (fn, match.groups())
                break
        if not route:
            raise ApiError(404, "Rota não encontrada.")
        fn, groups = route
        if fn not in PUBLIC and not current_user():
            raise ApiError(401, "Faça login para continuar.", {"needLogin": True})  # só este 401 significa "sessão acabou"
        CTX.need = NEED.get(fn, "owner")
        if groups and parsed.path.startswith("/api/servers/"):
            _pick(db_load(), groups[0])  # 404 se não tem acesso, 403 se o nível não basta

        body = {}
        temp = None
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin and origin not in ORIGINS:
                raise ApiError(403, "Origem não permitida.")
            stream = fn in STREAM_UPLOAD
            raw = stream or fn in RAW_UPLOAD
            expected = "application/octet-stream" if raw else "application/json"
            if self.headers.get("Content-Type", "").split(";")[0].strip() != expected:
                raise ApiError(415, f"Envie o conteúdo como {expected}.")
            length = int(self.headers.get("Content-Length") or 0)
            if length > (MAX_STREAM if stream else RAW_LIMIT.get(fn, MAX_UPLOAD) if raw else 65536):
                raise ApiError(413, "Requisição grande demais.")
            if stream:
                temp = body = self._save_stream(length)
            else:
                payload = self.rfile.read(length)
                if raw:
                    body = payload
                else:
                    try:
                        body = json.loads(payload or b"{}")
                    except json.JSONDecodeError:
                        raise ApiError(400, "JSON inválido.")
                    if not isinstance(body, dict):
                        raise ApiError(400, "JSON inválido.")
        try:
            status, data = fn(parse_qs(parsed.query), body, *groups)
        finally:
            if temp:
                temp.unlink(missing_ok=True)
        if isinstance(data, Raw):
            if data.path:
                self._send_file(status, data)
            else:
                self._send(status, data.body, data.ctype, data.cache)
        elif isinstance(data, Reply):
            self._json(status, data.data, data.cookies)
        else:
            self._json(status, data)

    def _not_found(self):
        self._send(404, (ROOT / "notfound.html").read_bytes(), "text/html; charset=utf-8")

    def _static(self, path, query=""):
        rel = unquote(path).lstrip("/") or "index.html"
        file = ROOT / rel
        if not STATIC_RE.match(rel) or not file.is_file():
            return self._not_found()
        if rel in PROTECTED_PAGES and not current_user():  # sem conta, essas páginas mandam para o login
            return self._redirect("/login.html?next=" + quote("/" + rel + ("?" + query if query else "")))
        if rel == "admin.html" and not (current_user() or {}).get("admin"):  # quem não administra nem fica sabendo que a página existe
            return self._not_found()
        if rel == "panel.html":  # servidor que não existe ou que a conta não pode ver: mesma página de "não existe"
            sid = (parse_qs(query).get("id") or [""])[0]
            if not any(s["id"] == sid and _role(s) for s in db_load()):
                return self._not_found()
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, file.read_bytes(), ctype)

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")


def migrate_legacy_names():
    """O projeto se chamava BlockHost: renomeia os arquivos internos que já existem para os nomes novos (sem perder nada)."""
    pairs = {"blockhost-icon.png": "aethelhost-icon.png", "blockhost-content.json": "aethelhost-content.json",
             "blockhost-install.json": "aethelhost-install.json"}
    if SERVERS_DIR.is_dir():
        for folder in SERVERS_DIR.iterdir():
            for old, new in pairs.items():
                if (folder / old).is_file() and not (folder / new).exists():
                    try:
                        (folder / old).rename(folder / new)
                    except OSError:
                        pass
    keys = DATA / "keys"
    if keys.is_dir():  # só as pastas das contas: a chave solta na raiz é tratada por _claim_legacy
        for folder in [d for d in keys.iterdir() if d.is_dir()]:
            for old, new in (("blockhost_ed25519", "aethelhost_ed25519"), ("blockhost_ed25519.pub", "aethelhost_ed25519.pub")):
                if (folder / old).is_file() and not (folder / new).exists():
                    try:
                        (folder / old).rename(folder / new)
                    except OSError:
                        pass


def main():
    DATA.mkdir(exist_ok=True)
    migrate_legacy_names()
    manage.cleanup_tmp()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=adopt_vps_servers, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AethelHost rodando em http://127.0.0.1:{PORT}  (Ctrl+C para parar)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Desligando os servidores de Minecraft…")
        shutdown_all()


if __name__ == "__main__":
    main()
