// Captcha (Cloudflare Turnstile) na criação de conta e de servidor. Só aparece se o administrador configurou uma chave.
let CAPTCHA_KEY = "";

async function loadCaptchaKey() {
  try { CAPTCHA_KEY = (await api("GET", "/captcha/config")).siteKey || ""; } catch { CAPTCHA_KEY = ""; }
  if (CAPTCHA_KEY && !document.getElementById("cfTurnstileScript")) {
    window.onTurnstileReady = () => window.dispatchEvent(new Event("turnstile-ready"));
    const s = document.createElement("script");
    s.id = "cfTurnstileScript";
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onTurnstileReady";
    s.async = true; s.defer = true;
    document.head.appendChild(s);
  }
}

// Devolve { box, getToken, reset }. `box` é null se o captcha não estiver configurado (não muda a tela).
function captchaField() {
  if (!CAPTCHA_KEY) return { box: null, getToken: () => "", reset: () => {} };
  let token = "", widgetId = null;
  const box = h("div", { class: "captcha-box" });
  const mount = () => {
    widgetId = window.turnstile.render(box, {
      sitekey: CAPTCHA_KEY, theme: document.documentElement.dataset.theme === "light" ? "light" : "dark",
      callback: (t) => { token = t; }, "expired-callback": () => { token = ""; },
    });
  };
  if (window.turnstile) mount(); else window.addEventListener("turnstile-ready", mount, { once: true });
  return { box, getToken: () => token, reset: () => { token = ""; if (widgetId !== null) window.turnstile.reset(widgetId); } };
}
