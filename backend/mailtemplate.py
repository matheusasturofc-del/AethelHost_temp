"""O visual dos e-mails de verificação (HTML escuro + texto simples) e das páginas de "bloquear este endereço"."""
from html import escape

BG, CARD, BORDER, TEXT, MUTED, ACCENT, INPUT = "#0b0d11", "#171a21", "#2a2f3a", "#e8eaf0", "#98a0b3", "#4ade80", "#0f1115"
SANS = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "Consolas,'SFMono-Regular',Menlo,'Courier New',monospace"

# (título, subtítulo) de cada motivo, em inglês e português
COPY = {
    "en": {
        "register": ("Put this code to confirm your email for AethelHost!", "Enter this code below to confirm your email!"),
        "emailchange": ("Put this code to confirm your new email for AethelHost!", "Enter this code below to confirm your new email!"),
        "login": ("Put this code to sign in to AethelHost!", "Enter this code below to sign in!"),
        "pwchange": ("Put this code to change your password on AethelHost!", "Enter this code below to confirm the change!"),
        "pwcreate": ("Put this code to create your password on AethelHost!", "Enter this code below to confirm it!"),
        "label": "Code requested", "or_copy": "Or copy it here:",
        "expires": "This code expires in 10 minutes.",
        "ignore": "If you didn't request this, you can safely ignore this email.",
        "block_q": "Want to prevent future emails to this address?", "block_a": "Block this address.",
    },
    "pt": {
        "register": ("Use este código para confirmar o seu e-mail no AethelHost!", "Digite o código abaixo para confirmar o seu e-mail!"),
        "emailchange": ("Use este código para confirmar o seu novo e-mail no AethelHost!", "Digite o código abaixo para confirmar o seu novo e-mail!"),
        "login": ("Use este código para entrar no AethelHost!", "Digite o código abaixo para entrar!"),
        "pwchange": ("Use este código para trocar a sua senha no AethelHost!", "Digite o código abaixo para confirmar a troca!"),
        "pwcreate": ("Use este código para criar a sua senha no AethelHost!", "Digite o código abaixo para confirmar!"),
        "label": "Código solicitado", "or_copy": "Ou copie daqui:",
        "expires": "Este código vale por 10 minutos.",
        "ignore": "Se não foi você, pode ignorar este e-mail com segurança.",
        "block_q": "Quer impedir novos e-mails para este endereço?", "block_a": "Bloquear este endereço.",
    },
}


def code_email(lang, purpose, code, block_url):
    """(texto simples, HTML) do e-mail com o código. `block_url` é o link de bloquear o endereço."""
    c = COPY["pt" if lang == "pt" else "en"]
    title, sub = c.get(purpose, c["login"])
    text = (f"{title}\n\n{sub}\n\n    {code}\n\n{c['expires']}\n{c['ignore']}\n\n{c['block_q']} {c['block_a']}\n{block_url}\n")

    # As 6 caixas dividem a largura (15% cada, 2% de espaço): cabem em qualquer tela, inclusive no celular.
    digit = f"width:15%;height:56px;background:{INPUT};border:1px solid {BORDER};border-radius:10px;text-align:center;vertical-align:middle;font:700 28px/56px {MONO};color:{ACCENT};"
    cells = "<td style=\"width:2%;font-size:0;line-height:0;\">&nbsp;</td>".join(f'<td align="center" style="{digit}">{escape(ch)}</td>' for ch in code)
    html = f"""<!DOCTYPE html>
<html lang="{'pt-BR' if lang == 'pt' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark"><title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};"><tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" align="center" width="560" cellpadding="0" cellspacing="0" style="width:100%;max-width:560px;margin:0 auto;">
    <tr><td style="padding:0 4px 18px;font:800 22px {SANS};color:{TEXT};letter-spacing:-0.02em;">Aethel<span style="color:{ACCENT};">Host</span></td></tr>
    <tr><td style="background:{CARD};border:1px solid {BORDER};border-radius:16px;padding:30px 24px;">
      <h1 style="margin:0 0 12px;font:800 26px/1.25 {SANS};color:{TEXT};">{escape(title)}</h1>
      <p style="margin:0 0 24px;font:400 16px/1.5 {SANS};color:{MUTED};">{escape(sub)}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#1e222b;border:1px solid {BORDER};border-radius:12px;"><tr><td style="padding:16px;">
        <div style="font:400 13px {SANS};color:{MUTED};margin:0 0 12px;">{escape(c['label'])}</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:340px;table-layout:fixed;"><tr>{cells}</tr></table>
        <div style="font:400 13px {SANS};color:{MUTED};margin:14px 0 0;">{escape(c['or_copy'])} <span style="font:700 15px {MONO};color:{TEXT};letter-spacing:0.15em;">{escape(code)}</span></div>
      </td></tr></table>
      <p style="margin:26px 0 6px;font:400 14px/1.5 {SANS};color:{TEXT};">{escape(c['expires'])}</p>
      <p style="margin:0 0 6px;font:400 14px/1.5 {SANS};color:{MUTED};">{escape(c['ignore'])}</p>
      <p style="margin:0;font:400 14px/1.5 {SANS};color:{MUTED};">{escape(c['block_q'])} <a href="{escape(block_url, quote=True)}" style="color:{ACCENT};text-decoration:underline;">{escape(c['block_a'])}</a></p>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""
    return text, html


# ---------------------------------------------------------------- páginas do link "bloquear"

PAGE = {
    "en": {"title": "Block this address", "invalid": "This link isn't valid.", "ask": "Stop AethelHost from sending emails to {email}?",
           "block": "Block this address", "done": "Done. AethelHost won't send emails to {email} anymore.", "undo": "Changed your mind?",
           "unblock": "Allow emails again", "back": "Emails to {email} are allowed again.", "already": "{email} is currently blocked.",
           "note": "While blocked, you won't get sign-in codes either, so you can't sign in or create an account with this email."},
    "pt": {"title": "Bloquear este endereço", "invalid": "Este link não é válido.", "ask": "Impedir o AethelHost de enviar e-mails para {email}?",
           "block": "Bloquear este endereço", "done": "Pronto. O AethelHost não vai mais enviar e-mails para {email}.", "undo": "Mudou de ideia?",
           "unblock": "Permitir e-mails de novo", "back": "Os e-mails para {email} estão permitidos de novo.", "already": "{email} está bloqueado no momento.",
           "note": "Enquanto estiver bloqueado, você também não recebe os códigos de entrada, então não dá para entrar nem criar conta com este e-mail."},
}


def block_page(lang, state, email="", query=""):
    """HTML da página. state: invalid | ask | blocked | unblocked | already. `query` reaproveita e/t/l nos formulários."""
    p = PAGE["pt" if lang == "pt" else "en"]
    e = escape(email)

    def form(action, label, primary):
        color = f"background:{ACCENT};color:#06210f;" if primary else f"background:{INPUT};color:{TEXT};border:1px solid {BORDER};"
        return (f'<form method="post" action="/mail/block" style="margin:18px 0 0;">'
                f'<input type="hidden" name="e" value="{escape(email, quote=True)}"><input type="hidden" name="t" value="{escape(query, quote=True)}">'
                f'<input type="hidden" name="l" value="{"pt" if lang == "pt" else "en"}"><input type="hidden" name="action" value="{action}">'
                f'<button type="submit" style="{color}border:0;border-radius:10px;padding:12px 20px;font:700 16px {SANS};cursor:pointer;">{escape(label)}</button></form>')

    if state == "invalid":
        body = f'<p style="color:{TEXT};">{escape(p["invalid"])}</p>'
    elif state == "ask":
        body = f'<p style="color:{TEXT};">{p["ask"].format(email=f"<b>{e}</b>")}</p><p style="color:{MUTED};font-size:14px;">{escape(p["note"])}</p>' + form("block", p["block"], True)
    elif state == "already":
        body = f'<p style="color:{TEXT};">{p["already"].format(email=f"<b>{e}</b>")}</p>' + form("unblock", p["unblock"], False)
    elif state == "blocked":
        body = f'<p style="color:{TEXT};">{p["done"].format(email=f"<b>{e}</b>")}</p><p style="color:{MUTED};font-size:14px;">{escape(p["undo"])}</p>' + form("unblock", p["unblock"], False)
    else:
        body = f'<p style="color:{TEXT};">{p["back"].format(email=f"<b>{e}</b>")}</p>'
    return f"""<!DOCTYPE html><html lang="{'pt-BR' if lang == 'pt' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{escape(p['title'])} — AethelHost</title></head>
<body style="margin:0;background:{BG};font:16px/1.5 {SANS};"><div style="max-width:520px;margin:48px auto;padding:0 16px;">
<div style="font:800 22px {SANS};color:{TEXT};margin-bottom:16px;">Aethel<span style="color:{ACCENT};">Host</span></div>
<div style="background:{CARD};border:1px solid {BORDER};border-radius:16px;padding:28px;"><h1 style="margin:0 0 12px;font:800 22px {SANS};color:{TEXT};">{escape(p['title'])}</h1>{body}</div></div></body></html>"""
