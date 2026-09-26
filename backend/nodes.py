"""Máquinas de jogo ("nós"): computadores ou VPS do administrador onde rodam os servidores do plano Grátis,
para o site (contas, painel, Explorar) poder ficar numa máquina só de painel. Ficam em data/nodes.json.

Um servidor Grátis que mora num nó guarda `node` (o id do nó) e é controlado por SSH, com o mesmo código dos servidores em VPS
(remote.py e RemoteRuntime). A chave SSH dos nós é uma só, da plataforma (data/keys/platform)."""
import json
import os
import re
import threading
import time
import uuid

from config import DATA
from errors import ContentError

FILE = DATA / "nodes.json"
LOCK = threading.RLock()
OWNER = "platform"          # "dono" da chave SSH dos nós: data/keys/platform
MAX_NODES = 50
STORE_FACTOR = 5            # um nó guarda até 5x mais servidores do que consegue ligar ao mesmo tempo
HOST_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$", re.I)


def _load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("localGames", True)
    data.setdefault("nodes", [])
    return data


def _save(data):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(FILE.name + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FILE)


def local_games():
    """Se este PC (o do site) também pode rodar servidores Grátis (quando não há nó com vaga, ou sempre, se não houver nós)."""
    return bool(_load()["localGames"])


def set_local_games(flag):
    if not isinstance(flag, bool):
        raise ContentError(400, "Informe localGames: true ou false.")
    with LOCK:
        data = _load()
        data["localGames"] = flag
        _save(data)


def all_nodes():
    with LOCK:
        return [dict(n) for n in _load()["nodes"]]


def get(nid):
    if not nid:
        return None
    with LOCK:
        return next((dict(n) for n in _load()["nodes"] if n["id"] == nid), None)


def _clean(fields, current=None):
    cur = dict(current or {})
    out = dict(cur)
    if "name" in fields or not cur:
        name = str(fields.get("name", "")).strip()
        if not 2 <= len(name) <= 40 or re.search(r"[<>\x00-\x1f]", name):
            raise ContentError(400, "O nome da máquina precisa ter de 2 a 40 caracteres.")
        out["name"] = name
    if "host" in fields or not cur:
        host = str(fields.get("host", "")).strip()
        if not HOST_RE.match(host) or len(host) > 253:
            raise ContentError(400, "Endereço da máquina inválido (IP ou nome, sem espaços).")
        out["host"] = host
    if "port" in fields or not cur:
        port = fields.get("port", 22)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ContentError(400, "Porta SSH inválida (1 a 65535).")
        out["port"] = port
    if "user" in fields or not cur:
        user = str(fields.get("user", "")).strip()
        if not USER_RE.match(user) or len(user) > 32:
            raise ContentError(400, "Usuário SSH inválido.")
        out["user"] = user
    if "maxServers" in fields or not cur:
        cap = fields.get("maxServers", 5)
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 1000:
            raise ContentError(400, "O máximo de servidores ligados ao mesmo tempo vai de 1 a 1000.")
        out["maxServers"] = cap
    if "enabled" in fields:
        if not isinstance(fields["enabled"], bool):
            raise ContentError(400, "Informe enabled: true ou false.")
        out["enabled"] = fields["enabled"]
    out.setdefault("enabled", True)
    return out


def create(fields):
    with LOCK:
        data = _load()
        if len(data["nodes"]) >= MAX_NODES:
            raise ContentError(429, f"No máximo {MAX_NODES} máquinas de jogo.")
        node = _clean(fields)
        if any(n["host"] == node["host"] and n["port"] == node["port"] and n["user"] == node["user"] for n in data["nodes"]):
            raise ContentError(409, "Esta máquina já está cadastrada.")
        node.update(id=uuid.uuid4().hex[:8], createdAt=int(time.time()), memMb=0)
        data["nodes"].append(node)
        _save(data)
        return dict(node)


def update(nid, fields):
    with LOCK:
        data = _load()
        for i, n in enumerate(data["nodes"]):
            if n["id"] == nid:
                data["nodes"][i] = {**n, **_clean(fields, n)}
                _save(data)
                return dict(data["nodes"][i])
    raise ContentError(404, "Máquina não encontrada.")


def remove(nid):
    with LOCK:
        data = _load()
        kept = [n for n in data["nodes"] if n["id"] != nid]
        if len(kept) == len(data["nodes"]):
            raise ContentError(404, "Máquina não encontrada.")
        data["nodes"] = kept
        _save(data)


def set_mem(nid, mb):
    with LOCK:
        data = _load()
        for n in data["nodes"]:
            if n["id"] == nid:
                n["memMb"] = int(mb)
        _save(data)


# ---- como os servidores usam isto

def is_remote(server):
    """O servidor roda numa VPS do cliente ou num nó: é controlado por SSH (remote.py)."""
    return server.get("plan") == "vps" or bool(server.get("node"))


def machine(server):
    """Onde o servidor Grátis roda: "local" (o PC do site) ou o id do nó."""
    return server.get("node") or "local"


def conn(server):
    """Dados da conexão SSH do servidor (VPS do cliente ou nó da plataforma)."""
    if server.get("node"):
        node = get(server["node"])
        if not node:
            raise RuntimeError("A máquina de jogo deste servidor não existe mais. Fale com a administração.")
        return {"provider": "AethelHost", "host": node["host"], "port": node["port"], "user": node["user"], "owner": OWNER, "trusted": True}
    return {**server["vps"], "owner": server["owner"]}


def host(server):
    """Endereço (IP ou nome) de onde o servidor remoto roda, para montar o endereço de jogo."""
    if server.get("node"):
        node = get(server["node"])
        return node["host"] if node else ""
    return server["vps"]["host"]
