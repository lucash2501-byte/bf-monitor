"""Script principal de monitorização de preços com Playwright e Telegram."""

import os
import sys
import time
import json
import sqlite3
import logging
from typing import List, Optional
from playwright.sync_api import sync_playwright

from config import load_settings
import scraper
from notifier import send_telegram_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("main")


class SimpleDatabase:
    """Banco de dados SQLite simples para registo histórico."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT,
                    price_cash REAL,
                    price_installment REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_price(self, product_id: str, price_cash: float, price_installment: Optional[float] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO price_history (product_id, price_cash, price_installment) VALUES (?, ?, ?)",
                (product_id, price_cash, price_installment)
            )
            conn.commit()

    def get_min_price(self, product_id: str) -> Optional[float]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT MIN(price_cash) FROM price_history WHERE product_id = ?",
                (product_id,)
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else None


def load_products_from_json() -> List[dict]:
    """Carrega os produtos do ficheiro products.json."""
    products_file = os.path.join(os.path.dirname(__file__), "products.json")
    if not os.path.exists(products_file):
        log.error("Ficheiro products.json não encontrado.")
        return []
    try:
        with open(products_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Erro ao ler products.json: %s", exc)
        return []


def run_monitoring_cycle(settings, products: List[dict], db: SimpleDatabase):
    """Executa o ciclo de raspagem usando a função do módulo scraper."""
    log.info("Iniciando rodada de monitorização para %d produto(s)...", len(products))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=getattr(settings, "headless", True),
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context()
        page = context.new_page()

        grouped_snapshots = {}

        for product in products:
            try:
                log.info("Verificando: %s", product.get("name"))
                
                # Executa a raspagem dinamicamente de acordo com a função disponível no scraper.py
                if hasattr(scraper, "scrape_product"):
                    snapshot = scraper.scrape_product(page, product, settings)
                elif hasattr(scraper, "run_scraper"):
                    snapshot = scraper.run_scraper(page, product)
                else:
                    log.error("Função de scraping não encontrada no scraper.py")
                    break

                if snapshot and getattr(snapshot, "price_cash", None):
                    price_cash = snapshot.price_cash
                    price_inst = getattr(snapshot, "price_installment", None)
                    db.save_price(product["id"], price_cash, price_inst)

                    group_id = product.get("group_id", product["id"])
                    if group_id not in grouped_snapshots:
                        grouped_snapshots[group_id] = []

                    grouped_snapshots[group_id].append({
                        "product": product,
                        "snapshot": snapshot
                    })

            except Exception as exc:
                log.error("Erro ao verificar %s: %s", product.get("name"), exc)

        browser.close()

        # Avaliação de alertas por grupo (menor preço campeão)
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

            if should_alert:
                send_telegram_alert(
                    bot_token=getattr(settings, "telegram_bot_token", ""),
                    chat_id=getattr(settings, "telegram_chat_id", ""),
                    snapshot=snapshot,
                    product=product,
                    discount_pct=discount_pct
                )


def main():
    """Loop principal de execução."""
    settings = load_settings()
    products = load_products_from_json()
    db_path = getattr(settings, "db_path", "prices.db")
    db = SimpleDatabase(db_path)

    if not products:
        log.error("Nenhum produto em products.json. A encerrar.")
        sys.exit(1)

    log.info("Serviço de monitorização iniciado com sucesso.")

    while True:
        try:
            run_monitoring_cycle(settings, products, db)
        except Exception as exc:
            log.error("Erro na rodada de monitorização: %s", exc)

        interval = getattr(settings, "check_interval_minutes", 30)
        log.info("Próxima rodada em %d minutos...", interval)
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
