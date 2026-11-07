"""Scraper com Playwright.

Estratégia de extração do preço (da mais confiável para a menos):
  1. Seletor CSS que você informar em products.json ("selectors.price")
  2. JSON-LD (schema.org/Product) embutido na página
  3. Meta tags (og:price / itemprop)
  4. Heurística no DOM (elementos com "price/preco" na classe, ignorando preço riscado e parcelas)

Estoque da variação: procura elementos (botões de tamanho/cor/capacidade) cujo texto seja
exatamente a variação desejada e verifica se estão desabilitados/riscados/marcados como
indisponíveis. Se não achar o elemento, o estoque fica DESCONHECIDO (None).
Em lojas teimosas, use "selectors.variation" (ex.: "button[data-size='{v}']").
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from coupons import Coupon, dedupe, extract_coupons, extract_from_json_text

log = logging.getLogger("scraper")

BRL_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})")
BRL_INT_RE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?![\d,]|\.\d)")  # "R$ 99" sem centavos (último recurso)
US_RE = re.compile(r"\d{1,3}(?:,\d{3})+\.\d{2}")                            # "1,299.90"
INSTALLMENT_RE = re.compile(r"(\d{1,2})\s*x\s*(?:de\s*)?R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})", re.I)
COUPON_LINE_RE = re.compile(r"(cupom|cupon|código promocional|codigo promocional|use o código|use o codigo)", re.I)
# palavra-chave em qualquer caixa, mas o CÓDIGO precisa estar em MAIÚSCULAS (ex.: "cupom BLACK10")
COUPON_CODE_RE = re.compile(
    r"(?i:cupom|cupon|código|codigo|use)\s*(?:de desconto)?\s*[:\-]?\s*[\"“']?([A-Z0-9]{4,20})\b"
)
COUPON_USEFUL_RE = re.compile(r"(\d+\s*%|R\$\s*\d)")
OUT_OF_STOCK_RE = re.compile(
    r"(produto indispon[ií]vel|esgotado|avise-?me quando|indispon[ií]vel no momento|sem estoque)", re.I
)
BLOCK_RE = re.compile(r"(captcha|access denied|verify you are human|unusual traffic|acesso negado)", re.I)


@dataclass
class Snapshot:
    price_cash: Optional[float] = None
    price_installment: Optional[float] = None  # total parcelado (n x valor)
    installment_text: Optional[str] = None
    in_stock: Optional[bool] = None
    coupons: list[str] = field(default_factory=list)
    title: Optional[str] = None
    error: Optional[str] = None


# ----------------------------------------------------------------------------- parsing helpers
def parse_price(text: Any) -> Optional[float]:
    """Converte '1.299,90', 'R$ 99,90', '1,299.90' ou 1299.9 em float. Retorna None se não achar."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    s = re.sub(r"\s+", " ", str(text)).strip()
    if not s:
        return None
    low = s.lower()
    if " por " in low:  # "de R$ 599,90 por R$ 299,90" -> vale o trecho depois do "por"
        s = s[low.rfind(" por ") + 5:]
    m = US_RE.search(s)
    if m:
        return float(m.group(0).replace(",", ""))
    m = BRL_RE.search(s)
    if m:
        return float(m.group(1).replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d+(\.\d+)?", s):
        v = float(s)
        return v if v > 0 else None
    m = BRL_INT_RE.search(s)
    if m:
        v = float(m.group(1).replace(".", ""))
        return v if v > 0 else None
    return None


def parse_installment(text: str) -> tuple[Optional[float], Optional[str]]:
    m = INSTALLMENT_RE.search(text or "")
    if not m:
        return None, None
    n = int(m.group(1))
    value = parse_price(m.group(2))
    if not value:
        return None, None
    return round(n * value, 2), f"{n}x de R$ {value:.2f}".replace(".", ",")


def _iter_nodes(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_nodes(v)


def _flatten_offers(offers: Any) -> list[dict]:
    out: list[dict] = []
    items = offers if isinstance(offers, list) else [offers] if offers else []
    for off in items:
        if not isinstance(off, dict):
            continue
        out.append(off)
        if off.get("offers"):  # AggregateOffer com ofertas aninhadas
            out.extend(_flatten_offers(off["offers"]))
    return out


def _offer_price(off: dict) -> Optional[float]:
    for key in ("price", "lowPrice"):
        p = parse_price(off.get(key))
        if p:
            return p
    spec = off.get("priceSpecification")
    for s in spec if isinstance(spec, list) else [spec] if spec else []:
        if not isinstance(s, dict):
            continue
        if re.search(r"(list|strikethrough|msrp)", str(s.get("priceType", "")), re.I):
            continue  # preço "de" (riscado), não o praticado
        p = parse_price(s.get("price"))
        if p:
            return p
    return None


def _is_available(availability: Any) -> bool:
    a = str(availability)
    return "InStock" in a or "LimitedAvailability" in a or "OnlineOnly" in a


def parse_jsonld(blobs: list[str]) -> tuple[Optional[float], Optional[bool], Optional[str]]:
    """Retorna (preço, em_estoque, título) a partir de blocos JSON-LD (primeiro Product com preço)."""
    title = None
    for blob in blobs:
        try:
            data = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        for node in _iter_nodes(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" not in types:
                continue
            title = title or node.get("name")
            offers = _flatten_offers(node.get("offers"))
            prices = [p for p in (_offer_price(o) for o in offers) if p]
            if not prices:
                continue
            avail = [_is_available(o["availability"]) for o in offers if o.get("availability")]
            return min(prices), (any(avail) if avail else None), title or node.get("name")
    return None, None, title


# ----------------------------------------------------------------------------- JS executado na página
JS_PRICE_CANDIDATES = r"""
() => {
  const out = [];
  const sel = '[itemprop="price"],[class*="price" i],[class*="preco" i],[class*="preço" i],[id*="price" i],[data-testid*="price" i]';
  const SKIP_CLS = /(old|was|before|strike|riscad|antigo|list-?price|installment|parcel|unit|shipping|frete)/i;
  const SKIP_TXT = /(frete|cashback|economize|acima de|m[ií]nim|cupom|% ?off|de desconto|\d+\s*x\b)/i;
  for (const e of document.querySelectorAll(sel)) {
    if (e.querySelector(sel)) continue;  // prefere o elemento mais interno (o contêiner mistura preço velho e novo)
    const t = (e.getAttribute('content') || e.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 80 || !/\d/.test(t)) continue;
    if (SKIP_TXT.test(t)) continue;
    const cls = (e.className && e.className.toString) ? e.className.toString() : '';
    if (SKIP_CLS.test(cls)) continue;
    const cs = getComputedStyle(e);
    if ((cs.textDecorationLine || '').includes('line-through')) continue;
    if (e.closest('s,del,strike')) continue;
    if (e.offsetParent === null && !e.getAttribute('content')) continue;  // invisível
    out.push(t);
    if (out.length >= 20) break;
  }
  return out;
}
"""

JS_VARIANT_CHECK = r"""
({v, sel}) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(v);
  const BAD = /(unavail|indispon|esgotad|soldout|sold-out|sold_out|out-of-stock|outofstock|no-stock|nostock|sem-estoque|disabled|crossed|strike)/i;
  const BAD_TEXT = /(indispon|esgotad|avise|sem estoque|unavailable|sold out)/i;
  const isBad = (e) => {
    let node = e;
    for (let i = 0; i < 3 && node; i++, node = node.parentElement) {
      if (node.disabled || node.getAttribute('aria-disabled') === 'true') return true;
      const cls = (node.className && node.className.toString) ? node.className.toString() : '';
      if (BAD.test(cls)) return true;
      const cs = getComputedStyle(node);
      if ((cs.textDecorationLine || '').includes('line-through')) return true;
      if (cs.pointerEvents === 'none') return true;
    }
    return false;
  };
  const visible = e => e.offsetParent !== null || e.tagName === 'OPTION' || e.tagName === 'INPUT'
                       || getComputedStyle(e).position === 'fixed';
  const matches = (e) => {
    const text = norm(e.innerText || e.value || '');
    const aria = norm(e.getAttribute('aria-label') || '');
    const title = norm(e.getAttribute('title') || '');
    const data = norm(e.getAttribute('data-value') || e.getAttribute('data-size') || '');
    if (text === want && e.children.length <= 2) return true;
    if (aria === want || title === want || data === want) return true;
    // "40 - Indisponível", "M esgotado": o texto começa com a variação e o resto indica falta de estoque
    if (text.startsWith(want + ' ') && text.length <= want.length + 40 && BAD_TEXT.test(text.slice(want.length))) return true;
    return false;
  };
  const evaluate = (els, filter) => {
    let found = 0, available = 0;
    for (const e of els) {
      if (filter && !matches(e)) continue;
      if (!visible(e)) continue;
      found++;
      const text = norm(e.innerText || e.value || '');
      if (!isBad(e) && !(text !== want && BAD_TEXT.test(text))) available++;
    }
    return {found, available};
  };
  if (sel) return evaluate([...document.querySelectorAll(sel)], false);
  // 1º nível: elementos interativos; só se nada for achado, tenta contêineres genéricos
  const tier1 = 'button,a,label,option,input,li,[role=radio],[role=option],[role=button],[role=tab]';
  let r = evaluate([...document.querySelectorAll(tier1)], true);
  if (r.found === 0) r = evaluate([...document.querySelectorAll('span,div,p')], true);
  return r;
}
"""

JS_BUY_BUTTON = r"""
() => {
  const re = /^(comprar|adicionar\s+(à|a|ao|na|no)\s+(sacola|carrinho|cesta)|add to (cart|bag)|eu quero)/i;
  let found = 0, enabled = 0;
  for (const e of document.querySelectorAll('button,a[role=button],input[type=submit],input[type=button]')) {
    if (e.offsetParent === null) continue;
    const t = (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 40 || !re.test(t)) continue;
    found++;
    if (!e.disabled && e.getAttribute('aria-disabled') !== 'true') enabled++;
  }
  return {found, enabled};
}
"""

JS_COUPON_LINES = r"""
() => {
  const lines = (document.body.innerText || '').split('\n').map(l => l.trim()).filter(Boolean);
  return lines.filter(l => l.length <= 180 && /(cupom|cupon|código promocional|codigo promocional|use o código|use o codigo)/i.test(l)).slice(0, 12);
}
"""

JS_EMBEDDED_JSON = r"""
() => {
  const out = [];
  for (const e of document.querySelectorAll('script#__NEXT_DATA__, script[type="application/json"]')) {
    const t = e.textContent || '';
    if (t.length > 3 && t.length <= 2000000 && /(cupom|cupon|coupon|voucher|promo)/i.test(t)) out.push(t);
    if (out.length >= 10) break;
  }
  for (const k of ['__INITIAL_STATE__', '__PRELOADED_STATE__', '__APOLLO_STATE__', '__NUXT__']) {
    try {
      const v = window[k];
      if (v) { const t = JSON.stringify(v); if (t.length <= 2000000 && /(cupom|cupon|coupon|voucher|promo)/i.test(t)) out.push(t); }
    } catch (e) {}
  }
  return out;
}
"""

# esconde os sinais mais óbvios de automação (uso pessoal, em intervalos razoáveis)
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""


# ----------------------------------------------------------------------------- scraping
def _is_useful_coupon(line: str) -> bool:
    """Descarta links de menu como 'Cupons'/'Meus cupons': exige percentual, valor em R$ ou um código."""
    return bool(COUPON_USEFUL_RE.search(line) or COUPON_CODE_RE.search(line))


def _extract_coupons(page, selectors: dict, api_coupons: Optional[list[Coupon]] = None) -> list[str]:
    """Cupons estruturados (API/estado embutido) primeiro; depois texto da página. Máx. 6, sem repetição."""
    structured: list[Coupon] = list(api_coupons or [])
    try:
        for blob in page.evaluate(JS_EMBEDDED_JSON):
            structured += extract_from_json_text(blob, source="página")
    except Exception:  # noqa: BLE001
        pass
    structured = dedupe(structured)
    structured.sort(key=lambda c: (c.valid_until is None, c.valid_until or date.max))

    found: list[str] = [c.format() for c in structured[:4]]
    codes = {c.code.lower() for c in structured}

    sel = selectors.get("coupon")
    if sel:  # seletor explícito do usuário: confia no que ele apontou
        for el in page.query_selector_all(sel)[:5]:
            txt = " ".join((el.inner_text() or "").split())
            if txt:
                found.append(txt[:180])
    try:
        for line in page.evaluate(JS_COUPON_LINES):
            if COUPON_LINE_RE.search(line) and _is_useful_coupon(line):
                found.append(line)
    except Exception:  # noqa: BLE001
        pass
    seen, unique = set(), []
    for c in found:
        k = c.lower()
        if k in seen:
            continue
        if any(code in k for code in codes) and c not in found[:len(structured[:4])]:
            continue  # linha de texto que só repete um cupom estruturado já listado
        seen.add(k)
        unique.append(c)
    return unique[:6]


def _check_variation(page, variant: str, selectors: dict) -> Optional[bool]:
    """True = disponível, False = indisponível, None = não encontrei o elemento."""
    sel = selectors.get("variation")
    sel = sel.replace("{v}", variant) if sel else None
    r = page.evaluate(JS_VARIANT_CHECK, {"v": variant, "sel": sel})
    if r["found"] == 0:
        return None
    return r["available"] > 0


def _extract_snapshot(page, product: dict, api_coupons: Optional[list[Coupon]] = None) -> Snapshot:
    snap = Snapshot()
    selectors = product.get("selectors", {}) or {}
    try:
        body_text = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body_text = ""

    if BLOCK_RE.search(page.title() or "") or (len(body_text) < 400 and BLOCK_RE.search(body_text)):
        snap.error = "Página bloqueada/captcha"
        return snap

    snap.title = page.title()

    # JSON-LD: sempre lido, pois também informa o estoque do produto
    blobs = page.eval_on_selector_all('script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
    jsonld_price, jsonld_stock, jsonld_title = parse_jsonld(blobs)
    snap.title = jsonld_title or snap.title

    # --- preço à vista
    sel = selectors.get("price")
    if sel:
        el = page.query_selector(sel)
        if el:
            snap.price_cash = parse_price(el.get_attribute("content") or el.inner_text())

    if snap.price_cash is None:
        snap.price_cash = jsonld_price

    if snap.price_cash is None:
        for css in ('meta[property="product:price:amount"]', 'meta[property="og:price:amount"]',
                    'meta[itemprop="price"]'):
            el = page.query_selector(css)
            if el:
                snap.price_cash = parse_price(el.get_attribute("content"))
                if snap.price_cash:
                    break

    if snap.price_cash is None:
        for txt in page.evaluate(JS_PRICE_CANDIDATES):
            if re.search(r"\d+\s*x\b", txt, re.I):  # é parcelamento, não preço à vista
                continue
            p = parse_price(txt)
            if p:
                snap.price_cash = p
                break

    # --- preço parcelado
    inst_sel = selectors.get("installment")
    inst_text = ""
    if inst_sel:
        el = page.query_selector(inst_sel)
        inst_text = el.inner_text() if el else ""
    snap.price_installment, snap.installment_text = parse_installment(inst_text or body_text)

    # --- estoque
    variations = product.get("variations") or []
    if variations:
        results = [_check_variation(page, v, selectors) for v in variations]
        if any(r is False for r in results):
            snap.in_stock = False
        elif all(r is True for r in results):
            snap.in_stock = True
        else:
            snap.in_stock = None  # alguma variação não foi encontrada: desconhecido
        # JSON-LD dizendo "OutOfStock" prevalece mesmo com a variação visível
        if jsonld_stock is False and snap.in_stock:
            snap.in_stock = False
    elif jsonld_stock is not None:
        snap.in_stock = jsonld_stock
    else:
        buy = page.evaluate(JS_BUY_BUTTON)
        if buy["enabled"] > 0:
            snap.in_stock = True
        elif buy["found"] > 0:
            snap.in_stock = False  # há botão de compra, mas todos desabilitados
        elif OUT_OF_STOCK_RE.search(body_text):
            snap.in_stock = False
        else:
            snap.in_stock = None

    snap.coupons = _extract_coupons(page, selectors, api_coupons)
    return snap


MAX_API_BODY = 1_500_000


def _collect_api_coupons(response, acc: list[Coupon]) -> None:
    """Lê respostas JSON (XHR/fetch) que a página recebe e guarda os cupons que a loja publicou nelas."""
    try:
        if response.request.resource_type not in ("xhr", "fetch") or response.status != 200:
            return
        if "json" not in (response.headers.get("content-type") or "").lower():
            return
        if len(acc) >= 50:
            return
        body = response.text()
        if len(body) > MAX_API_BODY or not re.search(r"(cupom|cupon|coupon|voucher|promo)", body, re.I):
            return
        host = response.url.split("/")[2] if "//" in response.url else ""
        acc.extend(extract_from_json_text(body, source=host))
    except Exception:  # noqa: BLE001 — resposta pode ter sido descartada pela navegação; ignorar
        pass


def _user_agent(browser) -> str:
    """UA coerente com a versão real do Chromium (o padrão headless se denuncia como 'HeadlessChrome')."""
    try:
        major = browser.version.split(".")[0]
    except Exception:  # noqa: BLE001
        major = "130"
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def _dump_debug(page, product: dict, settings) -> None:
    """Salva screenshot + HTML da página para você descobrir o seletor certo (DEBUG_DIR no .env)."""
    debug_dir = getattr(settings, "debug_dir", "")
    if not debug_dir:
        return
    try:
        os.makedirs(debug_dir, exist_ok=True)
        base = os.path.join(debug_dir, re.sub(r"[^a-z0-9._-]+", "_", str(product.get("id", "produto")).lower()))
        page.screenshot(path=base + ".png", full_page=False)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log.info("Debug salvo em %s.png/.html", base)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao salvar debug")


def scrape_product(browser, product: dict, settings) -> Snapshot:
    """Raspa um produto com retentativas e backoff."""
    last_error = "erro desconhecido"
    user_agent = _user_agent(browser)
    for attempt in range(1, settings.max_retries + 1):
        context = None
        try:
            context = browser.new_context(
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                viewport={"width": 1366, "height": 900},
                user_agent=user_agent,
            )
            context.add_init_script(STEALTH_JS)
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "font", "media"}
                else route.continue_(),
            )
            page = context.new_page()
            page.set_default_timeout(settings.page_timeout_ms)
            api_coupons: list[Coupon] = []
            page.on("response", lambda r, acc=api_coupons: _collect_api_coupons(r, acc))
            response = page.goto(product["url"], wait_until="domcontentloaded")
            status = response.status if response else None

            if status in (404, 410):  # URL quebrada: repetir não adianta
                return Snapshot(error=f"Página não encontrada (HTTP {status}) — confira a URL no products.json")

            if status is not None and status >= 400:
                last_error = f"HTTP {status} (possível bloqueio anti-bot)"
                _dump_debug(page, product, settings)
            else:
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:  # noqa: BLE001
                    pass  # muitos sites nunca ficam "idle"; seguimos
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(1200)
                snap = _extract_snapshot(page, product, api_coupons)
                if snap.error is None and snap.price_cash is None:
                    try:
                        title = " ".join((page.title() or "").split())[:70]
                    except Exception:  # noqa: BLE001
                        title = ""
                    snap.error = (f"Preço não encontrado (configure 'selectors.price' no products.json) "
                                  f"| página: {title!r} | url final: {page.url[:110]}")
                if snap.error is None:
                    return snap
                last_error = snap.error
                _dump_debug(page, product, settings)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if context:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
        if attempt < settings.max_retries:
            wait = 2 ** attempt + random.uniform(0, 2)
            log.warning("%s: tentativa %d/%d falhou (%s). Aguardando %.1fs",
                        product["name"], attempt, settings.max_retries, last_error, wait)
            time.sleep(wait)
    return Snapshot(error=last_error)
