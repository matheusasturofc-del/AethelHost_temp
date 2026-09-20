// Idioma da interface (Português / English). O código do site continua escrito em português:
// este arquivo troca os textos pelo inglês (dicionário em i18n-en.js) e volta ao português sem recarregar.
// Depende de i18n-en.js (I18N_EXACT, I18N_PATTERNS).

const I18N = (() => {
  const STORE_KEY = "bh_lang";
  const ATTRS = ["placeholder", "title", "alt", "aria-label"];
  // Não traduzimos o que é conteúdo do usuário, código ou saída do Minecraft.
  const SKIP_TEXT = "script, style, textarea, code, .mc-line, .mc-text, .mc-name, .console .t";
  const SKIP_ATTR = ".mc-line, .mc-text, .mc-name";  // o placeholder de um campo pode ser traduzido

  // Quem entra pela primeira vez vê em inglês; depois vale a escolha do botão PT | EN, que fica guardada no navegador.
  let lang = "en";
  try {
    const saved = localStorage.getItem(STORE_KEY);
    if (saved === "pt" || saved === "en") lang = saved;
  } catch { /* sem armazenamento: fica em inglês */ }

  const nodes = new Map();  // nó de texto -> { orig, out }
  const attrs = new Map();  // elemento -> { atributo: { orig, out } }

  // Textos montados pelo código ("a · b" ou "Frase um. Frase dois.") são traduzidos pedaço por pedaço.
  function translateParts(text, splitter, joiner) {
    const parts = text.split(splitter);
    if (parts.length < 2) return text;
    const out = parts.map(translateLine);
    return out.some((p, i) => p !== parts[i]) ? out.join(joiner) : text;
  }

  function translateLine(text) {
    if (I18N_EXACT[text] !== undefined) return I18N_EXACT[text];
    for (const [re, to] of I18N_PATTERNS) if (re.test(text)) return text.replace(re, to);
    if (text.includes(" · ")) { const r = translateParts(text, " · ", " · "); if (r !== text) return r; }
    if (/[.!?]\s/.test(text)) { const r = translateParts(text, /(?<=[.!?])\s+/, " "); if (r !== text) return r; }
    return text;
  }

  // Traduz um texto mantendo os espaços das pontas (o HTML tem quebras de linha e recuos).
  function tr(text) {
    text = String(text ?? "");
    if (lang === "pt") return text;
    const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(text);
    const core = m[2].replace(/\s+/g, " ");
    if (!core) return text;
    const out = translateLine(core);
    return out === core ? text : m[1] + out + m[3];
  }

  // translate="no" protege um bloco (nome de servidor, arquivo…), e translate="yes" dentro dele libera um pedaço.
  function skipped(node, selector = SKIP_TEXT) {
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return true;
    const flag = el.closest("[translate]");
    return (!!flag && flag.getAttribute("translate") === "no") || !!el.closest(selector);
  }

  function applyText(node) {
    if (skipped(node)) return;
    const cur = node.nodeValue;
    const rec = nodes.get(node);
    if (rec && rec.out === cur) return;  // já é a nossa tradução
    const out = tr(cur);
    if (out !== cur) { nodes.set(node, { orig: cur, out }); node.nodeValue = out; } else nodes.delete(node);
  }

  function applyAttr(el, name) {
    if (skipped(el, SKIP_ATTR)) return;
    const cur = el.getAttribute(name);
    if (cur === null) return;
    const rec = (attrs.get(el) || {})[name];
    if (rec && rec.out === cur) return;
    const out = tr(cur);
    if (out !== cur) {
      const all = attrs.get(el) || {};
      all[name] = { orig: cur, out };
      attrs.set(el, all);
      el.setAttribute(name, out);
    }
  }

  function walk(root) {
    if (lang === "pt" || !root) return;
    if (root.nodeType === 3) { applyText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    const base = root.nodeType === 9 ? root.documentElement : root;
    const tw = document.createTreeWalker(base, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) applyText(n);
    const els = base.querySelectorAll ? [base, ...base.querySelectorAll(ATTRS.map((a) => `[${a}]`).join(","))] : [];
    for (const el of els) for (const a of ATTRS) if (el.hasAttribute && el.hasAttribute(a)) applyAttr(el, a);
  }

  function restore() {
    for (const [node, rec] of nodes) if (node.isConnected && node.nodeValue === rec.out) node.nodeValue = rec.orig;
    for (const [el, all] of attrs) for (const [name, rec] of Object.entries(all)) if (el.getAttribute(name) === rec.out) el.setAttribute(name, rec.orig);
    nodes.clear();
    attrs.clear();
  }

  function paintButton() {
    const btn = document.querySelector(".lang-btn");
    if (!btn) return;
    btn.replaceChildren(...["pt", "en"].map((l) => Object.assign(document.createElement("span"), { textContent: l.toUpperCase(), className: l === lang ? "on" : "" })));
    btn.title = "Idioma / Language";
  }

  function set(next, announce = true) {
    if (next === lang && announce) return;
    if (lang === "en") restore();
    lang = next;
    try { localStorage.setItem(STORE_KEY, lang); } catch { /* tudo bem: só não lembra da escolha */ }
    document.documentElement.lang = lang === "en" ? "en" : "pt-BR";
    walk(document);
    paintButton();
    if (announce) window.dispatchEvent(new Event("langchange"));
  }

  function mountButton() {
    const nav = document.querySelector(".topnav");
    if (!nav || nav.querySelector(".lang-btn")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "lang-btn";
    btn.setAttribute("translate", "no");
    btn.addEventListener("click", () => set(lang === "en" ? "pt" : "en"));
    nav.append(btn);
  }

  // Tudo o que o código cria ou altera depois (mensagens, listas, avisos) também é traduzido.
  new MutationObserver((records) => {
    if (lang === "pt") return;
    for (const r of records) {
      if (r.type === "childList") r.addedNodes.forEach((n) => walk(n));
      else if (r.type === "characterData") applyText(r.target);
      else if (r.type === "attributes") applyAttr(r.target, r.attributeName);
    }
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });

  // Janelas do navegador (confirmar, perguntar, avisar) também falam o idioma escolhido.
  for (const name of ["confirm", "alert"]) {
    const native = window[name].bind(window);
    window[name] = (message) => native(tr(message));
  }
  const nativePrompt = window.prompt.bind(window);
  window.prompt = (message, value) => nativePrompt(tr(message), value);

  I18N_tr = tr;
  mountButton();
  set(lang, false);

  I18N_tr = tr;
  return { tr, set, get lang() { return lang; } };
})();
