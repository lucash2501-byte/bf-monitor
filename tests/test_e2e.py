"""Rodada completa (run_cycle) com Chromium real contra lojas locais falsas e Telegram simulado."""
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import db
import main
from conftest import make_settings
from test_scraper import QuietHandler, base_url  # noqa: F401  (servidor de lojas falsas, reaproveitado)


class FakeNotifier:
    configured = True

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, retries=3):
        self.sent.append(text)
        return True


def seed(settings, product_id, price, n=30):
    now = datetime.now(timezone.utc)
    with db.connect(settings.db_path) as conn:
        for i in range(n):
            ts = (now - timedelta(hours=n - i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO prices (product_id, ts, price_cash, in_stock) VALUES (?, ?, ?, 1)",
                         (product_id, ts, price))


def test_full_cycle_alert_failure_notice_and_crash_isolation(tmp_path, base_url):  # noqa: F811
    products = [
        {"id": "explode", "name": "Produto que explode", "url": f"{base_url}/deal.html", "target_price": 300},
        {"id": "deal", "name": "Tênis Oferta", "url": f"{base_url}/deal.html", "target_price": 300,
         "variations": [40]},
        {"id": "bloqueado", "name": "Loja bloqueada", "url": f"{base_url}/captcha.html", "target_price": 300},
        {"id": "caro", "name": "Preço normal", "url": f"{base_url}/jsonld_sizes.html", "target_price": 300,
         "variations": ["40"]},
    ]
    path = tmp_path / "products.json"
    path.write_text(json.dumps(products), encoding="utf-8")
    settings = make_settings(tmp_path, products_path=str(path), fail_notice_after=1)
    db.init_db(settings.db_path)
    seed(settings, "deal", 400.0)      # histórico a R$ 400 -> R$ 250 é desconto real (37,5%)
    seed(settings, "caro", 400.0)      # R$ 399,90 não é desconto
    notifier = FakeNotifier()

    real_process = main.process_product

    def process(product, *args, **kwargs):
        if product["id"] == "explode":
            raise RuntimeError("falha inesperada simulada")
        return real_process(product, *args, **kwargs)

    # o run_cycle abre o próprio Chromium (como em produção); só removemos as pausas "humanas"
    with mock.patch("main.process_product", side_effect=process), mock.patch("main.time.sleep"):
        main.run_cycle(settings, notifier)

    alerts = [m for m in notifier.sent if "Tênis Oferta" in m and "À vista" in m]
    assert len(alerts) == 1, notifier.sent
    assert "R$ 250,00" in alerts[0] and "37.5% abaixo" in alerts[0]
    assert db.get_last_alert(settings.db_path, "deal")["price"] == 250.0

    notices = [m for m in notifier.sent if "Não consigo ler" in m]
    assert len(notices) == 1 and "Loja bloqueada" in notices[0]      # aviso agrupado de leitura falhando
    assert not [m for m in notifier.sent if "Preço normal" in m and "À vista" in m]   # sem desconto real: sem alerta
    coupons = [m for m in notifier.sent if m.startswith("🎟️")]
    assert len(coupons) == 1 and "BLACK10" in coupons[0]            # cupom da página enviado, 1 mensagem por loja
    assert db.get_last_reading(settings.db_path, "bloqueado")["error"]
    assert db.get_last_reading(settings.db_path, "caro")["price_cash"] == 399.90   # seguiu após o crash
