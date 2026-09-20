"""Caminhos usados pelo backend. Tudo que é seu (mundos, jars, chaves) fica em data/."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SERVERS_DIR = DATA / "servers"
JARS_DIR = DATA / "jars"
CACHE_DIR = DATA / "cache"
BACKUPS_DIR = DATA / "backups"
