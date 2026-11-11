// Bot de consulta de cupons do Telegram (Cloudflare Workers, plano grátis).
// Você manda o nome da loja ("renner") e ele responde com os cupons que o monitor já coletou.
// O monitor (GitHub Actions) publica os cupons em coupons.json na branch "data"; este bot só LÊ esse arquivo.
//
// Variáveis no Cloudflare:  BOT_TOKEN (secret) · CHAT_ID · WEBHOOK_SECRET (secret, qualquer texto longo)
//                           COUPONS_URL (opcional; padrão abaixo)

const DEFAULT_COUPONS_URL = "https://raw.githubusercontent.com/lucash2501-byte/bf-monitor/data/coupons.json";
const STALE_HOURS = 8;       // avisa quando os dados estão velhos (o agendamento do GitHub atrasa às vezes)
const MAX_COUPONS = 10;

const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const norm = (t) => String(t).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "");
const words = (t) => String(t).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

function ago(iso, now = Date.now()) {
  if (!iso) return null;
  const min = Math.max(0, Math.round((now - Date.parse(iso)) / 60000));
  if (min < 2) return "agora há pouco";
  if (min < 90) return `há ${min} min`;
  const h = Math.round(min / 60);
  return h < 48 ? `há ${h} h` : `há ${Math.round(h / 24)} dias`;
}

// Quais lojas a mensagem cita? Aceita "renner", "cupons da Renner", "lojas renner", "Mercado Livre"...
export function findStores(text, stores) {
  const tokens = words(text);
  const joined = tokens.join("");
  const chunks = String(text).split(/\s+/).map(norm).filter(Boolean);   // "c&a" vira "ca" (uma palavra só)
  const hits = [];
  for (const s of stores) {
    const keys = [s.name, ...(s.aliases || [])].map(norm).filter(Boolean);
    const ok = keys.some((k) =>
      k.length <= 3 ? (tokens.includes(k) || chunks.includes(k))
        : joined.includes(k) || (tokens.length === 1 && tokens[0].length >= 3 && k.startsWith(tokens[0])));
    if (ok) hits.push(s);
  }
  return hits;
}

export function formatStore(s, now = Date.now()) {
  const own = s.coupons.filter((c) => c.source !== "agregador");
  const agg = s.coupons.filter((c) => c.source === "agregador");
  const lines = [`🎟️ <b>${esc(s.name)}</b>`];
  if (!own.length && !agg.length) {
    lines.push(s.error
      ? `Não consegui ler a loja agora (${esc(s.error)}): ela costuma bloquear o monitor. Confira direto no site.`
      : "Nenhum cupom publicado na página da loja agora.");
  }
  for (const c of own.slice(0, MAX_COUPONS)) lines.push(`• ${esc(c.text)}`);
  if (agg.length) {
    lines.push("", "<i>De sites de cupom (podem estar vencidos — confira no carrinho):</i>");
    for (const c of agg.slice(0, 5)) lines.push(`• ${esc(c.text)}`);
  }
  const when = ago(s.checked_at, now);
  lines.push("", `🕒 Conferido ${when || "ainda não"} · <a href="${esc(s.url)}">abrir a loja</a>`);
  return lines.join("\n");
}

export function answer(text, data, now = Date.now()) {
  const t = String(text || "").trim();
  const names = data.stores.map((s) => s.name).sort((a, b) => a.localeCompare(b, "pt-BR"));
  if (/^\/(start|ajuda|help)\b/i.test(t) || !t) {
    return "Mande o <b>nome da loja</b> (ex.: <code>renner</code>) e eu respondo com os cupons que o monitor achou.\n" +
           "<code>/lojas</code> mostra as lojas que eu acompanho.";
  }
  if (/^\/lojas\b/i.test(t)) return "Lojas que eu acompanho:\n" + names.map((n) => `• ${esc(n)}`).join("\n");
  const hits = findStores(t, data.stores);
  if (!hits.length) {
    return `Não acompanho essa loja. Tente: ${names.slice(0, 12).map(esc).join(", ")}… (veja <code>/lojas</code>).`;
  }
  let out = hits.slice(0, 3).map((s) => formatStore(s, now)).join("\n\n");
  const age = (now - Date.parse(data.generated_at)) / 3600000;
  if (age > STALE_HOURS) {
    out += `\n\n⚠️ Os dados são de ${ago(data.generated_at, now)}: o monitor está atrasado, então pode ter cupom novo.`;
  }
  return out;
}

async function telegram(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return r.json().catch(() => ({}));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET") {
      if (url.pathname === "/setup") {   // abra uma vez no navegador: liga o Telegram a este bot
        const res = await telegram(env, "setWebhook", {
          url: `${url.origin}/`, secret_token: env.WEBHOOK_SECRET, allowed_updates: ["message"],
        });
        return new Response(res.ok ? "Pronto! O bot está ligado. Mande o nome de uma loja no Telegram."
                                   : `Falhou: ${JSON.stringify(res)}`, { status: res.ok ? 200 : 500 });
      }
      return new Response("bf-monitor bot no ar");
    }
    if (request.method !== "POST") return new Response("método não permitido", { status: 405 });
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("não autorizado", { status: 401 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }
    const msg = update.message || update.edited_message;
    if (!msg || !msg.text || String(msg.chat?.id) !== String(env.CHAT_ID)) return new Response("ok");  // só responde a você

    let text;
    try {
      const res = await fetch(env.COUPONS_URL || DEFAULT_COUPONS_URL, { cf: { cacheTtl: 60 } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      text = answer(msg.text, await res.json());
    } catch (e) {
      text = "Ainda não tenho os dados dos cupons (o monitor precisa rodar ao menos uma vez). Tente de novo em alguns minutos.";
    }
    await telegram(env, "sendMessage", {
      chat_id: msg.chat.id, text, parse_mode: "HTML", disable_web_page_preview: true,
    });
    return new Response("ok");
  },
};
