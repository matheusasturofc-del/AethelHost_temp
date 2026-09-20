// Componentes de interface do AethelHost. Depende de store.js (h).
//
// enhanceSelect: troca o menu nativo de um <select> por uma lista com o nosso visual.
// O <select> original continua por baixo (escondido), então .value, .options, .disabled e o evento "change"
// funcionam como sempre e nenhum outro código precisa saber que existe uma lista bonita.

const CHEVRON = '<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M2 4.2 6 8l4-3.8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CHECK = '<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M2.2 6.4 5 9l4.8-5.6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

let openSelect = null;  // só uma lista aberta por vez

function enhanceSelect(select) {
  if (select.dataset.cs) return;
  select.dataset.cs = "1";
  const grouped = select.dataset.group === "version";  // versões do Minecraft agrupadas por série (1.21.x, 26.x…)

  const label = h("span", { class: "cs-label" });
  const button = h("button", { type: "button", class: "cs-btn", "aria-haspopup": "listbox", "aria-expanded": "false" },
    label, h("span", { class: "cs-chevron", innerHTML: CHEVRON }));
  const search = h("input", { type: "text", class: "cs-search", placeholder: "Buscar…", autocomplete: "off", spellcheck: false });
  const items = h("div", { class: "cs-items", role: "listbox" });
  const panel = h("div", { class: "cs-panel", hidden: true }, search, items);
  const wrap = h("div", { class: "cs" });
  select.before(wrap);
  wrap.append(select, button, panel);
  select.classList.add("cs-native");
  select.tabIndex = -1;

  const nativeValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  const seriesOf = (text) => { const m = /^(\d+)\.(\d+)/.exec(text); return m ? (Number(m[1]) >= 26 ? `${m[1]}.x` : `${m[1]}.${m[2]}.x`) : ""; };

  function syncLabel() {
    const opt = select.selectedOptions[0];
    label.textContent = opt ? opt.textContent : "";
    label.classList.toggle("cs-empty", !opt);
    button.disabled = select.disabled;
    wrap.classList.toggle("cs-disabled", select.disabled);
  }

  function pick(option) {
    if (option.disabled) return;
    nativeValue.set.call(select, option.value);
    syncLabel();
    select.dispatchEvent(new Event("change", { bubbles: true }));
    close();
  }

  function render(filter = "") {
    const q = filter.trim().toLowerCase();
    const rows = [];
    let lastSeries = null;
    for (const opt of select.options) {
      if (q && !opt.textContent.toLowerCase().includes(q)) continue;
      if (grouped) {
        const series = seriesOf(opt.textContent);
        if (series && series !== lastSeries) { rows.push(h("div", { class: "cs-group" }, series)); lastSeries = series; }
      }
      const selected = opt.value === select.value;
      rows.push(h("div", { class: "cs-item" + (selected ? " selected" : "") + (opt.disabled ? " disabled" : ""), role: "option",
                           "aria-selected": String(selected), "aria-disabled": String(opt.disabled), onmousedown: (e) => { e.preventDefault(); pick(opt); } },
        h("span", { class: "cs-text" }, opt.textContent), selected ? h("span", { class: "cs-check", innerHTML: CHECK }) : null));
    }
    items.replaceChildren(...(rows.length ? rows : [h("div", { class: "cs-none" }, "Nada encontrado")]));
    search.hidden = select.options.length < 12;  // busca só aparece nas listas grandes
  }

  function open() {
    if (select.disabled) return;
    if (openSelect && openSelect !== close) openSelect();
    search.value = "";
    render();
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    wrap.classList.add("open");
    openSelect = close;
    const current = items.querySelector(".selected");
    if (current) current.scrollIntoView({ block: "center" });
    if (!search.hidden) search.focus({ preventScroll: true });
  }

  function close() {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
    wrap.classList.remove("open");
    if (openSelect === close) openSelect = null;
  }

  function move(step) {
    const opts = [...select.options].filter((o) => !o.disabled);
    const at = opts.findIndex((o) => o.value === select.value);
    const next = opts[Math.min(opts.length - 1, Math.max(0, at + step))];
    if (next) { nativeValue.set.call(select, next.value); syncLabel(); render(search.value); select.dispatchEvent(new Event("change", { bubbles: true })); }
  }

  button.addEventListener("click", () => (panel.hidden ? open() : close()));
  button.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); panel.hidden ? open() : move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); panel.hidden ? open() : move(-1); }
    else if (e.key === "Escape") close();
  });
  search.addEventListener("input", () => render(search.value));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); button.focus(); }
    if (e.key === "Enter") {
      e.preventDefault();
      const first = [...select.options].find((o) => !o.disabled && o.textContent.toLowerCase().includes(search.value.trim().toLowerCase()));
      if (first) pick(first);
    }
  });

  // Quando o código mexe no select (troca as opções, o valor ou o disabled), a lista acompanha.
  Object.defineProperty(select, "value", { get() { return nativeValue.get.call(this); }, set(v) { nativeValue.set.call(this, v); syncLabel(); } });
  new MutationObserver(() => { syncLabel(); if (!panel.hidden) render(search.value); })
    .observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });

  syncLabel();
}

document.addEventListener("mousedown", (e) => {
  if (openSelect && !e.target.closest(".cs")) openSelect();
});

function enhanceAllSelects(root = document) {
  root.querySelectorAll("select:not([data-cs])").forEach(enhanceSelect);
}

enhanceAllSelects();
new MutationObserver(() => enhanceAllSelects()).observe(document.body, { childList: true, subtree: true });
