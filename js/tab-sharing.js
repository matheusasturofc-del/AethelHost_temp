// Aba "Acesso e Compartilhamento": quem mais pode usar este servidor, e o que o seu nível permite ver e fazer.
// Usa as variáveis do painel: $, base, server.

const SH_LEVEL = { full: "Completo", basic: "Básico" };
const SH_FILES = { none: "Nenhum", read: "Somente leitura", write: "Leitura e escrita" };
const RANK = { basic: 1, full: 2, owner: 3 };

// Cada aba do painel exige um nível (ou o de arquivos). Quem recebeu o servidor só vê as abas que pode usar.
const TAB_NEED = { options: "full", software: "full", content: "full", worlds: "full", backups: "full", files: "files_read", sharing: "basic" };

function applyRole() {
  const role = server.role || "owner";
  const files = server.files || "write";
  const body = document.body;
  ["owner", "full", "basic"].forEach((r) => body.classList.toggle("role-" + r, r === role));
  ["write", "read", "none"].forEach((f) => body.classList.toggle("files-" + f, f === files));
  document.querySelectorAll(".side button[data-tab]").forEach((btn) => {
    const need = TAB_NEED[btn.dataset.tab];
    if (!need) return;
    const ok = need === "files_read" ? role === "owner" || files !== "none" : RANK[role] >= RANK[need];
    btn.hidden = !ok || (btn.dataset.tab === "content" && !server.contentKind);
  });
  const active = document.querySelector(".side button.active");
  if (active && active.hidden) document.querySelector("[data-tab=server]").click();
  const by = $("sharedBy");
  by.hidden = !server.shared;
  by.textContent = server.shared ? "Compartilhado" : "";
  $("sharedFrom").hidden = !server.shared;
  $("sharedFromName").textContent = server.shared ? `${server.ownerName}${server.ownerUsername ? " (@" + server.ownerUsername + ")" : ""}` : "";
}

let shData = null;
let shBusy = false;

async function openSharing() {
  await loadSharing();
}

async function loadSharing() {
  try {
    shData = await api("GET", `${base}/shares`);
    $("shErr").textContent = "";
    renderSharing();
  } catch (e) {
    $("shErr").textContent = e.message;
  }
}

function avatarOf(u) {
  const fallback = h("span", { class: "avatar", translate: "no" }, (u.name || "?").trim().charAt(0).toUpperCase());
  if (!u.avatar) return fallback;
  const img = h("img", { class: "avatar", src: u.avatar, alt: "", referrerPolicy: "no-referrer" });
  img.addEventListener("error", () => img.replaceWith(fallback));
  return img;
}

function optionList(map, value) {
  return Object.entries(map).map(([k, label]) => h("option", { value: k, selected: k === value }, label));
}

async function shAct(promise, okText) {
  if (shBusy) return;
  shBusy = true;
  $("shErr").textContent = $("shOk").textContent = "";
  try {
    shData = await promise;
    if (okText) $("shOk").textContent = okText;
    renderSharing();
  } catch (e) {
    $("shErr").textContent = e.message;
    loadSharing();
  }
  shBusy = false;
}

function shRow(s) {
  const edit = shData.canEdit;
  const uid = s.user.id;
  const levelSel = h("select", { "aria-label": "Acesso", disabled: !edit, onchange: () => shAct(api("POST", `${base}/shares/${uid}`, { level: levelSel.value, files: filesSel.value }), "Acesso atualizado.") }, optionList(SH_LEVEL, s.level));
  const filesSel = h("select", { "aria-label": "Arquivos", disabled: !edit, onchange: () => shAct(api("POST", `${base}/shares/${uid}`, { level: levelSel.value, files: filesSel.value }), "Acesso atualizado.") }, optionList(SH_FILES, s.files));
  return h("div", { class: "share-row" },
    h("div", { class: "who" }, avatarOf(s.user), h("div", {}, h("strong", { translate: "no" }, s.user.name), h("small", { translate: "no" }, "@" + s.user.username))),
    s.status === "pending" ? h("span", { class: "badge pending" }, "Convite pendente") : null,
    levelSel, filesSel,
    edit ? h("button", { class: "btn btn-danger", type: "button", onclick: () => {
      if (confirm(s.status === "pending" ? "Cancelar este convite?" : "Tirar o acesso desta pessoa ao servidor?")) shAct(api("DELETE", `${base}/shares/${uid}`, {}), s.status === "pending" ? "Convite cancelado." : "Acesso removido.");
    } }, s.status === "pending" ? "Cancelar convite" : "Remover") : null);
}

function renderSharing() {
  const d = shData;
  const owner = h("div", { class: "share-row" },
    h("div", { class: "who" }, avatarOf(d.owner), h("div", {}, h("strong", { translate: "no" }, d.owner.name), h("small", { translate: "no" }, "@" + d.owner.username))),
    h("span", { class: "badge owner" }, "Dono"),
    h("span", { class: "badge" }, "Controle total"), h("span", { class: "badge" }, "Leitura e escrita"));
  $("shList").replaceChildren(owner, ...d.shares.map(shRow),
    ...(d.shares.length ? [] : [h("p", { class: "muted", style: "margin:12px 0 0" }, "Este servidor ainda não foi compartilhado com ninguém.")]));
  $("shAddCard").hidden = !d.canEdit;
  $("shReadOnly").hidden = d.canEdit;
  $("shLeave").hidden = d.canEdit;
}

$("shForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const username = $("shUser").value.trim().replace(/^@/, "");
  if (!username) return;
  await shAct(api("POST", `${base}/shares`, { username, level: $("shLevel").value, files: $("shFiles").value }), "Convite enviado. Ele aparece no sino da pessoa e só vale quando ela aceitar.");
  if (!$("shErr").textContent) $("shUser").value = "";
});

$("shLeave").addEventListener("click", async () => {
  if (!confirm("Sair deste servidor? Ele some da sua lista e só volta se o dono convidar de novo.")) return;
  try {
    await api("POST", `${base}/leave`, {});
    location.href = "servers.html";
  } catch (e) {
    $("shErr").textContent = e.message;
  }
});

window.addEventListener("langchange", () => { if (shData) renderSharing(); });
