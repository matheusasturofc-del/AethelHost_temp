# AethelHost

Painel para criar e gerenciar servidores de Minecraft Java, no estilo Aternos, com tema escuro.

## Como rodar

```bash
python backend/server.py
```

Abra http://127.0.0.1:8080 no navegador. Ctrl+C no terminal desliga o backend e os servidores de Minecraft.

**Antes de reiniciar o backend, desligue seus servidores pelo painel.** Se o backend for encerrado com um servidor ligado, o painel perde o controle dele.

## Contas e login

Para usar o painel é preciso entrar com uma conta: **Google, Microsoft, GitHub, Discord**, qualquer serviço OpenID Connect ("Outro") ou **e-mail e senha** (com confirmação por um código de 6 números enviado por e-mail). Cada conta só vê e mexe nos próprios servidores (e tem a própria chave SSH para a VPS).

**Configurar (uma vez).** Cada serviço pede que você registre um "aplicativo" no site dele e cole aqui o ID e a chave secreta. Na página `login.html`, enquanto não existe nenhuma conta, aparece a seção **Configurar os logins** com o passo a passo de cada serviço e o endereço de retorno para copiar:

| Serviço | Onde criar | Endereço de retorno |
|---|---|---|
| Google | console.cloud.google.com → APIs e serviços → Credenciais → ID do cliente OAuth (Aplicativo da Web) | `http://127.0.0.1:8080/auth/callback/google` |
| Microsoft | portal.azure.com → Microsoft Entra ID → Registros de aplicativo (contas de qualquer diretório e pessoais) | `http://127.0.0.1:8080/auth/callback/microsoft` |
| GitHub | Settings → Developer settings → OAuth Apps | `http://127.0.0.1:8080/auth/callback/github` |
| Discord | discord.com/developers/applications → OAuth2 | `http://127.0.0.1:8080/auth/callback/discord` |

Use sempre `127.0.0.1` (não `localhost`): o AethelHost redireciona para ele, e o cookie de login é ligado a esse endereço. Se um dia o site for para a internet, use o endereço público (https) no registro e em `BASE_URL` (`backend/server.py`).

### E-mail e senha

Na página de login aparecem os serviços configurados e, depois de um **ou**, o formulário de e-mail e senha, com **Criar conta** embaixo.

- **Criar conta:** nome exibido, nome de usuário (único, sem diferenciar maiúsculas de minúsculas; 3 a 20 letras, números, ponto, hífen ou `_`), e-mail e senha (8 a 128 caracteres). Um código de 6 números vai para o e-mail e a conta só passa a existir depois de digitá-lo.
- **Entrar:** e-mail e senha certos, e então o código de 6 números por e-mail (a cada login).
- **O código** vale 10 minutos, 5 tentativas e uma vez só; "Reenviar" espera 1 minuto (no máximo 4 envios por pedido, 5 e-mails por endereço por hora, 60 por hora no total). 5 senhas erradas seguidas travam aquele e-mail por 15 minutos.
- **Senha:** guardada só como hash scrypt com sal (em `data/users.json`), nunca em texto. A resposta de "senha errada" e "e-mail inexistente" é a mesma, e criar conta com um e-mail que já existe não revela isso na tela (a pessoa recebe um aviso por e-mail).
- **Configurar o envio de e-mail (uma vez, o administrador):** em `login.html` (ou pelo botão "Configurar logins e e-mail" na página Administração) abra o bloco **E-mail** e preencha o SMTP. O jeito mais simples é um Gmail só para o site: ative a verificação em duas etapas, crie uma "senha de app" em myaccount.google.com/apppasswords, clique em **Preencher para o Gmail**, ponha o Gmail em Usuário/Remetente e a senha de app em Senha, salve e use **Enviar teste**. Fica em `data/mail.json` (nunca volta para o navegador). Sem isso, o formulário de e-mail e senha não aparece.
- Ainda não existe "esqueci a senha" nem tela de configurações da conta.

- **Perfil (Google, GitHub…):** na primeira entrada por um serviço (e para contas antigas que ainda não têm), o site pede o **nome exibido** e o **nome de usuário** (único) antes de liberar as páginas. O nome escolhido não é mais sobrescrito pelo nome do serviço. `POST /api/auth/profile`.
- **A primeira conta que entrar é a administradora.** Ela herda servidores criados antes das contas existirem (e a chave SSH antiga) e é a única que pode mudar os logins depois.
- Contas são separadas por serviço: a mesma pessoa entrando pelo Google e pela Microsoft tem duas contas.
- Segurança: OAuth2 com PKCE e `state` amarrado ao navegador; a sessão dura 14 dias e só o hash dela fica em disco (`data/sessions.json`); o cookie é `HttpOnly` e `SameSite=Lax`. As chaves secretas ficam em `data/auth.json` e nunca voltam para o navegador.
- Ainda **não há limites por conta** (quantidade de servidores, RAM). Tudo roda no seu PC.
- Testes/scripts: `AETHELHOST_DATA` (pasta de dados) e `AETHELHOST_PORT` (porta) permitem subir uma instância isolada sem tocar nos servidores reais (os nomes antigos `BLOCKHOST_*` ainda funcionam). Scripts que chamam a API (`tools/gen_gamerules.py`) precisam do cookie em `AETHELHOST_COOKIE`.

## Painel do administrador

O administrador (a primeira conta) ganha o link **Administração** no topo (`admin.html`): totais (contas, servidores, ligados agora, jogadores online, RAM em uso neste PC), uma lista de todas as contas (nome, e-mail, serviço de login, data de entrada, último login, sessões abertas) e, dentro de cada uma, os servidores dela com plano, software, versão, RAM, porta, se tem endereço público, se está ligado e quantos jogadores. Tem busca por conta, e-mail ou servidor e atualiza sozinho a cada 5 segundos. Servidores cujo dono não existe mais aparecem numa lista à parte. É só leitura por enquanto (`GET /api/admin/overview`, só para o administrador; para os outros a API responde 403).

## Perfil, tema e mesclar contas

- **Menu do perfil:** clicar no seu nome/foto no topo abre um menu com **Ver perfil**, **Administração** (só a administradora) e **Sair**.
- **Ver perfil** (`profile.html`): banner com o ícone do AethelHost repetido (a edição do banner vem depois), foto, nome, `@usuário`, e-mail, formas de entrar e data de entrada, e a seção **Aparência** para escolher o fundo do site: **Escuro** (padrão) ou **Branco**. A escolha fica guardada no navegador (`bh_theme`).
- **Várias formas de entrar numa conta:** uma conta pode ter Google + e-mail e senha etc. (`links` em `data/users.json`). Login por qualquer uma delas cai na mesma conta.
- **Mesclar contas** (administradora): em Administração, o botão **Mesclar em outra conta…** junta uma conta duplicada na conta escolhida: os logins, os servidores e a chave SSH passam para ela; quem estava logado na conta antiga continua logado na única. Se as duas têm senha de e-mail, não mescla. `POST /api/admin/merge`.

## Como funciona

- `index.html`, `login.html`, `servers.html`, `create.html`, `panel.html`, `css/`, `js/`: o site.
- `backend/auth.py`: contas, login OAuth e sessões. `backend/accounts.py`: e-mail, senha e códigos. `backend/mail.py`: envio de e-mail (SMTP).
- `backend/tunnel.py`: endereço público dos servidores deste PC pelo playit.gg.
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

- **Ícone (capa)**: em Opções (ou ao criar o servidor) você escolhe qualquer imagem. O navegador a reduz para 64×64 (corte no centro, mantendo a transparência) e ela vira o `server-icon.png` do Minecraft, além de aparecer na lista, no cabeçalho do painel e na aba do navegador. Sem imagem, vale o ícone padrão do AethelHost (`assets/default-icon.png`, gerado por `python tools/make_icon.py`).
- **Subtítulo com cores**: editor com as 16 cores, negrito, itálico, sublinhado, riscado, ofuscado, limpar e alinhamento (o alinhamento é aproximado). Usa os códigos `&a`, `&l`, `&r`… (só minúsculas) e aceita até 2 linhas. O preview imita a lista de servidores do Minecraft.

## Painel

- **Opções**: as configurações do `server.properties` (vagas, modo de jogo, dificuldade, whitelist, PvP, distâncias, pacote de recursos…) e **todas as gamerules**, cada uma numa linha com o controle e a chave `chave=valor` embaixo, com busca e uma barra para salvar ou descartar. Além de nome, subtítulo e ícone. As propriedades só mudam com o servidor desligado; as gamerules podem mudar com ele ligado e são reaplicadas a cada início. As listas vêm de servidores reais (`tools/gen_gamerules.py` gera `backend/gamerules.json`), com os nomes antigos (até a 1.21.x, `keepInventory`) e os novos (a partir da 26.1, `keep_inventory`).
- **Jogadores**: quem está online (expulsar, tornar operador, banir), lista de permitidos (whitelist), operadores e banidos. Com o servidor ligado usa comandos; desligado, grava nos arquivos do Minecraft e consulta a Mojang para achar o UUID.
- **Arquivos**: navegar pela pasta do servidor, editar arquivos de texto, enviar, baixar (pastas viram .zip), renomear e apagar.
- **Mundos**: escolher o mundo em uso, criar (nome, tipo e semente), baixar, enviar um .zip seu e apagar.
- **Backups**: criar (com o servidor ligado ele salva antes de copiar), restaurar, baixar e apagar. O AethelHost também guarda um backup antes de trocar de software, restaurar ou apagar um mundo.
- Mudanças em arquivos, mundos, mods e propriedades só com o servidor desligado.
- As abas ficam nesta ordem: Servidor, Opções, Console, Jogadores, Software, Mods/Plugins (não aparece no Vanilla), Mundos, Arquivos e Backups.

## Idioma

O idioma padrão é o **inglês**. O botão **PT | EN** no topo troca a interface entre inglês e português e lembra a escolha no navegador (quem já tem uma escolha guardada, mesmo que automática, continua com ela). O dicionário está em `js/i18n-en.js`; para achar textos novos que ainda faltam, rode `python tools/extract_strings.py`.

## Listas de escolha

Os menus de versão, software, memória e tipo de mundo usam o componente próprio de `js/ui.js` (com busca e grupos por série de versão), não o menu padrão do navegador.

## Planos

- **Grátis**: roda neste PC. Fecha sozinho após 5 horas sem jogadores.
- **VPS**: conecta por SSH com uma chave gerada pelo app. Ao iniciar, instala Java e tmux na VPS e liga o servidor. Ainda não foi testado numa VPS real, e mods/plugins ainda não estão disponíveis nela.

## Jogar

Neste PC, use `localhost:PORTA` (a porta aparece no painel, começando em 25565).

### Amigos de fora: endereço público (playit.gg)

Sem abrir porta no roteador. Na aba **Servidor** de um servidor do plano Grátis, o cartão **Jogar com amigos** faz tudo:

1. **Ligar ao playit.gg** (só o administrador, uma vez): abre um link do playit.gg onde você entra (ou cria uma conta grátis) e aprova o AethelHost. A tela percebe sozinha quando termina. A chave do agente fica em `data/playit.json` (não compartilhe).
2. Ative **Endereço público** no servidor. Ao **Iniciar**, o AethelHost baixa o programa oficial do playit (versão 0.17.1 fixa, do GitHub oficial deles, com o SHA-256 conferido; ~5 MB, só na primeira vez, em `data/tools/playit/`), cria um túnel "Minecraft Java" só para esse servidor e mostra o endereço (ex.: `algo.joinmc.link`) com botão Copiar.
3. O programa do playit roda só enquanto houver servidor público ligado e é encerrado 1 minuto depois do último. Excluir o servidor apaga o túnel dele no playit.

Notas: o plano grátis do playit tem limite de túneis; o endereço é estável enquanto o túnel existir; qualquer pessoa com o endereço pode tentar entrar, então use a lista de permitidos (aba Jogadores) para deixar só os amigos. Desligar o playit.gg (link no cartão) remove a chave deste PC; para apagar o agente de vez, use playit.gg/account/agents. Suporte: Windows x64 e Linux (amd64/arm64); só foi testado até a etapa de aprovação com a API real, o resto com um playit de mentira.
