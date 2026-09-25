"""SSH para as VPS dos clientes: chave por conta, comandos, streaming e proteção contra endereços internos."""
import ipaddress
import os
import shlex
import socket
import subprocess
import threading
from pathlib import Path

from config import DATA
from errors import ContentError

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
# Para testar (ou hospedar) numa rede local: AETHELHOST_ALLOW_PRIVATE_VPS=1 libera endereços internos.
ALLOW_PRIVATE = os.environ.get("AETHELHOST_ALLOW_PRIVATE_VPS") == "1"


def key_paths(owner):
    """Cada conta tem a sua própria chave SSH: uma conta nunca consegue usar a VPS de outra."""
    folder = DATA / "keys" / owner
    return folder / "aethelhost_ed25519", folder / "known_hosts"


def ensure_key(owner):
    """Chave SSH da conta. A privada nunca sai deste PC: você só copia a pública."""
    key_file, _ = key_paths(owner)
    pub = Path(str(key_file) + ".pub")
    if not key_file.exists() or not pub.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.unlink(missing_ok=True)
        pub.unlink(missing_ok=True)
        try:
            r = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "aethelhost", "-f", str(key_file)],
                capture_output=True, timeout=30, creationflags=NO_WINDOW,
            )
        except FileNotFoundError:
            raise ContentError(500, "O ssh-keygen não foi encontrado neste PC.")
        if r.returncode != 0:
            raise ContentError(500, "Não consegui gerar a chave SSH: " + r.stderr.decode("utf-8", "replace").strip()[:200])
        if os.name == "nt":  # o ssh do Windows recusa chave que outros usuários possam ler
            subprocess.run(
                ["icacls", str(key_file), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                capture_output=True, creationflags=NO_WINDOW,
            )
    return pub.read_text(encoding="utf-8").strip()


def check_host(host):
    """Recusa endereços que não são da internet (o próprio PC, redes internas, nuvem interna): senão o site viraria um
    jeito de escanear a rede onde ele roda."""
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return  # o ssh mostra "não encontrei esse endereço" com uma mensagem melhor
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ContentError(400, "Esse endereço é interno (rede local, localhost…). Use o IP ou domínio público da sua VPS.")


def ssh_error(text):
    t, low = text.strip(), text.lower()
    if "permission denied" in low:
        return ("A VPS recusou a chave SSH. Adicione a chave pública do AethelHost ao arquivo "
                "~/.ssh/authorized_keys do usuário informado.")
    if "host key verification failed" in low or "identification has changed" in low:
        return "A identidade da VPS mudou (VPS reinstalada?). Se foi você, apague a linha dela em data/keys/known_hosts."
    if "timed out" in low or "no route to host" in low:
        return "Não consegui alcançar a VPS. Confira o IP, a porta SSH e o firewall do provedor."
    if "connection refused" in low:
        return "A VPS recusou a conexão. O SSH está rodando nessa porta?"
    if "could not resolve" in low:
        return "Não encontrei esse endereço. Confira o IP ou domínio da VPS."
    return "Falha na conexão SSH: " + (t.splitlines()[-1] if t else "sem detalhes")


def ssh_permanent(message):
    """Erros que uma nova tentativa não resolve (precisam de ação da pessoa, não é uma queda de rede)."""
    return "recusou a chave SSH" in message or "identidade da VPS mudou" in message


def ssh_cmd(vps, remote):
    check_host(vps["host"])
    key_file, known_hosts = key_paths(vps["owner"])
    return [
        "ssh", "-i", str(key_file), "-p", str(vps["port"]),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",  # confia na 1ª conexão e avisa se a VPS mudar depois
        "-o", f"UserKnownHostsFile={known_hosts.as_posix()}",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        f"{vps['user']}@{vps['host']}", remote,
    ]


def ssh_script(vps, script, timeout=60):
    """Roda um script bash na VPS. Devolve (código, saída); RuntimeError se não conseguir conectar."""
    ensure_key(vps["owner"])
    try:
        r = subprocess.run(
            ssh_cmd(vps, "bash -s"), input=script.encode("utf-8"), capture_output=True,
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("A VPS demorou demais para responder.")
    except FileNotFoundError:
        raise RuntimeError("O cliente SSH (ssh.exe) não foi encontrado neste PC.")
    if r.returncode == 255:  # 255 é o código do próprio ssh quando não conecta
        raise RuntimeError(ssh_error(r.stderr.decode("utf-8", "replace")))
    return r.returncode, r.stdout.decode("utf-8", "replace")


def ssh_run(vps, script, args=(), stdin=b"", timeout=60, stdin_file=None, stdout_file=None):
    """Roda `script` (bash) na VPS com `args` como $1, $2… e devolve (código, stdout em bytes, stderr em texto).
    O stdin fica livre para dados (envio de arquivos): o script vai na linha de comando. `stdin_file`/`stdout_file`
    são caminhos locais, para arquivos grandes (não passam pela memória)."""
    ensure_key(vps["owner"])
    remote = "bash -c " + shlex.quote(script) + " _ " + " ".join(shlex.quote(str(a)) for a in args)
    fin = open(stdin_file, "rb") if stdin_file else None
    fout = open(stdout_file, "wb") if stdout_file else None
    try:
        r = subprocess.run(
            ssh_cmd(vps, remote), input=None if fin else stdin, stdin=fin, stdout=fout or subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("A VPS demorou demais para responder.")
    except FileNotFoundError:
        raise RuntimeError("O cliente SSH (ssh.exe) não foi encontrado neste PC.")
    finally:
        if fin:
            fin.close()
        if fout:
            fout.close()
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode == 255:
        raise RuntimeError(ssh_error(err))
    return r.returncode, (r.stdout or b""), err


def ssh_stream(vps, script, on_line, limit=1200):
    """Como ssh_script, mas entrega cada linha assim que chega (para instalações demoradas)."""
    ensure_key(vps["owner"])
    try:
        p = subprocess.Popen(
            ssh_cmd(vps, "bash -s"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
        )
    except FileNotFoundError:
        raise RuntimeError("O cliente SSH (ssh.exe) não foi encontrado neste PC.")
    watchdog = threading.Timer(limit, p.kill)  # não deixa uma instalação travada para sempre
    watchdog.start()
    last, write_failed = [], False
    try:
        try:
            p.stdin.write(script.encode("utf-8"))
            p.stdin.close()
        except OSError:
            write_failed = True  # a conexão caiu bem no começo do envio; o que sobrar do stdout explica o motivo
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            last = (last + [line])[-5:]
            on_line(line)
        code = p.wait()
    finally:
        watchdog.cancel()
    if write_failed or code == 255:
        raise RuntimeError(ssh_error("\n".join(last)))
    return code
