"""Contas: entrar com Google, Microsoft, GitHub, Discord ou outro provedor OpenID Connect.

Usa o fluxo "Authorization Code" com PKCE, um `state` amarrado ao navegador (contra login forjado) e sessões
guardadas só como hash. O BlockHost nunca vê senha: quem autentica é o provedor.
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
NEXT_RE = re.compile(r"^/(?:servers|create|panel|index)\.html(?:\?[A-Za-z0-9=&%._-]{0,200})?$")


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
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", "User-Agent": "BlockHost/0.4", **(headers or {})})
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


def begin(pid, redirect_base, next_url):
    """Começa o login: devolve (endereço do provedor, cookie que amarra este navegador ao login)."""
    cfg = load_config()
    d = provider_def(pid, cfg)
    if not d or not is_configured(pid, cfg):
        raise LoginFailed("not_configured")
    state, verifier, bind = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    with LOCK:
        now = time.time()
        for k in [k for k, v in _pending.items() if v["exp"] < now]:
            del _pending[k]
        _pending[state] = {"provider": pid, "verifier": verifier, "bind": bind, "next": safe_next(next_url), "exp": now + PENDING_SECONDS}
    params = {"client_id": cfg["providers"][pid]["client_id"], "redirect_uri": f"{redirect_base}/auth/callback/{pid}",
              "response_type": "code", "scope": d["scope"], "state": state,
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    if pid in ("google", "microsoft"):
        params["prompt"] = "select_account"
    sep = "&" if "?" in d["authorize"] else "?"
    return d["authorize"] + sep + urllib.parse.urlencode(params), bind


def finish(pid, code, state, bind_cookie, redirect_base):
    """Termina o login: confere o state, troca o código pelo token, lê o perfil e cria a sessão."""
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
    user = _upsert_user(pid, profile)
    return user, _new_session(user["id"]), pend["next"]


# ---------------------------------------------------------------- usuários e sessões

def _upsert_user(pid, profile):
    """A conta é identificada por (provedor, id no provedor), nunca pelo e-mail: e-mail não verificado não vira acesso."""
    with LOCK:
        user = next((u for u in _users.values() if u["provider"] == pid and u["sub"] == profile["sub"]), None)
        fresh = user is None
        if fresh:
            user = {"id": uuid.uuid4().hex[:12], "provider": pid, "sub": profile["sub"], "admin": not _users,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _users[user["id"]] = user
        user.update(name=(profile.get("name") or "Sem nome")[:80], email=(profile.get("email") or "")[:120], avatar=profile.get("avatar") or "")
        _write(USERS_FILE, list(_users.values()))
    if fresh and user["admin"] and on_first_user:
        on_first_user(user)  # os servidores que já existiam passam a ser dela
    return user


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(user_id):
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


def public_user(user):
    return {"id": user["id"], "name": user["name"], "email": user["email"], "avatar": user["avatar"],
            "provider": user["provider"], "admin": bool(user.get("admin"))}


def session_cookie(token):
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}"


def bind_cookie(value):
    return f"{BIND_COOKIE}={value}; Path=/auth; HttpOnly; SameSite=Lax; Max-Age={PENDING_SECONDS}"


CLEAR_SESSION = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
CLEAR_BIND = f"{BIND_COOKIE}=; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=0"
