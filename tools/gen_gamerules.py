#!/usr/bin/env python3
"""Descobre as gamerules e as propriedades de cada geração do Minecraft perguntando a servidores de verdade e grava backend/gamerules.json.

Uso (o backend precisa estar rodando e as versões já baixadas; a API exige login, então passe o valor do cookie
bh_session de uma conta na variável BLOCKHOST_COOKIE):
    BLOCKHOST_COOKIE=... python tools/gen_gamerules.py 1.21.4 26.3

Para cada regra candidata (nomes achados no jar do servidor) envia `gamerule <nome>` ao console e lê a resposta:
regra existente devolve o valor atual (= padrão, num mundo novo); regra inexistente dá erro e é descartada.
"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8080/api"


def call(method, path, body=None):
    data = json.dumps(body or {}).encode() if method != "GET" else None
    headers = {"Content-Type": "application/json"} if data else {}
    if os.environ.get("BLOCKHOST_COOKIE"):  # a API exige login: copie o cookie bh_session do navegador (F12 → Application → Cookies)
        headers["Cookie"] = "bh_session=" + os.environ["BLOCKHOST_COOKIE"]
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read())}


def inner_jar(path):
    z = zipfile.ZipFile(path)
    names = [n for n in z.namelist() if n.startswith("META-INF/versions/") and n.endswith(".jar")]
    return zipfile.ZipFile(io.BytesIO(z.read(names[0]))) if names else z


def candidates(version):
    """Nomes possíveis de regras, lidos do próprio jar: chaves do idioma e textos da classe GameRules."""
    jar = inner_jar(ROOT / "data" / "jars" / f"{version}.jar")
    lang = json.loads(jar.read("assets/minecraft/lang/en_us.json"))
    names, labels = set(), {}
    for key, text in lang.items():
        m = re.fullmatch(r"gamerule\.(?:minecraft\.)?([A-Za-z0-9_]+)", key)
        if m and not key.startswith("gamerule.category."):
            names.add(m.group(1))
            labels[m.group(1)] = text
    for cls in jar.namelist():
        if cls.endswith("GameRules.class"):
            names |= {s.decode() for s in re.findall(rb"[a-z][a-z0-9_]{3,60}", jar.read(cls)) if b"_" in s}
    return sorted(names), labels


def main(versions):
    out = ROOT / "backend" / "gamerules.json"
    result = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}  # mantém as versões já descobertas
    for version in versions:
        names, labels = candidates(version)
        print(f"== {version}: {len(names)} nomes candidatos")
        server = call("POST", "/servers", {"name": f"gr-{version}", "subtitle": "", "ip": f"gr-{version.replace('.', '-')}",
                                           "software": "vanilla", "version": version, "plan": "free", "eula": True})
        sid = server["id"]
        try:
            call("POST", f"/servers/{sid}/start")
            for _ in range(90):
                time.sleep(3)
                if call("GET", f"/servers/{sid}")["runtime"]["state"] == "online":
                    break
            else:
                raise SystemExit("o servidor não ficou online")
            props = {}  # as propriedades que este servidor realmente gera, com os valores padrão
            for line in call("GET", f"/servers/{sid}/files/read?path=server.properties")["content"].splitlines():
                if line.strip() and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            found = {}
            for name in names:
                _, mark = (lambda c: (c["lines"], c["next"]))(call("GET", f"/servers/{sid}/console?since=0"))
                call("POST", f"/servers/{sid}/command", {"command": f"gamerule {name}"})
                for _ in range(20):
                    time.sleep(0.15)
                    lines = call("GET", f"/servers/{sid}/console?since={mark}")["lines"]
                    text = " ".join(l["text"] for l in lines)
                    m = re.search(r"currently set to:?\s*(\S+)", text)
                    if m or "Unknown or incomplete" in text or "Incorrect argument" in text:
                        break
                if m:
                    value = m.group(1)
                    found[name] = {"type": "bool" if value in ("true", "false") else "int", "default": value,
                                   "label": labels.get(name, "")}
            print(f"   {len(found)} regras confirmadas pelo servidor")
            result[version] = found
            result.setdefault("properties", {})[version] = props
        finally:
            call("POST", f"/servers/{sid}/stop")
            for _ in range(30):
                time.sleep(2)
                if call("GET", f"/servers/{sid}")["runtime"]["state"] == "offline":
                    break
            call("DELETE", f"/servers/{sid}")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("gravado:", out)


if __name__ == "__main__":
    main(sys.argv[1:] or ["1.21.4", "26.3"])
