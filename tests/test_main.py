import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import db
import main
from scraper import Snapshot


class FakeNotifier:
    configured = True

    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text, retries=3):
        self.sent.append(text)
        return self.ok


PRODUCT = {"id": "p1", "name": "Tênis", "url": "https://l.com/p", "target_price": 300.0, "variations": ["40"]}


def seed_history(settings, price=400.0, n=30):
    """Insere n leituras espaçadas por 1h (passado), para o analisador ter histórico suficiente."""
    db.init_db(settings.db_path)
    now = datetime.now(timezone.utc)
    with db.connect(settings.db_path) as conn:
        for i in range(n):
            ts = (now - timedelta(hours=n - i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO prices (product_id, ts, price_cash, in_stock) VALUES (?, ?, ?, 1)",
                         (PRODUCT["id"], ts, price))


def good(price=250.0, stock=True):
    return Snapshot(price_cash=price, in_stock=stock)


# ------------------------------------------------------------------ load_products
def write_products(tmp_path, data) -> str:
    p = tmp_path / "products.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_load_products_normalizes_and_validates(tmp_path):
    path = write_products(tmp_path, [
        {"name": "Ok Número", "url": "https://a.com/1", "target_price": 99.9, "variations": [40, "M"]},
        {"name": "Preço Texto", "url": "https://a.com/2", "target_price": "R$ 1.299,90", "variation": "G"},
        {"name": "Sem URL", "target_price": 10},
        {"name": "Preço Ruim", "url": "https://a.com/3", "target_price": "barato"},
        {"name": "URL Ruim", "url": "ftp://a.com", "target_price": 10},
        {"name": "Selectors Ruim", "url": "https://a.com/4", "target_price": 10, "selectors": "x"},
        {"id": "dup", "name": "A", "url": "https://a.com/5", "target_price": 1},
        {"id": "dup", "name": "B", "url": "https://a.com/6", "target_price": 1},
        "lixo",
    ])
    products = main.load_products(path)
    assert [p["name"] for p in products] == ["Ok Número", "Preço Texto", "A"]
    assert products[0]["variations"] == ["40", "M"]          # número virou texto
    assert products[1]["target_price"] == 1299.90 and products[1]["variations"] == ["G"]
    assert products[0]["id"] == "ok-numero"


def test_load_products_survives_bad_files(tmp_path):
    assert main.load_products(str(tmp_path / "nao-existe.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("[ {oops", encoding="utf-8")
    assert main.load_products(str(bad)) == []
    assert main.load_products(write_products(tmp_path, {"name": "não é lista"})) == []


def test_repo_products_json_is_valid():
    """O products.json real do projeto precisa carregar sem descartar nenhum item."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "products.json")
    raw = json.load(open(path, encoding="utf-8"))
    assert len(main.load_products(path)) == len(raw)


# ------------------------------------------------------------------ process_product
def test_error_snapshot_is_saved_and_reports_error(settings):
    db.init_db(settings.db_path)
    n = FakeNotifier()
    assert main.process_product(PRODUCT, Snapshot(error="bloqueado"), settings, n) == "error"
    assert db.get_consecutive_errors(settings.db_path, "p1") == 1 and not n.sent


def test_real_drop_sends_alert_and_records_it(settings):
    seed_history(settings)
    n = FakeNotifier()
    assert main.process_product(PRODUCT, good(250), settings, n) == "alert"
    assert len(n.sent) == 1 and "R$ 250,00" in n.sent[0]
    assert db.get_last_alert(settings.db_path, "p1")["price"] == 250.0


def test_no_history_no_alert(settings):
    db.init_db(settings.db_path)
    n = FakeNotifier()
    assert main.process_product(PRODUCT, good(250), settings, n) == "ok" and not n.sent


def test_cooldown_suppresses_repeat_but_not_a_deeper_drop(settings):
    seed_history(settings)
    n = FakeNotifier()
    assert main.process_product(PRODUCT, good(250), settings, n) == "alert"
    assert main.process_product(PRODUCT, good(250), settings, n) == "ok"        # repetido: suprimido
    assert main.process_product(PRODUCT, good(248), settings, n) == "ok"        # <1% mais barato: suprimido
    assert main.process_product(PRODUCT, good(200), settings, n) == "alert"     # caiu mais de 1%: alerta
    assert len(n.sent) == 2


def test_telegram_failure_does_not_record_alert(settings):
    seed_history(settings)
    n = FakeNotifier(ok=False)
    assert main.process_product(PRODUCT, good(250), settings, n) == "ok"
    assert db.get_last_alert(settings.db_path, "p1") is None  # tentará de novo na próxima rodada


def test_confirmation_blocks_a_misread(settings):
    """1ª leitura parece promoção, 2ª leitura mostra o preço normal: era leitura errada, não alerta."""
    seed_history(settings)
    n = FakeNotifier()
    with mock.patch("main.time.sleep"):
        status = main.process_product(PRODUCT, good(250), settings, n, rescrape=lambda: good(400))
    assert status == "ok" and not n.sent


def test_confirmation_passes_when_second_read_agrees(settings):
    seed_history(settings)
    n = FakeNotifier()
    with mock.patch("main.time.sleep"):
        status = main.process_product(PRODUCT, good(250), settings, n, rescrape=lambda: good(251))
    assert status == "alert" and "R$ 251,00" in n.sent[0]


def test_confirmation_error_postpones_alert(settings):
    seed_history(settings)
    n = FakeNotifier()
    with mock.patch("main.time.sleep"):
        status = main.process_product(PRODUCT, good(250), settings, n,
                                      rescrape=lambda: Snapshot(error="timeout"))
    assert status == "ok" and not n.sent
