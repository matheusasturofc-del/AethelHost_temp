// Aba Mundos: escolher o mundo em uso, criar, enviar, baixar e apagar.
// Usa as variáveis do painel: $, base, server.

async function openWorlds() {
  await loadWorlds();
}

async function loadWorlds() {
  let data;
  try {
    data = await api("GET", `${base}/worlds`);
  } catch (e) {
    $("wdErr").textContent = e.message;
    return;
  }
  $("wdMain").hidden = !data.supported;
  $("wdNotice").hidden = data.supported;
  if (!data.supported) {
    $("wdNotice").textContent = "Esta aba ainda não está disponível na VPS. Use o plano Grátis.";
    return;
  }
  renderWorlds(data.worlds);
}

function renderWorlds(worlds) {
  $("wdList").replaceChildren(...worlds.map((w) => {
    const info = w.pending ? "Será criado quando o servidor ligar" : [fmtSize(w.size), w.lastPlayed ? "jogado em " + fmtDate(w.lastPlayed) : null].filter(Boolean).join(" · ");
    const actions = [];
    if (!w.active) actions.push(h("button", { class: "btn btn-primary needs-offline", type: "button", onclick: () => wdDo(api("POST", `${base}/worlds/use`, { name: w.name }), `Agora o servidor usa o mundo "${w.name}".`) }, "Usar este mundo"));
    if (!w.pending) actions.push(h("a", { class: "btn", href: `/api${base}/worlds/download?name=${encodeURIComponent(w.name)}`, download: w.name + ".zip" }, "Baixar .zip"));
    if (!w.active) actions.push(h("button", { class: "btn btn-danger needs-offline", type: "button", onclick: () => deleteWorld(w.name) }, "Excluir"));
    return h("div", { class: "row" },
      h("div", { class: "row-main" },
        h("strong", { translate: "no" }, w.name, w.active ? h("span", { class: "badge on", translate: "yes" }, "Em uso") : null),
        h("small", {}, info)),
      h("div", { class: "row-actions" }, actions));
  }));
}

async function wdDo(promise, okText) {
  $("wdErr").textContent = $("wdOk").textContent = "";
  try {
    await promise;
    if (okText) $("wdOk").textContent = okText;
  } catch (e) {
    $("wdErr").textContent = e.message;
  }
  loadWorlds();
}

function deleteWorld(name) {
  if (confirm(`Excluir o mundo "${name}"? Um backup dele é guardado antes, mas o mundo some da pasta do servidor.`)) {
    wdDo(api("POST", `${base}/worlds/delete`, { name }), `Mundo "${name}" excluído (o backup ficou na aba Backups).`);
  }
}

$("wdCreate").addEventListener("click", () => {
  const name = $("wdName").value.trim();
  if (!name) { $("wdErr").textContent = "Dê um nome para o mundo."; return; }
  wdDo(api("POST", `${base}/worlds/create`, { name, type: $("wdType").value, seed: $("wdSeed").value.trim() }),
       `Pronto: o servidor vai criar e usar o mundo "${name}" na próxima vez que ligar.`);
});

$("wdUpload").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  const name = $("wdUpName").value.trim();
  $("wdErr").textContent = $("wdOk").textContent = "";
  if (!name) { $("wdErr").textContent = "Escreva o nome do mundo antes de escolher o arquivo."; return; }
  $("wdOk").textContent = `Enviando ${file.name} (${fmtSize(file.size)})… mundos grandes demoram um pouco.`;
  try {
    await apiUpload(`${base}/worlds/upload?name=${encodeURIComponent(name)}`, file);
    $("wdOk").textContent = `Mundo "${name}" enviado. Clique em "Usar este mundo" para ativá-lo.`;
    $("wdUpName").value = "";
  } catch (e) {
    $("wdOk").textContent = "";
    $("wdErr").textContent = e.message;
  }
  loadWorlds();
});
