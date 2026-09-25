// Aba Backups: criar, restaurar, baixar e apagar backups dos mundos, e guardar cópias no Google Drive.
// Usa as variáveis do painel: $, base, server.

let bkList = [];
let bkDrive = { account: null, linked: false, uploads: {}, remote: {}, prefix: "", error: "" };
let bkDriveTimer = null;
const DRIVE_ERRORS = {
  not_configured: "O administrador ainda não configurou o login do Google (ou a API do Google Drive).",
  denied: "Você cancelou a permissão do Google Drive.",
  state: "Não foi possível confirmar o vínculo (o link expirou ou foi aberto em outro navegador). Tente de novo.",
  no_refresh: "O Google não devolveu a permissão de longo prazo. Tente vincular de novo.",
  no_scope: "Você precisa marcar a permissão de acesso ao Google Drive para vincular.",
  provider_error: "O Google não respondeu direito. Tente de novo em instantes.",
};

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
  bkList = data.backups;
  renderBackups();
  loadDrive();
}

// "antes-de-apagar-criativo" -> "Antes de apagar o mundo criativo"
function backupLabel(tag) {
  if (tag === "manual") return "Manual";
  if (tag === "antes-de-mudar-software") return "Antes de mudar o software";
  if (tag === "antes-de-restaurar") return "Antes de restaurar um backup";
  const m = /^antes-de-apagar-(.+)$/.exec(tag);
  return m ? `Antes de apagar o mundo ${m[1]}` : tag;
}

function driveBadge(b) {
  const job = bkDrive.uploads[b.name];
  const file = bkDrive.remote[bkDrive.prefix + b.name];
  if (!bkDrive.linked) return null;
  if (job && job.state === "uploading") {
    const pct = job.total ? Math.min(99, Math.round((job.sent / job.total) * 100)) : 0;
    return h("span", { class: "badge pending", translate: "no" }, `Drive ${pct}%`);
  }
  if (file || (job && job.state === "done")) {
    const url = (file && file.url) || (job && job.url);
    return url ? h("a", { class: "badge on", href: url, target: "_blank", rel: "noopener" }, "No Drive") : h("span", { class: "badge on" }, "No Drive");
  }
  if (job && job.state === "error") return h("span", { class: "badge", title: job.message, style: "color:var(--danger);border-color:var(--danger)" }, "Erro no Drive");
  return null;
}

function driveButton(b) {
  if (!bkDrive.linked) return null;
  const job = bkDrive.uploads[b.name];
  const file = bkDrive.remote[bkDrive.prefix + b.name];
  if (job && job.state === "uploading") return null;
  if (file || (job && job.state === "done")) return null;
  return h("button", { class: "btn", type: "button", onclick: () => sendToDrive(b) }, job && job.state === "error" ? "Tentar de novo no Drive" : "Enviar ao Drive");
}

function renderBackups() {
  const list = bkList;
  $("bkList").replaceChildren(...(list.length ? list.map((b) => h("div", { class: "row" },
    h("div", { class: "row-main" }, h("strong", {}, backupLabel(b.tag), " ", driveBadge(b)), h("small", {}, `${fmtDate(b.created)} · ${fmtSize(b.size)}`),
      bkDrive.uploads[b.name] && bkDrive.uploads[b.name].state === "error" ? h("small", { class: "error" }, bkDrive.uploads[b.name].message) : null),
    h("div", { class: "row-actions" },
      driveButton(b),
      h("button", { class: "btn needs-offline", type: "button", onclick: () => restoreBackup(b) }, "Restaurar"),
      h("a", { class: "btn", href: `/api${base}/backups/download?name=${encodeURIComponent(b.name)}`, download: b.name }, "Baixar"),
      h("button", { class: "btn btn-danger", type: "button", onclick: () => deleteBackup(b) }, "Excluir"))))
    : [h("p", { class: "muted" }, "Nenhum backup ainda. Crie o primeiro no botão acima.")]));
}

// ---- Google Drive
async function loadDrive() {
  let account;
  try {
    account = await api("GET", "/drive");
  } catch {
    return;
  }
  let extra = { linked: account.linked, uploads: {}, remote: {}, prefix: "" };
  if (account.linked) {
    try {
      extra = await api("GET", `${base}/backups/drive`);
    } catch (e) {
      extra.error = e.message;
    }
  }
  bkDrive = { account, linked: !!extra.linked, uploads: extra.uploads || {}, remote: extra.remote || {}, prefix: extra.prefix || "", error: extra.error || "" };
  renderDrive();
  renderBackups();
  clearTimeout(bkDriveTimer);
  if (Object.values(bkDrive.uploads).some((j) => j.state === "uploading") && !$("tab-backups").hidden) bkDriveTimer = setTimeout(loadDrive, 2000);
}

function renderDrive() {
  const a = bkDrive.account;
  const card = $("bkDrive");
  if (!a) return;
  const params = new URLSearchParams(location.search);
  const notes = [];
  if (params.get("drive") === "linked") notes.push(h("div", { class: "ok-msg" }, "Google Drive vinculado!"));
  if (params.get("drive_error")) notes.push(h("div", { class: "error" }, DRIVE_ERRORS[params.get("drive_error")] || DRIVE_ERRORS.provider_error));
  const head = h("div", { class: "card-head" }, h("h3", { style: "margin:0" }, "Google Drive"),
    a.linked ? h("span", { class: "badge on" }, "Vinculado") : null);
  if (!a.available) {
    card.replaceChildren(head,
      h("p", { class: "muted small" }, "Guarde cópias dos seus backups no seu Google Drive. Para isso, o administrador precisa configurar o login com o Google."),
      ...(a.redirectUri ? [h("p", { class: "muted small" }, "Administrador: em Configurar logins, cadastre o Google. No Google Cloud, ative a ",
        h("b", {}, "Google Drive API"), ", adicione o escopo ", h("code", { translate: "no" }, "auth/drive.file"), " na tela de permissão e cadastre este endereço de retorno: ",
        h("code", { translate: "no" }, a.redirectUri))] : []), ...notes);
    return;
  }
  if (!a.linked) {
    card.replaceChildren(head,
      h("p", { class: "muted small" }, "Guarde cópias dos seus backups no seu Google Drive. O AethelHost só enxerga os arquivos que ele mesmo cria, numa pasta chamada \"AethelHost Backups\"; o resto do seu Drive fica fora do alcance dele."),
      h("a", { class: "btn btn-primary", href: `/auth/drive/start?server=${encodeURIComponent(server.id)}` }, "Vincular Google Drive"), ...notes);
    return;
  }
  const auto = h("input", { type: "checkbox", checked: !!a.auto, onchange: async (ev) => {
    try { await api("POST", "/drive/settings", { auto: ev.target.checked }); } catch (e) { $("bkErr").textContent = e.message; ev.target.checked = !ev.target.checked; }
  } });
  card.replaceChildren(head,
    h("p", { class: "muted small" }, "Conta: ", h("b", { translate: "no" }, a.email || "Google"), ". Os backups vão para a pasta \"AethelHost Backups\" do seu Drive."),
    h("label", { class: "check" }, auto, h("span", {}, "Enviar os backups manuais para o Drive automaticamente")),
    h("button", { class: "btn btn-danger", type: "button", onclick: unlinkDrive }, "Desvincular"),
    ...(bkDrive.error ? [h("div", { class: "error" }, bkDrive.error)] : []), ...notes);
}

async function unlinkDrive() {
  if (!(await confirmBox("Desvincular o Google Drive? Os backups que já estão no seu Drive continuam lá; o AethelHost só deixa de enviar novos.", { title: "Você tem certeza?", ok: "Desvincular", danger: true }))) return;
  try {
    await api("POST", "/drive/unlink", {});
  } catch (e) {
    $("bkErr").textContent = e.message;
  }
  loadDrive();
}

async function sendToDrive(b) {
  $("bkErr").textContent = $("bkOk").textContent = "";
  try {
    await api("POST", `${base}/backups/drive`, { name: b.name });
    bkDrive.uploads[b.name] = { state: "uploading", sent: 0, total: b.size };
    renderBackups();
    clearTimeout(bkDriveTimer);
    bkDriveTimer = setTimeout(loadDrive, 1000);
  } catch (e) {
    $("bkErr").textContent = e.message;
  }
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

async function restoreBackup(b) {
  if (await confirmBox(`Restaurar o backup de ${fmtDate(b.created)}? Os mundos atuais serão substituídos (um backup de segurança deles é guardado antes).`, { title: "Você tem certeza?", ok: "Restaurar", danger: true })) {
    bkDo(api("POST", `${base}/backups/restore`, { name: b.name }), "Backup restaurado. Um backup de segurança do que estava antes ficou na lista.");
  }
}

async function deleteBackup(b) {
  if (await confirmBox(`Excluir o backup de ${fmtDate(b.created)}? Não dá para desfazer.`, { title: "Você tem certeza?", ok: "Excluir", danger: true })) {
    bkDo(api("POST", `${base}/backups/delete`, { name: b.name }), "Backup excluído.");
  }
}

$("bkCreate").addEventListener("click", async () => {
  $("bkCreate").disabled = true;
  $("bkCreate").textContent = "Criando…";
  await bkDo(api("POST", `${base}/backups/create`), (r) => (r.drive ? "Backup criado. Enviando para o Google Drive…" : "Backup criado."));
  $("bkCreate").disabled = false;
  $("bkCreate").textContent = "Criar backup agora";
});

// Voltando do Google (?tab=backups&drive=linked): abre esta aba sozinha.
(() => {
  const tab = new URLSearchParams(location.search).get("tab");
  if (!tab) return;
  const tryOpen = (left) => {
    const btn = document.querySelector(`.side button[data-tab="${tab}"]`);
    if (btn && !btn.hidden) btn.click();
    else if (left > 0) setTimeout(() => tryOpen(left - 1), 300);
  };
  setTimeout(() => tryOpen(12), 400);
})();
