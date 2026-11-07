"""Procura o MESMO modelo (nome + cor + tamanho) em outras lojas e compara os preços.

Fluxo: busca o modelo na pesquisa de cada loja -> pontua os resultados contra o nome do produto ->
guarda os melhores candidatos -> a cada rodada lê o preço/estoque deles e usa as mesmas regras de
desconto real do monitor. Como a correspondência é automática, o alerta avisa "confira o modelo/cor".
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, quote_plus, urlparse

import db
from scraper import BLOCK_URL_RE, scrape_product

log = logging.getLogger("compare")

STORES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_stores.json")

# palavras que não identificam o modelo
STOPWORDS = {
    "tenis", "tenis", "de", "da", "do", "e", "com", "para", "em", "corrida", "running", "esportivo", "casual",
    "masculino", "feminino", "unissex", "adulto", "adultos", "original", "novo", "lancamento",
    "netshoes", "centauro", "dafiti", "mercadolivre", "amazon", "zara", "adidas_store", "loja", "oficial",
}
GENDER_WORDS = {"masculino": "m", "feminino": "f"}
KIDS_WORDS = {"infantil", "kids", "juvenil", "bebe", "crianca"}
COLORS = {
    "preto": {"preto", "preta", "pretos", "black", "core-black", "cblack"},
    "branco": {"branco", "branca", "white", "ftwwht", "cloud"},
    "cinza": {"cinza", "grey", "gray", "chumbo", "grafite"},
    "azul": {"azul", "blue", "navy", "marinho"},
    "vermelho": {"vermelho", "vermelha", "red"},
    "verde": {"verde", "green"},
    "rosa": {"rosa", "pink"},
    "amarelo": {"amarelo", "amarela", "yellow"},
    "laranja": {"laranja", "orange"},
    "marrom": {"marrom", "brown"},
    "bege": {"bege", "beige"},
    "roxo": {"roxo", "roxa", "purple"},
}
COLOR_OF_WORD = {w: c for c, ws in COLORS.items() for w in ws}

MIN_SCORE = 0.85

JS_RESULTS = """
() => {
  const out = [], seen = new Set();
  const priceRe = /R\\$\\s*\\d[\\d.]*,\\d{2}/;
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!href || seen.has(href) || !/^https?:/.test(href)) continue;
    let card = a;
    for (let i = 0; i < 5 && card; i++) {
      if (priceRe.test(card.innerText || '')) break;
      card = card.parentElement;
    }
    if (!card) continue;
    const text = (card.innerText || '');
    if (!priceRe.test(text) || text.length > 700) continue;
    const img = a.querySelector('img');
    let title = (a.getAttribute('title') || a.getAttribute('aria-label') || (img && img.alt) || a.innerText || '').trim();
    if (title.length < 12) {
      const line = text.split('\\n').map(t => t.trim()).find(t => t.length >= 12 && !/R\\$|%|^ou /i.test(t));
      if (line) title = line;
    }
    seen.add(href);
    out.push({href, title: title.slice(0, 200), card: text.slice(0, 400)});
    if (out.length >= 120) break;
  }
  return out;
}
"""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return text.lower()


def words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", _norm(text)) if w]


def model_tokens(name: str) -> list[str]:
    """Palavras que identificam o modelo (marca, linha, número), sem genéricas nem cor/gênero."""
    out: list[str] = []
    for w in words(name):
        if w in STOPWORDS or w in GENDER_WORDS or w in COLOR_OF_WORD or w in KIDS_WORDS:
            continue
        if w not in out:
            out.append(w)
    return out


def product_color(product: dict) -> Optional[str]:
    """Cor pedida: campo 'color' do produto, ou uma cor citada no nome/variações."""
    for source in (product.get("color") or "", " ".join(product.get("variations") or []), product.get("name") or ""):
        for w in words(source):
            if w in COLOR_OF_WORD:
                return COLOR_OF_WORD[w]
    return None


def product_gender(product: dict) -> Optional[str]:
    for w in words(product.get("name") or ""):
        if w in GENDER_WORDS:
            return GENDER_WORDS[w]
    return None


def build_query(product: dict) -> str:
    custom = product.get("search")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    toks = model_tokens(product.get("name") or "")
    color = product_color(product)
    return " ".join(toks + ([color] if color else []))


@dataclass
class Candidate:
    host: str
    url: str
    title: str
    score: float
    color_ok: Optional[bool]   # None = a listagem não diz a cor


def score_result(product: dict, title: str, href: str, card: str = "") -> Optional[tuple[float, Optional[bool]]]:
    """Nota 0..1 de quanto o resultado parece ser o mesmo modelo; None se deve ser descartado."""
    tokens = model_tokens(product.get("name") or "")
    nums = [t for t in tokens if t.isdigit()]
    rest = [t for t in tokens if not t.isdigit()]
    if len(rest) < 2:                      # nome genérico demais para comparar com segurança
        return None
    slug = urlparse(href).path
    found = set(words(" ".join([title, slug])))
    card_words = set(words(card))
    if any(n not in found and (n not in card_words or len(card) > 400) for n in nums):   # número do modelo (11 != 10) tem que bater
        return None
    pool = found | card_words if len(card) <= 400 else found
    hit = sum(1 for t in rest if t in pool)
    score = hit / len(rest)
    if score < MIN_SCORE:
        return None
    ident = found                           # cor/gênero/infantil só do título+link (o card traz muito ruído)
    if not (KIDS_WORDS & set(words(product.get("name") or ""))) and KIDS_WORDS & ident:
        return None
    gender = product_gender(product)
    if gender:
        cand_genders = {GENDER_WORDS[w] for w in ident if w in GENDER_WORDS}
        if cand_genders and gender not in cand_genders:
            return None
    color = product_color(product)
    color_ok: Optional[bool] = None
    if color:
        cand_colors = {COLOR_OF_WORD[w] for w in ident if w in COLOR_OF_WORD}
        if cand_colors:
            color_ok = color in cand_colors
            if not color_ok:
                return None
    return score, color_ok


def load_stores(path: str = STORES_PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("Não consegui ler %s: %s", path, exc)
        return []
    return [s for s in data if isinstance(s, dict) and s.get("name") and (s.get("url") or s.get("urls"))]


def _search_url(store: dict, query: str) -> str:
    return (store["url"].replace("{q_path}", quote(query)).replace("{q_dash}", "-".join(words(query)))
            .replace("{q}", quote_plus(query)))


def _looks_like_product_link(href: str, search_url: str) -> bool:
    p = urlparse(href)
    if not p.netloc or href.split("#")[0] == search_url.split("#")[0]:
        return False
    path = p.path.lower()
    if len(path.strip("/")) < 6:
        return False
    return not re.search(r"/(busca|search|catalogo|categoria|cart|carrinho|login|checkout)(/|$)", path)


def search_store(browser, store: dict, product: dict, settings,
                 diag: Optional[dict] = None) -> tuple[list[Candidate], Optional[str]]:
    """Pesquisa o modelo em uma loja. A loja pode ter vários formatos de link (`urls`): tenta o próximo só em 404
    (403 é bloqueio de IP e os outros links também bloqueariam)."""
    templates = store.get("urls") or [store["url"]]
    cands, err = [], None
    for tmpl in templates:
        cands, err = _search_once(browser, {**store, "url": tmpl}, product, settings, diag)
        if err != "HTTP 404":
            break
    return cands, err


def _search_once(browser, store: dict, product: dict, settings,
                 diag: Optional[dict] = None) -> tuple[list[Candidate], Optional[str]]:
    """Pesquisa o modelo em uma loja. Retorna (candidatos, erro). Uma só tentativa: busca é barata e opcional."""
    query = build_query(product)
    url = _search_url(store, query)
    host = store["name"]
    context = None
    try:
        context = browser.new_context(locale="pt-BR", timezone_id="America/Sao_Paulo",
                                      viewport={"width": 1366, "height": 900})
        context.route("**/*", lambda r: r.abort() if r.request.resource_type in {"image", "font", "media"}
                      else r.continue_())
        page = context.new_page()
        page.set_default_timeout(settings.page_timeout_ms)
        resp = page.goto(url, wait_until="domcontentloaded")
        if resp is not None and resp.status >= 400:
            return [], f"HTTP {resp.status}"
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(1000)
        if BLOCK_URL_RE.search(page.url):
            return [], "pediu verificação/captcha"
        raw = page.evaluate(JS_RESULTS)
        if diag is not None:
            try:
                diag["title"] = " ".join((page.title() or "").split())[:60]
            except Exception:  # noqa: BLE001
                diag["title"] = ""
            diag["final_url"] = page.url
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"[:120]
    finally:
        if context:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    best: dict[str, Candidate] = {}
    links = [it for it in raw if _looks_like_product_link(it.get("href", ""), url)]
    if diag is not None:
        diag["raw"] = len(raw)
        diag["links"] = len(links)
        diag["titles"] = [" ".join((it.get("title") or "").split())[:50] for it in links[:3]]
    for item in raw:
        href = item.get("href", "")
        if not _looks_like_product_link(href, url):
            continue
        scored = score_result(product, item.get("title", ""), href, item.get("card", ""))
        if scored is None:
            continue
        score, color_ok = scored
        title = " ".join((item.get("title") or "").split()) or href
        cand = Candidate(host=host, url=href, title=title[:120], score=score, color_ok=color_ok)
        if href not in best or (cand.score, cand.color_ok is True) > (best[href].score, best[href].color_ok is True):
            best[href] = cand
    ranked = sorted(best.values(), key=lambda c: (c.color_ok is True, c.score), reverse=True)
    return ranked[:2], None


def candidate_id(url: str) -> str:
    p = urlparse(url)
    return f"cmp-{re.sub(r'[^a-z0-9]+', '-', p.netloc.lower().removeprefix('www.')).strip('-')}-" \
           f"{hashlib.sha1(url.encode()).hexdigest()[:8]}"


def find_candidates(browser, product: dict, settings, blocked_hosts: set[str]) -> list[Candidate]:
    own_host = urlparse(product["url"]).netloc.lower().removeprefix("www.")
    found: list[Candidate] = []
    for store in load_stores():
        host = store["name"]
        if host.removeprefix("www.") == own_host or host in blocked_hosts:
            continue
        cands, err = search_store(browser, store, product, settings)
        if err:
            log.info("[%s] busca em %s falhou: %s", product["name"], host, err)
            if err.startswith(("HTTP 4", "HTTP 5", "pediu")):
                blocked_hosts.add(host)   # não insiste nesta loja pelo resto da rodada
            continue
        log.info("[%s] busca em %s: %d candidato(s)%s", product["name"], host, len(cands),
                 f" — melhor: {cands[0].title!r}" if cands else "")
        found.extend(cands)
    found.sort(key=lambda c: (c.color_ok is True, c.score), reverse=True)
    return found[: max(1, settings.compare_max_candidates)]


def _searchable(product: dict) -> bool:
    """Só vale buscar quando o nome identifica um modelo (marca + linha); 'Camiseta Preta' acharia qualquer coisa."""
    return sum(1 for t in model_tokens(product.get("name") or "") if not t.isdigit()) >= 2


def run_comparisons(browser, products: list[dict], settings, notifier, process_product) -> dict:
    """Busca (de tempos em tempos) e relê os candidatos de cada produto. Retorna contadores."""
    stats = {"searched": 0, "read": 0, "alert": 0}
    if not settings.compare_enabled:
        return stats
    blocked_hosts: set[str] = set()
    done_urls: set[str] = set()
    for product in products:
        if product.get("compare") is False or not _searchable(product):
            continue
        pid = product["id"]
        try:
            age = db.hours_since_candidates(settings.db_path, pid)
            if (age is None or age >= settings.compare_search_every_hours) \
                    and stats["searched"] < settings.compare_max_searches:
                cands = find_candidates(browser, product, settings, blocked_hosts)
                db.save_candidates(settings.db_path, pid, [(c.host, c.url, c.title, c.color_ok) for c in cands])
                stats["searched"] += 1
            for host, url, title, color_ok in db.get_candidates(settings.db_path, pid):
                if url in done_urls:
                    continue
                done_urls.add(url)
                stats["read"] += 1
                _read_candidate(browser, product, host, url, color_ok, settings, notifier,
                                process_product, stats)
        except Exception:  # noqa: BLE001 — comparação é um extra: nunca derruba a rodada
            log.exception("[%s] erro na comparação entre lojas", product["name"])
    log.info("Comparação entre lojas: %d busca(s), %d leitura(s), %d alerta(s)",
             stats["searched"], stats["read"], stats["alert"])
    return stats


def _threshold(product: dict, settings) -> float:
    """Preço que torna a outra loja interessante: bate o alvo OU fica X% abaixo da loja principal."""
    thr = float(product["target_price"])
    main_price = db.get_last_price(settings.db_path, product["id"])
    if main_price:
        thr = max(thr, main_price * (1 - settings.compare_min_diff_pct / 100))
    return thr


def _read_candidate(browser, product, host, url, color_ok, settings, notifier, process_product, stats):
    notes = ["Outra loja: encontrado por busca automática — confira modelo, cor e tamanho antes de comprar."]
    if color_ok is None:
        notes.append("Cor não confirmada pela listagem.")
    synthetic = {
        "id": candidate_id(url),
        "name": f"{product['name']} — {host}",
        "url": url,
        "target_price": _threshold(product, settings),
        "variations": list(product.get("variations") or []),
        "selectors": {},
        "notes": notes,
    }
    snap = scrape_product(browser, synthetic, settings)
    # Aqui o critério é "mais barato que a loja principal" (já embutido no preço-alvo sintético). Exigir
    # "desconto contra o próprio histórico" faria uma loja que é barata desde sempre nunca alertar; então só
    # barramos preço claramente inflado (>10% acima do próprio histórico) e aceitamos histórico curto.
    cmp_settings = dataclasses.replace(settings, min_discount_pct=-10.0, allow_alert_without_history=True)
    status = process_product(synthetic, snap, cmp_settings, notifier,
                             rescrape=lambda: scrape_product(browser, synthetic, settings))
    if status == "alert":
        stats["alert"] += 1



def debug_search(browser, query: str, settings) -> list[str]:
    """Roda a busca em TODAS as lojas para um nome e devolve linhas de relatório (não envia nada)."""
    product = {"id": "teste", "name": query, "url": "https://exemplo.invalid/", "target_price": 1.0}
    lines = [f"Busca: {build_query(product)!r}  (modelo: {model_tokens(query)}, cor: {product_color(product)})"]
    if len(model_tokens(query)) < 2:
        lines.append("Nome genérico demais: use marca + modelo (ex.: 'Nike Pegasus 41 preto').")
    for store in load_stores():
        diag: dict = {}
        cands, err = search_store(browser, store, product, settings, diag)
        if err:
            lines.append(f"  ✗ {store['name']:<22} {err}")
            continue
        if cands:
            for c in cands:
                cor = {True: "cor ok", False: "cor errada", None: "cor?"}[c.color_ok]
                lines.append(f"  ✓ {store['name']:<22} {c.title[:60]!r} [{cor}] {c.url[:90]}")
            continue
        if store.get("urls"):
            lines.append(f"      (link de busca que respondeu: {str(diag.get('final_url'))[:110]})")
        lines.append(f"  · {store['name']:<22} nenhum resultado bate com o modelo "
                     f"(a página mostrou {diag.get('links', 0)} produto(s))")
        lines.append(f"      página: {diag.get('title')!r} | url: {str(diag.get('final_url'))[:100]}")
        if diag.get("titles"):
            lines.append(f"      exemplos: {diag['titles']}")
        elif diag.get("raw", 0) == 0:
            lines.append("      (nenhum link com preço: a busca não carregou resultados ou o link de busca está errado)")
    return lines
