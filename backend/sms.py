"""SMS com os códigos de 6 números (recuperar a senha e cadastrar o celular). Configuração em data/sms.json.

Serviços: **Twilio** (o único que envia de verdade; precisa de uma conta, do Account SID, do Auth Token e de um número
ou Messaging Service) e **teste**, que não envia nada: só grava o código em data/sms_outbox.jsonl e no terminal
(serve para experimentar no seu PC)."""
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import DATA
from errors import ContentError

SMS_FILE = DATA / "sms.json"
OUTBOX_FILE = DATA / "sms_outbox.jsonl"
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
LOCK = threading.RLock()
PROVIDERS = ("twilio", "test")


def normalize_phone(text):
    """Número no formato internacional (+5511912345678), ou None. Sem o +, 10 ou 11 números viram Brasil (+55)."""
    raw = re.sub(r"[\s().\-]", "", str(text or ""))
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not raw.startswith("+") and raw.isdigit() and len(raw) in (10, 11):
        raw = "+55" + raw
    return raw if PHONE_RE.match(raw) else None


def mask_phone(phone):
    phone = str(phone or "")
    return f"{phone[:3]}{'*' * max(len(phone) - 7, 3)}{phone[-4:]}" if len(phone) >= 8 else "***"


def load():
    try:
        return json.loads(SMS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_configured():
    c = load()
    if c.get("provider") == "test":
        return True
    return bool(c.get("provider") == "twilio" and c.get("sid") and c.get("token") and c.get("from"))


def config_view():
    """O que a tela do administrador vê: nunca o Auth Token."""
    c = load()
    return {"provider": c.get("provider", ""), "sid": c.get("sid", ""), "from": c.get("from", ""),
            "hasToken": bool(c.get("token")), "configured": is_configured()}


def save(body):
    if body.get("clear"):
        SMS_FILE.unlink(missing_ok=True)
        return config_view()
    provider = str(body.get("provider", ""))
    if provider not in PROVIDERS:
        raise ContentError(400, "Escolha o serviço de SMS.")
    if provider == "test":
        data = {"provider": "test"}
    else:
        old = load()
        sid = str(body.get("sid", "")).strip()
        token = str(body.get("token", "")).strip() or (old.get("token", "") if old.get("sid") == sid else "")
        sender = re.sub(r"\s", "", str(body.get("from", "")))
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
            raise ContentError(400, "O Account SID da Twilio começa com AC e tem 34 caracteres.")
        if not token:
            raise ContentError(400, "Informe o Auth Token da Twilio.")
        if not (PHONE_RE.match(sender) or re.fullmatch(r"MG[0-9a-fA-F]{32}", sender)):
            raise ContentError(400, "O remetente é o número da Twilio no formato +15551234567 (ou o SID de um Messaging Service, que começa com MG).")
        data = {"provider": "twilio", "sid": sid, "token": token, "from": sender}
    SMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SMS_FILE.with_name(SMS_FILE.name + ".part")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # tem o Auth Token: só o dono do arquivo lê (no Windows é ignorado)
    except OSError:
        pass
    os.replace(tmp, SMS_FILE)
    return config_view()


def _base():
    """Endereço da Twilio. AETHELHOST_SMS_TEST_BASE (só http://127.0.0.1…) troca por uma Twilio de mentira nos testes."""
    test = os.environ.get("AETHELHOST_SMS_TEST_BASE", "")
    if test and urllib.parse.urlparse(test).hostname in ("127.0.0.1", "localhost"):
        return test
    return "https://api.twilio.com"


TWILIO_ERRORS = {
    "21211": "O número de celular parece inválido para o serviço de SMS.",
    "21608": "Esse número não está verificado na sua conta de teste da Twilio.",
    "21614": "Esse número não pode receber SMS.",
    "21408": "A sua conta da Twilio não pode enviar SMS para esse país.",
    "20003": "A Twilio recusou o login (confira o Account SID e o Auth Token).",
}


def send(to, text):
    """Envia o SMS pelo serviço configurado."""
    c = load()
    if not is_configured():
        raise ContentError(503, "O envio de SMS ainda não foi configurado pelo administrador.")
    if c["provider"] == "test":
        with LOCK:
            OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"to": to, "text": text, "at": int(time.time())}, ensure_ascii=False) + "\n")
        print(f"[SMS de teste] para {to}: {text}")
        return
    form = {"To": to, "Body": text}
    form["MessagingServiceSid" if c["from"].startswith("MG") else "From"] = c["from"]
    auth = base64.b64encode(f"{c['sid']}:{c['token']}".encode()).decode()
    req = urllib.request.Request(f"{_base()}/2010-04-01/Accounts/{c['sid']}/Messages.json", data=urllib.parse.urlencode(form).encode(),
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": "AethelHost/0.5"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read(10000)
    except urllib.error.HTTPError as e:
        try:
            code = str(json.loads(e.read(20000)).get("code", ""))
        except ValueError:
            code = ""
        raise ContentError(502, TWILIO_ERRORS.get(code, "O serviço de SMS recusou o envio (confira o número, o saldo e a configuração).")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ContentError(502, "Não consegui falar com o serviço de SMS. Confira a internet.") from None


def code_text(lang, purpose, code):
    """Texto curto (cabe em 1 SMS) com o código."""
    en = lang == "en"
    if purpose == "reset":
        return (f"AethelHost: {code} is your code to reset your password. It expires in 10 min. If it wasn't you, ignore this."
                if en else f"AethelHost: {code} é o seu código para redefinir a senha. Vale por 10 min. Se não foi você, ignore.")
    return (f"AethelHost: {code} is your code to confirm this phone number. It expires in 10 min."
            if en else f"AethelHost: {code} é o seu código para confirmar este celular. Vale por 10 min.")
