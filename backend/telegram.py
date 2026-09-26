"""Telegram: os códigos do "Esqueci a senha" também podem chegar por um bot (grátis e sem limite de envios).

- O administrador cria um bot no @BotFather e cola o token no site (data/telegram.json).
- Cada pessoa vincula o próprio Telegram: o site mostra um link `t.me/<bot>?start=<código>`; ao tocar em "Iniciar" no Telegram,
  o bot (que aqui escuta por *long polling*, sem precisar de endereço público) liga aquela conversa à conta.
- O código de vínculo vale 10 minutos e uma vez só, e só é criado por quem está logado e digitou a senha atual."""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import DATA
from errors import ContentError

TG_FILE = DATA / "telegram.json"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")
LINK_TTL = 600
LOCK = threading.RLock()
_links = {}  # código de vínculo -> {"user", "exp"}
on_link = None  # o accounts.py coloca aqui uma função (user_id, chat_id, nome) que liga a conta e avisa por e-mail


def _base():
    """Endereço do Telegram. AETHELHOST_TELEGRAM_TEST_BASE (só http://127.0.0.1…) troca por um Telegram de mentira nos testes."""
    test = os.environ.get("AETHELHOST_TELEGRAM_TEST_BASE", "")
    if test and urllib.parse.urlparse(test).hostname in ("127.0.0.1", "localhost"):
        return test
    return "https://api.telegram.org"


def _call(token, method, params=None, timeout=20):
    """(ok, resultado, texto de erro). Erros de rede viram ContentError 502."""
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{_base()}/bot{token}/{method}", data=data, headers={"User-Agent": "AethelHost/0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(20000))
        except ValueError:
            body = {}
        return False, None, str(body.get("description", f"erro {e.code}"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ContentError(502, "Não consegui falar com o Telegram. Confira a internet.") from None
    return bool(body.get("ok")), body.get("result"), str(body.get("description", ""))


# ---------------------------------------------------------------- configuração

def load():
    try:
        return json.loads(TG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_configured():
    c = load()
    return bool(c.get("token") and c.get("username"))


def config_view():
    """O que a tela do administrador vê: o nome do bot, nunca o token."""
    c = load()
    return {"username": c.get("username", ""), "hasToken": bool(c.get("token")), "configured": is_configured()}


def save(body):
    if body.get("clear"):
        TG_FILE.unlink(missing_ok=True)
        return config_view()
    token = str(body.get("token", "")).strip() or load().get("token", "")
    if not TOKEN_RE.match(token):
        raise ContentError(400, "Esse token não parece de um bot do Telegram (é do tipo 123456789:AAE…, que o @BotFather entrega).")
    ok, me, _ = _call(token, "getMe", timeout=15)
    if not ok or not (me or {}).get("username"):
        raise ContentError(400, "O Telegram não aceitou esse token. Confira se copiou inteiro (ou crie um novo com /token no @BotFather).")
    TG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TG_FILE.with_name(TG_FILE.name + ".part")
    tmp.write_text(json.dumps({"token": token, "username": me["username"]}), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # tem o token do bot: só o dono do arquivo lê (no Windows é ignorado)
    except OSError:
        pass
    os.replace(tmp, TG_FILE)
    return config_view()


# ---------------------------------------------------------------- enviar

def send(chat_id, text):
    c = load()
    if not is_configured():
        raise ContentError(503, "O envio pelo Telegram ainda não foi configurado pelo administrador.")
    ok, _, why = _call(c["token"], "sendMessage", {"chat_id": chat_id, "text": text})
    if not ok:
        if "blocked" in why.lower() or "chat not found" in why.lower() or "deactivated" in why.lower():
            raise ContentError(502, "Não consegui mandar a mensagem no Telegram. Abra o bot e toque em Iniciar (ou desbloqueie-o).")
        raise ContentError(502, "O Telegram recusou o envio da mensagem.")


def code_text(lang, purpose, code):
    en = lang == "en"
    if purpose == "reset":
        return (f"AethelHost\n\nYour code to reset your password: {code}\nIt expires in 10 minutes. If it wasn't you, ignore this message and don't share the code."
                if en else f"AethelHost\n\nSeu código para redefinir a senha: {code}\nVale por 10 minutos. Se não foi você, ignore esta mensagem e não passe o código para ninguém.")
    return (f"AethelHost\n\nYour verification code: {code}\nIt expires in 10 minutes." if en else f"AethelHost\n\nSeu código de verificação: {code}\nVale por 10 minutos.")


# ---------------------------------------------------------------- vincular

def new_link(user_id):
    """Código de vínculo e o link para abrir o bot. Vale 10 minutos e uma vez."""
    c = load()
    if not is_configured():
        raise ContentError(503, "O envio pelo Telegram ainda não foi configurado pelo administrador.")
    code = secrets.token_urlsafe(9)
    with LOCK:
        now = time.time()
        for k in [k for k, v in _links.items() if v["exp"] < now or v["user"] == user_id]:
            del _links[k]  # um pedido novo cancela o anterior da mesma conta
        _links[code] = {"user": user_id, "exp": now + LINK_TTL}
    return {"code": code, "bot": c["username"], "url": f"https://t.me/{c['username']}?start={code}", "expiresIn": LINK_TTL}


HELP = ("AethelHost: para vincular o Telegram à sua conta, abra Editar perfil → Segurança → Telegram no site e use o botão "
        "\"Vincular Telegram\".\n\nTo link Telegram to your account, open Edit profile → Security → Telegram on the site and use the \"Link Telegram\" button.")


def handle_update(update):
    """Uma mensagem para o bot. Só `/start <código>` (em conversa privada) faz alguma coisa."""
    msg = (update or {}).get("message") or {}
    chat = msg.get("chat") or {}
    text = str(msg.get("text") or "").strip()
    if chat.get("type") != "private" or not chat.get("id"):
        return
    m = re.match(r"^/start(?:@\w+)?(?:\s+(\S+))?$", text)
    if not m:
        return
    code = m.group(1)
    with LOCK:
        pend = _links.pop(code, None) if code else None
    if not pend or pend["exp"] < time.time():
        send(chat["id"], HELP)
        return
    who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "Telegram"
    try:
        name = on_link(pend["user"], chat["id"], who[:60]) if on_link else None
    except ContentError as e:
        send(chat["id"], f"AethelHost: {e.message}")
        return
    send(chat["id"], f"AethelHost: Telegram vinculado à conta @{name or ''}. Agora você pode receber por aqui o código do \"Esqueci a senha\".\n\n"
                     f"Telegram linked to your account. You can now receive your password-reset code here.")


def poll_forever():
    """Thread do bot: escuta as mensagens (long polling). Sem Telegram configurado, só espera."""
    offset = 0
    while True:
        try:
            c = load()
            if not is_configured():
                time.sleep(5)
                continue
            ok, updates, _ = _call(c["token"], "getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": json.dumps(["message"])}, timeout=35)
            if not ok:
                time.sleep(10)
                continue
            for u in updates or []:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                try:
                    handle_update(u)
                except Exception as e:  # uma mensagem ruim não pode derrubar o bot
                    print(f"[aviso] Telegram: {e}")
            if not updates and _base().startswith("http://"):
                time.sleep(0.3)  # o Telegram de mentira (testes) não segura a conexão
        except ContentError:
            time.sleep(10)  # sem internet agora
        except Exception as e:
            print(f"[aviso] Telegram: {e}")
            time.sleep(10)
