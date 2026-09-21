// Tema do site: escuro (padrão) ou claro (branco). Vai no <head> de cada página para o tema certo aparecer já na primeira pintura.
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
