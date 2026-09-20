#!/usr/bin/env python3
"""BlockHost — backend local.

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
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # deixa importar config, net, software, content

import content  # noqa: E402
import auth  # noqa: E402
import manage  # noqa: E402
import options  # noqa: E402
from config import BACKUPS_DIR, DATA, JARS_DIR, ROOT, SERVERS_DIR  # noqa: E402
from props import COLOR_CODE_RE, motd_to_properties, prop_escape, read_properties, set_properties  # noqa: E402
from software import (INSTALLERS, SOFTWARE, ensure_jar, mc_releases, prefetch_java, prepare_launch,  # noqa: E402
                      ram_options, required_java, software_versions, spec_for, system_ram_mb, vkey)

DB_FILE = DATA / "servers.json"

HOST = "127.0.0.1"  # só este PC acessa a API
PORT = int(os.environ.get("BLOCKHOST_PORT") or 8080)  # outra porta só para testes
DOMAIN = "blockhost.net"  # nome provisório
FIRST_MC_PORT = 25565
MAX_PLAYERS = 20
DEFAULT_RAM_MB = 1024
IDLE_LIMIT = 5 * 3600  # plano grátis fecha após 5h sem jogadores
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

IP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$")
HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$", re.I)
JOIN_RE = re.compile(r"\]: (\w{1,16}) joined the game$")
LEAVE_RE = re.compile(r"\]: (\w{1,16}) left the game$")


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


def _owned(server):
    user = current_user()
    return bool(user and server.get("owner") == user["id"])


def _pick(servers, sid):
    """O servidor `sid`, se ele for da conta logada. Para os outros, é como se não existisse."""
    server = next((s for s in servers if s["id"] == sid and _owned(s)), None)
    if not server:
        raise ApiError(404, "Servidor não encontrado.")
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
    return SERVERS_DIR / sid / "blockhost-icon.png"  # a "capa" escolhida por você


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

def key_paths(owner):
    """Cada conta tem a sua própria chave SSH: uma conta nunca consegue usar a VPS de outra."""
    folder = DATA / "keys" / owner
    return folder / "blockhost_ed25519", folder / "known_hosts"


def ensure_key(owner):
    """Chave SSH da conta. A privada nunca sai deste PC: você só copia a pública."""
    key_file, _ = key_paths(owner)
    pub = Path(str(key_file) + ".pub")
    if not key_file.exists() or not pub.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.unlink(missing_ok=True)
        pub.unlink(missing_ok=True)
        try:
            r = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "blockhost", "-f", str(key_file)],
                capture_output=True, timeout=30, creationflags=NO_WINDOW,
            )
        except FileNotFoundError:
            raise ApiError(500, "O ssh-keygen não foi encontrado neste PC.")
        if r.returncode != 0:
            raise ApiError(500, "Não consegui gerar a chave SSH: " + r.stderr.decode("utf-8", "replace").strip()[:200])
        if os.name == "nt":  # o ssh do Windows recusa chave que outros usuários possam ler
            subprocess.run(
                ["icacls", str(key_file), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                capture_output=True, creationflags=NO_WINDOW,
            )
    return pub.read_text(encoding="utf-8").strip()


def ssh_error(text):
    t, low = text.strip(), text.lower()
    if "permission denied" in low:
        return ("A VPS recusou a chave SSH. Adicione a chave pública do BlockHost ao arquivo "
                "~/.ssh/authorized_keys do usuário informado.")
    if "host key verification failed" in low or "identification has changed" in low:
        return "A identidade da VPS mudou (VPS reinstalada?). Se foi você, apague a linha dela em data/keys/known_hosts."
    if "timed out" in low or "no route to host" in low:
        return "Não consegui alcançar a VPS. Confira o IP, a porta SSH e o firewall do provedor."
    if "connection refused" in low:
        return "A VPS recusou a conexão. O SSH está rodando nessa porta?"
    if "could not resolve" in low:
        return "Não encontrei esse endereço. Confira o IP ou domínio da VPS."
    return "Falha na conexão SSH: " + (t.splitlines()[-1] if t else "sem detalhes")


def ssh_cmd(vps, remote):
    key_file, known_hosts = key_paths(vps["owner"])
    return [
        "ssh", "-i", str(key_file), "-p", str(vps["port"]),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",  # confia na 1ª conexão e avisa se a VPS mudar depois
        "-o", f"UserKnownHostsFile={known_hosts.as_posix()}",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        f"{vps['user']}@{vps['host']}", remote,
    ]


def ssh_script(vps, script, timeout=60):
    """Roda um script bash na VPS. Devolve (código, saída); RuntimeError se não conseguir conectar."""
    ensure_key(vps["owner"])
    try:
        r = subprocess.run(
            ssh_cmd(vps, "bash -s"), input=script.encode("utf-8"), capture_output=True,
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("A VPS demorou demais para responder.")
    except FileNotFoundError:
        raise RuntimeError("O cliente SSH (ssh.exe) não foi encontrado neste PC.")
    if r.returncode == 255:  # 255 é o código do próprio ssh quando não conecta
        raise RuntimeError(ssh_error(r.stderr.decode("utf-8", "replace")))
    return r.returncode, r.stdout.decode("utf-8", "replace")


def ssh_stream(vps, script, on_line, limit=1200):
    """Como ssh_script, mas entrega cada linha assim que chega (para instalações demoradas)."""
    ensure_key(vps["owner"])
    try:
        p = subprocess.Popen(
            ssh_cmd(vps, "bash -s"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
        )
    except FileNotFoundError:
        raise RuntimeError("O cliente SSH (ssh.exe) não foi encontrado neste PC.")
    watchdog = threading.Timer(limit, p.kill)  # não deixa uma instalação travada para sempre
    watchdog.start()
    try:
        p.stdin.write(script.encode("utf-8"))
        p.stdin.close()
        last = []
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            last = (last + [line])[-5:]
            on_line(line)
        code = p.wait()
    finally:
        watchdog.cancel()
    if code == 255:
        raise RuntimeError(ssh_error("\n".join(last)))
    return code


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
DIR="$HOME/blockhost/$ID"
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
setprop max-players "$MAXP"

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
XMX=$((MEM * 6 / 10))
if [ "$XMX" -gt 1024 ]; then XMX=1024; fi
rm -f logs/latest.log
say "Iniciando o servidor (Java $(java_major), ${XMX} MB)..."
tmux new-session -d -s "$SESSION" -c "$DIR" "exec java -Xms128M -Xmx${XMX}M -jar '$JAR_NAME' nogui"
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
            if server["software"] in INSTALLERS:
                raise RuntimeError(f"{SOFTWARE[server['software']]['label']} ainda não funciona no plano VPS. Use Vanilla, Paper, Purpur ou Fabric.")
            jar = spec_for(server["software"], server["version"])
            if jar.get("note"):
                self.log(jar["note"])
            code = ssh_stream(self.vps, remote_setup_script(server, need, jar), self._setup_line)
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

    def _tail(self, start_at):
        threading.Thread(target=self._poll_session, daemon=True).start()
        while not self.gone:
            cmd = f'tail -n {start_at} -F "$HOME/blockhost/{self.sid}/logs/latest.log" 2>&1'
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
    return {
        **server,
        "publicName": f"{server['ip']}.{DOMAIN}",
        "address": f"{server['vps']['host']}:{server['port']}" if server["plan"] == "vps" else f"localhost:{server['port']}",
        "maxPlayers": _max_players(server),
        "softwareLabel": SOFTWARE[server["software"]]["label"],
        "contentKind": content.kind_of(server),
        "iconVersion": icon_version(server["id"]),
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
    return 200, [view(s) for s in db_load() if _owned(s)]


def api_create(query, body):
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
    if plan == "vps" and software in INSTALLERS:
        raise ApiError(400, f"{SOFTWARE[software]['label']} ainda não funciona no plano VPS. Use Vanilla, Paper, Purpur ou Fabric.")

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
        vps = {"provider": str(v.get("provider", "Outro"))[:40], "host": host, "port": port, "user": user}

    with DB_LOCK:
        servers = db_load()
        if any(s["ip"] == ip for s in servers):
            raise ApiError(409, "Este endereço já está em uso. Escolha outro.")
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
            "plan": plan, "port": port, "eula": True, "owner": current_user()["id"],
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
        if not isinstance(ram, int) or (ram not in ram_options() and ram != server["ramMb"]):
            raise ApiError(400, "Quantidade de RAM inválida para este PC.")
        if server["plan"] == "vps" and sw in INSTALLERS:
            raise ApiError(400, f"{SOFTWARE[sw]['label']} ainda não funciona no plano VPS. Use Vanilla, Paper, Purpur ou Fabric.")

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
        had_content = changed and any(
            f.name.endswith((".jar", ".jar.disabled"))
            for kind in ("mods", "plugins") for f in (SERVERS_DIR / sid / kind).glob("*") if f.is_file()
        )
        server.update(software=sw, version=version, ramMb=ram)
        db_save(servers)
    return 200, {**view(server), "backup": backup, "warnContent": had_content}


def _content_server(sid, mutate=False):
    server = find_server(sid)
    if server["plan"] != "free":
        raise ApiError(501, "Mods e plugins na VPS ainda não estão disponíveis. Use o plano Grátis.")
    if not content.kind_of(server):
        raise ApiError(400, "O Vanilla não aceita mods nem plugins. Em Software, escolha Paper, Purpur ou Fabric.")
    if mutate:
        _require_offline(sid, "mexer em mods e plugins")
    return server


def api_content_list(query, body, sid):
    server = find_server(sid)
    kind = content.kind_of(server)
    if server["plan"] != "free" or not kind:
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


def api_delete(query, body, sid):
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
    return 200, {"info": info, "needJava": required_java(server["version"])}


# ---------------------------------------------------------------- Jogadores, arquivos, mundos e backups

def _manage_server(sid):
    server = find_server(sid)
    if server["plan"] != "free":
        raise ApiError(501, "Esta aba ainda não está disponível na VPS. Use o plano Grátis.")
    return server


def _state(sid):
    rt = RUNTIMES.get(sid)
    return rt.state if rt else "offline"


def _first(query, key):
    return query.get(key, [""])[0]


def api_players(query, body, sid):
    if find_server(sid)["plan"] != "free":
        return 200, {"supported": False}
    rt = RUNTIMES.get(sid)
    snap = rt.snapshot() if rt else {"state": "offline", "players": []}
    return 200, {"supported": True, "state": snap["state"], "online": snap["players"], **manage.get_players(sid)}


def api_players_action(query, body, sid):
    _manage_server(sid)
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
    manage.offline_change(sid, action, name, reason)
    return 200, {"mode": "file"}


def api_files_list(query, body, sid):
    if find_server(sid)["plan"] != "free":
        return 200, {"supported": False}
    return 200, {"supported": True, **manage.list_dir(sid, _first(query, "path"))}


def api_files_read(query, body, sid):
    _manage_server(sid)
    return 200, manage.read_text(sid, _first(query, "path"))


def api_files_write(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "editar arquivos")
    manage.write_text(sid, str(body.get("path", "")), body.get("content", ""))
    return 200, {"ok": True}


def api_files_upload(query, data, sid):
    _manage_server(sid)
    _require_offline(sid, "enviar arquivos")
    manage.save_upload(sid, _first(query, "dir"), _first(query, "name"), data)
    return 200, {"ok": True}


def api_files_mkdir(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "criar pastas")
    manage.make_dir(sid, str(body.get("path", "")), str(body.get("name", "")))
    return 200, {"ok": True}


def api_files_delete(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "apagar arquivos")
    manage.delete_path(sid, str(body.get("path", "")))
    return 200, {"ok": True}


def api_files_rename(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "renomear arquivos")
    manage.rename_path(sid, str(body.get("path", "")), str(body.get("name", "")))
    return 200, {"ok": True}


def api_files_download(query, body, sid):
    _manage_server(sid)
    target = manage.safe_path(sid, _first(query, "path"))
    if target.is_dir():
        label = target.name or "servidor"
        return 200, Raw(path=manage.tmp_zip([(target, label)], label), ctype="application/zip",
                        filename=label + ".zip", delete_after=True)
    return 200, Raw(path=target, filename=target.name)


def api_worlds(query, body, sid):
    if find_server(sid)["plan"] != "free":
        return 200, {"supported": False}
    return 200, {"supported": True, "worlds": manage.list_worlds(sid)}


def api_world_use(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "trocar de mundo")
    manage.use_world(sid, str(body.get("name", "")))
    return 200, {"ok": True}


def api_world_create(query, body, sid):
    server = _manage_server(sid)
    _require_offline(sid, "criar um mundo")
    manage.create_world(sid, str(body.get("name", "")), body.get("seed", ""), str(body.get("type", "normal")),
                        vkey(server["version"]))
    return 200, {"ok": True}


def api_world_delete(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "apagar um mundo")
    manage.delete_world(sid, str(body.get("name", "")))
    return 200, {"ok": True}


def api_world_download(query, body, sid):
    _manage_server(sid)
    name = _first(query, "name")
    return 200, Raw(path=manage.world_zip(sid, name), ctype="application/zip", filename=f"{name}.zip", delete_after=True)


def api_world_upload(query, zip_path, sid):
    _manage_server(sid)
    _require_offline(sid, "enviar um mundo")
    manage.upload_world(sid, _first(query, "name"), zip_path)
    return 200, {"ok": True}


def api_backups(query, body, sid):
    if find_server(sid)["plan"] != "free":
        return 200, {"supported": False}
    return 200, {"supported": True, "backups": manage.list_backups(sid), "state": _state(sid)}


def api_backup_create(query, body, sid):
    _manage_server(sid)
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
            name = manage.create_backup(sid, "manual")
        finally:
            try:
                rt.command("save-on")
            except ApiError:
                pass
    else:
        name = manage.create_backup(sid, "manual")
    if not name:
        raise ApiError(400, "Ainda não há mundo para guardar: ligue o servidor uma vez.")
    return 200, {"name": name}


def api_backup_restore(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "restaurar um backup")
    return 200, manage.restore_backup(sid, str(body.get("name", "")))


def api_backup_delete(query, body, sid):
    _manage_server(sid)
    manage.delete_backup(sid, str(body.get("name", "")))
    return 200, {"ok": True}


def api_backup_download(query, body, sid):
    _manage_server(sid)
    f = manage.backup_path(sid, _first(query, "name"))
    return 200, Raw(path=f, ctype="application/zip", filename=f.name)


def api_settings(query, body, sid):
    server = find_server(sid)
    if server["plan"] != "free":
        return 200, {"supported": False}
    return 200, {"supported": True, "state": _state(sid), **options.get_settings(server)}


def api_settings_properties(query, body, sid):
    _manage_server(sid)
    _require_offline(sid, "editar as configurações do jogo")
    options.set_property_changes(sid, body.get("changes"))
    return 200, {"ok": True}


def api_settings_gamerules(query, body, sid):
    """Guarda as regras no painel (valem a cada início) e, com o servidor ligado, aplica na hora."""
    with DB_LOCK:
        servers = db_load()
        server = _pick(servers, sid)
        if server["plan"] != "free":
            raise ApiError(501, "Esta aba ainda não está disponível na VPS. Use o plano Grátis.")
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
PROTECTED_PAGES = {"servers.html", "create.html", "panel.html"}


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
        for name in ("blockhost_ed25519", "blockhost_ed25519.pub", "known_hosts"):
            if (old / name).exists():
                shutil.move(str(old / name), str(dest / name))


auth.on_first_user = _claim_legacy


def _require_setup():
    if not auth.setup_allowed(current_user()):
        raise ApiError(403, "Só o administrador pode configurar os logins.")


def api_auth_providers(query, body):
    return 200, {"providers": auth.list_providers(), "canSetup": auth.setup_allowed(current_user())}


def api_auth_me(query, body):
    user = current_user()
    return 200, {"user": auth.public_user(user) if user else None}


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


ID = r"([a-z0-9]{1,32})"
ROUTES = [
    ("GET", r"^/api/auth/providers$", api_auth_providers),
    ("GET", r"^/api/auth/me$", api_auth_me),
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
    ("POST", rf"^/api/servers/{ID}/backups/create$", api_backup_create),
    ("POST", rf"^/api/servers/{ID}/backups/restore$", api_backup_restore),
    ("POST", rf"^/api/servers/{ID}/backups/delete$", api_backup_delete),
    ("GET", rf"^/api/servers/{ID}/backups/download$", api_backup_download),
    ("GET", rf"^/api/servers/{ID}/settings$", api_settings),
    ("POST", rf"^/api/servers/{ID}/settings/properties$", api_settings_properties),
    ("POST", rf"^/api/servers/{ID}/settings/gamerules$", api_settings_gamerules),
]
PUBLIC = {api_auth_providers, api_auth_me, api_auth_logout, api_auth_config, api_auth_config_save}  # não exigem login
RAW_UPLOAD = {api_content_upload, api_icon_set, api_files_upload}  # recebem o arquivo cru (octet-stream) em vez de JSON
STREAM_UPLOAD = {api_world_upload}  # arquivos grandes: vão direto para o disco, sem ocupar a memória
MAX_UPLOAD = 64 * 1024 * 1024
MAX_STREAM = 2 * 1024 * 1024 * 1024

# Só estes arquivos do site são entregues (a pasta data/ e o .git nunca saem daqui).
STATIC_RE = re.compile(r"^(?:[a-z]+\.html|css/[\w.-]+\.css|js/[\w.-]+\.js|assets/[\w.-]+\.(?:svg|png))$")
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "BlockHost"

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
        m = re.match(r"^/auth/(login|callback)/([a-z]{1,20})$", parsed.path)
        if not m:
            raise ApiError(404, "Página não encontrada.")
        action, pid = m.groups()
        query = parse_qs(parsed.query)
        try:
            if action == "login":
                url, bind = auth.begin(pid, BASE_URL, _first(query, "next"))
                return self._redirect(url, [auth.bind_cookie(bind)])
            if _first(query, "error"):
                raise auth.LoginFailed("denied")
            _, token, next_url = auth.finish(pid, _first(query, "code"), _first(query, "state"),
                                             auth.bind_from_cookie(self.headers.get("Cookie")), BASE_URL)
            return self._redirect(BASE_URL + next_url, [auth.session_cookie(token), auth.CLEAR_BIND])
        except auth.LoginFailed as e:
            return self._redirect(f"{BASE_URL}/login.html?error={e.code}", [auth.CLEAR_BIND])

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
            raise ApiError(401, "Faça login para continuar.")

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
            if length > (MAX_STREAM if stream else MAX_UPLOAD if raw else 65536):
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

    def _static(self, path, query=""):
        rel = unquote(path).lstrip("/") or "index.html"
        file = ROOT / rel
        if not STATIC_RE.match(rel) or not file.is_file():
            raise ApiError(404, "Página não encontrada.")
        if rel in PROTECTED_PAGES and not current_user():  # sem conta, essas páginas mandam para o login
            return self._redirect("/login.html?next=" + quote("/" + rel + ("?" + query if query else "")))
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, file.read_bytes(), ctype)

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")


def main():
    DATA.mkdir(exist_ok=True)
    manage.cleanup_tmp()
    threading.Thread(target=monitor, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BlockHost rodando em http://127.0.0.1:{PORT}  (Ctrl+C para parar)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Desligando os servidores de Minecraft…")
        shutdown_all()


if __name__ == "__main__":
    main()
