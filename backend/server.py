#!/usr/bin/env python3
"""BlockHost — backend local.

Serve o site e a API, e liga/desliga servidores de Minecraft de verdade neste PC.
Usa só a biblioteca padrão do Python (nada para instalar).

Rodar:  python backend/server.py   ->   http://127.0.0.1:8080
"""
import glob
import json
import mimetypes
import os
import re
import shlex
import shutil
import socket
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
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # deixa importar config, net, software, content

import content  # noqa: E402
from config import BACKUPS_DIR, DATA, JARS_DIR, ROOT, SERVERS_DIR  # noqa: E402
from software import (SOFTWARE, ensure_jar, mc_releases, prefetch_java, ram_options,  # noqa: E402
                      required_java, software_versions, spec_for, system_ram_mb, vkey)

DB_FILE = DATA / "servers.json"

HOST = "127.0.0.1"  # só este PC acessa a API
PORT = 8080
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
    """Servidores criados antes de existir Software/RAM ganham os valores padrão."""
    server.setdefault("software", "vanilla")
    server.setdefault("ramMb", DEFAULT_RAM_MB)
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


def find_server(sid):
    server = next((s for s in db_load() if s["id"] == sid), None)
    if not server:
        raise ApiError(404, "Servidor não encontrado.")
    return server


# ---------------------------------------------------------------- Processo do Minecraft

def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def prop_escape(value):
    s = str(value).replace("\\", "\\\\").replace("\r", " ").replace("\n", " ")
    b = s.encode("utf-16-le")
    units = [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]
    return "".join(chr(u) if u < 128 else f"\\u{u:04x}" for u in units)


def set_properties(path, values):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            out.append(f"{key}={prop_escape(values[key])}")
            done.add(key)
        else:
            out.append(line)
    out += [f"{k}={prop_escape(v)}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


class Runtime:
    """Estado de um servidor ligado (ou desligado) neste PC."""

    def __init__(self, sid):
        self.sid = sid
        self.plan = "free"
        self.state = "offline"  # offline | starting | online | stopping
        self.proc = None
        self.players = set()
        self.idle_since = None
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
            set_properties(sdir / "server.properties", {
                "server-port": server["port"],
                "motd": server["subtitle"] or server["name"],
                "max-players": MAX_PLAYERS,
            })
            ram = server["ramMb"]
            self.log(f"Iniciando {SOFTWARE[server['software']]['label']} {server['version']} com Java {java[0]} e {ram} MB de RAM…")
            self.proc = subprocess.Popen(
                [java[1], f"-Xms{min(512, ram)}M", f"-Xmx{ram}M",
                 "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",  # acentos no console
                 "-jar", str(jar), "nogui"],
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

    def command(self, text):
        text = text.replace("\r", " ").replace("\n", " ").strip()[:200]
        with self.lock:
            if not text:
                raise ApiError(400, "Comando vazio.")
            if self.state != "online" or not self.proc:
                raise ApiError(409, "O servidor precisa estar online para receber comandos.")
            self.log(f"> {text}", "cmd")
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()


# ---------------------------------------------------------------- VPS (SSH)

KEY_FILE = DATA / "keys" / "blockhost_ed25519"
KNOWN_HOSTS = DATA / "keys" / "known_hosts"


def ensure_key():
    """Chave SSH do BlockHost. A privada nunca sai deste PC: você só copia a pública."""
    pub = Path(str(KEY_FILE) + ".pub")
    if not KEY_FILE.exists() or not pub.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.unlink(missing_ok=True)
        pub.unlink(missing_ok=True)
        try:
            r = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "blockhost", "-f", str(KEY_FILE)],
                capture_output=True, timeout=30, creationflags=NO_WINDOW,
            )
        except FileNotFoundError:
            raise ApiError(500, "O ssh-keygen não foi encontrado neste PC.")
        if r.returncode != 0:
            raise ApiError(500, "Não consegui gerar a chave SSH: " + r.stderr.decode("utf-8", "replace").strip()[:200])
        if os.name == "nt":  # o ssh do Windows recusa chave que outros usuários possam ler
            subprocess.run(
                ["icacls", str(KEY_FILE), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
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
    return [
        "ssh", "-i", str(KEY_FILE), "-p", str(vps["port"]),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",  # confia na 1ª conexão e avisa se a VPS mudar depois
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS.as_posix()}",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        f"{vps['user']}@{vps['host']}", remote,
    ]


def ssh_script(vps, script, timeout=60):
    """Roda um script bash na VPS. Devolve (código, saída); RuntimeError se não conseguir conectar."""
    ensure_key()
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
    ensure_key()
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
    motd = prop_escape(server["subtitle"] or server["name"])
    head = (
        "set -e\n"
        f"ID={q(server['id'])}\nNEED={need}\nPORT={server['port']}\nMAXP={MAX_PLAYERS}\n"
        f"JAR_URL={q(jar['url'])}\nJAR_NAME={q(jar['name'])}\n"
        f"JAR_ALGO={q(jar['hash'][0] if jar['hash'] else '')}\nJAR_HASH={q(jar['hash'][1] if jar['hash'] else '')}\n"
        f"MOTD={q(motd)}\n"
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
        self.vps = server["vps"]
        self.session_up = self.gone = self._adopted = False
        try:
            need = required_java(server["version"])
            self.log(f"Conectando à VPS {self.vps['host']}…")
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

    def command(self, text):
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

def view(server):
    rt = RUNTIMES.get(server["id"])
    return {
        **server,
        "publicName": f"{server['ip']}.{DOMAIN}",
        "address": f"{server['vps']['host']}:{server['port']}" if server["plan"] == "vps" else f"localhost:{server['port']}",
        "maxPlayers": MAX_PLAYERS,
        "softwareLabel": SOFTWARE[server["software"]]["label"],
        "contentKind": content.kind_of(server),
        "runtime": rt.snapshot() if rt else {"state": "offline", "players": [], "idleLeft": None},
    }


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
    return 200, [view(s) for s in db_load()]


def api_create(query, body):
    name = clean_text(body, "name", 3, 30, "Nome")
    subtitle = clean_text(body, "subtitle", 0, 60, "Subtítulo")
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
            "plan": plan, "port": port, "eula": True,
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
    subtitle = clean_text(body, "subtitle", 0, 60, "Subtítulo")
    with DB_LOCK:
        servers = db_load()
        server = next((s for s in servers if s["id"] == sid), None)
        if not server:
            raise ApiError(404, "Servidor não encontrado.")
        server.update(name=name, subtitle=subtitle)
        db_save(servers)
    return 200, view(server)


def _require_offline(sid, what):
    rt = RUNTIMES.get(sid)
    if rt and rt.state != "offline":
        raise ApiError(409, f"Desligue o servidor antes de {what}.")


def backup_world(sid, tag):
    """Guarda um .zip dos mundos (world, world_nether, world_the_end) em data/backups/. None se não há mundo."""
    folder = SERVERS_DIR / sid
    worlds = [p for p in folder.glob("world*") if p.is_dir()]
    if not worlds:
        return None
    dest_dir = BACKUPS_DIR / sid
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
    name = f"{stamp}-{tag}.zip"
    while (dest_dir / name).exists():  # duas trocas no mesmo segundo não podem sobrescrever o backup
        n += 1
        name = f"{stamp}-{tag}-{n}.zip"
    with zipfile.ZipFile(dest_dir / name, "w", zipfile.ZIP_DEFLATED) as z:
        for w in worlds:
            for f in w.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(folder))
    return name


def api_software_set(query, body, sid):
    """Troca software, versão e/ou RAM. Só com o servidor desligado; guarda um backup do mundo antes."""
    with DB_LOCK:
        servers = db_load()
        server = next((s for s in servers if s["id"] == sid), None)
        if not server:
            raise ApiError(404, "Servidor não encontrado.")
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

        changed = (sw, version) != (server["software"], server["version"])
        backup = None
        if changed and server["plan"] == "free":
            need = required_java(version)
            if not pick_java(need):
                raise ApiError(400, f"A versão {version} precisa do Java {need}, que não está instalado neste PC.")
            world_exists = any(p.is_dir() for p in (SERVERS_DIR / sid).glob("world*"))
            if world_exists and vkey(version) < vkey(server["version"]) and body.get("confirm") is not True:
                raise ApiError(409, "Voltar para uma versão mais antiga pode corromper o mundo.",
                               {"needConfirm": True})
            backup = backup_world(sid, "antes-de-mudar-software")
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


def api_delete(query, body, sid):
    with DB_LOCK:
        servers = db_load()
        if not any(s["id"] == sid for s in servers):
            raise ApiError(404, "Servidor não encontrado.")
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
    return 200, {"publicKey": ensure_key()}


def api_vps_check(query, body, sid):
    server = find_server(sid)
    if server["plan"] != "vps":
        raise ApiError(400, "Este servidor não usa VPS.")
    try:
        _, out = ssh_script(server["vps"], CHECK_SCRIPT, 40)
    except RuntimeError as e:
        raise ApiError(502, str(e))
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return 200, {"info": info, "needJava": required_java(server["version"])}


ID = r"([a-z0-9]{1,32})"
ROUTES = [
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
]
RAW_UPLOAD = {api_content_upload}  # recebem o arquivo cru (octet-stream) em vez de JSON
MAX_UPLOAD = 64 * 1024 * 1024

# Só estes arquivos do site são entregues (a pasta data/ e o .git nunca saem daqui).
STATIC_RE = re.compile(r"^(?:[a-z]+\.html|css/[\w.-]+\.css|js/[\w.-]+\.js)$")
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "BlockHost"

    def log_message(self, *args):  # o polling do painel encheria o terminal
        pass

    def _send(self, status, payload, ctype):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _handle(self, method):
        try:
            # Barra outros sites que tentem usar esta API pelo seu navegador.
            if self.headers.get("Host", "").lower() not in {o.split("//")[1] for o in ORIGINS}:
                raise ApiError(403, "Host não permitido.")
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._api(method, parsed)
            elif method == "GET":
                self._static(parsed.path)
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

        body = {}
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin and origin not in ORIGINS:
                raise ApiError(403, "Origem não permitida.")
            raw = fn in RAW_UPLOAD
            expected = "application/octet-stream" if raw else "application/json"
            if self.headers.get("Content-Type", "").split(";")[0].strip() != expected:
                raise ApiError(415, f"Envie o conteúdo como {expected}.")
            length = int(self.headers.get("Content-Length") or 0)
            if length > (MAX_UPLOAD if raw else 65536):
                raise ApiError(413, "Requisição grande demais.")
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
        status, data = fn(parse_qs(parsed.query), body, *groups)
        self._json(status, data)

    def _static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        file = ROOT / rel
        if not STATIC_RE.match(rel) or not file.is_file():
            raise ApiError(404, "Página não encontrada.")
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
