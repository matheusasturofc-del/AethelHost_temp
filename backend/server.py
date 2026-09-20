#!/usr/bin/env python3
"""BlockHost — backend local.

Serve o site e a API, e liga/desliga servidores de Minecraft de verdade neste PC.
Usa só a biblioteca padrão do Python (nada para instalar).

Rodar:  python backend/server.py   ->   http://127.0.0.1:8080
"""
import glob
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SERVERS_DIR = DATA / "servers"
JARS_DIR = DATA / "jars"
DB_FILE = DATA / "servers.json"

HOST = "127.0.0.1"  # só este PC acessa a API
PORT = 8080
DOMAIN = "blockhost.net"  # nome provisório
FIRST_MC_PORT = 25565
MAX_PLAYERS = 20
IDLE_LIMIT = 5 * 3600  # plano grátis fecha após 5h sem jogadores
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

VERSIONS = [
    "1.21.4", "1.21.1", "1.20.6", "1.20.4", "1.20.1",
    "1.19.4", "1.18.2", "1.16.5", "1.12.2", "1.8.9",
]
MOJANG_HOSTS = {
    "launchermeta.mojang.com", "piston-meta.mojang.com",
    "piston-data.mojang.com", "launcher.mojang.com",
}
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

IP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$")
HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$", re.I)
JOIN_RE = re.compile(r"\]: (\w{1,16}) joined the game$")
LEAVE_RE = re.compile(r"\]: (\w{1,16}) left the game$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


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


def required_java(version):
    parts = tuple(int(p) for p in version.split("."))
    parts += (0,) * (3 - len(parts))
    if parts >= (1, 20, 5):
        return 21
    if parts >= (1, 18, 0):
        return 17
    return 8


def pick_java(need):
    """O menor Java instalado que atende à versão exigida."""
    for major, exe in find_javas():
        if major >= need:
            return major, exe
    return None


# ---------------------------------------------------------------- Mojang

def fetch(url, timeout=30):
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in MOJANG_HOSTS:
        raise RuntimeError(f"Endereço não permitido: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "BlockHost/0.1"})
    return urllib.request.urlopen(req, timeout=timeout)


def ensure_jar(version, log):
    """Devolve o server.jar oficial da versão, baixando da Mojang se ainda não tiver."""
    jar = JARS_DIR / f"{version}.jar"
    if jar.exists():
        return jar
    log(f"Buscando a versão {version} na Mojang…")
    with fetch(MANIFEST_URL) as r:
        manifest = json.load(r)
    entry = next((v for v in manifest["versions"] if v["id"] == version), None)
    if not entry:
        raise RuntimeError(f"Versão {version} não encontrada na Mojang.")
    with fetch(entry["url"]) as r:
        info = json.load(r)["downloads"]["server"]
    log(f"Baixando o server.jar ({info['size'] // 1_000_000} MB). Só acontece na primeira vez.")
    JARS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = jar.with_suffix(".part")
    sha = hashlib.sha1()
    with fetch(info["url"], timeout=60) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
            sha.update(chunk)
    if sha.hexdigest() != info["sha1"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado está corrompido (SHA1 não confere).")
    os.replace(tmp, jar)
    log("Download concluído e verificado.")
    return jar


# ---------------------------------------------------------------- Banco (arquivo JSON)

DB_LOCK = threading.RLock()


def db_load():
    try:
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
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
        threading.Thread(target=self._run, args=(server,), daemon=True).start()

    def _run(self, server):
        try:
            need = required_java(server["version"])
            java = pick_java(need)
            if not java:
                raise RuntimeError(f"Este PC não tem Java {need} ou superior instalado.")
            jar = ensure_jar(server["version"], self.log)
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
            self.log(f"Iniciando com Java {java[0]}…")
            self.proc = subprocess.Popen(
                [java[1], "-Xms512M", "-Xmx1G", "-jar", str(jar), "nogui"],
                cwd=sdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW,
            )
        except Exception as e:
            self.log(f"Erro: {e}", "error")
            with self.lock:
                self.state = "offline"
            return
        self._watch()

    def _watch(self):
        for raw in self.proc.stdout:
            line = raw.rstrip()
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


RUNTIMES = {}
RUNTIMES_LOCK = threading.Lock()


def runtime(sid):
    with RUNTIMES_LOCK:
        return RUNTIMES.setdefault(sid, Runtime(sid))


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
    for rt in list(RUNTIMES.values()):
        try:
            rt.stop()
        except ApiError:
            pass
    deadline = time.time() + 60
    while time.time() < deadline and any(rt.state != "offline" for rt in RUNTIMES.values()):
        time.sleep(0.5)


# ---------------------------------------------------------------- API

def view(server):
    rt = RUNTIMES.get(server["id"])
    return {
        **server,
        "publicName": f"{server['ip']}.{DOMAIN}",
        "address": f"localhost:{server['port']}",
        "maxPlayers": MAX_PLAYERS,
        "runtime": rt.snapshot() if rt else {"state": "offline", "players": [], "idleLeft": None},
    }


def clean_text(body, key, minimum, maximum, label):
    value = str(body.get(key, "")).strip()
    if not minimum <= len(value) <= maximum:
        raise ApiError(400, f"{label}: use de {minimum} a {maximum} caracteres." if minimum else
                       f"{label}: no máximo {maximum} caracteres.")
    return value


def api_meta(query, body):
    versions = [
        {"version": v, "java": required_java(v), "available": pick_java(required_java(v)) is not None}
        for v in VERSIONS
    ]
    return 200, {"domain": DOMAIN, "versions": versions, "maxPlayers": MAX_PLAYERS}


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
    if not IP_RE.match(ip):
        raise ApiError(400, "Endereço inválido: use de 3 a 24 caracteres (letras minúsculas, números e hífen).")
    if body.get("eula") is not True:
        raise ApiError(400, "É preciso aceitar o EULA do Minecraft.")
    if version not in VERSIONS:
        raise ApiError(400, "Versão inválida.")
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
        used = {s["port"] for s in servers}
        port = FIRST_MC_PORT
        while port in used or not port_free(port):
            port += 1
        server = {
            "id": uuid.uuid4().hex[:12], "name": name, "subtitle": subtitle, "ip": ip,
            "edition": "java", "version": version, "plan": plan, "port": port, "eula": True,
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
    if server["plan"] == "vps":
        raise ApiError(501, "A conexão com a VPS ainda não foi implementada.")
    need = required_java(server["version"])
    if not pick_java(need):
        raise ApiError(400, f"Este PC não tem Java {need} instalado (a versão {server['version']} precisa dele).")
    runtime(sid).start(server)
    return 202, view(server)


def api_stop(query, body, sid):
    find_server(sid)
    runtime(sid).stop()
    return 202, view(find_server(sid))


def api_command(query, body, sid):
    find_server(sid)
    runtime(sid).command(str(body.get("command", "")))
    return 200, {"ok": True}


def api_console(query, body, sid):
    find_server(sid)
    try:
        since = max(0, int(query.get("since", ["0"])[0]))
    except ValueError:
        since = 0
    lines, nxt = runtime(sid).read(since)
    return 200, {"lines": lines, "next": nxt}


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
]

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
            self._json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Erro interno do servidor."})

    def _api(self, method, parsed):
        body = {}
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin and origin not in ORIGINS:
                raise ApiError(403, "Origem não permitida.")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ApiError(415, "Envie JSON (Content-Type: application/json).")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ApiError(413, "Requisição grande demais.")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ApiError(400, "JSON inválido.")
            if not isinstance(body, dict):
                raise ApiError(400, "JSON inválido.")
        for m, pattern, fn in ROUTES:
            match = re.match(pattern, parsed.path)
            if m == method and match:
                status, data = fn(parse_qs(parsed.query), body, *match.groups())
                return self._json(status, data)
        raise ApiError(404, "Rota não encontrada.")

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
