"""Testes de integração do scraper: Chromium real contra páginas locais que imitam lojas."""
import functools
import http.server
import os
import threading

import pytest

from conftest import make_settings
from scraper import scrape_product

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(QuietHandler, directory=FIXTURES)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium indisponível: {exc}")
        yield b
        b.close()


def scrape(browser, base_url, page, variations=(), selectors=None, **settings_kw):
    product = {"id": "t", "name": "Teste", "url": f"{base_url}/{page}", "target_price": 1000.0,
              "variations": list(variations), "selectors": selectors or {}}
    return scrape_product(browser, product, make_settings(**settings_kw))


def test_jsonld_price_installment_and_size_stock(browser, base_url):
    s = scrape(browser, base_url, "jsonld_sizes.html", ["40"])
    assert s.error is None
    assert s.price_cash == 399.90
    assert s.price_installment == 399.90 and s.installment_text == "10x de R$ 39,99"
    assert s.in_stock is True


def test_size_buttons_disabled_unavailable_and_missing(browser, base_url):
    assert scrape(browser, base_url, "jsonld_sizes.html", ["41"]).in_stock is False   # atributo disabled
    assert scrape(browser, base_url, "jsonld_sizes.html", ["42"]).in_stock is False   # classe "unavailable"
    assert scrape(browser, base_url, "jsonld_sizes.html", ["43"]).in_stock is False   # "43 - Indisponível"
    assert scrape(browser, base_url, "jsonld_sizes.html", ["45"]).in_stock is None    # tamanho inexistente


def test_multiple_variations_all_must_be_available(browser, base_url):
    assert scrape(browser, base_url, "jsonld_sizes.html", ["39", "40"]).in_stock is True
    assert scrape(browser, base_url, "jsonld_sizes.html", ["40", "41"]).in_stock is False
    assert scrape(browser, base_url, "jsonld_sizes.html", ["40", "45"]).in_stock is None


def test_coupons_ignore_menu_links(browser, base_url):
    s = scrape(browser, base_url, "jsonld_sizes.html", ["40"])
    assert s.coupons == ["Use o cupom BLACK10 e ganhe 10% OFF"]


def test_dom_heuristic_ignores_old_price_installments_and_shipping(browser, base_url):
    s = scrape(browser, base_url, "dom_price_old_new.html")
    assert s.error is None and s.price_cash == 349.90


def test_price_selector_beats_jsonld_and_variation_selector_works(browser, base_url):
    sel = {"price": "#preco-pix", "variation": "div.sku[data-size='{v}']"}
    s = scrape(browser, base_url, "selector_override.html", ["M"], sel)
    assert s.price_cash == 1234.56 and s.in_stock is True
    assert scrape(browser, base_url, "selector_override.html", ["G"], sel).in_stock is False


def test_stock_without_variations(browser, base_url):
    assert scrape(browser, base_url, "jsonld_out_of_stock.html").in_stock is False       # JSON-LD OutOfStock
    ok = scrape(browser, base_url, "buy_button_enabled.html")
    assert ok.price_cash == 189.90 and ok.in_stock is True    # "esgotado" de item relacionado não conta
    assert scrape(browser, base_url, "buy_button_disabled.html").in_stock is False


def test_blocked_page_reports_error(browser, base_url):
    s = scrape(browser, base_url, "captcha.html")
    assert s.price_cash is None and "bloqueada" in s.error.lower()


def test_page_without_price_reports_actionable_error(browser, base_url):
    s = scrape(browser, base_url, "no_price.html")
    assert "selectors.price" in s.error


def test_404_fails_fast_without_retries(browser, base_url):
    s = scrape(browser, base_url, "nao-existe.html", max_retries=3)
    assert "404" in s.error


def test_unreachable_host_returns_error_instead_of_raising(browser):
    product = {"id": "t", "name": "T", "url": "http://127.0.0.1:1/x", "target_price": 1.0}
    s = scrape_product(browser, product, make_settings(max_retries=1, page_timeout_ms=3000))
    assert s.error and s.price_cash is None


def test_debug_dump_written_on_failure(browser, base_url, tmp_path):
    scrape(browser, base_url, "no_price.html", debug_dir=str(tmp_path / "dbg"))
    assert (tmp_path / "dbg" / "t.png").exists() and (tmp_path / "dbg" / "t.html").exists()
