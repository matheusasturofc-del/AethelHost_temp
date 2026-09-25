"""Google Drive para backups: cada conta vincula o próprio Drive e envia backups para a pasta "AethelHost Backups".

Usa o mesmo aplicativo do login com Google (ID e chave secreta já configurados) e só o acesso `drive.file`: o AethelHost
enxerga apenas os arquivos que ele mesmo criou, nunca o resto do Drive. O refresh token fica em data/drive.json."""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import auth
from config import DATA
from errors import ContentError

DRIVE_FILE = DATA / "drive.json"
LOCK = threading.RLock()
SCOPE = "openid email https://www.googleapis.com/auth/drive.file"
FOLDER_NAME = "AethelHost Backups"
CHUNK = 8 * 1024 * 1024  # múltiplo de 256 KiB, como o Drive exige
PENDING_SECONDS = 600
HOSTS = {"oauth2.googleapis.com", "openidconnect.googleapis.com", "www.googleapis.com"}

_pending = {}  # state -> pedido de vínculo em andamento
_tokens = {}  # conta -> (access token, expira em)
JOBS = {}  # (conta, "servidor/backup") -> andamento do envio


def endpoints():
    """Endereços do Google. AETHELHOST_DRIVE_TEST_BASE (só http://127.0.0.1…) troca por um Google de mentira nos testes."""
    base = os.environ.get("AETHELHOST_DRIVE_TEST_BASE", "")
    if base and urllib.parse.urlparse(base).hostname in ("127.0.0.1", "localhost"):
        return {"authorize": base + "/authorize", "token": base + "/token", "userinfo": base + "/userinfo",
                "api": base + "/drive/v3", "upload": base + "/upload/drive/v3", "revoke": base + "/revoke", "test": True}
    return {"authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
            "userinfo": "https://openidconnect.googleapis.com/v1/userinfo", "api": "https://www.googleapis.com/drive/v3",
            "upload": "https://www.googleapis.com/upload/drive/v3", "revoke": "https://oauth2.googleapis.com/revoke", "test": False}


def _client():
    return (auth.load_config().get("providers") or {}).get("google") or {}


def configured():
    c = _client()
    return bool(c.get("client_id") and c.get("client_secret"))


# ---------------------------------------------------------------- armazenamento

def _load():
    try:
        return json.loads(DRIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data):
    DRIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DRIVE_FILE.with_name(DRIVE_FILE.name + ".part")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # tem o refresh token: só o dono do arquivo lê (no Windows é ignorado)
    except OSError:
        pass
    os.replace(tmp, DRIVE_FILE)


def entry(user_id):
    return _load().get(user_id)


def linked(user_id):
    return bool(entry(user_id))


def status(user_id, redirect_base=None):
    e = entry(user_id) or {}
    out = {"available": configured(), "linked": bool(e), "email": e.get("email", ""), "auto": bool(e.get("auto"))}
    if redirect_base:
        out["redirectUri"] = f"{redirect_base}/auth/drive/callback"
    return out


def set_auto(user_id, value):
    with LOCK:
        data = _load()
        if user_id not in data:
            raise ContentError(409, "Vincule o Google Drive primeiro.")
        data[user_id]["auto"] = bool(value)
        _save(data)


# ---------------------------------------------------------------- HTTP

def _check(url):
    u = urllib.parse.urlparse(url)
    if endpoints()["test"]:
        ok = u.hostname in ("127.0.0.1", "localhost")
    else:
        ok = u.scheme == "https" and u.hostname in HOSTS
    if not ok:
        raise ContentError(502, "Endereço do Google inesperado.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 308 "continue enviando" do upload não é um redirecionamento; e o Google não nos manda para outro lugar


_opener = urllib.request.build_opener(_NoRedirect)


def _http(method, url, headers=None, data=None, form=None, timeout=60):
    """(código, cabeçalhos, corpo em bytes). Erros de rede viram ContentError 502; códigos HTTP voltam para quem chamou."""
    _check(url)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "AethelHost/0.5", **(headers or {})})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ContentError(502, "Não consegui falar com o Google. Confira a internet.")


def _json(body):
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}


# ---------------------------------------------------------------- vincular (OAuth2 com PKCE)

def begin(user_id, redirect_base, server_id):
    c = _client()
    if not configured():
        raise ContentError(409, "O administrador ainda não configurou o login do Google.")
    state, verifier, bind = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    with LOCK:
        now = time.time()
        for k in [k for k, v in _pending.items() if v["exp"] < now]:
            del _pending[k]
        _pending[state] = {"user": user_id, "verifier": verifier, "bind": bind, "server": server_id, "exp": now + PENDING_SECONDS}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": c["client_id"], "redirect_uri": f"{redirect_base}/auth/drive/callback", "response_type": "code",
              "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
              "access_type": "offline", "prompt": "consent"}  # offline + consent: garante o refresh token
    return endpoints()["authorize"] + "?" + urllib.parse.urlencode(params), bind


class DriveLinkFailed(Exception):
    def __init__(self, code, server_id=""):
        super().__init__(code)
        self.code, self.server = code, server_id


def server_of(state):
    """Servidor de onde saiu um pedido em andamento (para voltar a ele mesmo se o Google devolver um erro)."""
    with LOCK:
        pend = _pending.pop(state or "", None)
    return (pend or {}).get("server", "")


def finish(code, state, bind_cookie, redirect_base, user_id):
    """Troca o código pelo refresh token e guarda. Devolve o id do servidor para onde voltar."""
    with LOCK:
        pend = _pending.pop(state or "", None)  # só vale uma vez
    if not pend or pend["exp"] < time.time() or pend["user"] != user_id:
        raise DriveLinkFailed("state")
    if not bind_cookie or not secrets.compare_digest(pend["bind"], bind_cookie):
        raise DriveLinkFailed("state", pend["server"])
    c = _client()
    if not code or not configured():
        raise DriveLinkFailed("not_configured", pend["server"])
    ep = endpoints()
    st, _, body = _http("POST", ep["token"], form={
        "grant_type": "authorization_code", "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": f"{redirect_base}/auth/drive/callback", "code_verifier": pend["verifier"]})
    tokens = _json(body)
    if st != 200 or not tokens.get("access_token"):
        raise DriveLinkFailed("provider_error", pend["server"])
    if not tokens.get("refresh_token"):
        raise DriveLinkFailed("no_refresh", pend["server"])
    granted = str(tokens.get("scope", ""))
    if granted and "drive.file" not in granted:
        raise DriveLinkFailed("no_scope", pend["server"])  # a pessoa desmarcou a permissão do Drive
    st, _, body = _http("GET", ep["userinfo"], headers={"Authorization": f"Bearer {tokens['access_token']}"})
    email = _json(body).get("email", "") if st == 200 else ""
    with LOCK:
        data = _load()
        old = data.get(user_id) or {}
        data[user_id] = {"refresh": tokens["refresh_token"], "email": email[:120], "folder": "", "auto": bool(old.get("auto")),
                         "linked": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save(data)
        _tokens[user_id] = (tokens["access_token"], time.time() + int(tokens.get("expires_in", 3600)) - 60)
    return pend["server"]


def unlink(user_id):
    with LOCK:
        data = _load()
        e = data.pop(user_id, None)
        _save(data)
        _tokens.pop(user_id, None)
    if e:
        try:  # avisa o Google para invalidar o token (se falhar, tudo bem: já esquecemos dele aqui)
            _http("POST", endpoints()["revoke"], form={"token": e["refresh"]}, timeout=10)
        except ContentError:
            pass


# ---------------------------------------------------------------- token de acesso

def _access(user_id):
    e = entry(user_id)
    if not e:
        raise ContentError(409, "Vincule o Google Drive primeiro.")
    with LOCK:
        cached = _tokens.get(user_id)
        if cached and cached[1] > time.time():
            return cached[0]
    c = _client()
    st, _, body = _http("POST", endpoints()["token"], form={
        "grant_type": "refresh_token", "refresh_token": e["refresh"], "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", "")})
    tokens = _json(body)
    if st in (400, 401) and tokens.get("error") == "invalid_grant":
        unlink(user_id)  # o Google cancelou a permissão (ou o token expirou): precisa vincular de novo
        raise ContentError(401, "A conexão com o Google Drive expirou ou foi removida. Vincule de novo.")
    if st != 200 or not tokens.get("access_token"):
        raise ContentError(502, "O Google não respondeu direito. Tente de novo em instantes.")
    with LOCK:
        _tokens[user_id] = (tokens["access_token"], time.time() + int(tokens.get("expires_in", 3600)) - 60)
    return tokens["access_token"]


def _api(user_id, method, path, *, query=None, body=None, base="api", headers=None, raw=None, timeout=60):
    url = endpoints()[base] + path + ("?" + urllib.parse.urlencode(query) if query else "")
    hdrs = {"Authorization": f"Bearer {_access(user_id)}", **(headers or {})}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json; charset=UTF-8"
    return _http(method, url, headers=hdrs, data=data, timeout=timeout)


# ---------------------------------------------------------------- pasta e arquivos

def _folder(user_id):
    with LOCK:
        data = _load()
        fid = (data.get(user_id) or {}).get("folder")
    if fid:
        st, _, body = _api(user_id, "GET", f"/files/{urllib.parse.quote(fid)}", query={"fields": "id,trashed"})
        if st == 200 and not _json(body).get("trashed"):
            return fid
    q = f"mimeType='application/vnd.google-apps.folder' and name='{FOLDER_NAME}' and trashed=false"
    st, _, body = _api(user_id, "GET", "/files", query={"q": q, "fields": "files(id)", "pageSize": 1})
    files = _json(body).get("files") or []
    if st == 200 and files:
        fid = files[0]["id"]
    else:
        st, _, body = _api(user_id, "POST", "/files", body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
                           query={"fields": "id"})
        fid = _json(body).get("id")
        if st not in (200, 201) or not fid:
            raise ContentError(502, "Não consegui criar a pasta no Google Drive.")
    with LOCK:
        data = _load()
        if user_id in data:
            data[user_id]["folder"] = fid
            _save(data)
    return fid


def list_files(user_id):
    """{nome: {id, size, url}} dos arquivos que o AethelHost guardou na pasta."""
    fid = _folder(user_id)
    st, _, body = _api(user_id, "GET", "/files", query={"q": f"'{fid}' in parents and trashed=false", "pageSize": 1000,
                                                       "fields": "files(id,name,size,webViewLink)"})
    if st != 200:
        raise ContentError(502, "Não consegui listar os arquivos do Google Drive.")
    return {f["name"]: {"id": f["id"], "size": int(f.get("size") or 0), "url": f.get("webViewLink", "")} for f in _json(body).get("files", [])}


def upload(user_id, path, name, progress=None):
    """Envia um arquivo grande em pedaços (upload retomável). Devolve {id, url}."""
    size = os.path.getsize(path)
    fid = _folder(user_id)
    st, hdrs, body = _api(user_id, "POST", "/files", base="upload", query={"uploadType": "resumable", "fields": "id,webViewLink"},
                          body={"name": name, "parents": [fid]},
                          headers={"X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(size)})
    location = next((v for k, v in hdrs.items() if k.lower() == "location"), "")
    if st != 200 or not location:
        raise ContentError(502, "O Google Drive recusou o envio (confira se há espaço no seu Drive).")
    sent = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            rng = f"bytes {sent}-{end}/{size}" if chunk else f"bytes */{size}"
            for attempt in (1, 2, 3):
                try:
                    st, _, body = _http("PUT", location, headers={"Content-Range": rng, "Content-Type": "application/octet-stream"},
                                        data=chunk, timeout=300)
                    if st < 500:
                        break
                except ContentError:
                    if attempt == 3:
                        raise
                time.sleep(2 * attempt)
            if st in (200, 201):
                info = _json(body)
                if progress:
                    progress(size, size)
                return {"id": info.get("id", ""), "url": info.get("webViewLink", "")}
            if st != 308:
                raise ContentError(502, "O envio para o Google Drive falhou no meio. Tente de novo.")
            sent += len(chunk)
            if progress:
                progress(sent, size)
            if not chunk:
                raise ContentError(502, "O envio para o Google Drive não terminou.")


def delete_remote(user_id, file_id):
    st, _, _ = _api(user_id, "DELETE", f"/files/{urllib.parse.quote(file_id)}")
    if st not in (200, 204, 404):
        raise ContentError(502, "Não consegui apagar o arquivo no Google Drive.")


# ---------------------------------------------------------------- envios em segundo plano

def job(user_id, key):
    return JOBS.get((user_id, key))


def start_upload(user_id, key, get_file, drive_name):
    """Envia em uma thread. `get_file()` devolve (caminho, apagar_depois)."""
    with LOCK:
        cur = JOBS.get((user_id, key))
        if cur and cur["state"] == "uploading":
            raise ContentError(409, "Esse backup já está sendo enviado.")
        JOBS[(user_id, key)] = {"state": "uploading", "sent": 0, "total": 0, "message": "", "url": ""}
    state = JOBS[(user_id, key)]

    def run():
        cleanup = None
        try:
            path, delete_after = get_file()
            cleanup = path if delete_after else None
            state["total"] = os.path.getsize(path)

            def progress(sent, total):
                state["sent"], state["total"] = sent, total

            r = upload(user_id, path, drive_name, progress)
            state.update(state="done", url=r["url"], sent=state["total"])
        except ContentError as e:
            state.update(state="error", message=e.message)
        except Exception as e:  # nada pode derrubar a thread sem avisar a tela
            state.update(state="error", message=f"Erro inesperado: {e}")
        finally:
            if cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()
