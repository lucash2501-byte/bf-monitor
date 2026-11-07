"""Monitor de preços para Black Friday.

Uso:
    python main.py                       # loop contínuo
    python main.py --once                # uma rodada e sai
    python main.py --once --only mizuno  # só os produtos cujo id/nome contém "mizuno" (bom p/ ajustar seletores)
    python main.py --test-telegram       # envia mensagem de teste
    python main.py --stats               # resumo do histórico por produto
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import statistics
import sys
import time
import unicodedata
from typing import Callable, Optional
from urllib.parse import urlparse

import analyzer
import db
from config import Settings, load_settings
from notifier import TelegramNotifier, build_alert_message, build_coupon_message
from scraper import Snapshot, parse_price, scrape_product

log = logging.getLogger("main")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_products(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        log.error("Arquivo de produtos não encontrado: %s", path)
        return []
    except json.JSONDecodeError as exc:
        log.error("%s tem JSON inválido (linha %d, coluna %d): %s", path, exc.lineno, exc.colno, exc.msg)
        return []
    if not isinstance(raw, list):
        log.error("%s deve conter uma LISTA de produtos ([...])", path)
        return []

    products = []
    seen_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            log.error("Produto ignorado (não é um objeto JSON): %r", item)
            continue
        missing = [k for k in ("name", "url", "target_price") if k not in item]
        if missing:
            log.error("Produto ignorado (faltando %s): %s", missing, item)
            continue

        target = parse_price(item["target_price"])  # aceita 299.9, "299.90" e "R$ 299,90"
        if target is None:
            log.error("Produto ignorado (target_price inválido %r): %s", item["target_price"], item["name"])
            continue
        item["target_price"] = target

        item["url"] = str(item["url"]).strip()
        if not item["url"].lower().startswith(("http://", "https://")):
            log.error("Produto ignorado (url precisa começar com http/https): %s", item["name"])
            continue

        var = item.get("variations") or item.get("variation") or []
        if isinstance(var, (str, int, float)):
            var = [var]
        item["variations"] = [str(v).strip() for v in var if str(v).strip()]  # 40 (número) vira "40"

        if not isinstance(item.get("selectors") or {}, dict):
            log.error("Produto ignorado ('selectors' deve ser um objeto): %s", item["name"])
            continue

        item["id"] = str(item.get("id") or slugify(item["name"]))  # respeita o "id" do JSON, se existir
        if item["id"] in seen_ids:
            log.error("Produto ignorado (id duplicado '%s'): %s", item["id"], item["name"])
            continue
        seen_ids.add(item["id"])
        products.append(item)
    return products


def process_product(product: dict, snap: Snapshot, settings: Settings, notifier: TelegramNotifier,
                    rescrape: Optional[Callable[[], Snapshot]] = None) -> str:
    """Salva a leitura, decide se alerta e envia. Retorna 'error', 'ok' ou 'alert'."""
    pid = product["id"]
    history = db.get_price_history(settings.db_path, pid, settings.history_days)  # antes de salvar a atual
    db.save_snapshot(settings.db_path, pid, snap)

    if snap.error:
        log.warning("[%s] erro: %s", product["name"], snap.error)
        return "error"

    log.info("[%s] à vista R$ %.2f | parcelado %s | estoque=%s | cupons=%d",
             product["name"], snap.price_cash,
             f"R$ {snap.price_installment:.2f}" if snap.price_installment else "—",
             snap.in_stock, len(snap.coupons))

    decision = analyzer.evaluate(product, snap, history, settings)
    if not decision.alert:
        log.info("[%s] sem alerta: %s", product["name"], "; ".join(decision.failed))
        return "ok"

    last = db.get_last_alert(settings.db_path, pid)
    if last and db.hours_since(last["ts"]) < settings.cooldown_hours \
            and snap.price_cash >= last["price"] * 0.99:
        log.info("[%s] alerta suprimido (cooldown)", product["name"])
        return "ok"

    # Confirma com uma 2ª leitura: evita alertar por leitura errada/transitória (página meio carregada,
    # preço de outro item etc.)
    if settings.confirm_alerts and rescrape is not None:
        log.info("[%s] possível alerta (R$ %.2f) — confirmando com nova leitura...", product["name"], snap.price_cash)
        time.sleep(random.uniform(4, 8))
        snap2 = rescrape()
        if snap2.error or snap2.price_cash is None:
            log.warning("[%s] confirmação falhou (%s); alerta adiado", product["name"], snap2.error)
            return "ok"
        decision2 = analyzer.evaluate(product, snap2, history, settings)
        if not decision2.alert or abs(snap2.price_cash - snap.price_cash) > snap.price_cash * 0.02:
            log.info("[%s] a nova leitura (R$ %.2f) não confirmou o alerta (antes R$ %.2f)",
                     product["name"], snap2.price_cash, snap.price_cash)
            return "ok"
        snap, decision = snap2, decision2

    if notifier.send(build_alert_message(product, snap, decision)):
        db.save_alert(settings.db_path, pid, snap.price_cash)
        log.info("[%s] 🔔 ALERTA ENVIADO", product["name"])
        return "alert"
    log.error("[%s] alerta NÃO enviado (falha no Telegram); tentará de novo na próxima rodada", product["name"])
    return "ok"


def _launch_browser(pw, settings: Settings):
    return pw.chromium.launch(
        headless=settings.headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",  # necessário em containers (Docker/Render)
        ],
    )


def _failure_cause(error: str) -> str:
    """Resume o erro numa causa curta para agrupar produtos da mesma loja."""
    first = (error or "erro desconhecido").splitlines()[0]
    if first.startswith("Bloqueado pela loja"):
        return "a loja pede verificação/captcha (bloqueio deste IP)"
    if first.startswith("HTTP 403"):
        return "a loja bloqueia o acesso (HTTP 403)"
    if first.startswith("Preço não encontrado"):
        return "preço não encontrado na página"
    return first.split(" | ")[0][:80]


def _send_failure_notice(notifier: TelegramNotifier, failing: list[tuple[dict, str]], settings: Settings) -> None:
    """Um aviso só, agrupado por loja (em vez de uma linha por produto)."""
    e = html.escape
    groups: dict[tuple[str, str], list[str]] = {}
    for product, error in failing:
        groups.setdefault((store_host(product["url"]), _failure_cause(error)), []).append(product["name"])
    lines = [f"⚠️ <b>Não consigo ler o preço de {len(failing)} produto(s)</b> "
             f"({settings.fail_notice_after} tentativas seguidas):", ""]
    for (host, cause), names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(names[:3]) + (f" e mais {len(names) - 3}" if len(names) > 3 else "")
        lines.append(f"• <b>{e(host)}</b> ({len(names)}): {e(cause)}")
        lines.append(f"   {e(shown)}")
    lines.append("")
    lines.append("Sem leitura não há alerta. Bloqueio por IP não se resolve pelo código; "
                 "erro de preço pode ser ajustado com <code>selectors</code> no products.json.")
    notifier.send("\n".join(lines))


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt  # trata SIGTERM (docker stop) como Ctrl+C: fecha o navegador e sai limpo


def store_host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


MAX_COUPONS_PER_MESSAGE = 8


def send_coupon_digest(batch: list[tuple[dict, list[str]]], settings: Settings, notifier: TelegramNotifier) -> int:
    """Manda uma mensagem por loja com os cupons NOVOS do lote. Retorna quantas mensagens foram enviadas."""
    by_host: dict[str, dict] = {}
    for product, texts in batch:
        entry = by_host.setdefault(store_host(product["url"]),
                                   {"url": product["url"], "name": product["name"], "texts": []})
        for text in texts:
            if text not in entry["texts"]:
                entry["texts"].append(text)
    sent = 0
    for host, entry in by_host.items():
        fresh = db.new_coupons(settings.db_path, host, entry["texts"], settings.coupon_renotify_hours)
        fresh = fresh[:MAX_COUPONS_PER_MESSAGE]  # o excedente vai na próxima rodada (não é marcado como enviado)
        if not fresh:
            continue
        if notifier.send(build_coupon_message(host, fresh, entry["url"], entry["name"])):
            db.mark_coupons_notified(settings.db_path, host, fresh)
            sent += 1
            log.info("[%s] %d cupom(ns) novo(s) enviado(s)", host, len(fresh))
        else:
            log.error("[%s] cupons NÃO enviados (falha no Telegram); tentará de novo na próxima rodada", host)
        time.sleep(1)
    return sent


def only_compare_skip(only: Optional[str]) -> bool:
    """Com --only (ajuste de um produto) não gastamos tempo comparando lojas."""
    return bool(only)


def run_cycle(settings: Settings, notifier: TelegramNotifier, only: Optional[str] = None) -> None:
    from playwright.sync_api import sync_playwright  # import tardio: facilita testes

    products = load_products(settings.products_path)  # recarrega: pode editar o JSON com o loop rodando
    if only:
        needle = only.lower()
        products = [p for p in products if needle in p["id"].lower() or needle in p["name"].lower()]
    if not products:
        log.error("Nenhum produto válido em %s%s", settings.products_path, f" para --only {only!r}" if only else "")
        return
    log.info("Rodada iniciada: %d produto(s)", len(products))

    counts = {"ok": 0, "error": 0, "alert": 0, "crash": 0}
    failing: list[tuple[dict, str]] = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw, settings)
        try:
            for i, product in enumerate(products):
                try:
                    if not browser.is_connected():  # Chromium caiu: reabre em vez de falhar o resto da rodada
                        log.warning("Navegador desconectado; reabrindo")
                        browser = _launch_browser(pw, settings)
                    snap = scrape_product(browser, product, settings)
                    status = process_product(
                        product, snap, settings, notifier,
                        rescrape=lambda p=product, b=browser: scrape_product(b, p, settings),
                    )
                    if not snap.error and snap.coupons and settings.notify_coupons and notifier.configured:
                        try:   # manda na hora (não espera a rodada acabar); cupom já avisado não se repete
                            send_coupon_digest([(product, snap.coupons)], settings, notifier)
                        except Exception:  # noqa: BLE001 — falha nos cupons não pode estragar a rodada
                            log.exception("[%s] erro ao enviar cupons", product["name"])
                    if status == "error" and settings.fail_notice_after and \
                            db.get_consecutive_errors(settings.db_path, product["id"]) == settings.fail_notice_after:
                        failing.append((product, snap.error or ""))
                except Exception:  # noqa: BLE001 — um produto com problema não pode derrubar os outros
                    log.exception("[%s] erro inesperado ao processar; seguindo para o próximo", product["name"])
                    status = "crash"
                counts[status] += 1
                if i < len(products) - 1:
                    time.sleep(random.uniform(3, 8))  # pausa "humana" entre produtos
            if settings.compare_enabled and not only_compare_skip(only):
                try:
                    import compare
                    compare.run_comparisons(browser, products, settings, notifier, process_product)
                except Exception:  # noqa: BLE001 — a comparação é um extra
                    log.exception("Erro na comparação entre lojas")
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    log.info("Rodada concluída: %d ok | %d com erro | %d alerta(s) | %d falha(s) inesperada(s)",
             counts["ok"], counts["error"], counts["alert"], counts["crash"])
    if failing and notifier.configured:
        _send_failure_notice(notifier, failing, settings)


def show_stats(settings: Settings) -> None:
    for p in load_products(settings.products_path):
        hist = [price for _, price in db.get_price_history(settings.db_path, p["id"], settings.history_days)]
        errors = db.count_errors(settings.db_path, p["id"], 24)
        if hist:
            last = db.get_last_reading(settings.db_path, p["id"])
            last_txt = f"R$ {last['price_cash']:.2f}" if last and last["price_cash"] else "erro"
            print(f"{p['name']}: {len(hist)} leituras | mediana R$ {statistics.median(hist):.2f} | "
                  f"mín R$ {min(hist):.2f} | máx R$ {max(hist):.2f} | última {last_txt} | "
                  f"alvo R$ {p['target_price']:.2f} | erros 24h: {errors}")
        else:
            print(f"{p['name']}: sem leituras válidas ainda | erros 24h: {errors}")


def start_health_server() -> None:
    """Abre uma porta HTTP mínima se a variável PORT existir (necessário para Web Service do Render)."""
    port = os.getenv("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"bf-monitor ok")

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silencia o log de cada requisição
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Servidor de saúde na porta %s", port)


def _setup_logging(log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=handlers, force=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor de preços Black Friday")
    parser.add_argument("--once", action="store_true", help="roda uma vez e sai")
    parser.add_argument("--only", metavar="TEXTO", help="processa só produtos cujo id/nome contém TEXTO")
    parser.add_argument("--test-telegram", action="store_true", help="envia mensagem de teste")
    parser.add_argument("--stats", action="store_true", help="mostra resumo do histórico")
    parser.add_argument("--search-test", metavar="NOME",
                        help="testa a busca do modelo em todas as lojas e mostra o resultado (não envia nada)")
    args = parser.parse_args()

    settings = load_settings()
    _setup_logging(settings.log_file)
    db.init_db(settings.db_path)
    notifier = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)

    if args.test_telegram:
        ok = notifier.send("✅ Monitor Black Friday conectado ao Telegram!")
        print("Mensagem enviada!" if ok else "Falhou. Verifique TOKEN e CHAT_ID no .env")
        return 0 if ok else 1

    if args.stats:
        show_stats(settings)
        return 0

    if args.search_test:
        from playwright.sync_api import sync_playwright
        import compare
        with sync_playwright() as pw:
            browser = _launch_browser(pw, settings)
            try:
                for line in compare.debug_search(browser, args.search_test, settings):
                    print(line, flush=True)
            finally:
                browser.close()
        return 0

    if not notifier.configured:
        log.warning("Telegram NÃO configurado: o histórico será coletado, mas nenhum alerta será enviado.")

    if args.once:
        run_cycle(settings, notifier, only=args.only)
        return 0

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    start_health_server()
    log.info("Loop iniciado (a cada ~%.0f min). Ctrl+C para parar.", settings.interval_minutes)
    if notifier.configured:
        try:
            n = len(load_products(settings.products_path))
            notifier.send(f"🟢 Monitor iniciado: {n} produto(s), rodada a cada ~{settings.interval_minutes:.0f} min.")
        except Exception:  # noqa: BLE001
            log.exception("Falha ao enviar mensagem de início")
    try:
        while True:
            try:
                run_cycle(settings, notifier, only=args.only)
            except Exception:  # noqa: BLE001 — o loop nunca deve morrer
                log.exception("Erro inesperado na rodada")
            sleep_s = settings.interval_minutes * 60 * random.uniform(0.85, 1.15)  # jitter
            log.info("Próxima rodada em %.1f min", sleep_s / 60)
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
