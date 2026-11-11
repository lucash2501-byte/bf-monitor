"""Vigia os cupons publicados na página das lojas (stores.json), sem precisar de um produto específico.

Abre a página inicial (e as páginas de cupom que você listar) de cada loja, lê os cupons que a loja publica
(respostas JSON da página, estado embutido e banners de texto) e manda para o Telegram só os que ainda não
foram avisados. Não precisa de preço, então funciona mesmo em loja onde o preço do produto não é lido.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import db
from coupons import Coupon
from scraper import (BLOCK_URL_RE, STEALTH_JS, _collect_api_coupons, _extract_coupons, _user_agent)

log = logging.getLogger("store_coupons")

STORES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.json")


# código de cupom de verdade: token em MAIÚSCULAS com 5-16 caracteres (ex.: PRIMEIRA10, 11DO11); "OFF", "PIX"
# e nomes de loja em maiúsculas não contam. Agregadores trazem muita frase de marketing sem código.
_CODE_TOKEN = re.compile(r"\b(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,16}\b")   # precisa ter ao menos uma letra (11DO11 vale, 10000 não)
_NOT_CODES = {"CUPOM", "CUPONS", "DESCONTO", "FRETE", "GRATIS", "GRÁTIS", "BLACK", "FRIDAY", "NETSHOES", "CENTAURO",
              "MERCADO", "LIVRE", "COMPRA", "COMPRAS", "OFERTA", "OFERTAS", "NOVEMBRO", "EXTRA",
              "PROMOBIT", "CUPONOMIA", "PELANDO", "MELIUZ", "MÉLIUZ"}   # nomes dos próprios agregadores


def has_real_code(line: str) -> bool:
    return any(t not in _NOT_CODES for t in _CODE_TOKEN.findall(line))


def load_stores(path: str = STORES_PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("Não consegui ler %s: %s", path, exc)
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        urls = item.get("urls") or ([item["url"]] if item.get("url") else [])
        urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if urls:
            entry = {"name": item["name"], "urls": urls}
            if item.get("store"):          # entrada de agregador: a que loja os cupons pertencem
                entry["store"] = str(item["store"])
            if isinstance(item.get("aliases"), list):
                entry["aliases"] = [str(a) for a in item["aliases"]]
            if item.get("label"):          # cupom vindo de agregador: chave própria, para não misturar lojas
                entry["label"] = str(item["label"])
            out.append(entry)
    return out


def scrape_store_coupons(browser, url: str, settings,
                         diag: Optional[dict] = None) -> tuple[list[str], Optional[str]]:
    """Lê os cupons de UMA página. Retorna (cupons, erro). Uma tentativa: é um extra, não vale insistir.
    `diag` (opcional) recebe a URL final, o título e o início do texto, para diagnosticar páginas sem cupom."""
    context = None
    try:
        context = browser.new_context(locale="pt-BR", timezone_id="America/Sao_Paulo",
                                      viewport={"width": 1366, "height": 900}, user_agent=_user_agent(browser))
        context.add_init_script(STEALTH_JS)
        context.route("**/*", lambda r: r.abort() if r.request.resource_type in {"image", "font", "media"}
                      else r.continue_())
        page = context.new_page()
        page.set_default_timeout(settings.page_timeout_ms)
        api: list[Coupon] = []
        page.on("response", lambda r, acc=api: _collect_api_coupons(r, acc))
        resp = page.goto(url, wait_until="domcontentloaded")
        if resp is not None and resp.status >= 400:
            return [], f"HTTP {resp.status}"
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:  # noqa: BLE001
            pass
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(1200)
        if diag is not None:
            diag["final_url"] = page.url
            try:
                diag["title"] = " ".join((page.title() or "").split())[:70]
                diag["text"] = " ".join((page.inner_text("body") or "").split())[:160]
            except Exception:  # noqa: BLE001
                pass
        if BLOCK_URL_RE.search(page.url):
            return [], f"pediu verificação/captcha ({urlparse(page.url).netloc})"
        return _extract_coupons(page, {}, api), None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"[:120]
    finally:
        if context:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def read_store(browser, store: dict, settings, notes: Optional[list] = None) -> tuple[list[str], Optional[str], str]:
    """Junta os cupons de todas as páginas da loja. Retorna (cupons, erro se TODAS falharam, url do 1º sucesso).
    `notes` (opcional) recebe uma linha de diagnóstico por página."""
    coupons: list[str] = []
    errors: list[str] = []
    first_ok = store["urls"][0]
    got_page = False
    for url in store["urls"]:
        diag: dict = {}
        found, err = scrape_store_coupons(browser, url, settings, diag if notes is not None else None)
        if notes is not None:
            notes.append(f"{url[:70]} → {str(diag.get('final_url', '?'))[:80]} | título: {diag.get('title', '')!r} | "
                         f"{'ERRO ' + err if err else str(len(found)) + ' cupom(ns)'} | texto: {diag.get('text', '')!r}")
        if err:
            errors.append(err)
            if err.startswith("HTTP 403") and not store.get("label"):
                break        # bloqueio de IP da loja: as outras páginas dela também bloqueiam (agregadores: tenta o próximo site)
            continue
        if not got_page:
            first_ok, got_page = url, True
        if store.get("label"):      # agregador: só linhas com código de verdade
            found = [c for c in found if has_real_code(c)]
        coupons += [c for c in found if c not in coupons]
    return coupons, (None if got_page else (errors[0] if errors else "sem página")), first_ok


def run_store_coupons(browser, settings, notifier, send_digest) -> dict:
    """Confere as lojas de stores.json que estão 'vencidas' (a cada STORE_COUPONS_EVERY_HOURS) e avisa os cupons novos."""
    stats = {"checked": 0, "errors": 0, "sent": 0}
    if not settings.store_coupons_enabled:
        return stats
    for store in load_stores():
        name = store["name"]
        age = db.hours_since_store_check(settings.db_path, name)
        if age is not None and age < settings.store_coupons_every_hours:
            continue
        coupons, err, url = read_store(browser, store, settings)
        stats["checked"] += 1
        db.mark_store_checked(settings.db_path, name)     # mesmo com erro: não martela uma loja que bloqueia
        base = store.get("store") or name
        if store.get("label"):                            # agregador: não sobrescreve o status da loja de verdade
            if coupons:
                db.cache_coupons(settings.db_path, base, coupons, "agregador")
        else:
            db.set_store_status(settings.db_path, base, err)
            if coupons:
                db.cache_coupons(settings.db_path, base, coupons, "loja")
        if err:
            stats["errors"] += 1
            log.info("[%s] cupons da loja: %s", name, err)
            continue
        log.info("[%s] cupons da loja: %d encontrado(s)", name, len(coupons))
        if coupons and settings.notify_coupons and notifier.configured:
            product = {"name": name, "url": url}
            if store.get("label"):
                product["label"] = store["label"]
            stats["sent"] += send_digest([(product, coupons)], settings, notifier)
    log.info("Cupons das lojas: %d conferida(s), %d com erro, %d mensagem(ns)",
             stats["checked"], stats["errors"], stats["sent"])
    return stats


def debug_stores(browser, settings) -> list[str]:
    """Relatório de teste: o que cada loja de stores.json devolve (não envia nada)."""
    lines = []
    for store in load_stores():
        notes: list[str] = []
        coupons, err, url = read_store(browser, store, settings, notes)
        if err:
            lines.append(f"  ✗ {store['name']:<28} {err}")
        elif coupons:
            lines.append(f"  ✓ {store['name']:<28} {len(coupons)} cupom(ns):")
            lines += [f"        • {c[:110]}" for c in coupons]
        else:
            lines.append(f"  · {store['name']:<28} página abriu, mas nenhum cupom publicado")
        if not coupons or len(store["urls"]) > 1:
            lines += [f"        ↳ {n}" for n in notes]
    return lines


# ------------------------------------------------------------------ cache para o bot do Telegram
def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def store_for_url(url: str, stores: Optional[list[dict]] = None) -> str:
    """Nome da loja (de stores.json) a que uma URL de produto pertence; sem cadastro, usa o próprio domínio."""
    host = _host(url)
    for st in stores if stores is not None else load_stores():
        if st.get("label"):
            continue
        for u in st["urls"]:
            h = _host(u)
            if host == h or host.endswith("." + h):
                return st["name"]
    return host


def cache_product_coupons(settings, product: dict, coupons: list[str]) -> None:
    """Os cupons lidos na página de um produto também entram no cache da loja dele."""
    if coupons:
        try:
            db.cache_coupons(settings.db_path, store_for_url(product["url"]), coupons, "produto")
        except Exception:  # noqa: BLE001 — cache é um extra
            log.exception("Falha ao guardar cupons no cache")


CACHE_MAX_AGE_HOURS = 36.0


def build_export(settings, stores: Optional[list[dict]] = None) -> dict:
    """Dados que o bot consulta: cada loja de stores.json com seus cupons recentes e quando foi conferida."""
    stores = stores if stores is not None else load_stores()
    cached = db.get_cached_coupons(settings.db_path, CACHE_MAX_AGE_HOURS)
    status = db.get_store_statuses(settings.db_path)
    by_store: dict[str, list[dict]] = {}
    for store_name, text, source, seen in cached:
        by_store.setdefault(store_name, []).append({"text": text, "source": source, "seen_at": seen.replace(" ", "T") + "Z"})
    out, known = [], set()
    for st in stores:
        if st.get("label"):
            continue
        name = st["name"]
        known.add(name)
        checked, err = status.get(name, (None, None))
        core = _host(st["urls"][0]).split(".")[0]
        out.append({"name": name, "aliases": sorted({core, *st.get("aliases", [])}), "url": st["urls"][0],
                    "checked_at": checked.replace(" ", "T") + "Z" if checked else None, "error": err,
                    "coupons": by_store.get(name, [])})
    for name, coupons in by_store.items():           # lojas que só aparecem por produto cadastrado
        if name not in known:
            out.append({"name": name, "aliases": [name.split(".")[0]], "url": f"https://{name}/", "checked_at": None,
                        "error": None, "coupons": coupons})
    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stores": out}


def export_json(settings, path: Optional[str] = None) -> str:
    """Grava coupons.json ao lado do banco (a branch 'data' o publica para o bot)."""
    path = path or os.path.join(os.path.dirname(settings.db_path) or ".", "coupons.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(build_export(settings), fh, ensure_ascii=False, indent=1)
    return path
