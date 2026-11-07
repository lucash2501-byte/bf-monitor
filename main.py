"""Script principal para monitoramento de preços de produtos com Playwright e alertas via Telegram."""

import os
import sys
import time
import logging
from dataclasses import dataclass
from typing import List, Optional

from config import load_settings, load_products
from db import Database
from scraper import init_browser, scrape_product
from notifier import send_telegram_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("main")


def run_monitoring_cycle(settings, products: List[dict], db: Database):
    """Executa um ciclo completo de raspagem e envio de alertas."""
    log.info("Iniciando rodada de monitoramento para %d produto(s)...", len(products))
    
    with init_browser(headless=settings.headless) as page:
        # Agrupar ofertas por group_id para selecionar o menor preço por produto
        grouped_snapshots = {}

        for product in products:
            try:
                log.info("Verificando: %s", product.get("name"))
                snapshot = scrape_product(page, product, settings)
                
                if snapshot and snapshot.price_cash:
                    # Grava o preço atual no banco de dados SQLite
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

        # Avaliar e enviar alertas para a melhor oferta de cada grupo
        for group_id, items in grouped_snapshots.items():
            # Ordena pelo menor preço à vista
            items.sort(key=lambda x: x["snapshot"].price_cash)
            best_item = items[0]  # Loja campeã (menor preço)
            
            product = best_item["product"]
            snapshot = best_item["snapshot"]
            
            min_price_history = db.get_min_price(product["id"])
            should_alert = False
            discount_pct = 0.0

            # Regra 1: Desconto de 10% ou mais em relação ao histórico
            if min_price_history and min_price_history > 0:
                if snapshot.price_cash < min_price_history:
                    discount_pct = ((min_price_history - snapshot.price_cash) / min_price_history) * 100
                    if discount_pct >= 10.0:
                        should_alert = True

            # Regra 2: Preço igual ou abaixo do target_price cadastrado
            target_price = product.get("target_price", 0.0)
            if target_price > 0 and snapshot.price_cash <= target_price:
                should_alert = True

            # Regra 3: Alerta inicial sem histórico (se configurado no .env)
            if settings.allow_alert_without_history and not should_alert:
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
    products = load_products()
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

        log.info("Pr
