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


# ---- limites de cada servidor do plano Grátis (RAM e jogadores). O plano com VPS própria não tem esses limites.
# Vanilla usa pouca memória; com mods ou plugins (Paper, Purpur, Fabric, Quilt, Forge, NeoForge) o teto é maior.
LIMITS_DEFAULT = {"vanillaRamMb": 1024, "moddedRamMb": 2048, "maxPlayers": 20}
RAM_STEPS = (512, 1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288, 16384)


def limits():
    saved = _load().get("limits")
    if isinstance(saved, dict) and _limits_valid(saved):
        return {k: saved[k] for k in LIMITS_DEFAULT}
    return dict(LIMITS_DEFAULT)


def _limits_valid(c):
    return (all(k in c for k in LIMITS_DEFAULT) and _int(c["vanillaRamMb"], 512, 16384) and _int(c["moddedRamMb"], 1024, 16384)
            and c["moddedRamMb"] >= c["vanillaRamMb"] and _int(c["maxPlayers"], 1, 1000))


def set_limits(values):
    cfg = {**limits(), **{k: values[k] for k in LIMITS_DEFAULT if isinstance(values, dict) and k in values}}
    if not _limits_valid(cfg):
        raise ValueError
    full = _load()
    full["limits"] = cfg
    _save(full)


def ram_limit(software):
    """Teto de RAM (MB) de um servidor do plano Grátis com este software."""
    c = limits()
    return c["vanillaRamMb"] if software == "vanilla" else c["moddedRamMb"]


def ram_options(software, system_mb):
    """Quantidades de RAM que o plano Grátis oferece para este software (também limitadas a 75% da memória do PC)."""
    top = min(ram_limit(software), int(system_mb * 0.75)) if system_mb else ram_limit(software)
    floor = 512 if software == "vanilla" else 1024
    return [m for m in RAM_STEPS if floor <= m <= top] or [max(floor, min(top, ram_limit(software)))]


def clamp_ram(software, ram, system_mb):
    """`ram` dentro do que o plano Grátis oferece para este software (o maior valor permitido que não passa do pedido)."""
    options = ram_options(software, system_mb)
    fit = [m for m in options if m <= ram]
    return fit[-1] if fit else options[0]


# ---- vagas de contas no plano Grátis: no máximo N contas diferentes com servidor Grátis (0 = sem limite).
# Quem já tem servidor Grátis continua tendo; as outras esperam numa lista e são avisadas quando abre vaga.
WAITLIST = DATA / "waitlist.json"
SEAT_HOLD = 48 * 3600  # depois de avisada, a pessoa tem 48 h para criar o servidor antes de perder a vez


def max_accounts():
    value = _load().get("maxFreeAccounts", 0)
    return value if _int(value, 0, 1_000_000) else 0


def set_max_accounts(value):
    if not _int(value, 0, 1_000_000):
        raise ValueError
    cfg = _load()
    cfg["maxFreeAccounts"] = value
    _save(cfg)


def _wl_load():
    try:
        items = json.loads(WAITLIST.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _wl_save(items):
    WAITLIST.parent.mkdir(parents=True, exist_ok=True)
    tmp = WAITLIST.with_name(WAITLIST.name + ".part")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    os.replace(tmp, WAITLIST)


def waitlist():
    with LOCK:
        return _wl_load()


def wait_position(user):
    with LOCK:
        for i, e in enumerate(_wl_load()):
            if e["user"] == user:
                return i + 1
    return None


def wait_join(user):
    with LOCK:
        items = _wl_load()
        if not any(e["user"] == user for e in items):
            items.append({"user": user, "since": int(time.time()), "notified": None})
            _wl_save(items)
        return wait_position(user)


def wait_leave(user):
    with LOCK:
        items = _wl_load()
        kept = [e for e in items if e["user"] != user]
        if len(kept) != len(items):
            _wl_save(kept)
        return len(kept) != len(items)


def wait_update(fn):
    """Deixa `fn(lista)` mexer na lista de espera e guarda o resultado."""
    with LOCK:
        items = _wl_load()
        fn(items)
        _wl_save(items)


def position(sid):
    """Posição na fila da máquina do servidor (1 = próximo), ou None se ele não está na fila."""
    with LOCK:
        mine = next((e for e in _queue if e["sid"] == sid), None)
        if not mine:
            return None
        return 1 + sum(1 for e in _queue[:_queue.index(mine)] if e["machine"] == mine["machine"])


def add(sid, user, machine="local"):
    """Põe o servidor no fim da fila da sua máquina ("local" = o PC do site, ou o id de um nó)."""
    with LOCK:
        if position(sid) is None:
            _queue.append({"sid": sid, "user": user, "machine": machine, "since": int(time.time())})
        return position(sid)


def remove(sid):
    with LOCK:
        before = len(_queue)
        _queue[:] = [e for e in _queue if e["sid"] != sid]
        return len(_queue) != before


def waiting(machine=None):
    with LOCK:
        return sum(1 for e in _queue if machine is None or e["machine"] == machine)


def first():
    with LOCK:
        return dict(_queue[0]) if _queue else None


def entries():
    with LOCK:
        return [dict(e) for e in _queue]
