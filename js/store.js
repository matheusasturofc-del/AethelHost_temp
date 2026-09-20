// Conversa com o backend (backend/server.py). Os dados ficam no PC, na pasta data/.

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch("/api" + path, options);
  } catch {
    throw new Error("Não consegui falar com o backend. Ele está rodando?");
  }
  if (res.status === 401 && !location.pathname.endsWith("login.html")) {  // sessão acabou: volta para o login
    location.href = "login.html?next=" + encodeURIComponent(location.pathname + location.search);
    return new Promise(() => {});  // a página está saindo; não deixa o código seguinte mostrar erro
  }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const err = new Error((data && data.error) || `Erro ${res.status}`);
    err.status = res.status;
    err.data = data || {};
    throw err;
  }
  return data;
}

// Envia um arquivo (ex.: um .jar de mod) sem mexer no conteúdo.
async function apiUpload(path, file) {
  let res;
  try {
    res = await fetch("/api" + path, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
  } catch {
    throw new Error("Não consegui falar com o backend. Ele está rodando?");
  }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error((data && data.error) || `Erro ${res.status}`);
  return data;
}

// "Meu Servidor Épico!" -> "meu-servidor-epico"
function slugify(text) {
  return text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24);
}

// Evita que texto digitado (ou vindo de fora) vire HTML, inclusive dentro de atributos.
function esc(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Monta elementos sem usar innerHTML: h("button", { class: "btn", onclick: fn }, "Texto")
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "translate") el.setAttribute("translate", v);  // a propriedade só aceita true/false
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c);
  return el;
}

function fmtSize(bytes) {
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(2) + " GB";
  return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
}

// Data e hora no idioma escolhido (o botão PT/EN muda o atributo lang da página).
function fmtDate(seconds) {
  if (!seconds) return "—";
  const locale = (document.documentElement.lang || "pt-BR").startsWith("en") ? "en-US" : "pt-BR";
  return new Date(seconds * 1000).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" });
}

const STATUS_LABEL = { offline: "Offline", starting: "Iniciando…", online: "Online", stopping: "Parando…" };
