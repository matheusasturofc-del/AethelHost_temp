"""Envio de e-mail (SMTP) para os códigos de confirmação. A configuração fica em data/mail.json."""
import json
import os
import re
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from config import DATA
from content import ContentError

MAIL_FILE = DATA / "mail.json"
SITE_NAME = "BlockHost"
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
    password = str(body.get("password", ""))[:300] or (old.get("password", "") if old.get("username") == username else "")
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

def send(to, subject, text):
    cfg = load()
    if not is_configured():
        raise ContentError(503, "O envio de e-mail ainda não foi configurado pelo administrador.")
    msg = EmailMessage()
    msg["From"] = formataddr((SITE_NAME, cfg["from"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["from"].split("@")[1])
    msg.set_content(text)
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
    if en:
        why = "to finish creating your account" if purpose == "register" else "to sign in"
        return (f"{code} is your {SITE_NAME} code",
                f"Your {SITE_NAME} verification code {why} is:\n\n    {code}\n\nIt expires in 10 minutes. "
                f"If it wasn't you, ignore this email and don't share the code with anyone.")
    why = "para terminar de criar a sua conta" if purpose == "register" else "para entrar"
    return (f"{code} é o seu código do {SITE_NAME}",
            f"O seu código de verificação do {SITE_NAME} {why} é:\n\n    {code}\n\nEle vale por 10 minutos. "
            f"Se não foi você, ignore este e-mail e não passe o código para ninguém.")
