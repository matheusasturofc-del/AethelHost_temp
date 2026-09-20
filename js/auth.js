// Conta do usuário: menu no topo (nome, foto e Sair) e proteção das páginas. Depende de store.js (api, h).

(() => {
  const PROTECTED = ["servers.html", "create.html", "panel.html", "admin.html"];
  const page = location.pathname.split("/").pop() || "index.html";

  function avatar(user) {
    const initial = (user.name || "?").trim().charAt(0).toUpperCase();
    const fallback = h("span", { class: "avatar", translate: "no" }, initial);
    if (!user.avatar) return fallback;
    const img = h("img", { class: "avatar", src: user.avatar, alt: "", referrerPolicy: "no-referrer" });
    img.addEventListener("error", () => img.replaceWith(fallback));
    return img;
  }

  function mountMenu(user) {
    const nav = document.querySelector(".topnav");
    if (!nav) return;
    if (!user) {
      if (page !== "login.html") nav.append(h("a", { class: "btn nav-login", href: "login.html" }, "Entrar"));
      return;
    }
    if (user.admin) nav.append(h("a", { href: "admin.html", class: page === "admin.html" ? "active" : "" }, "Administração"));
    nav.append(h("div", { class: "user-menu", title: user.email || "" },
      avatar(user),
      h("span", { class: "user-name", translate: "no" }, user.name),
      h("button", { class: "btn nav-out", type: "button", onclick: async () => {
        try { await api("POST", "/auth/logout"); } catch { /* sai mesmo assim */ }
        location.href = "index.html";
      } }, "Sair")));
  }

  window.BH_ME_READY = (async () => {
    let user = null;
    try {
      const me = await (await fetch("/api/auth/me")).json();
      user = me.user;
      window.BH_SUGGESTED = me.suggestedUsername || "";
    } catch { /* backend fora do ar: cada página mostra o próprio aviso */ }
    window.BH_USER = user;
    if (user && user.needsProfile && PROTECTED.includes(page)) {  // conta criada por Google, GitHub…: falta escolher nome exibido e usuário
      location.replace("login.html?profile=1&next=" + encodeURIComponent("/" + page + location.search));
      return;
    }
    if (!user && PROTECTED.includes(page)) {  // o servidor já redireciona; isto cobre uma sessão que acabou de expirar
      location.replace("login.html?next=" + encodeURIComponent("/" + page + location.search));
      return;
    }
    mountMenu(user);
  })();
})();
