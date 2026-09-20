// Aba Opções: configurações do jogo (server.properties) e regras do jogo (gamerules).
// Cada opção é uma linha com o controle e, embaixo, a chave como o Minecraft a escreve (chave=valor).
// Usa as variáveis do painel: $, base. Rótulos em options-labels.js.

const opt = { data: null, dirtyProps: {}, dirtyRules: {} };

async function openOptions() {
  await loadOptions();
}

async function loadOptions() {
  let data;
  try {
    data = await api("GET", `${base}/settings`);
  } catch (e) {
    $("opErr").textContent = e.message;
    return;
  }
  $("opSections").hidden = !data.supported;
  $("opNotice").hidden = data.supported;
  if (!data.supported) {
    $("opNotice").textContent = "Esta aba ainda não está disponível na VPS. Use o plano Grátis.";
    return;
  }
  opt.data = data;
  renderOptions();
}

const optIsRunning = () => !!opt.data && opt.data.state !== "offline";
const optString = (v) => (typeof v === "boolean" ? String(v) : String(v ?? ""));

function optChoice(key, value) {
  const pair = (OPT_CHOICES[key] || {})[value];
  return pair ? pair[isEnglish() ? 1 : 0] : value;
}

function renderOptions() {
  const d = opt.data;
  const running = optIsRunning();
  $("opRunNote").hidden = !running;
  const wl = opt.dirtyProps["white-list"] ?? (d.properties["white-list"] || {}).value;
  $("opWlHint").hidden = !wl;

  const keys = Object.keys(OPT_LABELS).filter((k) => d.properties[k]);
  $("opProps").replaceChildren(...keys.map((k) => optRow("prop", k, d.properties[k], running)));
  $("opPropsCount").textContent = keys.length;

  const collator = new Intl.Collator(isEnglish() ? "en" : "pt");
  const names = Object.keys(d.gamerules).sort((a, b) => collator.compare(ruleLabel(a, d.gamerules[a].label), ruleLabel(b, d.gamerules[b].label)));
  $("opRules").replaceChildren(...names.map((n) => optRow("rule", n, d.gamerules[n], false)));
  $("opRulesCount").textContent = names.length;

  applyOptFilter();
  updateOptBar();
}

function optRow(kind, key, entry, disabled) {
  const store = kind === "prop" ? opt.dirtyProps : opt.dirtyRules;
  const label = kind === "prop" ? optLabel(key) : ruleLabel(key, entry.label);
  const keyLine = h("div", { class: "opt-key", translate: "no" });
  const row = h("div", { class: "opt" + (kind === "rule" ? " compact" : ""), "data-q": `${label} ${key}`.toLowerCase() });

  const paint = () => {
    const value = key in store ? store[key] : entry.value;
    keyLine.textContent = `${key}=${optString(value)}`;
    row.classList.toggle("dirty", key in store);
  };
  const set = (value) => {
    if (value === entry.value) delete store[key]; else store[key] = value;
    paint();
    updateOptBar();
    if (key === "white-list") $("opWlHint").hidden = !value;
  };

  const current = key in store ? store[key] : entry.value;
  row.append(
    h("div", { class: "opt-main" }, h("span", { class: "opt-label", translate: "no", title: label }, label), optControl(entry, key, current, set, disabled)),
    keyLine,
  );
  paint();
  return row;
}

function optControl(entry, key, current, set, disabled) {
  if (entry.type === "bool") {
    return h("label", { class: "tgl" + (disabled ? " off" : "") },
      h("input", { type: "checkbox", checked: !!current, disabled, onchange: (e) => set(e.target.checked) }),
      h("span", { class: "tgl-box" }));
  }
  if (entry.type === "select") {
    return h("select", { disabled, onchange: (e) => set(e.target.value) },
      entry.options.map((o) => h("option", { value: o, selected: o === current }, optChoice(key, o))));
  }
  if (entry.type === "int") {
    const input = h("input", { type: "number", value: current, min: entry.min, max: entry.max, disabled });
    const commit = (raw) => {
      let n = Math.round(Number(raw));
      if (!Number.isFinite(n)) n = entry.value;
      if (entry.min !== undefined) n = Math.max(entry.min, n);
      if (entry.max !== undefined) n = Math.min(entry.max, n);
      input.value = n;
      set(n);
    };
    input.addEventListener("change", () => commit(input.value));
    const step = (by) => h("button", { type: "button", class: "num-btn", disabled, onclick: () => commit(Number(input.value) + by) }, by > 0 ? "+" : "−");
    return h("div", { class: "num" }, step(-1), input, step(1));
  }
  return h("input", { type: "text", class: "opt-text", value: current, disabled, maxLength: 250,
                      placeholder: key === "resource-pack" ? "https://example.com/pack.zip" : "", oninput: (e) => set(e.target.value) });
}

function applyOptFilter() {
  const q = $("opSearch").value.trim().toLowerCase();
  document.querySelectorAll("#opProps .opt, #opRules .opt").forEach((row) => { row.hidden = !!q && !row.dataset.q.includes(q); });
}
$("opSearch").addEventListener("input", applyOptFilter);

function updateOptBar() {
  const n = Object.keys(opt.dirtyProps).length + Object.keys(opt.dirtyRules).length;
  $("opBar").hidden = n === 0;
  $("opCount").textContent = n === 1 ? "1 alteração não salva" : `${n} alterações não salvas`;
}

$("opDiscard").addEventListener("click", () => {
  opt.dirtyProps = {};
  opt.dirtyRules = {};
  $("opErr").textContent = $("opOk").textContent = "";
  renderOptions();
});

$("opSave").addEventListener("click", async () => {
  $("opErr").textContent = $("opOk").textContent = "";
  $("opSave").disabled = true;
  const props = opt.dirtyProps, rules = opt.dirtyRules;
  try {
    let applied = false;
    if (Object.keys(props).length) await api("POST", `${base}/settings/properties`, { changes: props });
    if (Object.keys(rules).length) applied = (await api("POST", `${base}/settings/gamerules`, { changes: rules })).applied;
    const said = ["Alterações salvas."];
    if (Object.keys(props).length) said.push("As configurações valem na próxima vez que o servidor ligar.");
    if (Object.keys(rules).length) said.push(applied ? "As regras do jogo já foram aplicadas." : "As regras do jogo serão aplicadas quando o servidor ligar.");
    opt.dirtyProps = {};
    opt.dirtyRules = {};
    $("opOk").textContent = said.join(" ");
    await loadOptions();
  } catch (e) {
    $("opErr").textContent = e.message;
  } finally {
    $("opSave").disabled = false;
  }
});
