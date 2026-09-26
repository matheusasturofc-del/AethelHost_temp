"""Contas: entrar com Google, Microsoft, GitHub, Discord ou outro provedor OpenID Connect
(o cadastro com e-mail e senha fica em accounts.py e usa as mesmas contas e sessões daqui).

Usa o fluxo "Authorization Code" com PKCE, um `state` amarrado ao navegador (contra login forjado) e sessões
guardadas só como hash. Nos logins de provedor o AethelHost nunca vê senha: quem autentica é o provedor.
Dados em data/: auth.json (credenciais dos provedores), users.json e sessions.json.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie

from config import DATA
from content import ContentError

CONFIG_FILE = DATA / "auth.json"
USERS_FILE = DATA / "users.json"
SESSIONS_FILE = DATA / "sessions.json"
AVATARS = DATA / "avatars"
BANNERS = DATA / "banners"
BANNER_PRESETS = ("green", "blue", "purple", "orange", "red", "gray")  # o primeiro é o padrão
SESSION_COOKIE = "bh_session"
BIND_COOKIE = "bh_oauth"
SESSION_SECONDS = 14 * 24 * 3600
PENDING_SECONDS = 600
LOCK = threading.RLock()


class LoginFailed(Exception):
    """Falha no login. `code` vai para a tela de login, que mostra a mensagem certa."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


BUILTIN = {
    "google": {"label": "Google", "kind": "oidc", "scope": "openid email profile",
               "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
               "token": "https://oauth2.googleapis.com/token",
               "userinfo": "https://openidconnect.googleapis.com/v1/userinfo"},
    "microsoft": {"label": "Microsoft", "kind": "oidc", "scope": "openid email profile",
                  "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                  "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                  "userinfo": "https://graph.microsoft.com/oidc/userinfo"},
    "github": {"label": "GitHub", "kind": "github", "scope": "read:user user:email",
               "authorize": "https://github.com/login/oauth/authorize",
               "token": "https://github.com/login/oauth/access_token",
               "userinfo": "https://api.github.com/user"},
    "discord": {"label": "Discord", "kind": "discord", "scope": "identify email",
                "authorize": "https://discord.com/oauth2/authorize",
                "token": "https://discord.com/api/oauth2/token",
                "userinfo": "https://discord.com/api/users/@me"},
}
ORDER = ["google", "microsoft", "github", "discord", "custom"]
NEXT_RE = re.compile(r"^/(?:servers|create|panel|admin|profile|settings|index)\.html(?:\?[A-Za-z0-9=&%._-]{0,200})?$")


# ---------------------------------------------------------------- arquivos

def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # credenciais e sessões: só o dono do arquivo lê (no Windows é ignorado)
    except OSError:
        pass
    os.replace(tmp, path)


_users = {u["id"]: u for u in _read(USERS_FILE, [])}
_sessions = _read(SESSIONS_FILE, {})  # hash do token -> {user, exp}


def _lowercase_usernames():
    """Nomes de usuário só têm minúsculas: os que foram criados com maiúscula viram minúscula (a unicidade já ignorava maiúsculas,
    então dois nomes nunca colidem)."""
    changed = False
    for u in _users.values():
        name = u.get("username")
        if name and name != name.lower():
            u["username"] = name.lower()
            changed = True
    if changed:
        _write(USERS_FILE, list(_users.values()))


_lowercase_usernames()
_pending = {}  # state -> dados do login em andamento (some sozinho em 10 minutos)
on_first_user = None  # o servidor coloca aqui uma função chamada quando a primeira conta é criada


def load_config():
    cfg = _read(CONFIG_FILE, {})
    cfg.setdefault("providers", {})
    return cfg


# ---------------------------------------------------------------- provedores

def _check_url(url):
    """Endereços de provedor: https, ou http só para o próprio computador (testes)."""
    u = urllib.parse.urlparse(str(url))
    ok_host = u.hostname in ("127.0.0.1", "localhost")
    if u.scheme not in ("https", "http") or not u.hostname or u.username or (u.scheme == "http" and not ok_host):
        raise ContentError(400, "Endereço inválido: use https:// (ou http:// só para o seu próprio computador).")
    return str(url)


def provider_def(pid, cfg):
    if pid in BUILTIN:
        return dict(BUILTIN[pid])
    c = cfg["providers"].get("custom") if pid == "custom" else None
    if c and all(c.get(k) for k in ("authorize_url", "token_url", "userinfo_url")):
        return {"label": c.get("label") or "Outro", "kind": "oidc", "scope": c.get("scope") or "openid email profile",
                "authorize": c["authorize_url"], "token": c["token_url"], "userinfo": c["userinfo_url"]}
    return None


def is_configured(pid, cfg):
    c = cfg["providers"].get(pid) or {}
    return bool(provider_def(pid, cfg) and c.get("client_id") and c.get("client_secret"))


def list_providers():
    cfg = load_config()
    out = []
    for pid in ORDER:
        d = provider_def(pid, cfg)
        if d or pid == "custom":
            out.append({"id": pid, "label": (d or {}).get("label", "Outro"), "configured": is_configured(pid, cfg)})
    return out


def setup_allowed(user):
    """Configurar os logins: na primeira vez qualquer um (o computador é seu); depois só o administrador."""
    return not _users or bool(user and user.get("admin"))


def config_view(redirect_base):
    cfg = load_config()
    out = {}
    for pid in ORDER:
        c = cfg["providers"].get(pid) or {}
        entry = {"label": (provider_def(pid, cfg) or {}).get("label", "Outro"), "client_id": c.get("client_id", ""),
                 "hasSecret": bool(c.get("client_secret")), "configured": is_configured(pid, cfg),
                 "redirectUri": f"{redirect_base}/auth/callback/{pid}"}
        if pid == "custom":
            entry.update({k: c.get(k, "") for k in ("authorize_url", "token_url", "userinfo_url", "scope")})
            entry["label"] = c.get("label", "")
        out[pid] = entry
    return out


def save_provider(pid, fields):
    if pid not in ORDER:
        raise ContentError(400, "Provedor desconhecido.")
    clean = lambda k, n=300: re.sub(r"[\x00-\x1f]", "", str(fields.get(k, ""))).strip()[:n]
    cid, secret = clean("client_id"), clean("client_secret")
    with LOCK:
        cfg = load_config()
        current = cfg["providers"].get(pid) or {}
        if not cid:
            cfg["providers"].pop(pid, None)  # campo vazio remove o provedor
        else:
            entry = {"client_id": cid, "client_secret": secret or current.get("client_secret", "")}
            if not entry["client_secret"]:
                raise ContentError(400, "Informe também a chave secreta (client secret).")
            if pid == "custom":
                entry["label"] = clean("label", 40) or "Outro"
                for k in ("authorize_url", "token_url", "userinfo_url"):
                    entry[k] = _check_url(clean(k, 500))
                entry["scope"] = clean("scope", 200) or "openid email profile"
                if not re.fullmatch(r"[\w .:/-]{1,200}", entry["scope"]):
                    raise ContentError(400, "Escopo inválido.")
            cfg["providers"][pid] = entry
        _write(CONFIG_FILE, cfg)


# ---------------------------------------------------------------- HTTP para os provedores

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # um provedor não pode nos mandar para outro lugar com a chave secreta


_opener = urllib.request.build_opener(_NoRedirect)


def _http(url, form=None, headers=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", "User-Agent": "AethelHost/0.4", **(headers or {})})
    try:
        with _opener.open(req, timeout=20) as r:
            return json.loads(r.read(1_000_000))
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise LoginFailed("provider_error")


def _https_only(url):
    u = urllib.parse.urlparse(str(url or ""))
    return url if u.scheme == "https" else ""


def _profile(d, access):
    bearer = {"Authorization": f"Bearer {access}"}
    if d["kind"] == "github":
        user = _http(d["userinfo"], headers={**bearer, "X-GitHub-Api-Version": "2022-11-28"})
        email = user.get("email")
        if not email:  # o e-mail pode estar escondido: procura o principal e verificado
            try:
                for e in _http("https://api.github.com/user/emails", headers=bearer):
                    if e.get("primary") and e.get("verified"):
                        email = e.get("email")
            except (LoginFailed, TypeError):
                email = None
        return {"sub": str(user.get("id", "")), "name": user.get("name") or user.get("login"), "email": email,
                "avatar": _https_only(user.get("avatar_url"))}
    if d["kind"] == "discord":
        u = _http(d["userinfo"], headers=bearer)
        avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png" if u.get("avatar") and u.get("id") else ""
        return {"sub": str(u.get("id", "")), "name": u.get("global_name") or u.get("username"), "email": u.get("email"), "avatar": avatar}
    info = _http(d["userinfo"], headers=bearer)  # OpenID Connect (Google, Microsoft, outros)
    return {"sub": str(info.get("sub", "")), "name": info.get("name") or info.get("email"), "email": info.get("email"),
            "avatar": _https_only(info.get("picture"))}


# ---------------------------------------------------------------- login

def safe_next(value):
    return value if value and NEXT_RE.match(value) else "/servers.html"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def begin(pid, redirect_base, next_url, link_user=None):
    """Começa o login (ou, com link_user, a conexão de mais um login a essa conta):
    devolve (endereço do provedor, cookie que amarra este navegador ao pedido)."""
    cfg = load_config()
    d = provider_def(pid, cfg)
    if not d or not is_configured(pid, cfg):
        raise LoginFailed("not_configured")
    state, verifier, bind = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    with LOCK:
        now = time.time()
        for k in [k for k, v in _pending.items() if v["exp"] < now]:
            del _pending[k]
        _pending[state] = {"provider": pid, "verifier": verifier, "bind": bind, "next": safe_next(next_url), "link": link_user,
                           "exp": now + PENDING_SECONDS}
    params = {"client_id": cfg["providers"][pid]["client_id"], "redirect_uri": f"{redirect_base}/auth/callback/{pid}",
              "response_type": "code", "scope": d["scope"], "state": state,
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    if pid in ("google", "microsoft"):
        params["prompt"] = "select_account"
    sep = "&" if "?" in d["authorize"] else "?"
    return d["authorize"] + sep + urllib.parse.urlencode(params), bind


def finish(pid, code, state, bind_cookie, redirect_base, current_user_id=None):
    """Termina o login: confere o state, troca o código pelo token, lê o perfil e cria a sessão.
    No modo "conectar" não cria sessão: liga o login novo à conta que já está logada (o token vem None)."""
    with LOCK:
        pend = _pending.pop(state or "", None)  # só vale uma vez
    if not pend or pend["provider"] != pid or pend["exp"] < time.time():
        raise LoginFailed("state")
    if not bind_cookie or not hmac.compare_digest(pend["bind"], bind_cookie):
        raise LoginFailed("state")  # o login foi iniciado em outro navegador
    cfg = load_config()
    d = provider_def(pid, cfg)
    if not d or not is_configured(pid, cfg) or not code:
        raise LoginFailed("not_configured")
    c = cfg["providers"][pid]
    tokens = _http(d["token"], form={"grant_type": "authorization_code", "code": code, "client_id": c["client_id"],
                                     "client_secret": c["client_secret"], "redirect_uri": f"{redirect_base}/auth/callback/{pid}",
                                     "code_verifier": pend["verifier"]})
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not access:
        raise LoginFailed("provider_error")
    profile = _profile(d, access)
    if not profile["sub"]:
        raise LoginFailed("profile")
    if pend.get("link"):
        if current_user_id != pend["link"]:
            raise LoginFailed("link_session")  # a pessoa trocou de conta no meio do caminho
        user = link_identity(_users.get(pend["link"]), pid, profile)
        return user, None, pend["next"]
    user = _upsert_user(pid, profile)
    return user, new_session(user["id"]), pend["next"]


# ---------------------------------------------------------------- usuários e sessões

def _upsert_user(pid, profile):
    """A conta é identificada por (provedor, id no provedor), nunca pelo e-mail: e-mail não verificado não vira acesso."""
    with LOCK:
        user = next((u for u in _users.values() if (pid, profile["sub"]) in identities(u)), None)
        fresh = user is None
        if fresh:
            user = {"id": uuid.uuid4().hex[:12], "provider": pid, "sub": profile["sub"], "admin": not _users,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _users[user["id"]] = user
        if not user.get("username"):  # depois de escolher o perfil, o nome é da pessoa (o serviço não o sobrescreve mais)
            user["name"] = (profile.get("name") or "Sem nome")[:80]
        if not user.get("emailCustom"):  # se a pessoa trocou o e-mail aqui, o do serviço não o sobrescreve mais
            user["email"] = (profile.get("email") or "")[:120]
        user.update(avatar=profile.get("avatar") or "", lastLogin=time.strftime("%Y-%m-%dT%H:%M:%S"))
        _write(USERS_FILE, list(_users.values()))
    if fresh and user["admin"] and on_first_user:
        on_first_user(user)  # os servidores que já existiam passam a ser dela
    return user


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def link_identity(user, pid, profile):
    """Liga mais um login (Google, Discord…) a uma conta. Se aquele login já é de outra conta, não liga: são duas contas
    e quem decide juntar é a administradora (mesclar)."""
    if not user:
        raise LoginFailed("link_session")
    with LOCK:
        for u in _users.values():
            if (pid, profile["sub"]) in identities(u):
                if u is user:
                    return user  # já estava ligado a esta mesma conta
                raise LoginFailed("already_linked")
        if pid in [p for p, _ in identities(user)]:
            raise LoginFailed("already_connected")  # esta conta já tem um login desse serviço
        user.setdefault("links", []).append({"provider": pid, "sub": profile["sub"]})
        if not user.get("avatar") and profile.get("avatar"):
            user["avatar"] = profile["avatar"]
        _write(USERS_FILE, list(_users.values()))
    return user


def unlink_provider(user, pid):
    """Tira um login da conta, desde que sobre pelo menos uma forma de entrar."""
    with LOCK:
        ids = identities(user)
        if pid == "password":
            raise ContentError(400, "A senha não se desconecta por aqui. Ela fica na aba Segurança.")
        if pid not in [p for p, _ in ids]:
            raise ContentError(404, "Esse login não está conectado a esta conta.")
        remaining = [(p, s) for p, s in ids if p != pid]
        if not remaining and not user.get("pw"):
            raise ContentError(409, "Você precisa manter pelo menos uma forma de entrar.")
        if not remaining:  # só sobra a senha: ela vira o login principal da conta
            remaining = [("password", pw_email(user))]
        user["provider"], user["sub"] = remaining[0]
        user["links"] = [{"provider": p, "sub": s} for p, s in remaining[1:]]
        _write(USERS_FILE, list(_users.values()))
    return user


def identities(user):
    """Todas as formas de entrar nesta conta: (serviço, id no serviço). Contas mescladas têm mais de uma."""
    return [(user["provider"], user["sub"])] + [(i["provider"], i["sub"]) for i in user.get("links", [])]


def pw_email(user):
    """O e-mail usado para entrar com senha nesta conta (ou None se ela não tem senha)."""
    if not user.get("pw"):
        return None
    return user.get("pwEmail") or (user["sub"] if user["provider"] == "password" else None)


def merge_users(keep_id, drop_id, name=None, username=None):
    """Junta duas contas numa só: `drop` deixa de existir e todos os seus logins passam a entrar em `keep`."""
    with LOCK:
        keep, drop = _users.get(keep_id), _users.get(drop_id)
        if not keep or not drop:
            raise ContentError(404, "Conta não encontrada.")
        if keep is drop:
            raise ContentError(400, "Escolha duas contas diferentes.")
        if keep.get("pw") and drop.get("pw"):
            raise ContentError(409, "As duas contas têm senha de e-mail. Só uma pode ficar.")
        have = identities(keep)
        links = keep.setdefault("links", [])
        for prov, sub in identities(drop):
            if (prov, sub) not in have:
                links.append({"provider": prov, "sub": sub})
        if drop.get("pw"):
            keep["pw"], keep["pwEmail"] = drop["pw"], pw_email(drop)
        if not keep.get("username") and drop.get("username"):  # o perfil completo é o que vale
            keep["username"], keep["name"] = drop["username"], drop["name"]
        if name:
            keep["name"] = name
        if username:
            other = find_by_username(username)
            if other and other is not keep and other is not drop:
                raise ContentError(409, "Este nome de usuário já está em uso. Escolha outro.")
            keep["username"] = username
        keep["admin"] = bool(keep.get("admin") or drop.get("admin"))
        keep["avatar"] = keep.get("avatar") or drop.get("avatar", "")
        keep["email"] = keep.get("email") or drop.get("email", "")
        for s in _sessions.values():  # quem estava logado na conta antiga continua logado, agora na conta única
            if s["user"] == drop_id:
                s["user"] = keep_id
        del _users[drop_id]
        _write(USERS_FILE, list(_users.values()))
        _write(SESSIONS_FILE, _sessions)
        return keep


def create_user(fields):
    """Conta nova (cadastro com e-mail). A primeira conta do sistema é a administradora e fica com os servidores antigos."""
    with LOCK:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        user = {"id": uuid.uuid4().hex[:12], "admin": not _users, "created": now, "avatar": "", **fields}
        _users[user["id"]] = user
        _write(USERS_FILE, list(_users.values()))
    if user["admin"] and on_first_user:
        on_first_user(user)
    return user


def get_user(uid):
    with LOCK:
        return _users.get(uid)


def find_by_username(username):
    """Nomes de usuário são únicos sem diferenciar maiúsculas de minúsculas."""
    low = str(username).lower()
    with LOCK:
        return next((u for u in _users.values() if (u.get("username") or "").lower() == low), None)


def update_user(user, **fields):
    with LOCK:
        user.update(fields)
        _write(USERS_FILE, list(_users.values()))


def touch_login(user):
    with LOCK:
        user["lastLogin"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write(USERS_FILE, list(_users.values()))


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    with LOCK:
        now = time.time()
        for k in [k for k, v in _sessions.items() if v["exp"] < now]:
            del _sessions[k]
        _sessions[_hash(token)] = {"user": user_id, "exp": now + SESSION_SECONDS}
        _write(SESSIONS_FILE, _sessions)
    return token


def _cookie_value(header, name):
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
        return jar[name].value if name in jar else None
    except Exception:
        return None


def user_from_cookie(header):
    token = _cookie_value(header, SESSION_COOKIE)
    if not token:
        return None
    with LOCK:
        s = _sessions.get(_hash(token))
        if not s or s["exp"] < time.time():
            return None
        return _users.get(s["user"])


def bind_from_cookie(header):
    return _cookie_value(header, BIND_COOKIE)


def logout(header):
    token = _cookie_value(header, SESSION_COOKIE)
    if token:
        with LOCK:
            if _sessions.pop(_hash(token), None) is not None:
                _write(SESSIONS_FILE, _sessions)


def self_user(user):
    """A conta vista por ela mesma: o que os outros não precisam saber (se tem senha)."""
    import sms  # aqui para não criar ciclo
    return {**public_user(user), "hasPassword": bool(user.get("pw")), "hasPhone": bool(user.get("phone")),
            "phoneMask": sms.mask_phone(user["phone"]) if user.get("phone") else "",
            "hasTelegram": bool(user.get("telegram")), "telegramName": (user.get("telegram") or {}).get("name", "")}


def all_users():
    """Todas as contas, com o último login e quantas sessões ainda valem. Só para o painel do administrador."""
    now = time.time()
    with LOCK:
        open_sessions = {}
        for s in _sessions.values():
            if s["exp"] > now:
                open_sessions[s["user"]] = open_sessions.get(s["user"], 0) + 1
        return [{**public_user(u), "created": u.get("created"), "lastLogin": u.get("lastLogin"), "sessions": open_sessions.get(u["id"], 0)}
                for u in _users.values()]


def image_file(kind, user_id):
    """Onde ficam a foto (kind='avatar') e a imagem do banner (kind='banner') de uma conta, se existirem."""
    folder = AVATARS if kind == "avatar" else BANNERS
    for ext in ("png", "jpg"):
        p = folder / f"{user_id}.{ext}"
        if p.is_file():
            return p
    return None


def save_image(kind, user, data, image_kind):
    folder = AVATARS if kind == "avatar" else BANNERS
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{user['id']}.*"):
        old.unlink(missing_ok=True)
    ext = "png" if image_kind == "png" else "jpg"
    (folder / f"{user['id']}.{ext}").write_bytes(data)
    update_user(user, **{("avatarV" if kind == "avatar" else "bannerImgV"): int(time.time())})


def delete_image(kind, user):
    folder = AVATARS if kind == "avatar" else BANNERS
    if folder.is_dir():
        for old in folder.glob(f"{user['id']}.*"):
            old.unlink(missing_ok=True)
    fields = {"avatarV": None} if kind == "avatar" else {"bannerImgV": None}
    update_user(user, **fields)


def avatar_url(user):
    """A foto que aparece para os outros: a que a pessoa enviou, ou a do serviço (Google…)."""
    if user.get("avatarV"):
        return f"/api/users/{user['id']}/avatar?v={user['avatarV']}"
    return user.get("avatar", "")


def banner_of(user):
    """{"preset": id} ou {"image": url}. O padrão é o verde com os ícones do AethelHost."""
    if user.get("bannerImgV"):
        return {"image": f"/api/users/{user['id']}/banner?v={user['bannerImgV']}"}
    preset = user.get("bannerPreset")
    return {"preset": preset if preset in BANNER_PRESETS else BANNER_PRESETS[0]}


def set_password(user, pw_hash, login_email=None):
    with LOCK:
        user["pw"] = pw_hash
        if login_email and not pw_email(user):
            user["pwEmail"] = login_email
        _write(USERS_FILE, list(_users.values()))


def set_phone(user, phone):
    """Celular de recuperação (só depois de confirmado por SMS). None remove."""
    with LOCK:
        if phone:
            user["phone"] = phone
        else:
            user.pop("phone", None)
        _write(USERS_FILE, list(_users.values()))


def set_telegram(user, chat, name):
    """Liga (ou, com chat=None, desliga) o Telegram usado para receber códigos."""
    with LOCK:
        if chat:
            user["telegram"] = {"chat": chat, "name": name, "at": time.time()}
        else:
            user.pop("telegram", None)
        _write(USERS_FILE, list(_users.values()))


def logout_all(user_id):
    """Encerra TODAS as sessões da conta (depois de recuperar a senha)."""
    with LOCK:
        gone = [k for k, s in _sessions.items() if s["user"] == user_id]
        for k in gone:
            del _sessions[k]
        if gone:
            _write(SESSIONS_FILE, _sessions)


def set_email(user, new_email):
    """Troca o e-mail da conta e o de entrar com senha (se ela tem senha)."""
    with LOCK:
        if user["provider"] == "password":
            user["sub"] = new_email
        elif user.get("pw"):
            user["pwEmail"] = new_email
        for link in user.get("links", []):
            if link["provider"] == "password":
                link["sub"] = new_email
        user["email"], user["emailCustom"] = new_email, True
        _write(USERS_FILE, list(_users.values()))


def logout_others(user_id, header):
    """Encerra todas as sessões da conta, menos a que fez o pedido (usado depois de trocar a senha)."""
    keep = _hash(_cookie_value(header, SESSION_COOKIE) or "")
    with LOCK:
        gone = [k for k, s in _sessions.items() if s["user"] == user_id and k != keep]
        for k in gone:
            del _sessions[k]
        if gone:
            _write(SESSIONS_FILE, _sessions)
    return len(gone)


def public_user(user):
    methods = []  # formas de entrar (sem repetir): google, password…
    for prov, _ in identities(user):
        if prov not in methods:
            methods.append(prov)
    if user.get("pw") and "password" not in methods:
        methods.append("password")  # quem criou uma senha na aba Segurança também entra por e-mail e senha
    return {"id": user["id"], "name": user["name"], "username": user.get("username") or "", "email": user["email"],
            "avatar": avatar_url(user), "banner": banner_of(user), "prefs": user.get("prefs") or {},
            "provider": user["provider"], "methods": methods, "created": user.get("created"),
            "admin": bool(user.get("admin")),
            "needsProfile": not user.get("username")}  # conta criada por um serviço: falta escolher nome exibido e usuário


def session_cookie(token):
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}"


def bind_cookie(value):
    return f"{BIND_COOKIE}={value}; Path=/auth; HttpOnly; SameSite=Lax; Max-Age={PENDING_SECONDS}"


CLEAR_SESSION = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
CLEAR_BIND = f"{BIND_COOKIE}=; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=0"
