// Aparência do servidor: cores e formatação do Minecraft (&a, &l…) e o ícone (capa).
// Depende de store.js (h, esc).

const MC_COLORS = [
  ["0", "#000000", "Preto"], ["1", "#0000AA", "Azul escuro"], ["2", "#00AA00", "Verde escuro"], ["3", "#00AAAA", "Ciano escuro"],
  ["4", "#AA0000", "Vermelho escuro"], ["5", "#AA00AA", "Roxo"], ["6", "#FFAA00", "Dourado"], ["7", "#AAAAAA", "Cinza"],
  ["8", "#555555", "Cinza escuro"], ["9", "#5555FF", "Azul"], ["a", "#55FF55", "Verde"], ["b", "#55FFFF", "Ciano"],
  ["c", "#FF5555", "Vermelho"], ["d", "#FF55FF", "Rosa"], ["e", "#FFFF55", "Amarelo"], ["f", "#FFFFFF", "Branco"],
];
const MC_COLOR = Object.fromEntries(MC_COLORS.map(([code, hex]) => [code, hex]));
const MC_STYLE = { k: "obf", l: "bold", m: "strike", n: "underline", o: "italic" };
const MC_FORMATS = [
  ["l", "B", "Negrito", "font-weight:800"], ["o", "I", "Itálico", "font-style:italic"],
  ["n", "U", "Sublinhado", "text-decoration:underline"], ["m", "S", "Riscado", "text-decoration:line-through"],
  ["k", "?", "Ofuscado (letras embaralhadas)", ""], ["r", "↺", "Limpar cor e formatação", ""],
];
const LIST_WIDTH = 265;  // largura aproximada do texto na lista de servidores do Minecraft, em pixels

// ---------------------------------------------------------------- leitura e desenho

const freshStyle = () => ({ color: null, bold: false, italic: false, underline: false, strike: false, obf: false });

// "&aOi &lmundo" -> [{text:"Oi ", color:"#55FF55", ...}, {text:"mundo", bold:true, ...}]
function parseMc(text) {
  const segs = [];
  let style = freshStyle();
  let buf = "";
  const flush = () => { if (buf) { segs.push({ ...style, text: buf }); buf = ""; } };
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "&" && /[0-9a-fk-or]/.test(text[i + 1] || "")) {
      flush();
      const c = text[++i];
      if (c === "r") style = freshStyle();
      else if (MC_COLOR[c]) style = { ...freshStyle(), color: MC_COLOR[c] };  // no Minecraft, uma cor zera o estilo
      else style = { ...style, [MC_STYLE[c]]: true };
    } else {
      buf += text[i];
    }
  }
  flush();
  return segs;
}

const mcPlain = (text) => parseMc(text).map((s) => s.text).join("");

// Texto formatado como elementos (sem innerHTML, então nada digitado vira HTML).
// { trim: true } tira os espaços do começo (usados para alinhar) quando o texto aparece no site.
function mcNodes(text, { trim = false } = {}) {
  const frag = document.createDocumentFragment();
  for (const line of String(text ?? "").split("\n").slice(0, 2)) {
    const row = h("div", { class: "mc-line" });
    let first = true;
    for (const seg of parseMc(line)) {
      let t = seg.text;
      if (trim && first) t = t.replace(/^ +/, "");
      if (!t) continue;
      first = false;
      const decor = [seg.underline && "underline", seg.strike && "line-through"].filter(Boolean).join(" ");
      const span = h("span", {
        class: (seg.obf ? "obf " : "") + (seg.color === "#000000" ? "blk" : ""),
        style: [seg.color && `color:${seg.color}`, seg.bold && "font-weight:800", seg.italic && "font-style:italic",
                decor && `text-decoration:${decor}`].filter(Boolean).join(";"),
      }, t);
      if (seg.obf) span.dataset.orig = t;
      row.append(span);
    }
    frag.append(row);
  }
  startObfuscation();
  return frag;
}

let obfTimer = null;
function startObfuscation() {
  if (obfTimer) return;
  const pool = "!#$%&*+-=?@ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";
  obfTimer = setInterval(() => {
    document.querySelectorAll(".obf").forEach((el) => {
      el.textContent = [...(el.dataset.orig || "")].map((c) => (c === " " ? " " : pool[Math.floor(Math.random() * pool.length)])).join("");
    });
  }, 90);
}

// ---------------------------------------------------------------- largura aproximada e alinhamento

// Avanço (em pixels) de cada letra na fonte padrão do Minecraft. O que não está aqui vale 6.
const NARROW = { i: 2, "!": 2, ".": 2, ",": 2, ":": 2, ";": 2, "'": 2, "|": 2, l: 3, "`": 3, I: 4, t: 4, "[": 4, "]": 4,
  "(": 4, ")": 4, "{": 4, "}": 4, "*": 4, '"': 4, " ": 4, f: 5, k: 5, "<": 5, ">": 5, "@": 7, "~": 7 };

function mcWidth(line) {
  return parseMc(line).reduce((sum, seg) => sum + [...seg.text].reduce((w, ch) => w + (NARROW[ch] ?? 6) + (seg.bold ? 1 : 0), 0), 0);
}

// Alinha uma linha colocando espaços à esquerda (é assim que se faz no Minecraft).
function alignLine(line, mode) {
  const bare = line.replace(/^ +/, "");
  if (mode === "left") return bare;
  const free = LIST_WIDTH - mcWidth(bare);
  const pixels = mode === "center" ? free / 2 : free;
  return " ".repeat(Math.max(0, Math.round(pixels / 4))) + bare;  // um espaço tem 4 pixels
}

// ---------------------------------------------------------------- editor

const ALIGN_ICONS = {
  left: '<svg viewBox="0 0 16 16" width="16" height="16"><path d="M2 3h12M2 7h8M2 11h12M2 15h6" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>',
  center: '<svg viewBox="0 0 16 16" width="16" height="16"><path d="M2 3h12M4 7h8M2 11h12M5 15h6" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>',
  right: '<svg viewBox="0 0 16 16" width="16" height="16"><path d="M2 3h12M6 7h8M2 11h12M8 15h6" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>',
};

// Monta o editor dentro de `root`. Devolve { value, setIcon(url), setName(text) }.
function mountMotdEditor(root, { value = "", iconUrl = "assets/default-icon.png", name = "", onChange = () => {} } = {}) {
  const input = h("textarea", { class: "motd-input", rows: 2, maxLength: 160, spellcheck: false, placeholder: "&aBem-vindo ao meu servidor!" });
  input.value = value;
  const icon = h("img", { class: "mc-icon", src: iconUrl, alt: "" });
  const nameEl = h("div", { class: "mc-name" }, name || "Meu servidor");
  const text = h("div", { class: "mc-text" });

  const refresh = () => {
    // No máximo 2 linhas: é o que cabe na lista do Minecraft.
    const lines = input.value.replace(/\r/g, "").split("\n");
    if (lines.length > 2) input.value = lines.slice(0, 2).join("\n");
    text.replaceChildren(mcNodes(input.value));
    onChange(input.value);
  };

  const insert = (code) => {
    const { selectionStart: s, selectionEnd: e, value: v } = input;
    if (e > s) {  // com texto selecionado, vale só para ele
      input.value = v.slice(0, s) + code + v.slice(s, e) + "&r" + v.slice(e);
      input.setSelectionRange(e + code.length + 2, e + code.length + 2);
    } else {
      input.value = v.slice(0, s) + code + v.slice(e);
      input.setSelectionRange(s + code.length, s + code.length);
    }
    input.focus();
    refresh();
  };

  const align = (mode) => {
    const lines = input.value.split("\n");
    const at = input.value.slice(0, input.selectionStart).split("\n").length - 1;
    lines[at] = alignLine(lines[at], mode);
    input.value = lines.join("\n");
    input.focus();
    refresh();
  };

  const colors = MC_COLORS.map(([code, hex, label]) =>
    h("button", { type: "button", class: "sw" + (hex === "#000000" ? " sw-dark" : ""), title: `${label} (&${code})`, style: `--c:${hex}`, onclick: () => insert("&" + code) }, code));
  const formats = MC_FORMATS.map(([code, label, tip, css]) =>
    h("button", { type: "button", class: "tb", title: `${tip} (&${code})`, style: css, onclick: () => insert("&" + code) }, label));
  const aligns = ["left", "center", "right"].map((mode) =>
    h("button", { type: "button", class: "tb", title: { left: "Alinhar à esquerda", center: "Centralizar (aproximado)", right: "Alinhar à direita (aproximado)" }[mode],
                  innerHTML: ALIGN_ICONS[mode], onclick: () => align(mode) }));

  root.replaceChildren(
    h("div", { class: "motd-toolbar" }, h("div", { class: "tb-group" }, colors), h("div", { class: "tb-group" }, formats), h("div", { class: "tb-group" }, aligns)),
    input,
    h("div", { class: "hint" }, "Digite ou use a barra: &a deixa verde, &l negrito, &r limpa. Enter cria a 2ª linha (máximo 2). Selecione um trecho para formatar só ele."),
    h("div", { class: "mc-list" }, icon, h("div", { class: "mc-body" }, nameEl, text)),
  );
  input.addEventListener("input", refresh);
  refresh();

  return {
    get value() { return input.value; },
    setIcon(url) { icon.src = url; },
    setName(t) { nameEl.textContent = t || "Meu servidor"; },
  };
}

// ---------------------------------------------------------------- ícone (capa)

const iconUrl = (server) => `/api/servers/${encodeURIComponent(server.id)}/icon?v=${server.iconVersion}`;

// Qualquer imagem -> PNG 64x64 (o Minecraft só aceita esse tamanho), cortando o centro. Mantém a transparência.
async function iconBlobFromFile(file) {
  if (!file.type.startsWith("image/")) throw new Error("Escolha um arquivo de imagem (PNG, JPG, WebP ou GIF).");
  if (file.size > 10 * 1024 * 1024) throw new Error("A imagem passa de 10 MB.");
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("Não consegui abrir essa imagem."));
      el.src = url;
    });
    const side = Math.min(img.naturalWidth, img.naturalHeight);
    if (!side) throw new Error("Essa imagem não tem tamanho definido. Use PNG, JPG ou WebP.");
    const canvas = Object.assign(document.createElement("canvas"), { width: 64, height: 64 });
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(img, (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, 64, 64);
    return await new Promise((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Falha ao converter a imagem."))), "image/png"));
  } finally {
    URL.revokeObjectURL(url);
  }
}

function setFavicon(href, type) {
  let link = document.querySelector('link[rel="icon"]');
  if (!link) link = document.head.appendChild(h("link", { rel: "icon" }));
  link.type = type;
  link.href = href;
}
