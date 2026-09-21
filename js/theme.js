// Tema do site: escuro (padrão) ou claro (branco). Vai no <head> de cada página para o tema certo aparecer já na primeira pintura.
// Quem abre o site pela primeira vez (ou escolheu inglês) vê em inglês. O HTML está escrito em português, então a página fica
// escondida até o i18n.js traduzir (ele tira a classe i18n-wait); sem isso apareceria um instante em português.
// Se algo der errado e o i18n.js não rodar, a página aparece sozinha depois de 2,5 s.
(() => {
  let saved = null;
  try { saved = localStorage.getItem("bh_lang"); } catch { /* sem armazenamento: o site abre em inglês */ }
  if (saved !== "pt") {
    document.documentElement.classList.add("i18n-wait");
    setTimeout(() => document.documentElement.classList.remove("i18n-wait"), 2500);
  }
})();

(() => {
  const KEY = "bh_theme";
  const read = () => {
    try { const t = localStorage.getItem(KEY); return t === "light" || t === "dark" ? t : "dark"; } catch { return "dark"; }
  };
  document.documentElement.dataset.theme = read();
  window.BH_THEME = {
    get: read,
    set(theme) {
      theme = theme === "light" ? "light" : "dark";
      try { localStorage.setItem(KEY, theme); } catch { /* tudo bem: só não lembra da escolha */ }
      document.documentElement.dataset.theme = theme;
      window.dispatchEvent(new Event("themechange"));
    },
  };
})();
