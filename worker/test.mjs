// Teste do bot: node worker/test.mjs  (sem dependências; simula o Telegram e o GitHub)
import assert from "node:assert/strict";
import worker from "./worker.js";

const NOW = Date.parse("2026-11-07T22:00:00Z");
const DATA = {
  generated_at: "2026-11-07T21:30:00Z",
  stores: [
    { name: "Renner", aliases: ["renner", "lojasrenner"], url: "https://www.lojasrenner.com.br/", checked_at: "2026-11-07T21:00:00Z", error: null,
      coupons: [{ text: "use o cupom: VOUDEAPP", source: "loja" }] },
    { name: "Mercado Livre", aliases: ["mercadolivre", "ml"], url: "https://www.mercadolivre.com.br/", checked_at: "2026-11-07T21:00:00Z", error: null, coupons: [] },
    { name: "Netshoes", aliases: ["netshoes"], url: "https://www.netshoes.com.br/", checked_at: "2026-11-07T20:00:00Z", error: "HTTP 403",
      coupons: [{ text: "NETS20", source: "agregador" }] },
    { name: "Centauro", aliases: ["centauro"], url: "https://www.centauro.com.br/", checked_at: "2026-11-07T20:00:00Z", error: "HTTP 403", coupons: [] },
    { name: "C&A", aliases: ["cea", "c&a"], url: "https://www.cea.com.br/", checked_at: "2026-11-07T20:00:00Z", error: null,
      coupons: [{ text: "35% OFF - Cupom 11DO11", source: "loja" }] },
    { name: "Authentic Feet", aliases: ["authenticfeet"], url: "https://www.authenticfeet.com.br/", checked_at: null, error: null, coupons: [] },
  ],
};
const ENV = { BOT_TOKEN: "T0KEN", CHAT_ID: "123", WEBHOOK_SECRET: "segredo" };

let sent = [], hooks = [], coupons = DATA, couponsStatus = 200;
globalThis.fetch = async (url, init) => {
  if (String(url).startsWith("https://raw.githubusercontent.com/")) {
    return new Response(JSON.stringify(coupons), { status: couponsStatus });
  }
  const m = String(url).match(/api\.telegram\.org\/botT0KEN\/(\w+)/);
  assert.ok(m, `URL inesperada ${url}`);
  const body = JSON.parse(init.body);
  (m[1] === "sendMessage" ? sent : hooks).push(body);
  return new Response(JSON.stringify({ ok: true }));
};

const post = (text, { chat = 123, secret = "segredo" } = {}) =>
  worker.fetch(new Request("https://bot.example.workers.dev/", {
    method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": secret, "content-type": "application/json" },
    body: JSON.stringify({ message: { text, chat: { id: chat } } }) }), ENV);
const lastText = () => sent[sent.length - 1].text;

// loja pelo nome, de várias formas
for (const q of ["renner", "Renner", "cupons da Renner", "lojas renner", "RENNER?"]) {
  sent = []; await post(q);
  assert.match(lastText(), /Renner/); assert.match(lastText(), /VOUDEAPP/, q);
}
sent = []; await post("rennr"); assert.match(lastText(), /Não acompanho/);       // erro de digitação: não chuta
sent = []; await post("cea"); assert.match(lastText(), /11DO11/);                // apelido curto
sent = []; await post("c&a"); assert.match(lastText(), /11DO11/);
sent = []; await post("mercado livre"); assert.match(lastText(), /Nenhum cupom publicado/);
sent = []; await post("authentic feet"); assert.match(lastText(), /ainda não/);
sent = []; await post("ren"); assert.match(lastText(), /VOUDEAPP/);              // prefixo de uma palavra só
sent = []; await post("cupom da ca"); assert.match(lastText(), /11DO11/);      // curto só vale como palavra inteira
sent = []; await post("cupons da casa"); assert.match(lastText(), /Não acompanho/);   // não casa dentro de outra palavra

// loja bloqueada: explica; cupom de agregador vem marcado
sent = []; await post("centauro"); assert.match(lastText(), /Não consegui ler a loja agora \(HTTP 403\)/);
sent = []; await post("netshoes"); assert.match(lastText(), /NETS20/); assert.match(lastText(), /podem estar vencidos/);

// comandos
sent = []; await post("/start"); assert.match(lastText(), /nome da loja/);
sent = []; await post("/lojas"); assert.match(lastText(), /Renner/); assert.match(lastText(), /Netshoes/);
sent = []; await post("xyz"); assert.match(lastText(), /Não acompanho essa loja/);

// segurança: só o seu chat e só com o segredo do webhook
sent = []; await post("renner", { chat: 999 }); assert.equal(sent.length, 0);
const bad = await post("renner", { secret: "errado" }); assert.equal(bad.status, 401); assert.equal(sent.length, 0);

// HTML escapado; dados velhos geram aviso; falha ao buscar os dados
coupons = { ...DATA, generated_at: "2026-11-07T10:00:00Z", stores: [{ ...DATA.stores[0], name: "Renner", coupons: [{ text: "<b>X</b> & Y", source: "loja" }] }] };
sent = []; await post("renner"); assert.match(lastText(), /&lt;b&gt;X&lt;\/b&gt; &amp; Y/);
couponsStatus = 404; sent = []; await post("renner"); assert.match(lastText(), /Ainda não tenho os dados/); couponsStatus = 200;

// /setup liga o webhook com o segredo
coupons = DATA; hooks = [];
const r = await worker.fetch(new Request("https://bot.example.workers.dev/setup"), ENV);
assert.equal(r.status, 200); assert.equal(hooks[0].secret_token, "segredo"); assert.equal(hooks[0].url, "https://bot.example.workers.dev/");

// idade dos dados: fora do fetch, direto na função
const { answer } = await import("./worker.js").then((m) => ({ answer: m.answer }));
assert.match(answer("renner", { ...DATA, generated_at: "2026-11-07T10:00:00Z" }, NOW), /monitor está atrasado/);
assert.doesNotMatch(answer("renner", DATA, NOW), /atrasado/);
console.log("worker: todos os testes passaram");
