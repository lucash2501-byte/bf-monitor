"""Monitor de preços para Black Friday.

Uso:
    python main.py                 # loop contínuo
    python main.py --once          # uma rodada e sai
    python main.py --test-telegram # envia mensagem de teste
    python main.py --stats         # resumo do histórico por produto
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import statistics
import sys
import time
import unicodedata

import analyzer
import db
from config import Settings, load_settings
from notifier import TelegramNotifier, build_alert_message
from scraper import scrape_product

log = logging.getLogger("main")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_products(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    products = []
    seen_ids: set[str] = set()
    for item in raw:
        missing = [k for k in ("name", "url", "target_price") if k not in item]
        if missing:
            log.error("Produto ignorado (faltando %s): %s", missing, item)
            continue
        var = item.get("variations") or item.get("variation") or []
        item["variations"] = [var] if isinstance(var, str) else list(var)
        item["id"] = str(item.get("id") or slugify(item["name"]))  # respeita o "id" do JSON, se existir
        if item["id"] in seen_ids:
            log.error("Produto ignorado (id duplicado '%s'): %s", item["id"], item["name"])
            continue
        seen_ids.add(item["id"])
        products.append(item)
    return products


def process_product(product: dict, snap, settings: Settings, notifier: TelegramNotifier) -> None:
    pid = product["id"]
    history = db.get_price_history(settings.db_path, pid, settings.history_days)  # antes de salvar a atual
    db.save_snapshot(settings.db_path, pid, snap)

    if snap.error:
        log.warning("[%s] erro: %s", product["name"], snap.error)
        return

    log.info("[%s] à vista R$ %.2f | parcelado %s | estoque=%s | cupons=%d",
             product["name"], snap.price_cash,
             f"R$ {snap.price_installment:.2f}" if snap.price_installment else "—",
             snap.in_stock, len(snap.coupons))

    decision = analyzer.evaluate(product, snap, history, settings)
    if not decision.alert:
        log.info("[%s] sem alerta: %s", product["name"], "; ".join(decision.failed))
        return

    last = db.get_last_alert(settings.db_path, pid)
    if last and db.hours_since(last["ts"]) < settings.cooldown_hours \
            and snap.price_cash >= last["price"] * 0.99:
        log.info("[%s] alerta suprimido (cooldown)", product["name"])
        return

    if notifier.send(build_alert_message(product, snap, decision)):
        db.save_alert(settings.db_path, pid, snap.price_cash)
        log.info("[%s] 🔔 ALERTA ENVIADO", product["name"])


def run_cycle(settings: Settings, notifier: TelegramNotifier) -> None:
    from playwright.sync_api import sync_playwright  # import tardio: facilita testes

    products = load_products(settings.products_path)  # recarrega: pode editar o JSON com o loop rodando
    if not products:
        log.error("Nenhum produto válido em %s", settings.products_path)
        return
    log.info("Rodada iniciada: %d produto(s)", len(products))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=settings.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",  # necessário em containers (Docker/Render)
            ],
        )
        try:
            for i, product in enumerate(products):
                snap = scrape_product(browser, product, settings)
                process_product(product, snap, settings, notifier)
                if i < len(products) - 1:
                    time.sleep(random.uniform(3, 8))  # pausa "humana" entre produtos
        finally:
            browser.close()


def show_stats(settings: Settings) -> None:
    for p in load_products(settings.products_path):
        hist = db.get_price_history(settings.db_path, p["id"], settings.history_days)
        if hist:
            print(f"{p['name']}: {len(hist)} leituras | média R$ {statistics.mean(hist):.2f} | "
                  f"mín R$ {min(hist):.2f} | máx R$ {max(hist):.2f} | alvo R$ {p['target_price']:.2f}")
        else:
            print(f"{p['name']}: sem histórico ainda")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor de preços Black Friday")
    parser.add_argument("--once", action="store_true", help="roda uma vez e sai")
    parser.add_argument("--test-telegram", action="store_true", help="envia mensagem de teste")
    parser.add_argument("--stats", action="store_true", help="mostra resumo do histórico")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("monitor.log", encoding="utf-8")],
    )
    settings = load_settings()
    db.init_db(settings.db_path)
    notifier = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)

    if args.test_telegram:
        ok = notifier.send("✅ Monitor Black Friday conectado ao Telegram!")
        print("Mensagem enviada!" if ok else "Falhou. Verifique TOKEN e CHAT_ID no .env")
        return 0 if ok else 1

    if args.stats:
        show_stats(settings)
        return 0

    if not notifier.configured:
        log.warning("Telegram NÃO configurado: o histórico será coletado, mas nenhum alerta será enviado.")

    if args.once:
        run_cycle(settings, notifier)
        return 0

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
                run_cycle(settings, notifier)
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
