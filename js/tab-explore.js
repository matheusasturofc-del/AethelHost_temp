// Cartão "Explorar" (aba Servidor, só o dono): mostrar o servidor na página Explorar.
// Usa as variáveis do painel: $, base, server.

let exShown = null;
let exBusy = false;
let exErr = "";
let exCap = null;  // Cloudflare Turnstile: só para publicar (a lista é pública, então confere que não é robô)

if (typeof loadCaptchaKey === "function") loadCaptchaKey().then(() => { exShown = null; if (typeof renderExplore === "function") renderExplore(); });

async function exSave(patch) {
  if (exBusy) return;
  if (patch.listed === true && exCap && exCap.box && !exCap.getToken()) {
    exErr = "Confirme que você não é um robô antes de publicar.";
    exShown = null;
    renderExplore();
    return;
  }
  if (patch.listed === true && exCap && exCap.box) patch.captcha = exCap.getToken();
  exBusy = true;
  exErr = "";
  renderExplore();
  try {
    server = await api("POST", `${base}/explore`, patch);
  } catch (e) { exErr = e.message; if (exCap && exCap.box) exCap.reset(); }
  exBusy = false;
  exShown = null;
  renderExplore();
}

function renderExplore() {
  const card = $("exploreCard");
  if (!server || server.role !== "owner") { card.hidden = true; return; }
  card.hidden = false;
  const ex = server.explore || {};
  const needPublic = server.plan === "free" && !server.public;
  const sig = JSON.stringify([ex, server.plan, server.public, exBusy, exErr, document.documentElement.lang]);
  if (sig === exShown) return;
  exShown = sig;
  exCap = !ex.listed && !ex.blocked && typeof captchaField === "function" ? captchaField() : null;

  const about = h("textarea", { maxlength: 200, rows: 3, placeholder: "Conte em poucas palavras como é o seu servidor (modo de jogo, regras, idioma…)", value: ex.about || "", disabled: exBusy });
  const parts = [h("h3", { style: "margin-top:0" }, "Explorar")];
  parts.push(h("p", { class: "muted" }, "Mostre o seu servidor na página Explorar, para qualquer pessoa achar e entrar."));

  if (ex.blocked) {
    parts.push(h("div", { class: "error" }, "A administração tirou este servidor do Explorar. Se foi um engano, fale com o Suporte."));
  } else {
    if (exCap && exCap.box) parts.push(h("p", { class: "muted small", style: "margin:0 0 6px" }, "Para publicar, confirme que você não é um robô:"), exCap.box);
    parts.push(h("div", { class: "card-head", style: "margin-bottom:10px" },
      h("span", { class: "muted" }, ex.listed ? "Aparecendo no Explorar" : "Fora do Explorar"),
      h("label", { class: "switch" },
        h("input", { type: "checkbox", checked: !!ex.listed, disabled: exBusy || (needPublic && !ex.listed),
          onchange: (ev) => exSave({ listed: ev.target.checked, about: about.value }) }),
        h("span", { class: "knob" }), h("span", {}, "Ativado"))));
    if (needPublic && !ex.listed) parts.push(h("p", { class: "muted small" }, "Antes, ative o endereço público em “Jogar com amigos” (acima): sem ele ninguém de fora consegue entrar."));
    parts.push(h("label", { class: "small muted", style: "display:block;margin-bottom:6px" }, "Descrição (até 200 caracteres)"), about,
      h("div", { class: "btn-row", style: "margin-top:10px" },
        h("button", { class: "btn", type: "button", disabled: exBusy, onclick: () => exSave({ about: about.value }) }, "Salvar descrição"),
        ex.listed ? h("a", { class: "btn", href: "explore.html" }, "Ver no Explorar") : null));
    if (server.plan === "vps") parts.push(h("p", { class: "muted small" }, "Atenção: o endereço da sua VPS (o IP) fica visível para todo mundo que abrir o Explorar."));
    parts.push(h("p", { class: "muted small", style: "margin-bottom:0" }, "Qualquer pessoa pode entrar. Se quiser só amigos, ative a lista de permitidos (whitelist) na aba Jogadores."));
  }
  if (ex.verified) {
    parts.push(h("div", { class: "trust-note", style: "margin-top:14px" }, trustBadge(ex.verifiedAt),
      h("div", { class: "muted small" }, "A equipe testou este servidor. Se você trocar o software, a versão ou mexer nos mods, o selo cai até a equipe olhar de novo.")));
  } else if (ex.listed) {
    parts.push(h("p", { class: "muted small", style: "margin:14px 0 0" }, "Servidores que a equipe do AethelHost testa ganham o selo Confiável por AethelHost."));
  }
  if (exErr) parts.push(h("div", { class: "error" }, exErr));
  card.replaceChildren(...parts);
}
