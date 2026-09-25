"""Arquivos, jogadores, mundos, backups e mods de um servidor que roda numa VPS (tudo por SSH).

Na VPS a pasta do servidor é ~/aethelhost/<id> e os backups ficam em ~/aethelhost/backups/<id>.
As listas pequenas (server.properties, ops.json, whitelist.json, banned-players.json, level.dat) são copiadas para
uma pasta espelho neste PC (SERVERS_DIR/<id>), onde o código que já existe para o plano Grátis trabalha normalmente;
depois o que mudou volta para a VPS."""
import re
import time
import uuid
from pathlib import Path

import manage
from config import DATA, SERVERS_DIR
from errors import ContentError
from props import read_properties
from sshx import ssh_run

LIGHT = ("server.properties", "ops.json", "whitelist.json", "banned-players.json")
BACKUP_RE = re.compile(r"^\d{8}-\d{6}(?:-[A-Za-z0-9\-]+)?\.tar\.gz$")
KEEP_AUTO_BACKUPS = manage.KEEP_AUTO_BACKUPS
JAR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+() \-]{0,120}\.jar(?:\.disabled)?$")

# Todo script começa igual: $1 é o id do servidor; os argumentos seguintes ficam em $1, $2… depois do shift.
PRELUDE = r'''
SID="$1"; shift
D="$HOME/aethelhost/$SID"
B="$HOME/aethelhost/backups/$SID"
mkdir -p "$D"
inside() {  # o caminho (mesmo com atalhos) precisa continuar dentro da pasta do servidor
  local r; r=$(realpath -m -- "$1") || return 1
  local base; base=$(realpath -m -- "$D")
  case "$r" in "$base"|"$base"/*) return 0;; *) return 1;; esac
}
'''


def _vps(server):
    return {**server["vps"], "owner": server["owner"]}


def run(server, body, args=(), stdin=b"", timeout=90, **kw):
    """Roda `body` na VPS. Códigos de saída do script: 3 = não existe, 4 = caminho fora da pasta, 9 = já existe."""
    code, out, err = ssh_run(_vps(server), PRELUDE + body, [server["id"], *args], stdin=stdin, timeout=timeout, **kw)
    if code == 4:
        raise manage.bad("Caminho inválido.")
    if code == 3:
        raise manage.bad("Não encontrei esse arquivo ou pasta.", 404)
    if code == 9:
        raise manage.bad("Já existe algo com esse nome.", 409)
    if code != 0:
        raise manage.bad("A VPS não conseguiu fazer isso: " + (err.strip().splitlines()[-1] if err.strip() else f"erro {code}"), 502)
    return out


def _rel(rel):
    """Caminho relativo já limpo (sem '..', sem caracteres estranhos), como texto com barras."""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or manage.BAD_CHARS.search(p) for p in parts):
        raise manage.bad("Caminho inválido.")
    return "/".join(parts)


# ---------------------------------------------------------------- pasta espelho (arquivos pequenos)

def mirror(server):
    return SERVERS_DIR / server["id"]


def pull(server, rels=LIGHT):
    """Copia os arquivos para a pasta espelho (o que não existe na VPS é apagado do espelho)."""
    base = mirror(server)
    base.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        rel = _rel(rel)
        local = base / rel
        try:
            out = run(server, 'R="$D/$1"; inside "$R" || exit 4; [ -f "$R" ] || exit 3; cat -- "$R"', [rel])
        except ContentError as e:
            if e.status == 404:
                local.unlink(missing_ok=True)
                continue
            raise
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(out)


def push(server, rels=LIGHT):
    base = mirror(server)
    for rel in rels:
        rel = _rel(rel)
        local = base / rel
        if local.is_file():
            run(server, 'R="$D/$1"; inside "$R" || exit 4; mkdir -p "$(dirname "$R")"; cat > "$R.part" && mv -f "$R.part" "$R"',
                [rel], stdin=local.read_bytes())


# ---------------------------------------------------------------- jogadores e opções

def get_players(server):
    pull(server)
    return manage.get_players(server["id"])


def offline_change(server, action, name, reason=""):
    pull(server)
    manage.offline_change(server["id"], action, name, reason)
    push(server)


def pull_for_settings(server):
    """Traz server.properties e o level.dat do mundo em uso (as regras do jogo ficam nele)."""
    pull(server, ("server.properties",))
    level = manage.level_name(server["id"])
    if manage.WORLD_RE.match(level):
        pull(server, (f"{level}/level.dat", f"{level}/data/minecraft/game_rules.dat"))


def push_properties(server):
    push(server, ("server.properties",))


# ---------------------------------------------------------------- arquivos

def list_dir(server, rel):
    rel = _rel(rel)
    out = run(server, r'''R="$D/$1"; inside "$R" || exit 4; [ -d "$R" ] || exit 3
find "$R" -mindepth 1 -maxdepth 1 -printf '%Y\t%s\t%T@\t%f\n' | head -n 2001''', [rel])
    items = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        kind, size, mtime, name = parts
        is_dir = kind == "d"
        try:
            size = int(size)
            mtime = int(float(mtime))
        except ValueError:
            continue
        items.append({"name": name, "dir": is_dir, "size": None if is_dir else size, "modified": mtime,
                      "text": not is_dir and Path(name).suffix.lower() in manage.TEXT_EXT and size <= manage.MAX_TEXT})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return {"path": rel, "items": items[:2000], "truncated": len(items) > 2000}


def read_text(server, rel):
    rel = _rel(rel)
    if Path(rel).suffix.lower() not in manage.TEXT_EXT:
        raise manage.bad("Só dá para editar arquivos de texto (.properties, .json, .txt, .yml…).")
    out = run(server, r'''R="$D/$1"; inside "$R" || exit 4; [ -f "$R" ] || exit 3
S=$(stat -c %s -- "$R"); echo "$S"; [ "$S" -le 1048576 ] && cat -- "$R"''', [rel])
    head, _, body = out.partition(b"\n")
    size = int(head or 0)
    if size > manage.MAX_TEXT:
        raise manage.bad("O arquivo passa de 1 MB: baixe para editar no seu computador.")
    try:
        return {"content": body.decode("utf-8"), "size": size}
    except UnicodeDecodeError:
        raise manage.bad("Esse arquivo não está em UTF-8, então não dá para editar aqui.")


def write_text(server, rel, content):
    rel = _rel(rel)
    if not rel or Path(rel).suffix.lower() not in manage.TEXT_EXT:
        raise manage.bad("Só dá para salvar arquivos de texto (.properties, .json, .txt, .yml…).")
    data = str(content).encode("utf-8")
    if len(data) > manage.MAX_TEXT:
        raise manage.bad("O texto passa de 1 MB.")
    run(server, r'''R="$D/$1"; inside "$R" || exit 4; [ -d "$(dirname "$R")" ] || exit 3
cat > "$R.part" && mv -f "$R.part" "$R"''', [rel], stdin=data)


def save_upload(server, dir_rel, name, data):
    d = _rel(dir_rel)
    name = manage.clean_name(name)
    run(server, r'''R="$D/$1/$2"; inside "$R" || exit 4; [ -d "$D/$1" ] || exit 3
cat > "$R.part" && mv -f "$R.part" "$R"''', [d, name], stdin=data, timeout=600)


def make_dir(server, rel, name):
    parent = _rel(rel)
    name = manage.clean_name(name)
    run(server, r'''R="$D/$1/$2"; inside "$R" || exit 4; [ -d "$D/$1" ] || exit 3
[ -e "$R" ] && exit 9; mkdir "$R"''', [parent, name])


def delete_path(server, rel):
    rel = _rel(rel)
    if not rel:
        raise manage.bad("Não dá para apagar a pasta raiz do servidor.")
    run(server, 'R="$D/$1"; inside "$R" || exit 4; [ -e "$R" ] || exit 3; rm -rf -- "$R"', [rel])


def rename_path(server, rel, new_name):
    rel = _rel(rel)
    if not rel:
        raise manage.bad("Não dá para renomear a pasta raiz do servidor.")
    new = manage.clean_name(new_name)
    run(server, r'''R="$D/$1"; inside "$R" || exit 4; [ -e "$R" ] || exit 3
N="$(dirname "$R")/$2"; inside "$N" || exit 4; [ -e "$N" ] && exit 9; mv -- "$R" "$N"''', [rel, new])


def tmp_file(label, ext):
    tmp = DATA / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp / f"{uuid.uuid4().hex}-{re.sub(r'[^A-Za-z0-9_-]', '_', label)}{ext}"


def download(server, rel):
    """Baixa um arquivo (ou uma pasta, em .tar.gz) da VPS para um arquivo temporário. Devolve (caminho, nome, tipo)."""
    rel = _rel(rel)
    info = run(server, r'''R="$D/$1"; inside "$R" || exit 4; [ -e "$R" ] || exit 3; [ -d "$R" ] && echo d || echo f''', [rel]).strip()
    name = Path(rel).name or "servidor"
    if info == b"d":
        dest = tmp_file(name, ".tar.gz")
        run(server, r'''R="$D/$1"; inside "$R" || exit 4; tar czf - -C "$(dirname "$R")" "$(basename "$R")"''',
            [rel], stdout_file=dest, timeout=1800)
        return dest, name + ".tar.gz", "application/gzip"
    dest = tmp_file(name, ".bin")
    run(server, r'''R="$D/$1"; inside "$R" || exit 4; cat -- "$R"''', [rel], stdout_file=dest, timeout=1800)
    return dest, name, None


# ---------------------------------------------------------------- mundos

def _scan(server):
    out = run(server, r'''cd "$D" || exit 0
for d in */; do d="${d%/}"; [ -d "$d" ] || continue
  lv=0; mt=0; if [ -f "$d/level.dat" ]; then lv=1; mt=$(stat -c %Y -- "$d/level.dat"); fi
  sz=$(du -sb -- "$d" 2>/dev/null | cut -f1)
  printf '%s\t%s\t%s\t%s\n' "$d" "$lv" "$mt" "${sz:-0}"
done''')
    rows = {}
    for line in out.decode("utf-8", "replace").splitlines():
        p = line.split("\t")
        if len(p) == 4:
            rows[p[0]] = {"level": p[1] == "1", "mtime": int(p[2] or 0), "size": int(p[3] or 0)}
    return rows


def world_names(server):
    rows = _scan(server)
    found = {n for n, r in rows.items() if r["level"]}
    return sorted(n for n in found if not any(n == f"{o}{s}" for o in found for s in ("_nether", "_the_end"))), rows


def world_dirs(name, rows):
    return [d for d in (name, f"{name}_nether", f"{name}_the_end") if d in rows]


def list_worlds(server):
    pull(server, ("server.properties",))
    active = manage.level_name(server["id"])
    names, rows = world_names(server)
    out = []
    for n in names:
        out.append({"name": n, "active": n == active, "size": sum(rows[d]["size"] for d in world_dirs(n, rows)),
                    "lastPlayed": rows[n]["mtime"], "pending": False})
    if active not in names:
        out.insert(0, {"name": active, "active": True, "size": 0, "lastPlayed": None, "pending": True})
    return out


def use_world(server, name):
    names, _ = world_names(server)
    if name not in names:
        raise manage.bad("Esse mundo não existe.", 404)
    pull(server, ("server.properties",))
    manage.set_properties(mirror(server) / "server.properties", {"level-name": name})
    push_properties(server)


def create_world(server, name, seed, wtype, version_tuple):
    props = manage.world_properties(name, seed, wtype, version_tuple)
    names, rows = world_names(server)
    if name in rows or name in names:
        raise manage.bad("Já existe um mundo ou pasta com esse nome.", 409)
    pull(server, ("server.properties",))
    manage.set_properties(mirror(server) / "server.properties", props, raw=("level-type",))
    push_properties(server)


def delete_world(server, name):
    names, rows = world_names(server)
    if name not in names:
        raise manage.bad("Esse mundo não existe.", 404)
    pull(server, ("server.properties",))
    if name == manage.level_name(server["id"]):
        raise manage.bad("Esse é o mundo em uso. Escolha outro mundo antes de apagar este.", 409)
    create_backup(server, f"antes-de-apagar-{name}", names=[name])
    dirs = world_dirs(name, rows)
    run(server, r'''for d in "$@"; do R="$D/$d"; inside "$R" || exit 4; rm -rf -- "$R"; done''', dirs)


def world_download(server, name):
    names, rows = world_names(server)
    if name not in names:
        raise manage.bad("Esse mundo não existe.", 404)
    dest = tmp_file(name, ".tar.gz")
    run(server, r'''cd "$D" || exit 3; tar czf - --exclude=session.lock -- "$@"''', world_dirs(name, rows), stdout_file=dest, timeout=3600)
    return dest


UNZIP_PY = r'''
import sys, zipfile, os, re
zf = zipfile.ZipFile(sys.argv[1])
name, base = sys.argv[2], sys.argv[3]
names = [n.replace("\\", "/") for n in zf.namelist()]
levels = [n for n in names if n.endswith("level.dat") and n.count("/") <= 1]
if not levels:
    print("NOLEVEL"); sys.exit(21)
level = min(levels, key=lambda n: n.count("/"))
top = level[: -len("level.dat")]
if len(names) > 200000 or sum(i.file_size for i in zf.infolist()) > 4 * 1024 ** 3:
    print("TOOBIG"); sys.exit(22)
def extract(strip, dest):
    dest = os.path.realpath(dest); os.makedirs(dest, exist_ok=True)
    for info in zf.infolist():
        n = info.filename.replace("\\", "/")
        if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
            print("UNSAFE"); sys.exit(23)
        if strip:
            if not n.startswith(strip): continue
            n = n[len(strip):]
        parts = [p for p in n.split("/") if p]
        if not parts or n.endswith("/"): continue
        if any(p == ".." for p in parts):
            print("UNSAFE"); sys.exit(23)
        target = os.path.realpath(os.path.join(dest, *parts))
        if not target.startswith(dest + os.sep):
            print("UNSAFE"); sys.exit(23)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk: break
                out.write(chunk)
extract(top, os.path.join(base, name))
if top:
    b = top.rstrip("/")
    for suffix in ("_nether", "_the_end"):
        if any(n.startswith(b + suffix + "/") for n in names):
            extract(b + suffix + "/", os.path.join(base, name + suffix))
print("OK")
'''


def upload_world(server, name, zip_path):
    if not manage.WORLD_RE.match(name or ""):
        raise manage.bad("Nome do mundo: use de 1 a 32 letras, números, _ ou -.")
    names, rows = world_names(server)
    if name in rows:
        raise manage.bad("Já existe um mundo ou pasta com esse nome.", 409)
    tmp = f"/tmp/aethelhost-{uuid.uuid4().hex}.zip"
    script = r'''T="$1"; NAME="$2"; shift 2
command -v python3 >/dev/null 2>&1 || exit 6
cat > "$T" || exit 5
OUT=$(python3 -c "$PY" "$T" "$NAME" "$D" 2>&1); C=$?
rm -f "$T"
if [ $C -ne 0 ]; then
  for w in "$NAME" "${NAME}_nether" "${NAME}_the_end"; do rm -rf "$D/$w"; done
fi
echo "$OUT" >&2; exit $C'''
    # o script Python vai como variável de ambiente embutida no comando
    body = f"PY={_shq(UNZIP_PY)}\n" + script
    try:
        run(server, body, [tmp, name], stdin_file=str(zip_path), timeout=3600)
    except ContentError as e:
        msg = e.message
        if "NOLEVEL" in msg:
            raise manage.bad("Não achei o level.dat: o .zip precisa ter a pasta do mundo (com level.dat dentro).")
        if "TOOBIG" in msg:
            raise manage.bad("O arquivo compactado é grande demais.")
        if "UNSAFE" in msg:
            raise manage.bad("O arquivo compactado tem um caminho inseguro.")
        if "erro 6" in msg:
            raise manage.bad("Para enviar um mundo, a VPS precisa ter o python3 instalado.")
        if "BadZipFile" in msg or "zipfile" in msg:
            raise manage.bad("Esse arquivo não é um .zip válido.")
        raise


def _shq(text):
    import shlex
    return shlex.quote(text)


# ---------------------------------------------------------------- backups

def list_backups(server):
    out = run(server, r'''mkdir -p "$B"; find "$B" -mindepth 1 -maxdepth 1 -type f -printf '%f\t%s\t%T@\n' ''')
    items = []
    for line in out.decode("utf-8", "replace").splitlines():
        p = line.split("\t")
        if len(p) == 3 and BACKUP_RE.match(p[0]):
            tag = re.sub(r"^\d{8}-\d{6}-?", "", p[0][:-len(".tar.gz")]) or "manual"
            items.append({"name": p[0], "size": int(p[1]), "created": int(float(p[2])), "tag": tag})
    return sorted(items, key=lambda b: b["name"], reverse=True)


def _backup_name(name):
    if not BACKUP_RE.match(str(name)):
        raise manage.bad("Nome de backup inválido.")
    return name


def create_backup(server, tag="manual", names=None):
    """Guarda um .tar.gz dos mundos (com nether e end) na VPS. Devolve o nome, ou None se ainda não há mundo."""
    all_names, rows = world_names(server)
    dirs = [d for n in (names if names is not None else all_names) for d in world_dirs(n, rows)]
    if not dirs:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = run(server, r'''STAMP="$1"; TAG="$2"; shift 2
mkdir -p "$B"; N="$STAMP-$TAG.tar.gz"; i=1
while [ -e "$B/$N" ]; do i=$((i+1)); N="$STAMP-$TAG-$i.tar.gz"; done
cd "$D" || exit 3
tar czf "$B/$N.part" --exclude=session.lock -- "$@" && mv -f "$B/$N.part" "$B/$N" || { rm -f "$B/$N.part"; exit 1; }
ls -1 "$B" | grep -- '-antes-de-' | sort -r | tail -n +%d | while read -r f; do rm -f "$B/$f"; done
echo "$N"''' % (KEEP_AUTO_BACKUPS + 1), [stamp, re.sub(r"[^A-Za-z0-9\-]", "-", tag), *dirs], timeout=3600)
    return out.decode().strip().splitlines()[-1]


def restore_backup(server, name):
    name = _backup_name(name)
    out = run(server, r'''F="$B/$1"; [ -f "$F" ] || exit 3
tar tzf "$F" 2>/dev/null | cut -d/ -f1 | sort -u''', [name]).decode("utf-8", "replace").split()
    tops = [t for t in out if t]
    lst = run(server, r'''tar tzf "$B/$1" 2>/dev/null''', [name]).decode("utf-8", "replace").splitlines()
    if not any(f"{t}/level.dat" in lst for t in tops):
        raise manage.bad("Esse backup não tem nenhum mundo (level.dat).")
    safety = create_backup(server, "antes-de-restaurar")
    names, rows = world_names(server)
    dirs = [d for n in names for d in world_dirs(n, rows)]
    run(server, r'''F="$B/$1"; shift
for d in "$@"; do R="$D/$d"; inside "$R" || exit 4; rm -rf -- "$R"; done
tar xzf "$F" -C "$D" --no-same-owner''', [name, *dirs], timeout=3600)
    return {"restored": sorted(tops), "safety": safety}


def delete_backup(server, name):
    run(server, r'''F="$B/$1"; [ -f "$F" ] || exit 3; rm -f -- "$F"''', [_backup_name(name)])


def download_backup(server, name):
    name = _backup_name(name)
    dest = tmp_file(name, ".tar.gz")
    run(server, r'''F="$B/$1"; [ -f "$F" ] || exit 3; cat -- "$F"''', [name], stdout_file=dest, timeout=3600)
    return dest, name


# ---------------------------------------------------------------- mods e plugins

def content_list(server, kind):
    out = run(server, r'''mkdir -p "$D/$1"; find "$D/$1" -mindepth 1 -maxdepth 1 -type f -printf '%f\t%s\n' ''', [kind])
    items = []
    for line in out.decode("utf-8", "replace").splitlines():
        name, _, size = line.partition("\t")
        if JAR_NAME_RE.match(name):
            items.append((name, int(size or 0)))
    return items


def content_exists(server, kind, name):
    out = run(server, r'''[ -e "$D/$1/$2" ] && echo y || echo n''', [kind, name])
    return out.strip() == b"y"


def content_rename(server, kind, src, dest):
    run(server, r'''[ -e "$D/$1/$2" ] || exit 3; mv -f -- "$D/$1/$2" "$D/$1/$3"''', [kind, src, dest])


def content_delete(server, kind, names):
    run(server, r'''K="$1"; shift; n=0
for f in "$@"; do if [ -e "$D/$K/$f" ]; then rm -f -- "$D/$K/$f"; n=1; fi; done
[ $n = 1 ] || exit 3''', [kind, *names])


def content_write(server, kind, name, data):
    run(server, r'''mkdir -p "$D/$1"; cat > "$D/$1/$2.part" && mv -f "$D/$1/$2.part" "$D/$1/$2"; rm -f -- "$D/$1/$2.disabled"''',
        [kind, name], stdin=data, timeout=600)


def content_fetch(server, kind, name, url, digest, max_bytes):
    """A própria VPS baixa o .jar (o endereço já foi conferido aqui) e confere o hash."""
    algo, value = digest
    run(server, r'''K="$1"; N="$2"; U="$3"; A="$4"; H="$5"; M="$6"
mkdir -p "$D/$K"; T="$D/$K/$N.part"
curl -fsSL --proto '=https' --retry 3 --max-filesize "$M" -o "$T" "$U" || { rm -f "$T"; exit 1; }
echo "$H  $T" | "${A}sum" -c - >/dev/null 2>&1 || { rm -f "$T"; echo "hash" >&2; exit 1; }
[ "$(head -c2 "$T")" = "PK" ] || { rm -f "$T"; echo "nao-jar" >&2; exit 1; }
mv -f "$T" "$D/$K/$N"; rm -f -- "$D/$K/$N.disabled"''', [kind, name, url, algo, value, max_bytes], timeout=600)
