# BlockHost

Painel para criar e gerenciar servidores de Minecraft Java, no estilo Aternos, com tema escuro.

## Como rodar

```bash
python backend/server.py
```

Abra http://127.0.0.1:8080 no navegador. Ctrl+C no terminal desliga o backend e os servidores de Minecraft.

**Antes de reiniciar o backend, desligue seus servidores pelo painel.** Se o backend for encerrado com um servidor ligado, o painel perde o controle dele.

## Como funciona

- `index.html`, `servers.html`, `create.html`, `panel.html`, `css/`, `js/`: o site.
- `backend/server.py`: serve o site e a API, liga os servidores neste PC e controla a VPS por SSH.
- `backend/software.py`: versões do Minecraft e download dos programas de servidor (Vanilla, Paper, Purpur, Fabric).
- `backend/content.py`: mods e plugins (busca e instalação pelo Modrinth, envio de `.jar`).
- `backend/net.py`: downloads só de sites oficiais conhecidos, com conferência de hash.
- `data/`: servidores, mundos, backups, chaves SSH e jars baixados. Fica só no seu PC (não vai para o git).

## Software e versões

As versões vêm ao vivo dos sites oficiais (Mojang, PaperMC, Purpur, Fabric), então versões novas aparecem sozinhas.

| Software | Aceita | Fonte |
|---|---|---|
| Vanilla | nada | Mojang |
| Paper | plugins | PaperMC |
| Purpur | plugins | PurpurMC |
| Fabric | mods | FabricMC |

Cada versão exige um Java mínimo (a Mojang informa qual). O backend procura os Javas instalados e desativa as versões que o PC não consegue rodar, mostrando o comando `winget` para instalar o que falta.
Ex.: as versões 26.x exigem Java 25 (`winget install EclipseAdoptium.Temurin.25.JDK`).

Ao trocar de software ou de versão, o backend guarda um backup do mundo em `data/backups/`. Voltar para uma versão mais antiga pede confirmação.

## Mods e plugins

Na aba **Mods** (Fabric) ou **Plugins** (Paper/Purpur) você busca no Modrinth só o que é compatível com o seu servidor, instala (as dependências obrigatórias vêm junto), envia um `.jar` seu, ativa/desativa ou remove. Tudo isso só com o servidor desligado.

## Planos

- **Grátis**: roda neste PC. Fecha sozinho após 5 horas sem jogadores.
- **VPS**: conecta por SSH com uma chave gerada pelo app. Ao iniciar, instala Java e tmux na VPS e liga o servidor. Ainda não foi testado numa VPS real, e mods/plugins ainda não estão disponíveis nela.

## Jogar

Neste PC, use `localhost:PORTA` (a porta aparece no painel, começando em 25565).
Para amigos de fora entrarem, é preciso liberar a porta no roteador.
