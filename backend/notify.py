"""Notificações (o sino no topo): convites de compartilhamento e avisos. Ficam em data/notifications.json."""
import json
import os
import threading
import time
import uuid

from config import DATA

FILE = DATA / "notifications.json"
LOCK = threading.RLock()
MAX_PER_USER = 100


def _load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(FILE.name + ".part")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, FILE)


def add(user_id, kind, data):
    """Cria uma notificação para `user_id`. `kind`: share_invite, share_accepted, share_declined, share_changed, share_removed, share_left."""
    with LOCK:
        items = _load()
        item = {"id": uuid.uuid4().hex[:12], "user": user_id, "type": kind, "ts": int(time.time()), "read": False, "data": data}
        items.append(item)
        mine = [i for i in items if i["user"] == user_id]
        if len(mine) > MAX_PER_USER:  # descarta as mais antigas (que já foram lidas, se der)
            drop = {i["id"] for i in sorted(mine, key=lambda i: (not i["read"], i["ts"]))[: len(mine) - MAX_PER_USER]}
            items = [i for i in items if i["id"] not in drop]
        _save(items)
        return item


def for_user(user_id):
    """As notificações da pessoa, da mais nova para a mais antiga, e quantas ainda não foram lidas."""
    with LOCK:
        mine = sorted((i for i in _load() if i["user"] == user_id), key=lambda i: -i["ts"])
    return mine, sum(1 for i in mine if not i["read"])


def get(user_id, nid):
    with LOCK:
        return next((i for i in _load() if i["id"] == nid and i["user"] == user_id), None)


def mark_read(user_id, ids=None):
    with LOCK:
        items = _load()
        for i in items:
            if i["user"] == user_id and (ids is None or i["id"] in ids) and i["type"] != "share_invite":
                i["read"] = True  # convites só deixam de contar quando a pessoa aceita ou recusa
        _save(items)


def remove(user_id, nid):
    with LOCK:
        items = _load()
        kept = [i for i in items if not (i["id"] == nid and i["user"] == user_id)]
        if len(kept) != len(items):
            _save(kept)
        return len(kept) != len(items)


def remove_where(predicate):
    with LOCK:
        items = _load()
        kept = [i for i in items if not predicate(i)]
        if len(kept) != len(items):
            _save(kept)


def reassign(old_user, new_user):
    """Contas mescladas: as notificações da conta antiga passam para a que fica."""
    with LOCK:
        items = _load()
        for i in items:
            if i["user"] == old_user:
                i["user"] = new_user
        _save(items)
