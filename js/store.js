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
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const err = new Error((data && data.error) || `Erro ${res.status}`);
    err.status = res.status;
    throw err;
  }
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

// Evita que texto digitado pelo usuário vire HTML.
function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

const STATUS_LABEL = { offline: "Offline", starting: "Iniciando…", online: "Online", stopping: "Parando…" };
