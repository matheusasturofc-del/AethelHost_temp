"""Suporte (Beta): mensagens que as pessoas mandam para a administração e as respostas. Ficam em data/support.json."""
import json
import os
import threading
import time
import uuid

from config import DATA
from errors import ContentError
import explore

FILE = DATA / "support.json"
LOCK = threading.RLock()
SUBJECT_MAX = 80
MESSAGE_MAX = 2000
PER_HOUR = 5
PER_DAY = 15
OPEN_MAX = 10
TOPICS = ("account", "server", "vps", "backup", "explore", "bug", "other")


def _load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(FILE.name + ".part")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FILE)


def _view(t):
    return {"id": t["id"], "topic": t["topic"], "subject": t["subject"], "status": t["status"], "created": t["created"],
            "messages": [{"from": m["from"], "text": m["text"], "ts": m["ts"]} for m in t["messages"]]}


def create(user_id, topic, subject, text):
    if topic not in TOPICS:
        raise ContentError(400, "Escolha o assunto.")
    subject = explore.clean_text(subject, SUBJECT_MAX, "Título")
    text = explore.clean_text(text, MESSAGE_MAX, "Mensagem")
    if len(subject) < 3:
        raise ContentError(400, "Escreva um título (pelo menos 3 letras).")
    if len(text) < 10:
        raise ContentError(400, "Conte um pouco mais (pelo menos 10 letras).")
    now = time.time()
    with LOCK:
        items = _load()
        mine = [t for t in items if t["user"] == user_id]
        if sum(1 for t in mine if now - t["created"] < 3600) >= PER_HOUR or sum(1 for t in mine if now - t["created"] < 86400) >= PER_DAY:
            raise ContentError(429, "Você mandou muitas mensagens em pouco tempo. Tente de novo mais tarde.")
        if sum(1 for t in mine if t["status"] != "closed") >= OPEN_MAX:
            raise ContentError(429, f"Você já tem {OPEN_MAX} conversas abertas. Espere a resposta ou encerre alguma.")
        ticket = {"id": uuid.uuid4().hex[:12], "user": user_id, "topic": topic, "subject": subject, "status": "open",
                  "created": int(now), "updated": int(now), "messages": [{"from": "user", "text": text, "ts": int(now)}]}
        items.append(ticket)
        _save(items)
        return _view(ticket)


def mine(user_id):
    with LOCK:
        return [_view(t) for t in sorted((t for t in _load() if t["user"] == user_id), key=lambda t: -t["updated"])]


def close_mine(user_id, tid):
    with LOCK:
        items = _load()
        t = next((t for t in items if t["id"] == tid and t["user"] == user_id), None)
        if not t:
            raise ContentError(404, "Conversa não encontrada.")
        t["status"] = "closed"
        t["updated"] = int(time.time())
        _save(items)
        return _view(t)


def reply_mine(user_id, tid, text):
    text = explore.clean_text(text, MESSAGE_MAX, "Mensagem")
    if len(text) < 2:
        raise ContentError(400, "Escreva a mensagem.")
    with LOCK:
        items = _load()
        t = next((t for t in items if t["id"] == tid and t["user"] == user_id), None)
        if not t:
            raise ContentError(404, "Conversa não encontrada.")
        if len(t["messages"]) >= 60:
            raise ContentError(429, "Esta conversa ficou longa demais. Abra uma nova.")
        t["messages"].append({"from": "user", "text": text, "ts": int(time.time())})
        t["status"] = "open"
        t["updated"] = int(time.time())
        _save(items)
        return _view(t)


# ---- administração

def all_for_admin():
    with LOCK:
        return [{**_view(t), "user": t["user"], "updated": t["updated"]} for t in sorted(_load(), key=lambda t: (t["status"] == "closed", -t["updated"]))]


def admin_reply(tid, text, close):
    text = explore.clean_text(text, MESSAGE_MAX, "Resposta")
    with LOCK:
        items = _load()
        t = next((t for t in items if t["id"] == tid), None)
        if not t:
            raise ContentError(404, "Conversa não encontrada.")
        if text:
            t["messages"].append({"from": "team", "text": text, "ts": int(time.time())})
        t["status"] = "closed" if close else ("answered" if text else t["status"])
        t["updated"] = int(time.time())
        _save(items)
        return dict(t)
