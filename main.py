"""Script principal para monitoramento de preços de produtos com Playwright e alertas via Telegram."""

import os
import sys
import time
import json
import logging
from typing import List

from config import load_settings
from db import Database
from scraper import init_browser, scrape_product
from notifier import send_telegram_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("main")


def load_products_from_json() -> List[dict]:
    """Carrega os produtos cadastrados diretamente do arquivo products.json."""
    products_file = os.path.join(os.path.dirname(__file__), "products.json")
    if not os.path.exists(products_file):
        log.error("Arquivo products.json não encontrado em: %s", products_file)
        return []
    
    try:
        with open(products_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Erro ao ler o arquivo products.json: %s", exc)
        return []


def run_monitoring_cycle(settings, products: List[dict], db: Database):
    """Executa um ciclo completo de raspagem e envio de alertas."""
    log.info("Iniciando rodada de monitoramento para %d produto(s)...", len(products))
    
    with init_browser(headless=settings.headless) as page:
        grouped_snapshots = {}

        for product in products:
            try:
                log.info("Verificando: %s", product.get("name"))
                snapshot = scrape_product(page, product, settings)
                
                if snapshot and snapshot.price_cash:
                    db.save_price(product["id"], snapshot.price_cash, snapshot.price_installment)
                    
                    group_id = product.get("group_id", product["id"])
                    
                    if group_id not in grouped_snapshots:
                        grouped_snapshots[group_id] = []
                    
                    grouped_snapshots[group_id].append({
                        "product": product,
                        "snapshot": snapshot
                    })

            except Exception as exc:
                log.error("Erro ao verificar %s: %s", product.get("name"), exc)

        for group_id, items in grouped_snapshots.items():
            items.sort(key=lambda x: x["snapshot"].price_cash)
            best_item = items[0]
            
            product = best_item["product"]
            snapshot = best_item["snapshot"]
            
            min_price_history = db.get_min_price(product["id"])
            should_alert = False
            discount_pct = 0.0

            if min_price_history and min_price_history > 0:
                if snapshot.price_cash < min_price_history:
                    discount_pct = ((min_price_history - snapshot.price_cash) / min_price_history) * 100
                    if discount_pct >= 10.0:
                        should_alert = True

            target_price = product.get("target_price", 0.0)
            if target_price > 0 and snapshot.price_cash <= target_price:
                should_alert = True

            if getattr(settings, "allow_alert_without_history", False) and not should_alert:
                should_alert = True

            if should_alert:
                send_telegram_alert(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    snapshot=snapshot,
                    product=product,
                    discount_pct=discount_pct
                )


def main():
    """Função principal com loop de execução contínua."""
    settings = load_settings()
    products = load_products_from_json()
    db = Database(settings.db_path)

    if not products:
        log.error("Nenhum produto cadastrado no products.json. Encerrando.")
        sys.exit(1)

    log.info("Serviço de monitoramento iniciado com sucesso.")

    while True:
        try:
            run_monitoring_cycle(settings, products, db)
        except Exception as exc:
            log.error("Erro na rodada de monitoramento: %s", exc)

        check_interval = getattr(settings, "check_interval_minutes", 30)
        log.info("Próxima rodada em %d minutos...", check_interval)
        time.sleep(check_interval * 60)


if __name__ == "__main__":
    main()
