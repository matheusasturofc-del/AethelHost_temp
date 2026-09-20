// Aba Arquivos: navegar pela pasta do servidor, editar textos, enviar, baixar, renomear e apagar.
// Usa as variáveis do painel: $, base, server, running().

const fl = { path: "", editing: null };
const flRel = (name) => (fl.path ? fl.path + "/" : "") + name;
const flUrl = (kind, path) => `/api${base}/files/${kind}?path=${encodeURIComponent(path)}`;

async function openFiles() {
  $("flEditor").hidden = true;
  $("flBrowser").hidden = false;
  await loadFiles(fl.path);
}

async function loadFiles(path) {
  $("flErr").textContent = "";
  let data;
  try {
    data = await api("GET", `${base}/files?path=${encodeURIComponent(path)}`);
  } catch (e) {
    $("flErr").textContent = e.message;
    if (path) { fl.path = ""; loadFiles(""); }
    return;
  }
  $("flMain").hidden = !data.supported;
  $("flNotice").hidden = data.supported;
  if (!data.supported) {
    $("flNotice").textContent = "Esta aba ainda não está disponível na VPS. Use o plano Grátis.";
    return;
  }
  fl.path = data.path;
  renderCrumbs();
  renderFiles(data);
}

function renderCrumbs() {
  const parts = fl.path ? fl.path.split("/") : [];
  const crumbs = [h("button", { class: "crumb", type: "button", onclick: () => loadFiles("") }, "servidor")];
  parts.forEach((p, i) => {
    crumbs.push(h("span", { class: "sep" }, "/"));
    crumbs.push(h("button", { class: "crumb", type: "button", translate: "no", onclick: () => loadFiles(parts.slice(0, i + 1).join("/")) }, p));
  });
  $("flCrumbs").replaceChildren(...crumbs);
}

const FILE_ICON = { dir: '<svg viewBox="0 0 20 20" width="20" height="20"><path d="M2 5.5A1.5 1.5 0 0 1 3.5 4h4l2 2h7A1.5 1.5 0 0 1 18 7.5v7a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 2 14.5z" fill="currentColor"/></svg>',
                    file: '<svg viewBox="0 0 20 20" width="20" height="20"><path d="M5 2h6l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M11 2v4h4" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>' };

function renderFiles(data) {
  const rows = data.items.map((it) => {
    const main = it.dir
      ? h("button", { class: "file-name", type: "button", translate: "no", onclick: () => loadFiles(flRel(it.name)) }, it.name)
      : h("span", { class: "file-name plain", translate: "no" }, it.name);
    const actions = [];
    if (it.text) actions.push(h("button", { class: "btn", type: "button", onclick: () => editFile(it.name) }, "Editar"));
    actions.push(h("a", { class: "btn", href: flUrl("download", flRel(it.name)), download: it.dir ? it.name + ".zip" : it.name }, "Baixar"));
    actions.push(h("button", { class: "btn needs-offline", type: "button", onclick: () => renameFile(it.name) }, "Renomear"));
    actions.push(h("button", { class: "btn btn-danger needs-offline", type: "button", onclick: () => deleteFile(it) }, "Excluir"));
    return h("div", { class: "row file-row" },
      h("span", { class: "file-ico", innerHTML: it.dir ? FILE_ICON.dir : FILE_ICON.file }),
      h("div", { class: "row-main" }, main, h("small", {}, [it.dir ? null : fmtSize(it.size), fmtDate(it.modified)].filter(Boolean).join(" · "))),
      h("div", { class: "row-actions" }, actions));
  });
  $("flList").replaceChildren(...(rows.length ? rows : [h("p", { class: "muted" }, "Esta pasta está vazia.")]),
    ...(data.truncated ? [h("p", { class: "muted" }, "Só os primeiros 2000 itens são mostrados.")] : []));
}

async function flDo(promise, okText) {
  $("flErr").textContent = $("flOk").textContent = "";
  try {
    await promise;
    if (okText) $("flOk").textContent = okText;
  } catch (e) {
    $("flErr").textContent = e.message;
  }
  loadFiles(fl.path);
}

function renameFile(name) {
  const next = prompt("Novo nome:", name);
  if (next && next !== name) flDo(api("POST", `${base}/files/rename`, { path: flRel(name), name: next }));
}

function deleteFile(it) {
  if (confirm(`Excluir "${it.name}"${it.dir ? " e tudo o que está dentro dela" : ""}? Não dá para desfazer.`)) {
    flDo(api("POST", `${base}/files/delete`, { path: flRel(it.name) }));
  }
}

async function editFile(name) {
  $("flErr").textContent = "";
  try {
    const f = await api("GET", `${base}/files/read?path=${encodeURIComponent(flRel(name))}`);
    fl.editing = flRel(name);
    $("flEditName").textContent = fl.editing;
    $("flText").value = f.content;
    $("flEditErr").textContent = "";
    $("flBrowser").hidden = true;
    $("flEditor").hidden = false;
  } catch (e) {
    $("flErr").textContent = e.message;
  }
}

$("flSave").addEventListener("click", async () => {
  $("flEditErr").textContent = "";
  try {
    await api("POST", `${base}/files/write`, { path: fl.editing, content: $("flText").value });
    $("flSave").textContent = "Salvo!";
    setTimeout(() => ($("flSave").textContent = "Salvar"), 1500);
  } catch (e) {
    $("flEditErr").textContent = e.message;
  }
});

$("flClose").addEventListener("click", () => {
  $("flEditor").hidden = true;
  $("flBrowser").hidden = false;
  loadFiles(fl.path);
});

$("flMkdir").addEventListener("click", () => {
  const name = prompt("Nome da nova pasta:");
  if (name) flDo(api("POST", `${base}/files/mkdir`, { path: fl.path, name }));
});

$("flUpload").addEventListener("change", async (ev) => {
  const files = [...ev.target.files];
  ev.target.value = "";
  $("flErr").textContent = $("flOk").textContent = "";
  for (const f of files) {
    if (f.size > 64 * 1024 * 1024) { $("flErr").textContent = `"${f.name}" passa de 64 MB. Para mundos grandes use a aba Mundos.`; continue; }
    $("flOk").textContent = `Enviando ${f.name}…`;
    try {
      await apiUpload(`${base}/files/upload?dir=${encodeURIComponent(fl.path)}&name=${encodeURIComponent(f.name)}`, f);
      $("flOk").textContent = `${f.name} enviado.`;
    } catch (e) {
      $("flOk").textContent = "";
      $("flErr").textContent = e.message;
    }
  }
  loadFiles(fl.path);
});
