// Conta do usuário: menu no topo (nome, foto e Sair) e proteção das páginas. Depende de store.js (api, h).

(() => {
  const PROTECTED = ["servers.html", "create.html", "panel.html", "admin.html", "profile.html", "settings.html"];
  const page = location.pathname.split("/").pop() || "index.html";

  function avatar(user) {
    const initial = (user.name || "?").trim().charAt(0).toUpperCase();
    const fallback = h("span", { class: "avatar", translate: "no" }, initial);
    if (!user.avatar) return fallback;
    const img = h("img", { class: "avatar", src: user.avatar, alt: "", referrerPolicy: "no-referrer" });
    img.addEventListener("error", () => img.replaceWith(fallback));
    return img;
  }

  // ---- Sino de notificações: convites de compartilhamento e avisos
  const LEVEL = { full: "Completo", basic: "Básico" };
  const FILES = { none: "Nenhum", read: "Somente leitura", write: "Leitura e escrita" };
  const BELL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>';

  function notifText(n) {
    const d = n.data;
    const name = [h("b", { translate: "no" }, d.fromName), d.fromUsername ? h("span", { class: "muted", translate: "no" }, " @" + d.fromUsername) : null];
    const srv = h("b", { translate: "no" }, d.serverName);
    const parts = {
      share_invite: [" quer compartilhar o servidor ", srv, " com você."],
      share_accepted: [" aceitou o convite para o servidor ", srv, "."],
      share_declined: [" recusou o convite para o servidor ", srv, "."],
      share_changed: [" mudou o seu acesso ao servidor ", srv, "."],
      share_removed: [" removeu o seu acesso ao servidor ", srv, "."],
      share_left: [" saiu do servidor ", srv, "."],
    }[n.type] || [];
    return h("div", {}, ...name, ...parts);
  }

  function mountBell(nav) {
    const count = h("span", { class: "bell-count", hidden: true });
    const btn = h("button", { class: "bell", type: "button", "aria-haspopup": "true", "aria-expanded": "false", title: "Notificações", innerHTML: BELL }, count);
    const body = h("div", {});
    const pop = h("div", { class: "notif-pop", hidden: true },
      h("div", { class: "notif-head" }, h("span", {}, "Notificações"),
        h("button", { class: "linkbtn", type: "button", onclick: async () => { try { await api("POST", "/notifications/read", {}); } catch { /* segue */ } refresh(); } }, "Marcar como lidas")),
      body);

    async function answer(n, accept, item) {
      item.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        await api("POST", `/notifications/${n.id}/${accept ? "accept" : "decline"}`, {});
        window.dispatchEvent(new Event("bh-servers-changed"));  // a lista de servidores da página se atualiza
      } catch (e) {
        item.append(h("div", { class: "error", style: "margin-top:8px" }, e.message));
      }
      refresh();
    }

    function paint(data) {
      count.hidden = !data.unread;
      count.textContent = data.unread > 9 ? "9+" : String(data.unread);
      body.replaceChildren(...(data.items.length ? data.items.map((n) => {
        const item = h("div", { class: "notif-item" + (n.read ? "" : " unread") }, notifText(n),
          n.data.level ? h("div", { class: "lv" }, h("span", {}, "Acesso: ", h("b", {}, LEVEL[n.data.level] || "")), h("span", {}, "Arquivos: ", h("b", {}, FILES[n.data.files] || ""))) : null);
        if (n.type === "share_invite") {
          item.append(h("div", { class: "notif-actions" },
            h("button", { class: "btn btn-primary", type: "button", onclick: () => answer(n, true, item) }, "Aceitar"),
            h("button", { class: "btn", type: "button", onclick: () => answer(n, false, item) }, "Recusar")));
        } else {
          item.append(h("div", { class: "when" }, fmtDate(n.ts), " · ",
            h("button", { class: "linkbtn", type: "button", onclick: async () => { try { await api("DELETE", `/notifications/${n.id}`, {}); } catch { /* segue */ } refresh(); } }, "Dispensar")));
        }
        return item;
      }) : [h("div", { class: "notif-empty" }, "Nenhuma notificação.")]));
    }

    async function refresh() {
      try { paint(await api("GET", "/notifications")); } catch { /* sem sino atualizado por enquanto */ }
    }

    const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      pop.hidden = !pop.hidden;
      btn.setAttribute("aria-expanded", String(!pop.hidden));
      if (!pop.hidden) { await refresh(); try { await api("POST", "/notifications/read", {}); } catch { /* segue */ } setTimeout(refresh, 1500); }
    });
    document.addEventListener("click", (ev) => { if (!pop.contains(ev.target) && ev.target !== btn) close(); });
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close(); });
    nav.append(h("div", { class: "bell-wrap" }, btn, pop));
    refresh();
    setInterval(() => { if (!document.hidden && pop.hidden) refresh(); }, 20000);
    window.addEventListener("langchange", refresh);
  }

  // Clicar no perfil abre um menu: Ver perfil, Administração (só a administradora) e Sair.
  function mountMenu(user) {
    const nav = document.querySelector(".topnav");
    if (!nav) return;
    if (!user) {
      if (page !== "login.html") nav.append(h("a", { class: "btn nav-login", href: "login.html" }, "Entrar"));
      return;
    }
    const pop = h("div", { class: "user-pop", role: "menu", hidden: true },
      h("div", { class: "who" }, h("b", { translate: "no" }, user.name), user.username ? h("span", { translate: "no" }, "@" + user.username) : null),
      h("a", { href: "profile.html", role: "menuitem" }, "Ver perfil"),
      h("a", { href: "settings.html", role: "menuitem" }, "Editar perfil"),
      user.admin ? h("a", { href: "admin.html", role: "menuitem" }, "Administração") : null,
      h("button", { class: "danger", type: "button", role: "menuitem", onclick: async () => {
        try { await api("POST", "/auth/logout"); } catch { /* sai mesmo assim */ }
        location.href = "index.html";
      } }, "Sair"));
    const btn = h("button", { class: "user-btn", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", title: user.email || "" },
      avatar(user), h("span", { class: "user-name", translate: "no" }, user.name), h("span", { class: "caret" }));
    const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      pop.hidden = !pop.hidden;
      btn.setAttribute("aria-expanded", String(!pop.hidden));
    });
    document.addEventListener("click", (ev) => { if (!pop.contains(ev.target)) close(); });
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close(); });
    mountBell(nav);
    nav.append(h("div", { class: "user-menu" }, btn, pop));
  }

  window.BH_ME_READY = (async () => {
    let user = null;
    try {
      const me = await (await fetch("/api/auth/me")).json();
      user = me.user;
      window.BH_SUGGESTED = me.suggestedUsername || "";
    } catch { /* backend fora do ar: cada página mostra o próprio aviso */ }
    window.BH_USER = user;
    // O tema escolhido fica na conta: vale em qualquer aparelho em que a pessoa entrar
    if (user && user.prefs && user.prefs.theme && window.BH_THEME && BH_THEME.get() !== user.prefs.theme) BH_THEME.set(user.prefs.theme);
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
