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
- `backend/manage.py`: jogadores, arquivos, mundos e backups.
- `backend/props.py`: leitura e escrita do `server.properties`.
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
| Quilt | mods (Quilt e Fabric) | QuiltMC |
| Forge | mods | Forge (instalador oficial) |
| NeoForge | mods | NeoForged (instalador oficial) |

Quilt, Forge e NeoForge rodam um instalador oficial uma vez, dentro da pasta do servidor (só no plano Grátis por enquanto).

Cada versão exige um Java mínimo (a Mojang informa qual). O backend procura os Javas instalados e desativa as versões que o PC não consegue rodar, mostrando o comando `winget` para instalar o que falta.
Ex.: as versões 26.x exigem Java 25 (`winget install EclipseAdoptium.Temurin.25.JDK`).

Ao trocar de software ou de versão, o backend guarda um backup do mundo em `data/backups/`. Voltar para uma versão mais antiga pede confirmação.

## Mods e plugins

Na aba **Mods** (Fabric) ou **Plugins** (Paper/Purpur) você busca no Modrinth só o que é compatível com o seu servidor, instala (as dependências obrigatórias vêm junto), envia um `.jar` seu, ativa/desativa ou remove. Tudo isso só com o servidor desligado.

## Aparência: capa e subtítulo

- **Ícone (capa)**: em Opções (ou ao criar o servidor) você escolhe qualquer imagem. O navegador a reduz para 64×64 (corte no centro, mantendo a transparência) e ela vira o `server-icon.png` do Minecraft, além de aparecer na lista, no cabeçalho do painel e na aba do navegador. Sem imagem, vale o ícone padrão do BlockHost (`assets/default-icon.png`, gerado por `python tools/make_icon.py`).
- **Subtítulo com cores**: editor com as 16 cores, negrito, itálico, sublinhado, riscado, ofuscado, limpar e alinhamento (o alinhamento é aproximado). Usa os códigos `&a`, `&l`, `&r`… (só minúsculas) e aceita até 2 linhas. O preview imita a lista de servidores do Minecraft.

## Painel

- **Jogadores**: quem está online (expulsar, tornar operador, banir), lista de permitidos (whitelist), operadores e banidos. Com o servidor ligado usa comandos; desligado, grava nos arquivos do Minecraft e consulta a Mojang para achar o UUID.
- **Arquivos**: navegar pela pasta do servidor, editar arquivos de texto, enviar, baixar (pastas viram .zip), renomear e apagar.
- **Mundos**: escolher o mundo em uso, criar (nome, tipo e semente), baixar, enviar um .zip seu e apagar.
- **Backups**: criar (com o servidor ligado ele salva antes de copiar), restaurar, baixar e apagar. O BlockHost também guarda um backup antes de trocar de software, restaurar ou apagar um mundo.
- Mudanças em arquivos, mundos e mods só com o servidor desligado.

## Idioma

O botão **PT | EN** no topo troca a interface entre português e inglês e lembra a escolha. O dicionário está em `js/i18n-en.js`; para achar textos novos que ainda faltam, rode `python tools/extract_strings.py`.

## Listas de escolha

Os menus de versão, software, memória e tipo de mundo usam o componente próprio de `js/ui.js` (com busca e grupos por série de versão), não o menu padrão do navegador.

## Planos

- **Grátis**: roda neste PC. Fecha sozinho após 5 horas sem jogadores.
- **VPS**: conecta por SSH com uma chave gerada pelo app. Ao iniciar, instala Java e tmux na VPS e liga o servidor. Ainda não foi testado numa VPS real, e mods/plugins ainda não estão disponíveis nela.

## Jogar

Neste PC, use `localhost:PORTA` (a porta aparece no painel, começando em 25565).
Para amigos de fora entrarem, é preciso liberar a porta no roteador.
