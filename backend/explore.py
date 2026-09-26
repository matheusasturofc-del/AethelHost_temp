"""Explorar (Beta): servidores que o dono escolheu mostrar para todo mundo, o selo "Confiável por AethelHost" e as denúncias.
O anúncio e o selo ficam dentro do próprio servidor (`explore` e `verified` em data/servers.json); as denúncias, em data/reports.json."""
import json
import os
import re
import threading
import time
import uuid

from config import DATA
from errors import ContentError

REPORTS = DATA / "reports.json"
LOCK = threading.RLock()
ABOUT_MAX = 200
NOTE_MAX = 300
MAX_LISTED = 3          # servidores anunciados por conta
HIDE_AT = 5             # denúncias em aberto (de pessoas diferentes) que tiram o servidor da lista até o administrador olhar
REPORTS_PER_DAY = 10    # por conta
REASONS = {
    "offensive": "Conteúdo ofensivo ou ilegal",
    "malicious": "Mods ou plugins maliciosos",
    "scam": "Golpe ou pedido de dinheiro/dados",
    "broken": "Não liga ou o endereço não funciona",
    "other": "Outro motivo",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f<>]")


def clean_text(value, limit, label):
    text = _CONTROL.sub("", str(value or "")).replace("\r", "").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        raise ContentError(400, f"{label}: no máximo {limit} caracteres.")
    return text


def is_listed(server):
    e = server.get("explore") or {}
    return bool(e.get("listed")) and not e.get("blocked")


def is_verified(server):
    """O selo vale enquanto o servidor for o mesmo que foi testado (mesmo software e versão; os mods são conferidos nas rotas de mods)."""
    v = server.get("verified")
    return bool(v) and v.get("software") == server.get("software") and v.get("version") == server.get("version")


def public_view(server):
    """O que cada um vê do servidor do outro: nada de dono interno, dados da VPS, convites ou notas do administrador."""
    return {"listed": is_listed(server), "about": (server.get("explore") or {}).get("about", ""),
            "blocked": bool((server.get("explore") or {}).get("blocked")), "verified": is_verified(server),
            "verifiedAt": (server.get("verified") or {}).get("at") if is_verified(server) else None}


# ---------------------------------------------------------------- denúncias

def _load():
    try:
        return json.loads(REPORTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items):
    REPORTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORTS.with_name(REPORTS.name + ".part")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, REPORTS)


def add_report(sid, user_id, reason, note):
    if reason not in REASONS:
        raise ContentError(400, "Escolha o motivo da denúncia.")
    note = clean_text(note, NOTE_MAX, "Detalhes")
    now = time.time()
    with LOCK:
        items = _load()
        if any(r["server"] == sid and r["user"] == user_id and r["open"] for r in items):
            raise ContentError(409, "Você já denunciou este servidor. A administração vai analisar.")
        if sum(1 for r in items if r["user"] == user_id and now - r["ts"] < 86400) >= REPORTS_PER_DAY:
            raise ContentError(429, "Você já fez muitas denúncias hoje. Tente amanhã.")
        items.append({"id": uuid.uuid4().hex[:12], "server": sid, "user": user_id, "reason": reason, "note": note, "ts": int(now), "open": True})
        _save(items)


def open_reports(sid=None):
    with LOCK:
        return [r for r in _load() if r["open"] and (sid is None or r["server"] == sid)]


def hidden_by_reports(sid):
    return len({r["user"] for r in open_reports(sid)}) >= HIDE_AT


def dismiss_reports(sid):
    with LOCK:
        items = _load()
        for r in items:
            if r["server"] == sid and r["open"]:
                r["open"] = False
        _save(items)


def forget_server(sid):
    with LOCK:
        items = _load()
        kept = [r for r in items if r["server"] != sid]
        if len(kept) != len(items):
            _save(kept)


# ---------------------------------------------------------------- quanto cada servidor é jogado (para "Mais jogados")

STATS = DATA / "explore_stats.json"
STATS_DAYS = 7


def _stats_load():
    try:
        return json.loads(STATS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(counts):
    """`counts`: {id do servidor: jogadores agora}. Chamado a cada minuto; soma "jogador-minutos" no dia de hoje."""
    today = time.strftime("%Y-%m-%d")
    oldest = time.strftime("%Y-%m-%d", time.localtime(time.time() - STATS_DAYS * 86400))
    with LOCK:
        stats = _stats_load()
        for sid, n in counts.items():
            if n > 0:
                day = stats.setdefault(sid, {})
                day[today] = day.get(today, 0) + n
        for sid in list(stats):
            stats[sid] = {d: v for d, v in stats[sid].items() if d >= oldest}
            if not stats[sid]:
                del stats[sid]
        tmp = STATS.with_name(STATS.name + ".part")
        tmp.write_text(json.dumps(stats), encoding="utf-8")
        os.replace(tmp, STATS)


def plays(sid=None):
    """Jogador-minutos dos últimos 7 dias de um servidor (ou de todos, em um dicionário)."""
    oldest = time.strftime("%Y-%m-%d", time.localtime(time.time() - STATS_DAYS * 86400))
    with LOCK:
        stats = _stats_load()
    total = {s: sum(v for d, v in days.items() if d >= oldest) for s, days in stats.items()}
    return total if sid is None else total.get(sid, 0)
