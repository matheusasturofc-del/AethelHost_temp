"""Captcha (Cloudflare Turnstile) na criação de conta e de servidor. Configuração em data/captcha.json."""
import json

import net
from config import DATA
from content import ContentError

CAPTCHA_FILE = DATA / "captcha.json"
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def load():
    try:
        return json.loads(CAPTCHA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_configured():
    c = load()
    return bool(c.get("siteKey") and c.get("secretKey"))


def config_view():
    """O que a tela de configuração (admin) pode ver: a chave do site é pública, a secreta nunca volta."""
    c = load()
    return {"siteKey": c.get("siteKey", ""), "hasSecret": bool(c.get("secretKey")), "configured": is_configured()}


def public_config():
    """O que qualquer visitante pode ver: só a chave do site, para desenhar o widget (ou nada, se não estiver ligado)."""
    c = load()
    return {"siteKey": c.get("siteKey", "")} if is_configured() else {"siteKey": ""}


def save(body):
    if body.get("clear"):
        CAPTCHA_FILE.unlink(missing_ok=True)
        return config_view()
    old = load()
    site_key = str(body.get("siteKey", "")).strip()[:200]
    secret_key = str(body.get("secretKey", "")).strip()[:200]
    secret_key = secret_key or (old.get("secretKey", "") if old.get("siteKey") == site_key else "")
    if not site_key or not secret_key:
        raise ContentError(400, "Informe a chave do site e a chave secreta do Cloudflare Turnstile.")
    CAPTCHA_FILE.parent.mkdir(parents=True, exist_ok=True)
    CAPTCHA_FILE.write_text(json.dumps({"siteKey": site_key, "secretKey": secret_key}), encoding="utf-8")
    return config_view()


def check(token):
    """Confere o token do widget com a Cloudflare. True se passou (ou se o captcha nem está ligado)."""
    secret = load().get("secretKey")
    if not secret:
        return True
    token = str(token or "").strip()
    if not token:
        return False
    try:
        data = net.post_form(VERIFY_URL, {"secret": secret, "response": token}, timeout=10)
    except Exception:
        return False
    return bool(data.get("success"))


def require(token):
    """Usa em rotas que criam conta ou servidor. Não faz nada se o captcha não estiver configurado."""
    if not check(token):
        raise ContentError(400, "Confirme que você não é um robô e tente de novo.")
