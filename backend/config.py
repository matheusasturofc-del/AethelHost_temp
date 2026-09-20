"""Caminhos usados pelo backend. Tudo que é seu (mundos, jars, chaves) fica em data/."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# AETHELHOST_DATA (ou o antigo BLOCKHOST_DATA) permite rodar uma cópia de teste com outra pasta de dados, sem encostar nos seus servidores.
_DATA = os.environ.get("AETHELHOST_DATA") or os.environ.get("BLOCKHOST_DATA")
DATA = Path(_DATA).resolve() if _DATA else ROOT / "data"
SERVERS_DIR = DATA / "servers"
JARS_DIR = DATA / "jars"
CACHE_DIR = DATA / "cache"
BACKUPS_DIR = DATA / "backups"
