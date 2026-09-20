// Nomes das opções na tela, em português e inglês: [pt, en]. A ordem daqui é a ordem na aba Opções.
// As chaves (max-players, keepInventory…) são as do Minecraft e aparecem embaixo de cada opção.

const OPT_LABELS = {
  "max-players": ["Vagas", "Player slots"],
  "gamemode": ["Modo de jogo", "Game mode"],
  "difficulty": ["Dificuldade", "Difficulty"],
  "white-list": ["Whitelist", "Whitelist"],
  "online-mode": ["Só contas originais", "Genuine accounts only"],
  "allow-flight": ["Voar", "Allow flight"],
  "force-gamemode": ["Forçar o modo de jogo", "Force game mode"],
  "spawn-protection": ["Proteção do spawn (raio)", "Spawn protection (radius)"],
  "hardcore": ["Hardcore", "Hardcore"],
  "pvp": ["PvP (jogador contra jogador)", "PvP (player vs player)"],
  "allow-nether": ["Permitir o Nether", "Allow the Nether"],
  "enable-command-block": ["Blocos de comando", "Command blocks"],
  "spawn-monsters": ["Gerar monstros", "Spawn monsters"],
  "generate-structures": ["Gerar estruturas", "Generate structures"],
  "view-distance": ["Distância de renderização", "View distance"],
  "simulation-distance": ["Distância de simulação", "Simulation distance"],
  "max-world-size": ["Tamanho máximo do mundo", "Max world size"],
  "entity-broadcast-range-percentage": ["Alcance das entidades (%)", "Entity broadcast range (%)"],
  "player-idle-timeout": ["Expulsar por inatividade (min)", "Kick when idle (min)"],
  "op-permission-level": ["Nível de permissão dos operadores", "Operator permission level"],
  "function-permission-level": ["Nível de permissão das funções", "Function permission level"],
  "hide-online-players": ["Esconder jogadores online", "Hide online players"],
  "enforce-whitelist": ["Forçar a whitelist", "Enforce whitelist"],
  "enforce-secure-profile": ["Exigir perfil seguro", "Require secure profile"],
  "require-resource-pack": ["Pacote de recursos obrigatório", "Require resource pack"],
  "resource-pack": ["Pacote de recursos (link)", "Resource pack (link)"],
  "resource-pack-prompt": ["Mensagem do pacote de recursos", "Resource pack message"],
  "resource-pack-sha1": ["SHA-1 do pacote de recursos", "Resource pack SHA-1"],
  "network-compression-threshold": ["Compressão da rede (bytes)", "Network compression (bytes)"],
  "max-tick-time": ["Tempo máximo por tick (ms)", "Max tick time (ms)"],
  "pause-when-empty-seconds": ["Pausar sem jogadores (s)", "Pause when empty (s)"],
  "prevent-proxy-connections": ["Bloquear proxies", "Block proxy connections"],
  "rate-limit": ["Limite de pacotes", "Packet rate limit"],
  "sync-chunk-writes": ["Gravar chunks de forma síncrona", "Synchronous chunk writes"],
  "use-native-transport": ["Transporte de rede nativo", "Native network transport"],
  "enable-status": ["Aparecer na lista de servidores", "Show in the server list"],
  "log-ips": ["Registrar IPs no log", "Log IPs"],
  "accepts-transfers": ["Aceitar transferências", "Accept transfers"],
  "broadcast-console-to-ops": ["Mostrar o console aos operadores", "Show the console to operators"],
  "region-file-compression": ["Compressão dos arquivos do mundo", "World file compression"],
  "chat-spam-threshold-seconds": ["Limite de spam no chat (s)", "Chat spam threshold (s)"],
  "command-spam-threshold-seconds": ["Limite de spam de comandos (s)", "Command spam threshold (s)"],
  "enable-code-of-conduct": ["Código de conduta", "Code of conduct"],
};

// Valores das listas de escolha.
const OPT_CHOICES = {
  "gamemode": { survival: ["Sobrevivência", "Survival"], creative: ["Criativo", "Creative"], adventure: ["Aventura", "Adventure"], spectator: ["Espectador", "Spectator"] },
  "difficulty": { peaceful: ["Pacífico", "Peaceful"], easy: ["Fácil", "Easy"], normal: ["Normal", "Normal"], hard: ["Difícil", "Hard"] },
  "region-file-compression": { deflate: ["Deflate", "Deflate"], lz4: ["LZ4", "LZ4"], none: ["Nenhuma", "None"] },
};

// Gamerules. Os nomes novos (26.x, snake_case) usam o mesmo rótulo das antigas equivalentes (ver RULE_ALIAS).
const RULE_LABELS = {
  announceAdvancements: ["Anunciar conquistas", "Announce advancements"],
  blockExplosionDropDecay: ["Explosões de blocos destroem parte dos itens", "Block explosions destroy some drops"],
  commandBlockOutput: ["Mostrar a saída dos blocos de comando", "Show command block output"],
  commandModificationBlockLimit: ["Limite de blocos por comando", "Blocks per command limit"],
  disableElytraMovementCheck: ["Desativar a checagem de movimento com élitro", "Disable elytra movement check"],
  disablePlayerMovementCheck: ["Desativar a checagem de movimento", "Disable player movement check"],
  disableRaids: ["Desativar invasões (raids)", "Disable raids"],
  doDaylightCycle: ["Ciclo do dia (o tempo avança)", "Daylight cycle (time advances)"],
  doEntityDrops: ["Entidades soltam itens", "Entities drop items"],
  doFireTick: ["O fogo se espalha", "Fire spreads"],
  doImmediateRespawn: ["Renascer imediatamente", "Respawn immediately"],
  doInsomnia: ["Gerar fantasmas (phantoms)", "Spawn phantoms"],
  doLimitedCrafting: ["Exigir receita para fabricar", "Require recipe for crafting"],
  doMobLoot: ["Mobs soltam itens", "Mobs drop loot"],
  doMobSpawning: ["Gerar mobs", "Spawn mobs"],
  doPatrolSpawning: ["Gerar patrulhas de saqueadores", "Spawn pillager patrols"],
  doTileDrops: ["Blocos soltam itens", "Blocks drop items"],
  doTraderSpawning: ["Gerar mercadores errantes", "Spawn wandering traders"],
  doVinesSpread: ["As videiras se espalham", "Vines spread"],
  doWardenSpawning: ["Gerar Wardens", "Spawn Wardens"],
  doWeatherCycle: ["Ciclo do clima", "Weather cycle"],
  drowningDamage: ["Dano por afogamento", "Drowning damage"],
  enderPearlsVanishOnDeath: ["Pérolas do Ender somem quando o jogador morre", "Ender pearls vanish on death"],
  fallDamage: ["Dano de queda", "Fall damage"],
  fireDamage: ["Dano de fogo", "Fire damage"],
  forgiveDeadPlayers: ["Perdoar jogadores mortos", "Forgive dead players"],
  freezeDamage: ["Dano de congelamento", "Freeze damage"],
  globalSoundEvents: ["Sons globais", "Global sound events"],
  keepInventory: ["Manter o inventário ao morrer", "Keep inventory after death"],
  lavaSourceConversion: ["Lava vira fonte", "Lava converts to source"],
  logAdminCommands: ["Avisar comandos de administrador", "Broadcast admin commands"],
  maxCommandChainLength: ["Limite da cadeia de comandos", "Command chain size limit"],
  maxCommandForkCount: ["Limite de contexto de comandos", "Command context limit"],
  maxEntityCramming: ["Limite de entidades amontoadas", "Entity cramming threshold"],
  mobExplosionDropDecay: ["Explosões de mobs destroem parte dos itens", "Mob explosions destroy some drops"],
  mobGriefing: ["Mobs podem destruir o ambiente", "Mobs can destroy the environment"],
  naturalRegeneration: ["Regeneração natural de vida", "Natural health regeneration"],
  playersNetherPortalCreativeDelay: ["Atraso do portal do Nether (criativo)", "Nether portal delay (creative)"],
  playersNetherPortalDefaultDelay: ["Atraso do portal do Nether (outros modos)", "Nether portal delay (other modes)"],
  playersSleepingPercentage: ["Porcentagem de jogadores para dormir", "Sleeping players percentage"],
  projectilesCanBreakBlocks: ["Projéteis quebram blocos", "Projectiles can break blocks"],
  randomTickSpeed: ["Velocidade dos ticks aleatórios", "Random tick speed"],
  reducedDebugInfo: ["Reduzir informações de depuração", "Reduce debug info"],
  sendCommandFeedback: ["Mostrar a resposta dos comandos", "Send command feedback"],
  showDeathMessages: ["Mostrar mensagens de morte", "Show death messages"],
  snowAccumulationHeight: ["Altura do acúmulo de neve", "Snow accumulation height"],
  spawnChunkRadius: ["Raio dos chunks do spawn", "Spawn chunk radius"],
  spawnRadius: ["Raio do local de renascimento", "Respawn location radius"],
  spectatorsGenerateChunks: ["Espectadores geram terreno", "Spectators generate terrain"],
  tntExplosionDropDecay: ["Explosões de TNT destroem parte dos itens", "TNT explosions destroy some drops"],
  universalAnger: ["Ira universal", "Universal anger"],
  waterSourceConversion: ["Água vira fonte", "Water converts to source"],
  // só existem com os nomes novos (26.x)
  allow_entering_nether_using_portals: ["Permitir entrar no Nether por portais", "Allow entering the Nether through portals"],
  command_blocks_work: ["Blocos de comando funcionam", "Command blocks work"],
  fire_spread_radius_around_player: ["Raio de propagação do fogo", "Fire spread radius"],
  locator_bar: ["Barra de localização", "Locator bar"],
  pvp: ["PvP (jogador contra jogador)", "PvP (player vs player)"],
  raids: ["Invasões (raids)", "Raids"],
  elytra_movement_check: ["Checar o movimento com élitro", "Check elytra movement"],
  player_movement_check: ["Checar o movimento dos jogadores", "Check player movement"],
  spawn_monsters: ["Gerar monstros", "Spawn monsters"],
  spawner_blocks_work: ["Geradores de mobs funcionam", "Spawner blocks work"],
  tnt_explodes: ["O TNT explode", "TNT explodes"],
};

// Nome novo -> nome antigo equivalente (para reaproveitar o rótulo).
const RULE_ALIAS = {
  advance_time: "doDaylightCycle", advance_weather: "doWeatherCycle", block_drops: "doTileDrops",
  block_explosion_drop_decay: "blockExplosionDropDecay", command_block_output: "commandBlockOutput",
  drowning_damage: "drowningDamage", ender_pearls_vanish_on_death: "enderPearlsVanishOnDeath", entity_drops: "doEntityDrops",
  fall_damage: "fallDamage", fire_damage: "fireDamage", forgive_dead_players: "forgiveDeadPlayers", freeze_damage: "freezeDamage",
  global_sound_events: "globalSoundEvents", immediate_respawn: "doImmediateRespawn", keep_inventory: "keepInventory",
  lava_source_conversion: "lavaSourceConversion", limited_crafting: "doLimitedCrafting", log_admin_commands: "logAdminCommands",
  max_block_modifications: "commandModificationBlockLimit", max_command_forks: "maxCommandForkCount",
  max_command_sequence_length: "maxCommandChainLength", max_entity_cramming: "maxEntityCramming",
  max_snow_accumulation_height: "snowAccumulationHeight", mob_drops: "doMobLoot", mob_explosion_drop_decay: "mobExplosionDropDecay",
  mob_griefing: "mobGriefing", natural_health_regeneration: "naturalRegeneration",
  players_nether_portal_creative_delay: "playersNetherPortalCreativeDelay", players_nether_portal_default_delay: "playersNetherPortalDefaultDelay",
  players_sleeping_percentage: "playersSleepingPercentage", projectiles_can_break_blocks: "projectilesCanBreakBlocks",
  random_tick_speed: "randomTickSpeed", reduced_debug_info: "reducedDebugInfo", respawn_radius: "spawnRadius",
  send_command_feedback: "sendCommandFeedback", show_advancement_messages: "announceAdvancements", show_death_messages: "showDeathMessages",
  spawn_mobs: "doMobSpawning", spawn_patrols: "doPatrolSpawning", spawn_phantoms: "doInsomnia",
  spawn_wandering_traders: "doTraderSpawning", spawn_wardens: "doWardenSpawning", spectators_generate_chunks: "spectatorsGenerateChunks",
  spread_vines: "doVinesSpread", tnt_explosion_drop_decay: "tntExplosionDropDecay", universal_anger: "universalAnger",
  water_source_conversion: "waterSourceConversion",
};

const isEnglish = () => (document.documentElement.lang || "").startsWith("en");

function optLabel(key) {
  const pair = OPT_LABELS[key];
  return pair ? pair[isEnglish() ? 1 : 0] : key;
}

function ruleLabel(name, officialEnglish) {
  const pair = RULE_LABELS[name] || RULE_LABELS[RULE_ALIAS[name]];
  if (pair) return pair[isEnglish() ? 1 : 0];
  return isEnglish() && officialEnglish ? officialEnglish : name;  // regra desconhecida: mostra o texto oficial em inglês, ou o nome
}
