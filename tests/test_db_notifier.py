from unittest import mock

import requests

import db
from analyzer import Decision
from notifier import TelegramNotifier, build_alert_message
from scraper import Snapshot


# ------------------------------------------------------------------ db
def test_db_history_roundtrip_and_ordering(settings):
    db.init_db(settings.db_path)
    for price in (300.0, 310.0, 305.0):
        db.save_snapshot(settings.db_path, "p1", Snapshot(price_cash=price, in_stock=True, coupons=["a", "b"]))
    db.save_snapshot(settings.db_path, "p1", Snapshot(error="boom"))
    db.save_snapshot(settings.db_path, "outro", Snapshot(price_cash=1.0))
    hist = db.get_price_history(settings.db_path, "p1", 14)
    assert [p for _, p in hist] == [300.0, 310.0, 305.0]  # sem erro, sem outro produto, em ordem
    assert db.get_last_reading(settings.db_path, "p1")["error"] == "boom"


def test_consecutive_errors_and_error_count(settings):
    db.init_db(settings.db_path)
    db.save_snapshot(settings.db_path, "p", Snapshot(error="x"))
    db.save_snapshot(settings.db_path, "p", Snapshot(price_cash=10.0))
    assert db.get_consecutive_errors(settings.db_path, "p") == 0
    db.save_snapshot(settings.db_path, "p", Snapshot(error="x"))
    db.save_snapshot(settings.db_path, "p", Snapshot(error="y"))
    assert db.get_consecutive_errors(settings.db_path, "p") == 2
    assert db.count_errors(settings.db_path, "p", 24) == 3


def test_alert_roundtrip(settings):
    db.init_db(settings.db_path)
    assert db.get_last_alert(settings.db_path, "p") is None
    db.save_alert(settings.db_path, "p", 199.9)
    last = db.get_last_alert(settings.db_path, "p")
    assert last["price"] == 199.9 and db.hours_since(last["ts"]) < 0.1


# ------------------------------------------------------------------ notifier
def fake_response(status=200, text="{}", json_data=None):
    r = mock.Mock()
    r.ok = status < 400
    r.status_code = status
    r.text = text
    r.json.return_value = json_data if json_data is not None else {}
    return r


def test_send_success():
    with mock.patch("notifier.requests.post", return_value=fake_response()) as post:
        assert TelegramNotifier("123:ABC", "42").send("oi")
    assert post.call_args.kwargs["json"]["chat_id"] == "42"


def test_unconfigured_does_not_send():
    with mock.patch("notifier.requests.post") as post:
        assert not TelegramNotifier("COLE_AQUI_O_TOKEN", "COLE_AQUI").send("oi")
        assert not TelegramNotifier("", "").send("oi")
    post.assert_not_called()


def test_token_never_reaches_the_log(caplog):
    exc = requests.ConnectionError("HTTPSConnectionPool: url /bot123:ABC/sendMessage failed")
    with mock.patch("notifier.requests.post", side_effect=exc), mock.patch("notifier.time.sleep"):
        assert not TelegramNotifier("123:ABC", "42").send("oi", retries=2)
    assert "123:ABC" not in caplog.text and "***" in caplog.text


def test_client_error_is_not_retried():
    with mock.patch("notifier.requests.post", return_value=fake_response(401, "Unauthorized")) as post, \
            mock.patch("notifier.time.sleep"):
        assert not TelegramNotifier("123:ABC", "42").send("oi", retries=3)
    assert post.call_count == 1


def test_rate_limit_waits_and_retries():
    responses = [fake_response(429, "slow down", {"parameters": {"retry_after": 3}}), fake_response()]
    with mock.patch("notifier.requests.post", side_effect=responses), \
            mock.patch("notifier.time.sleep") as sleep:
        assert TelegramNotifier("123:ABC", "42").send("oi")
    sleep.assert_called_with(3.0)


def test_broken_html_falls_back_to_plain_text():
    responses = [fake_response(400, "Bad Request: can't parse entities"), fake_response()]
    with mock.patch("notifier.requests.post", side_effect=responses) as post, mock.patch("notifier.time.sleep"):
        assert TelegramNotifier("123:ABC", "42").send("<b>oi</b> &amp; tchau")
    second = post.call_args.kwargs["json"]
    assert "parse_mode" not in second and second["text"] == "oi & tchau"


def test_alert_message_content_and_escaping():
    product = {"name": "Tênis <X> & Y", "url": "https://l.com/p?a=1&b=2", "target_price": 300.0,
               "variations": ["40"]}
    snap = Snapshot(price_cash=1299.9, price_installment=1399.0, installment_text="10x de R$ 139,90",
                    in_stock=True, coupons=["Use BLACK10 <já>"])
    d = Decision(alert=True, ref_price=1500.0, min_price=1450.0, samples=30, span_hours=100,
                 discount_vs_ref_pct=13.3, warnings=["cuidado"])
    msg = build_alert_message(product, snap, d)
    assert "Tênis &lt;X&gt; &amp; Y" in msg
    assert "R$ 1.299,90" in msg and "13.3% abaixo" in msg and "4.2 dias" in msg
    assert "a=1&amp;b=2" in msg and "&lt;já&gt;" in msg and "⚠️ cuidado" in msg


def test_alert_message_flags_unknown_stock():
    product = {"name": "P", "url": "https://l.com", "target_price": 1.0, "variations": ["M"]}
    msg = build_alert_message(product, Snapshot(price_cash=1.0), Decision(alert=True, stock_unknown=True))
    assert "estoque não confirmado" in msg
