from datetime import datetime, timedelta, timezone

from analyzer import evaluate
from conftest import make_settings
from scraper import Snapshot

PRODUCT = {"name": "Tênis", "target_price": 300.0}


def history(prices, hours_between=1.0):
    """Histórico terminando agora, com leituras espaçadas de `hours_between` horas."""
    now = datetime.now(timezone.utc)
    n = len(prices)
    return [((now - timedelta(hours=hours_between * (n - i))).strftime("%Y-%m-%d %H:%M:%S"), p)
            for i, p in enumerate(prices)]


def snap(price, stock=True):
    return Snapshot(price_cash=price, in_stock=stock)


def test_real_discount_alerts():
    d = evaluate(PRODUCT, snap(250), history([400.0] * 30), make_settings())
    assert d.alert, d.failed
    assert d.discount_vs_ref_pct == 37.5


def test_fake_discount_is_rejected():
    """Preço normal 400, hoje 'promoção' de 390 (2,5%): não é desconto real."""
    d = evaluate({"name": "x", "target_price": 450}, snap(390), history([400.0] * 30), make_settings())
    assert not d.alert
    assert any("não é real" in f for f in d.failed)


def test_metade_do_dobro_price_pumped_before_sale():
    """Loja ficou 2 semanas a R$ 200, subiu para 400 e agora 'desconta' para 300 (25% off do inflado).
    Contra o preço real anterior (200) isso NÃO é desconto."""
    prices = [200.0] * 15 + [400.0] * 15
    d = evaluate({"name": "x", "target_price": 350}, snap(300), history(prices), make_settings())
    assert d.pump_detected
    assert not d.alert
    assert d.ref_price == 200.0


def test_slowly_falling_price_uses_median():
    prices = [500.0] * 10 + [450.0] * 10 + [400.0] * 10
    d = evaluate({"name": "x", "target_price": 400}, snap(380), history(prices), make_settings())
    assert d.ref_price == 450.0 and d.alert is True


def test_outlier_reading_does_not_distort_reference():
    prices = [400.0] * 20 + [9.9]  # uma leitura errada
    d = evaluate({"name": "x", "target_price": 400}, snap(390), history(prices), make_settings())
    assert d.median_price == 400.0


def test_not_enough_samples():
    d = evaluate(PRODUCT, snap(250), history([400.0] * 3), make_settings())
    assert not d.alert and any("Histórico insuficiente" in f for f in d.failed)


def test_enough_samples_but_too_short_span():
    """30 leituras em 30 minutos não provam nada (precisa de >= 24h)."""
    d = evaluate(PRODUCT, snap(250), history([400.0] * 30, hours_between=1 / 60), make_settings())
    assert not d.alert and any("Histórico insuficiente" in f for f in d.failed)


def test_allow_without_history_alerts_with_warning():
    d = evaluate(PRODUCT, snap(250), [], make_settings(allow_alert_without_history=True))
    assert d.alert and d.warnings


def test_price_above_target():
    d = evaluate(PRODUCT, snap(320), history([500.0] * 30), make_settings())
    assert not d.alert and any("acima do alvo" in f for f in d.failed)


def test_out_of_stock_blocks():
    d = evaluate(PRODUCT, snap(250, stock=False), history([400.0] * 30), make_settings())
    assert not d.alert and "Variação sem estoque" in d.failed


def test_unknown_stock_alerts_with_warning_by_default():
    d = evaluate(PRODUCT, snap(250, stock=None), history([400.0] * 30), make_settings())
    assert d.alert and d.stock_unknown and d.warnings


def test_unknown_stock_blocks_when_strict():
    d = evaluate(PRODUCT, snap(250, stock=None), history([400.0] * 30),
                 make_settings(alert_on_unknown_stock=False))
    assert not d.alert


def test_suspicious_drop_is_flagged():
    d = evaluate({"name": "x", "target_price": 300}, snap(50), history([400.0] * 30), make_settings())
    assert d.alert and d.suspicious_drop


def test_missing_or_zero_price_never_alerts():
    for s in (snap(None), snap(0)):
        assert not evaluate(PRODUCT, s, history([400.0] * 30), make_settings()).alert
