// Banner do perfil: um dos prontos (cores com os ícones do AethelHost) ou uma imagem enviada pela pessoa.
// user.banner vem da API: { preset: "green" } ou { image: "/api/users/…/banner?v=…" }.

const BANNER_PRESETS = ["green", "blue", "purple", "orange", "red", "gray"];

function paintBanner(el, user) {
  const b = (user && user.banner) || { preset: "green" };
  el.className = "banner" + (b.image ? " has-img" : " b-" + (BANNER_PRESETS.includes(b.preset) ? b.preset : "green"));
  el.style.backgroundImage = b.image ? `url("${b.image}")` : "";
}

// Recorta a imagem escolhida para o tamanho certo (no navegador, antes de enviar) e devolve um Blob.
async function cropToBlob(file, width, height, type, quality) {
  const bmp = await createImageBitmap(file);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const scale = Math.max(width / bmp.width, height / bmp.height);  // cobre a área toda, cortando o que sobra no centro
  const sw = width / scale, sh = height / scale;
  canvas.getContext("2d").drawImage(bmp, (bmp.width - sw) / 2, (bmp.height - sh) / 2, sw, sh, 0, 0, width, height);
  return new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Não consegui ler essa imagem."))), type, quality));
}
