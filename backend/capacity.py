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


def max_running():
    try:
        value = json.loads(FILE.read_text(encoding="utf-8")).get("maxRunning", DEFAULT_MAX)
    except (OSError, ValueError, AttributeError):
        return DEFAULT_MAX
    return value if isinstance(value, int) and 0 <= value <= MAX_ALLOWED else DEFAULT_MAX


def set_max(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_ALLOWED:
        raise ValueError
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(FILE.name + ".part")
    tmp.write_text(json.dumps({"maxRunning": value}), encoding="utf-8")
    os.replace(tmp, FILE)


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
