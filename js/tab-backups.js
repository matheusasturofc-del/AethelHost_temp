// Aba Backups: criar, restaurar, baixar e apagar backups dos mundos.
// Usa as variáveis do painel: $, base, server.

async function openBackups() {
  await loadBackups();
}

async function loadBackups() {
  let data;
  try {
    data = await api("GET", `${base}/backups`);
  } catch (e) {
    $("bkErr").textContent = e.message;
    return;
  }
  $("bkMain").hidden = !data.supported;
  $("bkNotice").hidden = data.supported;
  if (!data.supported) {
    $("bkNotice").textContent = "Esta aba ainda não está disponível na VPS. Use o plano Grátis.";
    return;
  }
  renderBackups(data.backups);
}

// "antes-de-apagar-criativo" -> "Antes de apagar o mundo criativo"
function backupLabel(tag) {
  if (tag === "manual") return "Manual";
  if (tag === "antes-de-mudar-software") return "Antes de mudar o software";
  if (tag === "antes-de-restaurar") return "Antes de restaurar um backup";
  const m = /^antes-de-apagar-(.+)$/.exec(tag);
  return m ? `Antes de apagar o mundo ${m[1]}` : tag;
}

function renderBackups(list) {
  $("bkList").replaceChildren(...(list.length ? list.map((b) => h("div", { class: "row" },
    h("div", { class: "row-main" }, h("strong", {}, backupLabel(b.tag)), h("small", {}, `${fmtDate(b.created)} · ${fmtSize(b.size)}`)),
    h("div", { class: "row-actions" },
      h("button", { class: "btn needs-offline", type: "button", onclick: () => restoreBackup(b) }, "Restaurar"),
      h("a", { class: "btn", href: `/api${base}/backups/download?name=${encodeURIComponent(b.name)}`, download: b.name }, "Baixar"),
      h("button", { class: "btn btn-danger", type: "button", onclick: () => deleteBackup(b) }, "Excluir"))))
    : [h("p", { class: "muted" }, "Nenhum backup ainda. Crie o primeiro no botão acima.")]));
}

async function bkDo(promise, okText) {
  $("bkErr").textContent = $("bkOk").textContent = "";
  try {
    const r = await promise;
    if (okText) $("bkOk").textContent = typeof okText === "function" ? okText(r) : okText;
  } catch (e) {
    $("bkErr").textContent = e.message;
  }
  loadBackups();
}

function restoreBackup(b) {
  if (confirm(`Restaurar o backup de ${fmtDate(b.created)}? Os mundos atuais serão substituídos (um backup de segurança deles é guardado antes).`)) {
    bkDo(api("POST", `${base}/backups/restore`, { name: b.name }), "Backup restaurado. Um backup de segurança do que estava antes ficou na lista.");
  }
}

function deleteBackup(b) {
  if (confirm(`Excluir o backup de ${fmtDate(b.created)}? Não dá para desfazer.`)) {
    bkDo(api("POST", `${base}/backups/delete`, { name: b.name }), "Backup excluído.");
  }
}

$("bkCreate").addEventListener("click", async () => {
  $("bkCreate").disabled = true;
  $("bkCreate").textContent = "Criando…";
  await bkDo(api("POST", `${base}/backups/create`), "Backup criado.");
  $("bkCreate").disabled = false;
  $("bkCreate").textContent = "Criar backup agora";
});
