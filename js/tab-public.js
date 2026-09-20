// Cartão "Jogar com amigos" (aba Servidor): endereço público pelo playit.gg, sem abrir porta no roteador.
// Usa as variáveis do painel: $, base, server.

let tunnelSt = null;      // estado do playit neste PC (GET /tunnel)
let tunnelFetching = false;
let pubShown = null;      // assinatura do que está desenhado (só redesenha quando muda, para não piscar)
let pubClaimErr = "";
let pubBusy = false;

async function refreshTunnel() {
  if (tunnelFetching) return;
  tunnelFetching = true;
  try {
    tunnelSt = await api("GET", "/tunnel");
  } catch { /* o cartão só mostra o que já sabe */ }
  tunnelFetching = false;
  renderPublic();
}

// Chamado a cada segundo pelo poll do painel.
let pubTick = 0;
function pubPoll() {
  pubTick++;
  const waiting = tunnelSt && tunnelSt.claim && tunnelSt.claim.state === "waiting";
  if (!tunnelSt || waiting || pubTick % 8 === 0) refreshTunnel();
}

async function pubAction(fn) {
  if (pubBusy) return;
  pubBusy = true;
  pubClaimErr = "";
  try { await fn(); } catch (e) { pubClaimErr = e.message; }
  pubBusy = false;
  await refreshTunnel();
  if (typeof poll === "function") poll();
}

function renderPublic() {
  const card = $("pubCard");
  if (!server || server.plan !== "free" || !tunnelSt) { card.hidden = true; return; }
  if (!tunnelSt.supported) { card.hidden = true; return; }
  card.hidden = false;

  const t = server.tunnel || { state: "off" };
  const claim = tunnelSt.claim;
  const sig = JSON.stringify([tunnelSt.linked, tunnelSt.admin, claim, tunnelSt.agent && [tunnelSt.agent.installing, tunnelSt.agent.error],
    server.public, t, server.runtime.state, pubClaimErr, pubBusy, document.documentElement.lang]);
  if (sig === pubShown) return;
  pubShown = sig;

  const parts = [h("h3", { style: "margin-top:0" }, "Jogar com amigos")];

  if (!tunnelSt.linked) {
    parts.push(h("p", { class: "muted" }, "Deixe amigos de fora entrarem sem abrir porta no roteador. O BlockHost usa o playit.gg, que é grátis."));
    if (!tunnelSt.admin) {
      parts.push(h("p", { class: "muted small" }, "Peça ao administrador do BlockHost para ligar o playit.gg (o primeiro a criar conta aqui)."));
    } else if (claim && claim.state === "waiting") {
      parts.push(h("ol", { class: "steps" },
        h("li", {}, "Abra o link abaixo e entre (ou crie uma conta grátis) no playit.gg."),
        h("li", {}, "Clique para aprovar o BlockHost. Esta tela avisa sozinha quando terminar.")));
      parts.push(h("div", { class: "btn-row" },
        h("a", { class: "btn btn-primary", href: claim.url, target: "_blank", rel: "noopener" }, "Abrir o playit.gg")));
      parts.push(h("p", { class: "muted small" }, "Aguardando a aprovação…"));
    } else {
      parts.push(h("div", { class: "btn-row" },
        h("button", { class: "btn btn-primary", type: "button", disabled: pubBusy, onclick: () => pubAction(() => api("POST", "/tunnel/link")) }, "Ligar ao playit.gg")));
      if (claim && claim.state === "failed") parts.push(h("div", { class: "error" }, claim.error || "A ligação falhou. Tente de novo."));
    }
  } else {
    parts.push(h("div", { class: "card-head", style: "margin-bottom:6px" },
      h("span", { class: "muted" }, "Endereço público (playit.gg)"),
      h("label", { class: "switch" },
        h("input", { type: "checkbox", checked: !!server.public, disabled: pubBusy,
          onchange: (ev) => pubAction(() => api("POST", `${base}/public`, { enabled: ev.target.checked })) }),
        h("span", { class: "knob" }), h("span", {}, "Ativado"))));

    if (server.public) {
      const on = server.runtime.state === "starting" || server.runtime.state === "online";
      if (t.state === "ready") {
        parts.push(h("div", { class: "addr" }, h("span", { translate: "no" }, t.address), " ",
          h("button", { class: "btn", type: "button", style: "padding:2px 10px;font-size:13px", onclick: (ev) => copyText(t.address, ev.target) }, "Copiar")));
        parts.push(h("p", { class: "muted small" }, "Passe este endereço para os seus amigos (Minecraft Java: Multijogador → Adicionar servidor)."));
        if (t.direct && t.direct !== t.address) {
          parts.push(h("p", { class: "muted small" }, "Se o endereço acima não funcionar: ", h("span", { translate: "no" }, t.direct)));
        }
      } else if (t.state === "error") {
        parts.push(h("div", { class: "error" }, t.error));
      } else if (on) {
        parts.push(h("p", { class: "muted" }, tunnelSt.agent && tunnelSt.agent.installing
          ? "Baixando o programa do playit (só na primeira vez)…" : "Criando o endereço…"));
      } else {
        parts.push(h("p", { class: "muted" }, "O endereço aparece aqui quando o servidor ligar."));
      }
      if (tunnelSt.agent && tunnelSt.agent.error) parts.push(h("div", { class: "error" }, tunnelSt.agent.error));
      parts.push(h("p", { class: "muted small" }, "Qualquer pessoa com o endereço pode tentar entrar. Para deixar só os amigos, ative a lista de permitidos na aba Jogadores."));
    } else {
      parts.push(h("p", { class: "muted small" }, "Desativado: só dá para jogar neste PC. Ative para receber amigos de fora."));
    }
    if (tunnelSt.admin) {
      parts.push(h("p", { class: "muted small", style: "margin-bottom:0" },
        h("a", { href: "#", onclick: (ev) => {
          ev.preventDefault();
          if (confirm("Desligar o playit.gg? Os endereços públicos deixam de funcionar.")) pubAction(() => api("POST", "/tunnel/unlink"));
        } }, "Desligar o playit.gg")));
    }
  }
  if (pubClaimErr) parts.push(h("div", { class: "error" }, pubClaimErr));
  card.replaceChildren(...parts);
}

async function copyText(text, btn) {
  const old = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Copiado!";
  } catch {
    btn.textContent = "Não foi possível copiar";
  }
  setTimeout(() => (btn.textContent = old), 1500);
}
