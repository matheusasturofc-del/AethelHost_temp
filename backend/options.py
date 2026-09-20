"""Opções do servidor: as configurações do server.properties e as gamerules.

As listas de gamerules e de propriedades vêm de gamerules.json, que foi gerado perguntando a servidores de verdade
(tools/gen_gamerules.py). Existem duas gerações: nomes antigos (até a 1.21.x, ex.: keepInventory)
e nomes novos (a partir da 26.1, ex.: keep_inventory).
"""
import gzip
import json
import re
import struct
from pathlib import Path

from config import SERVERS_DIR
from content import ContentError
from props import read_properties, set_properties
from software import vkey

DATA = json.loads((Path(__file__).with_name("gamerules.json")).read_text(encoding="utf-8"))
LEGACY_REF, MODERN_REF = "1.21.4", "26.3"  # versões usadas como referência de cada geração


def generation(version):
    return "modern" if vkey(version) >= (26,) else "legacy"


def _ref(gen):
    return MODERN_REF if gen == "modern" else LEGACY_REF


# ---------------------------------------------------------------- propriedades

# Só estas chaves podem ser editadas. Ficam de fora as que o BlockHost cuida (porta, subtítulo, mundo) e as secretas.
P_BOOL, P_INT, P_SELECT, P_TEXT = "bool", "int", "select", "text"
PROPERTIES = {
    "max-players": (P_INT, {"min": 1, "max": 100000}),
    "gamemode": (P_SELECT, {"options": ["survival", "creative", "adventure", "spectator"]}),
    "difficulty": (P_SELECT, {"options": ["peaceful", "easy", "normal", "hard"]}),
    "white-list": (P_BOOL, {}),
    "online-mode": (P_BOOL, {}),
    "allow-flight": (P_BOOL, {}),
    "force-gamemode": (P_BOOL, {}),
    "spawn-protection": (P_INT, {"min": 0, "max": 29999984}),
    "hardcore": (P_BOOL, {}),
    "pvp": (P_BOOL, {}),
    "allow-nether": (P_BOOL, {}),
    "enable-command-block": (P_BOOL, {}),
    "spawn-monsters": (P_BOOL, {}),
    "generate-structures": (P_BOOL, {}),
    "view-distance": (P_INT, {"min": 3, "max": 32}),
    "simulation-distance": (P_INT, {"min": 3, "max": 32}),
    "max-world-size": (P_INT, {"min": 1, "max": 29999984}),
    "entity-broadcast-range-percentage": (P_INT, {"min": 10, "max": 1000}),
    "player-idle-timeout": (P_INT, {"min": 0, "max": 100000}),
    "op-permission-level": (P_INT, {"min": 1, "max": 4}),
    "function-permission-level": (P_INT, {"min": 1, "max": 4}),
    "hide-online-players": (P_BOOL, {}),
    "enforce-whitelist": (P_BOOL, {}),
    "enforce-secure-profile": (P_BOOL, {}),
    "require-resource-pack": (P_BOOL, {}),
    "resource-pack": (P_TEXT, {"pattern": r"(https?://\S+)?", "max": 250}),
    "resource-pack-prompt": (P_TEXT, {"max": 200}),
    "resource-pack-sha1": (P_TEXT, {"pattern": r"[0-9a-fA-F]{0,40}", "max": 40}),
    "network-compression-threshold": (P_INT, {"min": -1, "max": 1000000}),
    "max-tick-time": (P_INT, {"min": -1, "max": 10000000}),
    "pause-when-empty-seconds": (P_INT, {"min": -1, "max": 1000000}),
    "prevent-proxy-connections": (P_BOOL, {}),
    "rate-limit": (P_INT, {"min": 0, "max": 100000}),
    "sync-chunk-writes": (P_BOOL, {}),
    "use-native-transport": (P_BOOL, {}),
    "enable-status": (P_BOOL, {}),
    "log-ips": (P_BOOL, {}),
    "accepts-transfers": (P_BOOL, {}),
    "broadcast-console-to-ops": (P_BOOL, {}),
    "region-file-compression": (P_SELECT, {"options": ["deflate", "lz4", "none"]}),
    "chat-spam-threshold-seconds": (P_INT, {"min": 0, "max": 100000}),
    "command-spam-threshold-seconds": (P_INT, {"min": 0, "max": 100000}),
    "enable-code-of-conduct": (P_BOOL, {}),
}
MIN_KEYS_WHEN_STARTED = 20  # um server.properties completo (gerado pelo Minecraft) tem bem mais chaves que isso


def _props_path(sid):
    return SERVERS_DIR / sid / "server.properties"


def _convert(kind, text):
    """Texto do arquivo -> valor para mostrar na tela."""
    if kind == P_BOOL:
        return str(text).strip().lower() == "true"
    if kind == P_INT:
        try:
            return int(str(text).strip())
        except ValueError:
            return 0
    return str(text)


def _normalize_prop(key, value):
    """Confere o valor que chegou da tela e devolve como vai no arquivo."""
    kind, spec = PROPERTIES[key]
    if kind == P_BOOL:
        if isinstance(value, bool):
            return "true" if value else "false"
        if str(value).lower() in ("true", "false"):
            return str(value).lower()
        raise ContentError(400, f"{key}: use ligado ou desligado.")
    if kind == P_INT:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"-?\d+", str(value).strip()):
            raise ContentError(400, f"{key}: use um número inteiro.")
        n = int(str(value).strip())
        if not spec["min"] <= n <= spec["max"]:
            raise ContentError(400, f"{key}: o valor deve ficar entre {spec['min']} e {spec['max']}.")
        return str(n)
    if kind == P_SELECT:
        if value not in spec["options"]:
            raise ContentError(400, f"{key}: escolha uma das opções da lista.")
        return value
    text = str(value if value is not None else "").strip()
    if "\n" in text or "\r" in text or len(text) > spec["max"]:
        raise ContentError(400, f"{key}: texto inválido ou longo demais.")
    if "pattern" in spec and not re.fullmatch(spec["pattern"], text):
        raise ContentError(400, f"{key}: formato inválido.")
    return text


def _properties_view(server):
    gen = generation(server["version"])
    file = read_properties(_props_path(server["id"]))
    started = len(file) >= MIN_KEYS_WHEN_STARTED
    reference = DATA["properties"][_ref(gen)]
    shown = [k for k in PROPERTIES if k in (file if started else reference)]
    out = {}
    for key in shown:
        kind, spec = PROPERTIES[key]
        default = reference.get(key, "")
        entry = {"type": kind, "default": _convert(kind, default), "value": _convert(kind, file.get(key, default))}
        entry.update({k: v for k, v in spec.items() if k in ("min", "max", "options")})
        out[key] = entry
    return out


def set_property_changes(sid, changes):
    if not isinstance(changes, dict) or not changes:
        raise ContentError(400, "Nenhuma alteração para salvar.")
    clean = {}
    for key, value in changes.items():
        if key not in PROPERTIES:
            raise ContentError(400, f"A configuração {key} não pode ser editada por aqui.")
        clean[key] = _normalize_prop(key, value)
    _props_path(sid).parent.mkdir(parents=True, exist_ok=True)
    set_properties(_props_path(sid), clean)


# ---------------------------------------------------------------- gamerules

# Versões em que cada regra antiga surgiu (as que vieram depois da 1.8). Só serve para esconder o que a versão não tem.
LEGACY_SINCE = {
    "announceAdvancements": (1, 12), "commandBlockOutput": (1, 9), "disableElytraMovementCheck": (1, 11),
    "doLimitedCrafting": (1, 12), "doWeatherCycle": (1, 11), "maxEntityCramming": (1, 11), "spawnRadius": (1, 13),
    "spectatorsGenerateChunks": (1, 9), "disableRaids": (1, 14), "doPatrolSpawning": (1, 14),
    "doTraderSpawning": (1, 14), "doInsomnia": (1, 15), "doImmediateRespawn": (1, 15), "drowningDamage": (1, 15),
    "fallDamage": (1, 15), "fireDamage": (1, 15), "forgiveDeadPlayers": (1, 16), "universalAnger": (1, 16),
    "freezeDamage": (1, 17), "playersSleepingPercentage": (1, 17), "snowAccumulationHeight": (1, 18),
    "doWardenSpawning": (1, 19), "blockExplosionDropDecay": (1, 19, 3), "mobExplosionDropDecay": (1, 19, 3),
    "tntExplosionDropDecay": (1, 19, 3), "waterSourceConversion": (1, 19, 3), "lavaSourceConversion": (1, 19, 3),
    "globalSoundEvents": (1, 19, 4), "commandModificationBlockLimit": (1, 19, 4), "maxCommandForkCount": (1, 20, 3),
    "maxCommandChainLength": (1, 20, 3), "projectilesCanBreakBlocks": (1, 20, 5), "enderPearlsVanishOnDeath": (1, 21, 2),
    "playersNetherPortalCreativeDelay": (1, 21, 2), "playersNetherPortalDefaultDelay": (1, 21, 2),
    "doVinesSpread": (1, 21, 2), "disablePlayerMovementCheck": (1, 21, 2), "spawnChunkRadius": (1, 21, 4),
}


def visible_rules(version):
    """{nome: {type, default, label}} das gamerules que esta versão tem."""
    gen = generation(version)
    rules = DATA[_ref(gen)]
    if gen == "modern":
        return rules
    return {n: r for n, r in rules.items() if vkey(version) >= LEGACY_SINCE.get(n, (1, 8))}


# ---- leitura do level.dat (NBT), para mostrar o valor que o mundo realmente tem

def _read_nbt(data):
    buf, pos = memoryview(data), 0

    def take(fmt):
        nonlocal pos
        value = struct.unpack_from(fmt, buf, pos)[0]
        pos += struct.calcsize(fmt)
        return value

    def string():
        nonlocal pos
        n = take(">H")
        text = bytes(buf[pos:pos + n]).decode("utf-8", "replace")
        pos += n
        return text

    def skip(n):
        nonlocal pos
        pos += n

    def payload(tag):
        if tag == 1: return take(">b")
        if tag == 2: return take(">h")
        if tag == 3: return take(">i")
        if tag == 4: return take(">q")
        if tag == 5: return take(">f")
        if tag == 6: return take(">d")
        if tag == 7: skip(take(">i")); return None
        if tag == 8: return string()
        if tag == 9:
            inner, n = take(">b"), take(">i")
            return [payload(inner) for _ in range(n)]
        if tag == 10:
            out = {}
            while True:
                t = take(">b")
                if t == 0:
                    return out
                name = string()
                out[name] = payload(t)
        if tag == 11: skip(4 * take(">i")); return None
        if tag == 12: skip(8 * take(">i")); return None
        raise ValueError("NBT desconhecido")

    root = take(">b")
    string()
    return payload(root)


def _find(node, names):
    """Procura, em qualquer nível, um bloco com um destes nomes."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in names and isinstance(value, dict):
                return value
            found = _find(value, names)
            if found is not None:
                return found
    return None


def _flatten(node, out=None):
    """Todos os valores simples de um bloco NBT, com o nome sem o prefixo 'minecraft:'."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict):
                _flatten(value, out)
            elif isinstance(value, (bool, int, float, str)):
                out[key.split(":")[-1]] = "true" if value is True else "false" if value is False else str(value)
    return out


def _level_rules(server):
    """Valores das gamerules gravados no mundo. Se algo der errado, {} (a tela usa os padrões).

    Até a 1.21.x ficam dentro do level.dat; a partir da 26.1 ficam em data/minecraft/game_rules.dat.
    """
    from manage import level_name  # import aqui para não criar um ciclo
    world = SERVERS_DIR / server["id"] / level_name(server["id"])
    for path in (world / "data" / "minecraft" / "game_rules.dat", world / "level.dat"):
        try:
            nbt = _read_nbt(gzip.decompress(path.read_bytes()))
        except Exception:
            continue
        rules = _flatten(_find(nbt, ("GameRules", "game_rules")) or nbt)
        if rules:
            return rules
    return {}


def _rule_value(kind, text):
    if kind == "bool":
        return str(text).strip().lower() in ("true", "1")
    try:
        return int(str(text).strip())
    except ValueError:
        return 0


def _rules_view(server):
    rules = visible_rules(server["version"])
    world = _level_rules(server)
    overrides = server.get("gamerules") or {}
    out = {}
    for name, r in rules.items():
        text = overrides.get(name, world.get(name, r["default"]))
        out[name] = {"type": r["type"], "default": _rule_value(r["type"], r["default"]), "value": _rule_value(r["type"], text),
                     "custom": name in overrides, "label": r.get("label", "")}
    return out


def check_rule_changes(server, changes):
    """Valida as mudanças de gamerules e devolve {nome: 'true'|'false'|'123'} para guardar e aplicar."""
    if not isinstance(changes, dict) or not changes:
        raise ContentError(400, "Nenhuma alteração para salvar.")
    rules = visible_rules(server["version"])
    clean = {}
    for name, value in changes.items():
        if name not in rules:
            raise ContentError(400, f"A regra {name} não existe nesta versão.")
        if rules[name]["type"] == "bool":
            if not isinstance(value, bool):
                raise ContentError(400, f"{name}: use ligado ou desligado.")
            clean[name] = "true" if value else "false"
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not -2147483648 <= value <= 2147483647:
                raise ContentError(400, f"{name}: use um número inteiro.")
            clean[name] = str(value)
    return clean


def get_settings(server):
    return {"generation": generation(server["version"]), "properties": _properties_view(server), "gamerules": _rules_view(server)}
