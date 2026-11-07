"""Vigia os cupons publicados na página das lojas (stores.json), sem precisar de um produto específico.

Abre a página inicial (e as páginas de cupom que você listar) de cada loja, lê os cupons que a loja publica
(respostas JSON da página, estado embutido e banners de texto) e manda para o Telegram só os que ainda não
foram avisados. Não precisa de preço, então funciona mesmo em loja onde o preço do produto não é lido.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import db
from coupons import Coupon
from scraper import (BLOCK_URL_RE, STEALTH_JS, _collect_api_coupons, _extract_coupons, _user_agent)

log = logging.getLogger("store_coupons")

STORES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.json")


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
            out.append({"name": item["name"], "urls": urls})
    return out


def scrape_store_coupons(browser, url: str, settings) -> tuple[list[str], Optional[str]]:
    """Lê os cupons de UMA página. Retorna (cupons, erro). Uma tentativa: é um extra, não vale insistir."""
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


def read_store(browser, store: dict, settings) -> tuple[list[str], Optional[str], str]:
    """Junta os cupons de todas as páginas da loja. Retorna (cupons, erro se TODAS falharam, url do 1º sucesso)."""
    coupons: list[str] = []
    errors: list[str] = []
    first_ok = store["urls"][0]
    got_page = False
    for url in store["urls"]:
        found, err = scrape_store_coupons(browser, url, settings)
        if err:
            errors.append(err)
            if err.startswith("HTTP 403"):   # bloqueio de IP: as outras páginas da loja também bloqueiam
                break
            continue
        if not got_page:
            first_ok, got_page = url, True
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
        if err:
            stats["errors"] += 1
            log.info("[%s] cupons da loja: %s", name, err)
            continue
        log.info("[%s] cupons da loja: %d encontrado(s)", name, len(coupons))
        if coupons and settings.notify_coupons and notifier.configured:
            stats["sent"] += send_digest([({"name": name, "url": url}, coupons)], settings, notifier)
    log.info("Cupons das lojas: %d conferida(s), %d com erro, %d mensagem(ns)",
             stats["checked"], stats["errors"], stats["sent"])
    return stats


def debug_stores(browser, settings) -> list[str]:
    """Relatório de teste: o que cada loja de stores.json devolve (não envia nada)."""
    lines = []
    for store in load_stores():
        coupons, err, url = read_store(browser, store, settings)
        if err:
            lines.append(f"  ✗ {store['name']:<24} {err}")
        elif coupons:
            lines.append(f"  ✓ {store['name']:<24} {len(coupons)} cupom(ns):")
            lines += [f"        • {c[:110]}" for c in coupons]
        else:
            lines.append(f"  · {store['name']:<24} página abriu, mas nenhum cupom publicado ({url[:70]})")
    return lines
