"""Capacidade do plano Grátis neste PC: teto de servidores ligados ao mesmo tempo e fila de espera.
O teto fica em data/capacity.json (o administrador muda em admin.html); a fila fica só na memória (recomeça vazia quando o site reinicia)."""
import json
import os
import threading
import time

from config import DATA

FILE = DATA / "capacity.json"
DEFAULT_MAX = 10   # servidores do plano Grátis ligados ao mesmo tempo; 0 = sem limite
MAX_ALLOWED = 1000
LOCK = threading.RLock()
_queue = []        # [{"sid", "user", "since"}], o primeiro da fila é o próximo a ligar


def _load():
    try:
        cfg = json.loads(FILE.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cfg):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(FILE.name + ".part")
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    os.replace(tmp, FILE)


def _int(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def max_running():
    value = _load().get("maxRunning", DEFAULT_MAX)
    return value if _int(value, 0, MAX_ALLOWED) else DEFAULT_MAX


def set_max(value):
    if not _int(value, 0, MAX_ALLOWED):
        raise ValueError
    cfg = _load()
    cfg["maxRunning"] = value
    _save(cfg)


# ---- tempo sem jogadores até o servidor fechar: depende de quantos servidores estão ligados
# Até `lowServers` ligados vale `lowMinutes`; a partir de `highServers` vale `highMinutes`; no meio diminui aos poucos.
IDLE_DEFAULT = {"lowServers": 10, "lowMinutes": 300, "highServers": 100, "highMinutes": 10}
IDLE_MAX_MINUTES = 24 * 60


def idle_config():
    saved = _load().get("idle")
    if isinstance(saved, dict) and _idle_valid(saved):
        return {k: saved[k] for k in IDLE_DEFAULT}
    return dict(IDLE_DEFAULT)


def _idle_valid(c):
    return (all(k in c for k in IDLE_DEFAULT) and _int(c["lowServers"], 1, MAX_ALLOWED) and _int(c["highServers"], 2, MAX_ALLOWED)
            and c["highServers"] > c["lowServers"] and _int(c["lowMinutes"], 1, IDLE_MAX_MINUTES) and _int(c["highMinutes"], 1, IDLE_MAX_MINUTES))


def set_idle(values):
    cfg = {**idle_config(), **{k: values[k] for k in IDLE_DEFAULT if isinstance(values, dict) and k in values}}
    if not _idle_valid(cfg):
        raise ValueError
    full = _load()
    full["idle"] = cfg
    _save(full)


def idle_limit(running):
    """Segundos sem jogadores até o servidor do plano Grátis fechar, com `running` servidores ligados agora."""
    c = idle_config()
    if running <= c["lowServers"]:
        minutes = c["lowMinutes"]
    elif running >= c["highServers"]:
        minutes = c["highMinutes"]
    else:
        part = (running - c["lowServers"]) / (c["highServers"] - c["lowServers"])
        minutes = c["lowMinutes"] + (c["highMinutes"] - c["lowMinutes"]) * part
    return max(60, int(minutes * 60))


def position(sid):
    """Posição na fila (1 = próximo), ou None se o servidor não está na fila."""
    with LOCK:
        for i, e in enumerate(_queue):
            if e["sid"] == sid:
                return i + 1
    return None


def add(sid, user):
    with LOCK:
        if position(sid) is None:
            _queue.append({"sid": sid, "user": user, "since": int(time.time())})
        return position(sid)


def remove(sid):
    with LOCK:
        before = len(_queue)
        _queue[:] = [e for e in _queue if e["sid"] != sid]
        return len(_queue) != before


def waiting():
    with LOCK:
        return len(_queue)


def first():
    with LOCK:
        return dict(_queue[0]) if _queue else None


def entries():
    with LOCK:
        return [dict(e) for e in _queue]
