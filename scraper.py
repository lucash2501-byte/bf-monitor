"""Scraper com Playwright.

Estratégia de extração (da mais confiável para a menos):
  1. Seletores CSS que você informar em products.json ("selectors")
  2. JSON-LD (schema.org/Product) embutido na página
  3. Meta tags (og:price / itemprop)
  4. Heurística no DOM (elementos com "price/preco" na classe)

Estoque da variação: procura elementos (botões de tamanho/cor/capacidade) cujo texto seja
exatamente a variação desejada e verifica se estão desabilitados/riscados/marcados como
indisponíveis. Se não achar o elemento, o estoque fica DESCONHECIDO (None) e nenhum alerta é enviado.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("scraper")

BRL_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})")
INSTALLMENT_RE = re.compile(r"(\d{1,2})\s*x\s*(?:de\s*)?R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})", re.I)
COUPON_LINE_RE = re.compile(r"(cupom|cupon|código promocional|codigo promocional|use o código|use o codigo)", re.I)
COUPON_CODE_RE = re.compile(
    r"(?:cupom|cupon|código|codigo)\s*(?:de desconto)?\s*[:\-]?\s*[\"“']?([A-Z0-9]{4,20})[\"”']?", re.I
)
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
    """Converte '1.299,90', 'R$ 99,90' ou 1299.9 em float. Retorna None se não achar."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    s = str(text).strip()
    m = BRL_RE.search(s)
    if m:
        return float(m.group(1).replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d+(\.\d+)?", s):
        v = float(s)
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


def parse_jsonld(blobs: list[str]) -> tuple[Optional[float], Optional[bool], Optional[str]]:
    """Retorna (preço, em_estoque, título) a partir de blocos JSON-LD."""
    price = stock = title = None
    for blob in blobs:
        try:
            data = json.loads(blob)
        except Exception:
            continue
        for node in _iter_nodes(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" not in types:
                continue
            title = title or node.get("name")
            offers = node.get("offers")
            offers_list = offers if isinstance(offers, list) else [offers] if offers else []
            prices, avail = [], []
            for off in offers_list:
                if not isinstance(off, dict):
                    continue
                for key in ("price", "lowPrice"):
                    p = parse_price(off.get(key))
                    if p:
                        prices.append(p)
                        break
                a = str(off.get("availability", ""))
                if a:
                    avail.append("InStock" in a or "LimitedAvailability" in a)
            if prices and price is None:
                price = min(prices)
            if avail and stock is None:
                stock = any(avail)
    return price, stock, title


# ----------------------------------------------------------------------------- JS executado na página
JS_PRICE_CANDIDATES = """
() => {
  const out = [];
  const sel = '[itemprop="price"],[class*="price" i],[class*="preco" i],[class*="preço" i],[id*="price" i],[data-testid*="price" i]';
  for (const e of document.querySelectorAll(sel)) {
    const t = (e.getAttribute('content') || e.innerText || '').trim();
    if (!t || t.length > 80 || !/\\d/.test(t)) continue;
    const cs = getComputedStyle(e);
    if ((cs.textDecorationLine || '').includes('line-through')) continue;
    if (e.closest('s,del,strike')) continue;
    out.push(t);
    if (out.length >= 20) break;
  }
  return out;
}
"""

JS_VARIANT_CHECK = """
(variant) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const v = norm(variant);
  const BAD = /(unavail|indispon|esgotad|soldout|sold-out|sold_out|out-of-stock|outofstock|no-stock|sem-estoque|disabled|inactive|crossed|strike)/i;
  const els = [...document.querySelectorAll(
    'button,a,li,label,option,input,[role=radio],[role=option],[role=button],span,div')];
  let found = 0, available = 0;
  for (const e of els) {
    const text = norm(e.innerText || e.value || '');
    const aria = norm(e.getAttribute('aria-label') || '');
    const title = norm(e.getAttribute('title') || '');
    const direct = (text === v && e.children.length <= 2) || aria === v || title === v;
    if (!direct) continue;
    if (e.offsetParent === null && e.tagName !== 'OPTION' && e.tagName !== 'INPUT') continue;
    found++;
    let bad = false, node = e;
    for (let i = 0; i < 3 && node; i++, node = node.parentElement) {
      if (node.disabled || node.getAttribute('aria-disabled') === 'true') bad = true;
      if (BAD.test(node.className && node.className.toString ? node.className.toString() : '')) bad = true;
      const cs = getComputedStyle(node);
      if ((cs.textDecorationLine || '').includes('line-through')) bad = true;
      if (cs.pointerEvents === 'none') bad = true;
    }
    if (!bad) available++;
  }
  return {found, available};
}
"""

JS_COUPON_LINES = """
() => {
  const lines = (document.body.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
  return lines.filter(l => l.length <= 180 && /(cupom|cupon|código promocional|codigo promocional|use o código|use o codigo)/i.test(l)).slice(0, 8);
}
"""


# ----------------------------------------------------------------------------- scraping
def _extract_coupons(page, selectors: dict) -> list[str]:
    found: list[str] = []
    sel = selectors.get("coupon")
    if sel:
        for el in page.query_selector_all(sel)[:5]:
            txt = (el.inner_text() or "").strip()
            if txt:
                found.append(txt[:180])
    try:
        for line in page.evaluate(JS_COUPON_LINES):
            if COUPON_LINE_RE.search(line):
                found.append(line)
    except Exception:
        pass
    # remove duplicados mantendo ordem
    seen, unique = set(), []
    for c in found:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique[:4]


def _extract_snapshot(page, product: dict) -> Snapshot:
    snap = Snapshot()
    selectors = product.get("selectors", {}) or {}
    body_text = page.inner_text("body") if page.query_selector("body") else ""

    if BLOCK_RE.search(page.title() or "") or (len(body_text) < 400 and BLOCK_RE.search(body_text)):
        snap.error = "Página bloqueada/captcha"
        return snap

    snap.title = page.title()

    # --- preço à vista
    sel = selectors.get("price")
    if sel:
        el = page.query_selector(sel)
        if el:
            snap.price_cash = parse_price(el.get_attribute("content") or el.inner_text())

    jsonld_stock = None
    if snap.price_cash is None:
        blobs = page.eval_on_selector_all(
            'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
        )
        price, jsonld_stock, title = parse_jsonld(blobs)
        snap.price_cash = price
        snap.title = title or snap.title

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
        results = []
        for v in variations:
            r = page.evaluate(JS_VARIANT_CHECK, v)
            if r["found"] == 0:
                results.append(None)  # não encontrei o botão: desconhecido
            else:
                results.append(r["available"] > 0)
        if any(r is False for r in results):
            snap.in_stock = False
        elif all(r is True for r in results):
            snap.in_stock = True
        else:
            snap.in_stock = None
    else:
        if jsonld_stock is not None:
            snap.in_stock = jsonld_stock
        elif OUT_OF_STOCK_RE.search(body_text):
            snap.in_stock = False
        else:
            buy_btn = page.query_selector(
                "button:has-text('Comprar'), button:has-text('Adicionar'), a:has-text('Comprar')"
            )
            snap.in_stock = True if buy_btn else None

    # JSON-LD dizendo "OutOfStock" prevalece mesmo com a variação visível
    if variations and jsonld_stock is False and snap.in_stock:
        snap.in_stock = False

    snap.coupons = _extract_coupons(page, selectors)
    return snap


def scrape_product(browser, product: dict, settings) -> Snapshot:
    """Raspa um produto com retentativas e backoff."""
    last_error = "erro desconhecido"
    for attempt in range(1, settings.max_retries + 1):
        context = None
        try:
            context = browser.new_context(
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "font", "media"}
                else route.continue_(),
            )
            page = context.new_page()
            page.set_default_timeout(settings.page_timeout_ms)
            page.goto(product["url"], wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass  # muitos sites nunca ficam "idle"; seguimos
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(1200)
            snap = _extract_snapshot(page, product)
            if snap.error is None and snap.price_cash is None:
                snap.error = "Preço não encontrado (configure 'selectors.price' no products.json)"
            if snap.error is None:
                return snap
            last_error = snap.error
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if context:
                context.close()
        wait = 2 ** attempt + random.uniform(0, 2)
        log.warning("%s: tentativa %d/%d falhou (%s). Aguardando %.1fs",
                    product["name"], attempt, settings.max_retries, last_error, wait)
        time.sleep(wait)
    return Snapshot(error=last_error)
