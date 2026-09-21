"""Contas com e-mail e senha, sempre com confirmação por um código de 6 números enviado por e-mail.

- A senha é guardada só como hash scrypt (com sal próprio). Nunca em texto.
- Entrar: e-mail + senha certos -> o código chega por e-mail -> com o código a sessão é criada.
- Criar conta: nome, usuário, e-mail e senha -> código por e-mail -> a conta só passa a existir com o código certo.
- O código vale 10 minutos, 5 tentativas e uma vez só; o "reenviar" espera 1 minuto.
- Erros de e-mail/senha não dizem qual dos dois estava errado, e criar conta com um e-mail que já existe não revela isso na tela
  (a pessoa recebe um aviso por e-mail): assim ninguém descobre quem tem conta.
"""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import unicodedata

import auth
import mail
from content import ContentError

CODE_TTL = 600
MAX_TRIES = 5
RESEND_AFTER = 60
MAX_SENDS = 4                 # envios por pedido (o primeiro + reenvios)
FAIL_LIMIT, FAIL_WINDOW = 5, 15 * 60          # senhas erradas por e-mail
SEND_PER_EMAIL, SEND_PER_ALL, SEND_WINDOW = 5, 60, 3600
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,20}$")  # só minúsculas
RESERVED = {"admin", "administrador", "administrator", "root", "system", "sistema", "suporte", "support", "moderador",
            "moderator", "staff", "null", "undefined", mail.SITE_NAME.lower()}
WEAK = {"12345678", "123456789", "1234567890", "87654321", "password", "password1", "qwertyui", "qwertyuiop", "abc12345",
        "senha123", "senha1234", "11111111", "00000000", "iloveyou", "minecraft", "minecraft1", "blockhost", "aethelhost"}

_LOCK = threading.RLock()
_challenges = {}  # hash do token -> pedido em andamento
_hits = {}        # chave do limitador -> horários
_PEPPER = secrets.token_bytes(32)  # só na memória: os pedidos também não sobrevivem a um reinício


# ---------------------------------------------------------------- senha

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$14$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2 ** int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password(secrets.token_hex(8))  # para gastar o mesmo tempo quando a conta não existe


def check_password(password, email, username):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ContentError(400, "A senha precisa ter de 8 a 128 caracteres.")
    low = password.lower()
    if low in WEAK or len(set(password)) < 3 or low in (email, username.lower(), email.split("@")[0]):
        raise ContentError(400, "Essa senha é fácil de adivinhar. Escolha outra.")


def check_username(username):
    if isinstance(username, str) and username != username.lower():
        raise ContentError(400, "O nome de usuário só pode ter letras minúsculas (a-z).")
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise ContentError(400, "O nome de usuário precisa ter de 3 a 20 caracteres: letras minúsculas, números, ponto, hífen ou _.")
    if username.lower() in RESERVED:
        raise ContentError(400, "Este nome de usuário não está disponível.")


def check_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not 2 <= len(name) <= 40 or re.search(r"[\x00-\x1f\x7f<>]", name):
        raise ContentError(400, "O nome exibido precisa ter de 2 a 40 caracteres (sem < ou >).")
    return name


# ---------------------------------------------------------------- limitador

def _wait_for(key, limit, window):
    """Segundos até poder tentar de novo (0 = liberado)."""
    now = time.time()
    with _LOCK:
        hits = [t for t in _hits.get(key, []) if now - t < window]
        _hits[key] = hits
        return int(window - (now - hits[0])) + 1 if len(hits) >= limit else 0


def _hit(key):
    with _LOCK:
        _hits.setdefault(key, []).append(time.time())


def _wait_text(seconds):
    return f"{seconds} segundos" if seconds < 90 else f"{(seconds + 59) // 60} minutos"


def _check_send_budget(email):
    for key, limit in ((f"send:{email}", SEND_PER_EMAIL), ("send:*", SEND_PER_ALL)):
        wait = _wait_for(key, limit, SEND_WINDOW)
        if wait:
            raise ContentError(429, f"Muitos e-mails enviados. Tente de novo em {_wait_text(wait)}.")


# ---------------------------------------------------------------- pedidos (challenges)

def _code_hash(tid, code):
    return hmac.new(_PEPPER, f"{tid}:{code}".encode(), hashlib.sha256).hexdigest()


def _purge():
    now = time.time()
    for k in [k for k, c in _challenges.items() if c["exp"] < now]:
        del _challenges[k]


def _deliver(tid, ch, lang):
    """Gera um código novo e o envia (ou, se o e-mail já tem conta, envia o aviso). Só conta como envio se der certo."""
    _check_send_budget(ch["email"])
    if ch.get("exists"):
        code = None
        subject, text = mail.code_message(lang, ch.get("exists_kind", "exists"), "")
        ch["code_hash"] = _code_hash(tid, secrets.token_hex(8))  # nenhum código serve
    else:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        subject, text = mail.code_message(lang, ch["purpose"], code)
        ch["code_hash"] = _code_hash(tid, code)
    mail.send(ch["email"], subject, text)
    _hit(f"send:{ch['email']}")
    _hit("send:*")
    ch.update(sent_at=time.time(), tries=0, sends=ch.get("sends", 0) + 1, exp=time.time() + CODE_TTL)


def _start(ch, lang):
    token = secrets.token_urlsafe(32)
    tid = auth._hash(token)
    _deliver(tid, ch, lang)
    with _LOCK:
        _purge()
        _challenges[tid] = ch
    return {"challenge": token, "email": mail.mask_email(ch["email"]), "resendIn": RESEND_AFTER}


def _need_mail():
    if not mail.is_configured():
        raise ContentError(503, "O envio de e-mail ainda não foi configurado pelo administrador.")


def _lang(body):
    return "pt" if body.get("lang") == "pt" else "en"  # sem escolha, inglês (o padrão do site)


def find_password_user(email):
    with auth.LOCK:
        return next((u for u in auth._users.values() if auth.pw_email(u) == email), None)


# ---------------------------------------------------------------- perfil (contas criadas por Google, GitHub…)

def suggest_username(user):
    """Um nome de usuário livre, montado a partir do nome ou do e-mail da conta."""
    for source in (user.get("name"), (user.get("email") or "").split("@")[0], "player"):
        ascii_name = unicodedata.normalize("NFKD", str(source or "")).encode("ascii", "ignore").decode().lower()
        base = re.sub(r"[^a-z0-9_.-]+", "", ascii_name.replace(" ", "_"))[:16].strip("._-")
        if len(base) >= 3:
            break
    n = 0
    candidate = base
    while auth.find_by_username(candidate) or candidate in RESERVED:
        n += 1
        candidate = f"{base[:20 - len(str(n))]}{n}"
    return candidate


def set_profile(user, body):
    """Primeira entrada por um serviço: a pessoa escolhe o nome exibido e o nome de usuário (único)."""
    if user.get("username"):
        raise ContentError(409, "O seu perfil já está completo.")
    name = check_name(body.get("name"))
    username = str(body.get("username", "")).strip()
    check_username(username)
    with auth.LOCK:
        if auth.find_by_username(username):
            raise ContentError(409, "Este nome de usuário já está em uso. Escolha outro.")
        auth.update_user(user, name=name, username=username)
    return user


# ---------------------------------------------------------------- criar conta

def register(body):
    _need_mail()
    name = check_name(body.get("name"))
    username = str(body.get("username", "")).strip()
    check_username(username)
    email = mail.normalize_email(body.get("email"))
    if not email:
        raise ContentError(400, "Esse e-mail não parece válido.")
    check_password(body.get("password"), email, username)
    pw_hash = hash_password(body["password"])  # o custo é o mesmo exista ou não a conta
    if auth.find_by_username(username):
        raise ContentError(409, "Este nome de usuário já está em uso. Escolha outro.")
    ch = {"purpose": "register", "email": email, "exists": bool(find_password_user(email)),
          "reg": {"name": name, "username": username, "pw": pw_hash}}
    return _start(ch, _lang(body))


# ---------------------------------------------------------------- entrar

def login(body):
    _need_mail()
    email = mail.normalize_email(body.get("email"))
    password = body.get("password")
    if not email or not isinstance(password, str) or not password or len(password) > 128:
        raise ContentError(401, "E-mail ou senha incorretos.")
    wait = _wait_for(f"fail:{email}", FAIL_LIMIT, FAIL_WINDOW)
    if wait:
        raise ContentError(429, f"Muitas tentativas erradas. Tente de novo em {_wait_text(wait)}.")
    user = find_password_user(email)
    good = verify_password(password, user["pw"] if user else _DUMMY) and user is not None
    if not good:
        _hit(f"fail:{email}")
        raise ContentError(401, "E-mail ou senha incorretos.")
    with _LOCK:
        _hits.pop(f"fail:{email}", None)
    return _start({"purpose": "login", "email": email, "user": user["id"]}, _lang(body))


# ---------------------------------------------------------------- código

def _get(token):
    tid = auth._hash(str(token or ""))
    with _LOCK:
        _purge()
        ch = _challenges.get(tid)
    if not ch:
        raise ContentError(410, "O código expirou ou o pedido não existe mais. Comece de novo.")
    return tid, ch


def resend(body):
    tid, ch = _get(body.get("challenge"))
    wait = int(ch["sent_at"] + RESEND_AFTER - time.time()) + 1
    if wait > 0:
        raise ContentError(429, f"Espere {wait} segundos para pedir outro código.")
    if ch["sends"] >= MAX_SENDS:
        with _LOCK:
            _challenges.pop(tid, None)
        raise ContentError(429, "Muitos códigos pedidos. Comece de novo.")
    _deliver(tid, ch, _lang(body))
    return {"ok": True, "resendIn": RESEND_AFTER}


def _consume_code(tid, ch, code):
    """Confere o código do pedido; se estiver certo, o pedido é gasto (vale uma vez só)."""
    code = re.sub(r"\D", "", str(code or ""))
    if ch["tries"] >= MAX_TRIES:
        with _LOCK:
            _challenges.pop(tid, None)
        raise ContentError(429, "Muitas tentativas. Comece de novo.")
    if len(code) != 6 or not hmac.compare_digest(_code_hash(tid, code), ch["code_hash"]):
        ch["tries"] += 1
        left = MAX_TRIES - ch["tries"]
        if left <= 0:
            with _LOCK:
                _challenges.pop(tid, None)
            raise ContentError(429, "Código incorreto. Muitas tentativas: comece de novo.")
        raise ContentError(400, f"Código incorreto. Você ainda tem {left} {'tentativa' if left == 1 else 'tentativas'}.")
    with _LOCK:
        _challenges.pop(tid, None)


def verify(body):
    """Confere o código. Devolve (conta, token da sessão) quando está certo."""
    tid, ch = _get(body.get("challenge"))
    if ch["purpose"] not in ("login", "register"):
        raise ContentError(410, "O código expirou ou o pedido não existe mais. Comece de novo.")
    _consume_code(tid, ch, body.get("code"))
    if ch["purpose"] == "login":
        user = auth.get_user(ch["user"])
        if not user:
            raise ContentError(410, "A conta não existe mais.")
    else:
        reg = ch["reg"]
        with auth.LOCK:  # confere de novo: alguém pode ter pegado o nome ou o e-mail nesse meio tempo
            if auth.find_by_username(reg["username"]) or find_password_user(ch["email"]):
                raise ContentError(409, "Este nome de usuário ou e-mail acabou de ser usado. Comece de novo.")
            user = auth.create_user({"provider": "password", "sub": ch["email"], "email": ch["email"], "name": reg["name"],
                                     "username": reg["username"], "pw": reg["pw"]})
    auth.touch_login(user)
    return user, auth.new_session(user["id"])


# ---------------------------------------------------------------- trocar senha e e-mail (conta já logada)

CHANGE_FAIL_LIMIT = 5


def _check_current(user, password):
    """A senha atual precisa estar certa; erros seguidos travam por um tempo (contra quem pegou uma sessão aberta)."""
    key = f"chg:{user['id']}"
    wait = _wait_for(key, CHANGE_FAIL_LIMIT, FAIL_WINDOW)
    if wait:
        raise ContentError(429, f"Muitas tentativas erradas. Tente de novo em {_wait_text(wait)}.")
    if not isinstance(password, str) or not password or len(password) > 128 or not verify_password(password, user.get("pw") or _DUMMY):
        _hit(key)
        raise ContentError(401, "A senha atual está incorreta.")
    with _LOCK:
        _hits.pop(key, None)


def start_password_change(user, body):
    """Passo 1: senha atual + nova + confirmação. O código vai para o e-mail da conta."""
    _need_mail()
    has_pw = bool(user.get("pw"))
    if has_pw:
        _check_current(user, body.get("current"))
    new, confirm = body.get("new"), body.get("confirm")
    if not isinstance(new, str) or new != confirm:
        raise ContentError(400, "A confirmação não é igual à nova senha.")
    email = auth.pw_email(user) or mail.normalize_email(user.get("email"))
    if not email:
        raise ContentError(400, "Esta conta não tem e-mail para receber o código.")
    check_password(new, email, user.get("username") or "")
    if has_pw and verify_password(new, user["pw"]):
        raise ContentError(400, "A nova senha precisa ser diferente da atual.")
    other = find_password_user(email)
    if not has_pw and other and other["id"] != user["id"]:
        raise ContentError(409, "Já existe outra conta com senha usando este e-mail. Peça ao administrador para mesclar as contas.")
    ch = {"purpose": "pwchange" if has_pw else "pwcreate", "email": email, "user": user["id"], "pw": hash_password(new)}
    return _start(ch, _lang(body))


def start_email_change(user, body):
    """Passo 1: senha atual + novo e-mail + confirmação. O código vai para o e-mail NOVO (prova que ele é seu)."""
    _need_mail()
    if not user.get("pw"):
        raise ContentError(409, "Crie uma senha primeiro: é ela que protege a troca de e-mail.")
    _check_current(user, body.get("current"))
    new = mail.normalize_email(body.get("email"))
    if not new:
        raise ContentError(400, "Esse e-mail não parece válido.")
    if new != mail.normalize_email(body.get("confirm")):
        raise ContentError(400, "Os dois e-mails precisam ser iguais.")
    if new == (auth.pw_email(user) or mail.normalize_email(user.get("email"))):
        raise ContentError(400, "Esse já é o e-mail da sua conta.")
    other = find_password_user(new)
    ch = {"purpose": "emailchange", "email": new, "user": user["id"], "exists": bool(other and other["id"] != user["id"]),
          "exists_kind": "exists_change"}
    return _start(ch, _lang(body))


def _notify(email, lang, kind):
    try:
        subject, text = mail.notice_message(lang, kind)
        mail.send(email, subject, text)
    except Exception:  # noqa: BLE001 - o aviso é um extra: a mudança já foi feita
        pass


def confirm_change(user, body, cookie_header):
    """Passo 2: o código certo aplica a mudança. Devolve a conta atualizada."""
    tid, ch = _get(body.get("challenge"))
    if ch.get("user") != user["id"] or ch["purpose"] not in ("pwchange", "pwcreate", "emailchange"):
        raise ContentError(410, "O código expirou ou o pedido não existe mais. Comece de novo.")
    _consume_code(tid, ch, body.get("code"))
    lang = _lang(body)
    if ch["purpose"] in ("pwchange", "pwcreate"):
        auth.set_password(user, ch["pw"], login_email=ch["email"])
        auth.logout_others(user["id"], cookie_header)
        _notify(ch["email"], lang, "pw_changed" if ch["purpose"] == "pwchange" else "pw_created")
    else:
        with auth.LOCK:  # confere de novo: outra conta pode ter ficado com esse e-mail nesse meio tempo
            other = find_password_user(ch["email"])
            if other and other["id"] != user["id"]:
                raise ContentError(409, "Este e-mail acabou de ser usado por outra conta. Comece de novo.")
            old = auth.pw_email(user) or mail.normalize_email(user.get("email"))
            auth.set_email(user, ch["email"])
        if old and old != ch["email"]:
            _notify(old, lang, "email_changed")
    return user


def resend_change(user, body):
    tid, ch = _get(body.get("challenge"))
    if ch.get("user") != user["id"] or ch["purpose"] not in ("pwchange", "pwcreate", "emailchange"):
        raise ContentError(410, "O código expirou ou o pedido não existe mais. Comece de novo.")
    return resend(body)
