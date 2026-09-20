"""Caminhos usados pelo backend. Tudo que é seu (mundos, jars, chaves) fica em data/."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# BLOCKHOST_DATA permite rodar uma cópia de teste com outra pasta de dados, sem encostar nos seus servidores.
DATA = Path(os.environ["BLOCKHOST_DATA"]).resolve() if os.environ.get("BLOCKHOST_DATA") else ROOT / "data"
SERVERS_DIR = DATA / "servers"
JARS_DIR = DATA / "jars"
CACHE_DIR = DATA / "cache"
BACKUPS_DIR = DATA / "backups"
