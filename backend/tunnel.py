"""Endereço público para os servidores deste PC, pelo playit.gg: os amigos entram sem você abrir porta no roteador.

Como funciona: o administrador liga o AethelHost a uma conta do playit.gg (uma vez, aprovando num link). O AethelHost
baixa o programa oficial do playit (versão fixa, com hash conferido), cria um túnel "Minecraft Java" por servidor
e mantém o programa rodando enquanto houver algum servidor com endereço público ligado.
"""
import json
import os
import platform
import secrets
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import net
from config import DATA

API = "https://api.playit.gg"
VERSION = "0.17.1"
_RELEASE = f"https://github.com/playit-cloud/playit-agent/releases/download/v{VERSION}/"
# Nome do arquivo e SHA-256 de cada plataforma (da página oficial da versão v0.17.1 no GitHub).
ASSETS = {
    ("windows", "amd64"): ("playit-windows-x86_64-signed.exe", "9b00d6ff7d37d1052e5ae097e1348e11deae8617cd7a8ba39d1777f2006316a3"),
    ("linux", "amd64"): ("playit-linux-amd64", "e78d463d93aa1e3ec36a06ded5a1f4fe879905fdceb865df8f4cef6124f8a555"),
    ("linux", "arm64"): ("playit-linux-aarch64", "cd3fa1cedac40a71d80a120e6353e08836308840340b58e659e8f25d00601f66"),
}
STATE_FILE = DATA / "playit.json"
TOOLS = DATA / "tools" / "playit"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
IDLE_STOP = 60  # segundos sem nenhum servidor público ligado até o programa do playit ser encerrado
CLAIM_TTL = 15 * 60

_LOCK = threading.RLock()
_WAKE = threading.Event()
_wants = {}     # sid -> porta local dos servidores que devem ter endereço público agora
_info = {}      # sid -> {"state": connecting|ready|error, "address", "direct", "error"}
_claim = None   # ligação em andamento: {"code", "state", "error", "since"}
_agent = {"proc": None, "lines": [], "error": None, "installing": False}
_worker_started = False

ACCOUNT_HELP = {
    "guest": "Termine o cadastro no playit.gg (adicione e-mail e senha em playit.gg/account) para poder criar túneis.",
    "email-not-verified": "Confirme o e-mail da sua conta do playit.gg (o playit enviou uma mensagem) e tente de novo.",
    "banned": "A conta do playit.gg está bloqueada.",
    "account-delete-scheduled": "A conta do playit.gg está marcada para exclusão.",
    "agent-disabled": "Este agente foi desativado no playit.gg (playit.gg/account/agents).",
    "agent-over-limit": "A conta do playit.gg passou do limite de agentes. Apague um agente antigo em playit.gg/account/agents.",
}
CREATE_HELP = {
    "RequiresVerifiedAccount": "Confirme o e-mail da sua conta do playit.gg e tente de novo.",
    "RequiresPlayitPremium": "O playit.gg pede o plano pago para este túnel (o limite do plano grátis foi atingido).",
    "AgentVersionTooOld": "O programa do playit está desatualizado.",
    "AgentNotFound": "O playit não reconhece mais este agente. Desligue e ligue de novo a conta.",
}


class TunnelError(Exception):
    code = ""   # o motivo que o playit devolveu (ex.: RequiresVerifiedAccount)
    kind = ""   # "fail" (pedido recusado), "error" (problema do servidor/chave) ou "" (nosso)


# ---------------------------------------------------------------- estado em disco

def _load():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, STATE_FILE)


def secret():
    return _load().get("secret")


# ---------------------------------------------------------------- API do playit

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a chave secreta nunca vai para outro endereço


_http = urllib.request.build_opener(_NoRedirect)


def _call(path, body, key=None, timeout=20):
    """POST em api.playit.gg. Devolve `data` se deu certo; senão levanta TunnelError com o motivo."""
    headers = {"Content-Type": "application/json", "User-Agent": net.USER_AGENT}
    if key:
        headers["Authorization"] = "Agent-Key " + key
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with _http.open(req, timeout=timeout) as r:
            raw = r.read(1 << 20)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise TunnelError("O playit.gg pediu para esperar um pouco. Tente de novo em instantes.") from None
        raw = e.read(1 << 20)
    except (urllib.error.URLError, OSError):
        raise TunnelError("Não consegui falar com o playit.gg. Confira a internet.") from None
    try:
        reply = json.loads(raw)
    except ValueError:
        raise TunnelError("Resposta inesperada do playit.gg.") from None
    if reply.get("status") == "success":
        return reply.get("data")
    detail = reply.get("data")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("type")
    err = TunnelError(str(detail or reply.get("status")))
    err.code, err.kind = str(detail), str(reply.get("status"))
    raise err


# ---------------------------------------------------------------- ligar a conta do playit (claim)

def claim_url(code):
    return f"https://playit.gg/claim/{code}"


def begin_claim():
    """Começa a ligação: devolve o link que o administrador abre para aprovar. O resto acontece em segundo plano."""
    global _claim
    with _LOCK:
        if _claim and _claim["state"] == "waiting" and time.time() - _claim["since"] < CLAIM_TTL:
            return {"state": "waiting", "url": claim_url(_claim["code"])}
        _claim = {"code": secrets.token_hex(5), "state": "waiting", "error": None, "since": time.time()}
        mine = _claim
    threading.Thread(target=_claim_worker, args=(mine,), daemon=True).start()
    return {"state": "waiting", "url": claim_url(mine["code"])}


def _claim_worker(mine):
    code = mine["code"]
    try:
        while time.time() - mine["since"] < CLAIM_TTL:
            step = _call("/claim/setup", {"code": code, "agent_type": "self-managed", "version": f"playit {VERSION}"})
            if step == "UserAccepted":
                break
            if step == "UserRejected":
                raise TunnelError("A ligação foi recusada no playit.gg.")
            time.sleep(1.5)
        else:
            raise TunnelError("O link expirou. Gere outro.")
        for _ in range(60):  # a chave leva um instante para ficar disponível depois da aprovação
            try:
                data = _call("/claim/exchange", {"code": code})
                break
            except TunnelError as e:
                if e.kind != "fail":  # "fail" = ainda não liberou a chave; qualquer outra coisa é erro de verdade
                    raise
                time.sleep(1)
        else:
            raise TunnelError("Não consegui buscar a chave depois da aprovação.")
        key = data["secret_key"] if isinstance(data, dict) else str(data)
        with _LOCK:
            state = _load()
            state["secret"] = key
            state.pop("agent_id", None)
            _save(state)
            mine["state"] = "linked"
        _WAKE.set()
    except Exception as e:  # noqa: BLE001 - qualquer falha vira uma mensagem para o painel
        with _LOCK:
            mine["state"], mine["error"] = "failed", str(e)


def unlink():
    """Esquece a conta do playit neste PC (os túneis continuam existindo lá; o agente pode ser apagado em playit.gg/account/agents)."""
    global _claim
    with _LOCK:
        _claim = None
        _wants.clear()
        _info.clear()
    _stop_agent()
    STATE_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------- programa do playit

def _platform_key():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "amd64" if machine in ("amd64", "x86_64") else "arm64" if machine in ("arm64", "aarch64") else machine
    return system, arch


def supported():
    return _platform_key() in ASSETS


def _exe_path():
    name, _ = ASSETS[_platform_key()]
    return TOOLS / f"v{VERSION}" / name


def ensure_agent_binary():
    exe = _exe_path()
    if exe.exists():
        return exe
    name, digest = ASSETS[_platform_key()]
    _agent["installing"] = True
    try:
        net.download(_RELEASE + name, exe, hash=("sha256", digest), max_bytes=50_000_000, timeout=120)
        if os.name != "nt":
            exe.chmod(0o755)
    finally:
        _agent["installing"] = False
    return exe


def _pid_file():
    return TOOLS / "agent.pid"


def _kill_stale():
    """Se o AethelHost caiu com o playit rodando, o processo antigo ainda pode estar ativo: encerra só se for mesmo o playit."""
    try:
        pid = int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 timeout=15, creationflags=NO_WINDOW).stdout.lower()
            if "playit" in out:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15, creationflags=NO_WINDOW)
        elif b"playit" in Path(f"/proc/{pid}/cmdline").read_bytes():
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass
    _pid_file().unlink(missing_ok=True)


def _drain(proc):
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _agent["lines"] = (_agent["lines"] + [line])[-40:]


def _start_agent(key):
    with _LOCK:
        proc = _agent["proc"]
        if proc and proc.poll() is None:
            return
    _kill_stale()
    exe = ensure_agent_binary()
    env = dict(os.environ, PLAYIT_SECRET=key)  # pela variável de ambiente: a chave não aparece na lista de processos
    proc = subprocess.Popen([str(exe), "-s", "start"], env=env, cwd=exe.parent, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    _pid_file().write_text(str(proc.pid))
    _agent["lines"] = []
    _agent["proc"] = proc
    threading.Thread(target=_drain, args=(proc,), daemon=True).start()


def _stop_agent():
    proc = _agent["proc"]
    _agent["proc"] = None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _pid_file().unlink(missing_ok=True)


def shutdown():
    _stop_agent()


# ---------------------------------------------------------------- túneis

def tunnel_name(sid):
    return f"aethelhost-{sid}"[:60]


def _legacy_tunnel_name(sid):
    return f"blockhost-{sid}"[:60]  # túneis criados quando o projeto se chamava BlockHost


def _agent_id(key):
    state = _load()
    if state.get("agent_id"):
        return state["agent_id"]
    data = _call("/agents/rundata", {}, key)
    status = data.get("account_status")
    if status in ACCOUNT_HELP:
        raise TunnelError(ACCOUNT_HELP[status])
    with _LOCK:
        state = _load()
        state["agent_id"] = data["agent_id"]
        _save(state)
    return data["agent_id"]


def _find(key, agent_id, sid):
    data = _call("/tunnels/list", {"tunnel_id": None, "agent_id": agent_id}, key)
    names = (tunnel_name(sid), _legacy_tunnel_name(sid))
    return next((t for t in data.get("tunnels", []) if t.get("name") in names), None)


def _local_port(tunnel):
    origin = (tunnel.get("origin") or {})
    return (origin.get("data") or {}).get("local_port")


def _addresses(tunnel):
    """(endereço para digitar no Minecraft, endereço direto com porta). O primeiro usa o registro SRV do playit."""
    alloc = tunnel.get("alloc") or {}
    if alloc.get("status") != "allocated":
        return None
    a = alloc["data"]
    direct = f"{a.get('ip_hostname') or a['assigned_domain']}:{a['port_start']}"
    if a.get("assigned_srv"):
        return a["assigned_domain"], direct
    return f"{a['assigned_domain']}:{a['port_start']}", direct


def _ensure(key, sid, port):
    """Garante que o túnel do servidor existe, aponta para a porta certa e já tem endereço."""
    agent_id = _agent_id(key)
    tunnel = _find(key, agent_id, sid)
    origin = {"type": "agent", "data": {"agent_id": agent_id, "local_ip": "127.0.0.1", "local_port": port}}
    if tunnel is None:
        try:
            _call("/tunnels/create", {"name": tunnel_name(sid), "tunnel_type": "minecraft-java", "port_type": "tcp",
                                      "port_count": 1, "origin": origin, "enabled": True, "alloc": None,
                                      "firewall_id": None, "proxy_protocol": None}, key)
        except TunnelError as e:
            raise TunnelError(CREATE_HELP.get(e.code, f"O playit não criou o túnel ({e}).")) from None
    elif _local_port(tunnel) != port or not tunnel.get("active", True):
        _call("/tunnels/update", {"tunnel_id": tunnel["id"], "local_ip": "127.0.0.1", "local_port": port,
                                  "agent_id": None, "enabled": True}, key)
    for _ in range(30):  # o endereço leva alguns segundos para ser reservado
        tunnel = _find(key, agent_id, sid)
        if tunnel and (found := _addresses(tunnel)):
            return found
        time.sleep(1)
    raise TunnelError("O playit ainda não liberou o endereço. Tente de novo em instantes.")


def forget(sid):
    """Servidor apagado: some da lista de desejos e o túnel é apagado no playit (se a conta estiver ligada)."""
    unwant(sid)
    key = secret()
    if not key:
        return

    def work():
        try:
            tunnel = _find(key, _agent_id(key), sid)
            if tunnel:
                _call("/tunnels/delete", {"tunnel_id": tunnel["id"]}, key)
        except Exception:  # noqa: BLE001 - apagar o servidor não pode falhar por causa do playit
            pass
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- quem quer endereço público

def want(sid, port):
    with _LOCK:
        if not secret():
            _info[sid] = {"state": "error", "error": "Ligue o AethelHost ao playit.gg primeiro (botão na aba Servidor)."}
            return
        changed = _wants.get(sid) != port
        _wants[sid] = port
        if changed or _info.get(sid, {}).get("state") != "ready":
            _info[sid] = {"state": "connecting"}
    _ensure_worker()
    _WAKE.set()


def unwant(sid):
    with _LOCK:
        _wants.pop(sid, None)
        _info.pop(sid, None)
    _WAKE.set()


def info(sid):
    with _LOCK:
        return dict(_info.get(sid) or {"state": "off"})


def _ensure_worker():
    global _worker_started
    with _LOCK:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, daemon=True).start()


def _worker():
    idle_since = None
    retry_at = {}
    while True:
        _WAKE.wait(5)
        _WAKE.clear()
        try:
            key = secret()
            with _LOCK:
                todo = dict(_wants)
            if not key or not todo:
                if _agent["proc"] and idle_since is None:
                    idle_since = time.time()
                if _agent["proc"] and time.time() - idle_since >= IDLE_STOP:
                    _stop_agent()
                    idle_since = None
                continue
            idle_since = None
            proc = _agent["proc"]
            if not proc or proc.poll() is not None:
                try:
                    _start_agent(key)
                    _agent["error"] = None
                except Exception as e:  # noqa: BLE001
                    _agent["error"] = f"Não consegui preparar o programa do playit: {e}"
            for sid, port in todo.items():
                with _LOCK:
                    if _info.get(sid, {}).get("state") == "ready" or time.time() < retry_at.get(sid, 0):
                        continue
                try:
                    address, direct = _ensure(key, sid, port)
                    result = {"state": "ready", "address": address, "direct": direct}
                    retry_at.pop(sid, None)
                except TunnelError as e:
                    result = {"state": "error", "error": str(e)}
                    retry_at[sid] = time.time() + 20
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    result = {"state": "error", "error": f"Erro inesperado: {e}"}
                    retry_at[sid] = time.time() + 20
                with _LOCK:
                    if sid in _wants:  # ainda quer? (pode ter desligado enquanto esperávamos)
                        _info[sid] = result
        except Exception:  # noqa: BLE001 - o vigia nunca pode morrer
            traceback.print_exc()


# ---------------------------------------------------------------- estado para o painel

def status():
    with _LOCK:
        claim = dict(_claim) if _claim else None
    proc = _agent["proc"]
    running = bool(proc and proc.poll() is None)
    return {
        "supported": supported(),
        "linked": bool(secret()),
        "claim": {"state": claim["state"], "url": claim_url(claim["code"]), "error": claim["error"]} if claim else None,
        "agent": {"running": running, "installing": _agent["installing"], "error": _agent["error"],
                  "log": _agent["lines"][-6:]},
    }
