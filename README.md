# BlockHost

Painel para criar e gerenciar servidores de Minecraft Java, no estilo Aternos, com tema escuro.

## Como rodar

```bash
python backend/server.py
```

Abra http://127.0.0.1:8080 no navegador. Ctrl+C no terminal desliga o backend e os servidores de Minecraft.

## Como funciona

- `index.html`, `servers.html`, `create.html`, `panel.html`, `css/`, `js/`: o site.
- `backend/server.py`: serve o site e a API, baixa o `server.jar` oficial da Mojang e liga os servidores neste PC.
- `data/`: servidores criados, mundos e jars baixados. Fica só no seu PC (não vai para o git).

## Planos

- **Grátis**: roda neste PC. Fecha sozinho após 5 horas sem jogadores.
- **VPS**: os dados são salvos, mas a conexão com a VPS por SSH ainda não foi implementada.

## Java

Cada versão do Minecraft exige um Java mínimo (1.20.5 em diante: 21; 1.18 a 1.20.4: 17; anteriores: 8).
O backend procura os Javas instalados e desativa no formulário as versões que o PC não consegue rodar.

## Jogar

Neste PC, use `localhost:PORTA` (a porta aparece no painel, começando em 25565).
Para amigos de fora entrarem, é preciso liberar a porta no roteador.
