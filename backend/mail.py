"""Envio de e-mail (SMTP) para os códigos de confirmação. A configuração fica em data/mail.json."""
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import threading
from urllib.parse import urlencode

import mailtemplate
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from config import DATA
from content import ContentError

MAIL_FILE = DATA / "mail.json"
BLOCK_FILE = DATA / "blocked_emails.json"
SECRET_FILE = DATA / "mail_secret.key"
PUBLIC_URL = "http://127.0.0.1:8080"  # o server.py coloca aqui o endereço do site (usado no link de bloquear)
SITE_NAME = "AethelHost"
LOCK = threading.RLock()
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
SECURITY = ("starttls", "ssl", "none")


def normalize_email(text):
    """E-mail em minúsculas e sem espaços, ou None se não parecer um e-mail."""
    text = str(text or "").strip().lower()
    return text if len(text) <= 254 and EMAIL_RE.match(text) else None


def mask_email(email):
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"


# ---------------------------------------------------------------- configuração

def load():
    try:
        return json.loads(MAIL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_configured():
    c = load()
    return bool(c.get("host") and c.get("port") and c.get("from"))


def config_view():
    """O que a tela de configuração pode ver: nunca a senha."""
    c = load()
    return {"host": c.get("host", ""), "port": c.get("port", 587), "security": c.get("security", "starttls"),
            "username": c.get("username", ""), "from": c.get("from", ""), "hasPassword": bool(c.get("password")),
            "configured": is_configured()}


def save(body):
    if body.get("clear"):
        MAIL_FILE.unlink(missing_ok=True)
        return
    old = load()
    host = str(body.get("host", "")).strip()
    if not HOST_RE.match(host):
        raise ContentError(400, "Endereço do servidor de e-mail inválido.")
    try:
        port = int(body.get("port"))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise ContentError(400, "Porta inválida.")
    security = str(body.get("security", "starttls"))
    if security not in SECURITY:
        raise ContentError(400, "Segurança inválida.")
    if security == "none" and host not in LOOPBACK:
        raise ContentError(400, "Sem criptografia só é aceito para o seu próprio computador (testes).")
    username = str(body.get("username", "")).strip()[:200]
    password = str(body.get("password", ""))[:300]
    if host.lower().endswith(("gmail.com", "googlemail.com")) and password:
        # O Google mostra a senha de app em 4 grupos de 4 letras: os espaços saem, e ela precisa ter as 16 letras.
        password = re.sub(r"\s+", "", password)
        if not re.fullmatch(r"[A-Za-z]{16}", password):
            raise ContentError(400, "O Gmail só aceita a senha de app: 16 letras, que o Google mostra em 4 grupos de 4. Crie uma em myaccount.google.com/apppasswords e cole aqui (não use a senha normal da conta).")
    password = password or (old.get("password", "") if old.get("username") == username else "")
    sender = normalize_email(body.get("from") or username)
    if not sender:
        raise ContentError(400, "Informe o e-mail de quem envia (o remetente).")
    if username and not password:
        raise ContentError(400, "Informe também a senha do e-mail (no Gmail, use uma senha de app).")
    data = {"host": host, "port": port, "security": security, "username": username, "password": password, "from": sender}
    MAIL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAIL_FILE.with_name(MAIL_FILE.name + ".part")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # tem a senha do e-mail: só o dono do arquivo lê (no Windows é ignorado)
    except OSError:
        pass
    os.replace(tmp, MAIL_FILE)


# ---------------------------------------------------------------- envio

# ---------------------------------------------------------------- endereços que pediram para não receber e-mails

def _secret():
    """Chave que assina os links de bloquear (guardada em data/, criada na primeira vez)."""
    with LOCK:
        try:
            return bytes.fromhex(SECRET_FILE.read_text().strip())
        except (OSError, ValueError):
            key = secrets.token_bytes(32)
            SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
            SECRET_FILE.write_text(key.hex())
            try:
                os.chmod(SECRET_FILE, 0o600)
            except OSError:
                pass
            return key


def _sig(email):
    return hmac.new(_secret(), f"block:{email}".encode(), hashlib.sha256).hexdigest()[:40]


def check_token(email, token):
    return bool(email) and hmac.compare_digest(_sig(email), str(token or ""))


def block_url(email, lang="en"):
    return f"{PUBLIC_URL}/mail/block?" + urlencode({"e": email, "t": _sig(email), "l": "pt" if lang == "pt" else "en"})


def _blocked():
    try:
        return set(json.loads(BLOCK_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def is_blocked(email):
    return str(email).strip().lower() in _blocked()


def set_blocked(email, blocked):
    with LOCK:
        items = _blocked()
        (items.add if blocked else items.discard)(email.strip().lower())
        BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        BLOCK_FILE.write_text(json.dumps(sorted(items)), encoding="utf-8")


def code_email(lang, purpose, code, to):
    """(assunto, texto simples, HTML) do e-mail com o código de 6 números."""
    subject, _ = code_message(lang, purpose, code)
    text, html = mailtemplate.code_email(lang, purpose, code, block_url(to, lang))
    return subject, text, html


def send(to, subject, text, html=None):
    """Envia o e-mail. Devolve False (sem enviar nada) se o destino pediu para não receber e-mails."""
    cfg = load()
    if not is_configured():
        raise ContentError(503, "O envio de e-mail ainda não foi configurado pelo administrador.")
    if is_blocked(to):
        return False
    msg = EmailMessage()
    msg["From"] = formataddr((SITE_NAME, cfg["from"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["from"].split("@")[1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if cfg.get("security") == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        with smtp:
            smtp.ehlo()
            if cfg.get("security") == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if cfg.get("username"):
                smtp.login(cfg["username"], cfg["password"])
            smtp.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError:
        raise ContentError(502, "O servidor de e-mail recusou o login. Confira o usuário e a senha (no Gmail, use uma senha de app).") from None
    except smtplib.SMTPRecipientsRefused:
        raise ContentError(502, "O servidor de e-mail recusou o endereço de destino.") from None
    except (smtplib.SMTPException, ssl.SSLError, OSError):
        raise ContentError(502, "Não consegui enviar o e-mail. Confira o servidor, a porta e a internet.") from None


# ---------------------------------------------------------------- textos dos e-mails (pt e en)

def code_message(lang, purpose, code):
    """(assunto, texto) do e-mail com o código de 6 números."""
    en = lang == "en"
    if purpose == "exists":
        if en:
            return (f"{SITE_NAME}: you already have an account",
                    f"Someone (hopefully you) tried to create a {SITE_NAME} account with this email, but an account already exists.\n\n"
                    f"Just sign in with your email and password. If it wasn't you, ignore this message.")
        return (f"{SITE_NAME}: você já tem uma conta",
                f"Alguém (esperamos que você) tentou criar uma conta no {SITE_NAME} com este e-mail, mas ela já existe.\n\n"
                f"É só entrar com o seu e-mail e a sua senha. Se não foi você, ignore esta mensagem.")
    if purpose == "exists_change":
        if en:
            return (f"{SITE_NAME}: this email is already in use",
                    f"Someone tried to use this email on another {SITE_NAME} account, but it already belongs to an account.\n\n"
                    f"If it was you, nothing else is needed. If it wasn't, ignore this message.")
        return (f"{SITE_NAME}: este e-mail já está em uso",
                f"Alguém tentou usar este e-mail em outra conta do {SITE_NAME}, mas ele já pertence a uma conta.\n\n"
                f"Se foi você, não precisa fazer mais nada. Se não foi, ignore esta mensagem.")
    if en:
        why = {"register": "to finish creating your account", "profilepw": "to finish creating your account", "pwchange": "to change your password", "pwcreate": "to create your password",
               "emailchange": "to confirm your new email", "reset": "to reset your password"}.get(purpose, "to sign in")
        return (f"{code} is your {SITE_NAME} code",
                f"Your {SITE_NAME} verification code {why} is:\n\n    {code}\n\nIt expires in 10 minutes. "
                f"If it wasn't you, ignore this email and don't share the code with anyone.")
    why = {"register": "para terminar de criar a sua conta", "profilepw": "para terminar de criar a sua conta", "pwchange": "para trocar a sua senha", "pwcreate": "para criar a sua senha",
           "emailchange": "para confirmar o seu novo e-mail", "reset": "para redefinir a sua senha"}.get(purpose, "para entrar")
    return (f"{code} é o seu código do {SITE_NAME}",
            f"O seu código de verificação do {SITE_NAME} {why} é:\n\n    {code}\n\nEle vale por 10 minutos. "
            f"Se não foi você, ignore este e-mail e não passe o código para ninguém.")


def notice_message(lang, kind):
    """(assunto, texto) dos avisos depois de uma mudança na conta (sem código)."""
    en = lang == "en"
    texts = {
        "pw_changed": (("Your password was changed", "Your password on {site} was just changed. Your other devices were signed out.\n\nIf it wasn't you, sign in and change it again right away."),
                       ("Sua senha foi alterada", "A senha da sua conta no {site} acabou de ser alterada. Os seus outros aparelhos foram desconectados.\n\nSe não foi você, entre e troque a senha de novo agora mesmo.")),
        "pw_created": (("A password was added to your account", "A password was just added to your {site} account. You can now sign in with your email and password.\n\nIf it wasn't you, sign in and change it right away."),
                       ("Uma senha foi criada na sua conta", "Uma senha acabou de ser criada na sua conta do {site}. Agora você também pode entrar com o e-mail e a senha.\n\nSe não foi você, entre e troque a senha agora mesmo.")),
        "phone_changed": (("Your recovery phone was changed", "The recovery phone on your {site} account was just changed.\n\nIf it wasn't you, sign in and change your password right away."),
                          ("Seu celular de recuperação foi alterado", "O celular de recuperação da sua conta no {site} acabou de ser alterado.\n\nSe não foi você, entre e troque a senha agora mesmo.")),
        "phone_removed": (("Your recovery phone was removed", "The recovery phone was just removed from your {site} account.\n\nIf it wasn't you, sign in and change your password right away."),
                          ("Seu celular de recuperação foi removido", "O celular de recuperação acabou de ser removido da sua conta no {site}.\n\nSe não foi você, entre e troque a senha agora mesmo.")),
        "email_changed": (("Your email was changed", "The email on your {site} account was just changed to another address.\n\nIf it wasn't you, contact the administrator."),
                          ("Seu e-mail foi alterado", "O e-mail da sua conta no {site} acabou de ser trocado por outro endereço.\n\nSe não foi você, fale com o administrador.")),
    }
    en_pair, pt_pair = texts[kind]
    subject, body = en_pair if en else pt_pair
    return f"{SITE_NAME}: {subject}", body.format(site=SITE_NAME)
