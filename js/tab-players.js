// Aba Jogadores: online agora, lista de permitidos, operadores e banidos.
// Usa as variáveis do painel: $, base, server.

let plTimer = null;
let plState = null;

async function openPlayers() {
  await loadPlayers();
  clearInterval(plTimer);
  plTimer = setInterval(() => { if (!$("tab-players").hidden) loadPlayers(); }, 3000);
}

async function loadPlayers() {
  let data;
  try {
    data = await api("GET", `${base}/players`);
  } catch (e) {
    $("plErr").textContent = e.message;
    return;
  }
  $("plMain").hidden = !data.supported;
  $("plNotice").hidden = data.supported;
  if (!data.supported) {
    $("plNotice").textContent = "Esta aba ainda não está disponível na VPS. Use o plano Grátis.";
    return;
  }
  plState = data;
  renderPlayers(data);
}

const plLower = (list) => list.map((n) => n.toLowerCase());

function plRow(name, extra, ...buttons) {
  return h("div", { class: "row" },
    h("div", { class: "row-main" }, h("strong", { translate: "no" }, name), extra ? h("small", {}, extra) : null),
    ...buttons);
}

function plButton(text, action, name, opts = {}) {
  return h("button", { class: "btn" + (opts.danger ? " btn-danger" : "") + (opts.offline ? " needs-offline" : ""), type: "button",
                       onclick: () => plAct(action, name, opts.reason ? prompt(opts.reason, "") ?? null : "") }, text);
}

function renderPlayers(d) {
  const online = d.state === "online";
  const ops = plLower(d.ops.map((o) => o.name));
  const wl = plLower(d.whitelist.players);
  $("plOnline").replaceChildren(...(d.online.length
    ? d.online.map((n) => plRow(n, [ops.includes(n.toLowerCase()) ? "Operador" : null, wl.includes(n.toLowerCase()) ? "Na lista de permitidos" : null].filter(Boolean).join(" · "),
        plButton("Expulsar", "kick", n, { reason: "Motivo da expulsão (opcional):" }),
        ops.includes(n.toLowerCase()) ? plButton("Tirar de operador", "deop", n) : plButton("Tornar operador", "op", n),
        plButton("Banir", "ban", n, { danger: true, reason: "Motivo do banimento (opcional):" })))
    : [h("p", { class: "muted" }, online ? "Ninguém está online no momento." : "O servidor está desligado.")]));

  const list = (items, empty) => (items.length ? items : [h("p", { class: "muted" }, empty)]);
  $("plWl").replaceChildren(...list(d.whitelist.players.map((n) => plRow(n, "", plButton("Remover", "whitelist_remove", n))), "Ninguém na lista ainda."));
  $("plOps").replaceChildren(...list(d.ops.map((o) => plRow(o.name, `Nível ${o.level}`, plButton("Remover", "deop", o.name))), "Nenhum operador ainda."));
  $("plBans").replaceChildren(...list(d.banned.map((b) => plRow(b.name, b.reason, plButton("Perdoar", "pardon", b.name))), "Ninguém banido."));
  $("plWlOn").checked = d.whitelist.enabled;
}

async function plAct(action, name, reason) {
  if (reason === null) return;  // cancelou a pergunta do motivo
  $("plErr").textContent = $("plOk").textContent = "";
  try {
    const r = await api("POST", `${base}/players/action`, { action, name, reason });
    $("plOk").textContent = r.mode === "command" ? "Comando enviado ao servidor." : "Salvo. O servidor está desligado, então vale quando ele ligar.";
  } catch (e) {
    $("plErr").textContent = e.message;
  }
  loadPlayers();
  setTimeout(loadPlayers, 1000);  // com o servidor ligado, o efeito aparece um instante depois
}

function plForm(id, action, reasonInput) {
  $(id).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const inputs = $(id).querySelectorAll("input");
    const name = inputs[0].value.trim();
    if (!name) return;
    await plAct(action, name, reasonInput ? inputs[1].value.trim() : "");
    inputs.forEach((i) => (i.value = ""));
  });
}

plForm("plWlForm", "whitelist_add");
plForm("plOpForm", "op");
plForm("plBanForm", "ban", true);
$("plWlOn").addEventListener("change", (ev) => plAct(ev.target.checked ? "whitelist_on" : "whitelist_off", ""));
