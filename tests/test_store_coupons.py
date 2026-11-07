"""Cupons por loja (stores.json): leitura da página inicial, aviso só do que é novo, lojas com erro."""
import db
import main
import store_coupons
from conftest import make_settings
from test_scraper import base_url, browser  # noqa: F401


class Notifier:
    configured = True

    def __init__(self):
        self.sent = []

    def send(self, text, retries=3):
        self.sent.append(text)
        return True


def stores(base_url):
    return [{"name": "Loja Boa", "urls": [f"{base_url}/jsonld_sizes.html"]},
            {"name": "Loja Morta", "urls": [f"{base_url}/nao-existe.html"]}]


def test_repo_stores_file_is_valid():
    st = store_coupons.load_stores()
    assert len(st) >= 20 and all(s["urls"] for s in st)


def test_run_sends_new_store_coupons_once_and_checks_periodically(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(store_coupons, "load_stores", lambda path=None: stores(base_url))
    settings = make_settings(tmp_path, store_coupons_enabled=True, store_coupons_every_hours=2.0)
    db.init_db(settings.db_path)
    n = Notifier()
    stats = store_coupons.run_store_coupons(browser, settings, n, main.send_coupon_digest)
    assert stats == {"checked": 2, "errors": 1, "sent": 1}
    assert len(n.sent) == 1 and "BLACK10" in n.sent[0] and "Loja Boa" in n.sent[0]
    # logo depois: nada vencido, não confere de novo
    assert store_coupons.run_store_coupons(browser, settings, n, main.send_coupon_digest) == {"checked": 0, "errors": 0, "sent": 0}
    # vencido, mas o cupom já foi avisado: confere e não repete
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE store_checks SET ts = datetime('now', '-3 hours')")
    stats = store_coupons.run_store_coupons(browser, settings, n, main.send_coupon_digest)
    assert stats["checked"] == 2 and stats["sent"] == 0 and len(n.sent) == 1


def test_disabled_does_nothing(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(store_coupons, "load_stores", lambda path=None: stores(base_url))
    settings = make_settings(tmp_path, store_coupons_enabled=False)
    db.init_db(settings.db_path)
    assert store_coupons.run_store_coupons(browser, settings, Notifier(), main.send_coupon_digest)["checked"] == 0


def test_debug_report(browser, base_url, monkeypatch):  # noqa: F811
    monkeypatch.setattr(store_coupons, "load_stores", lambda path=None: stores(base_url))
    text = "\n".join(store_coupons.debug_stores(browser, make_settings()))
    assert "✓ Loja Boa" in text and "BLACK10" in text and "✗ Loja Morta" in text and "HTTP 404" in text


def test_second_page_of_a_store_is_merged_and_first_failure_tolerated(browser, base_url):  # noqa: F811
    store = {"name": "X", "urls": [f"{base_url}/nao-existe.html", f"{base_url}/jsonld_sizes.html"]}
    coupons, err, url = store_coupons.read_store(browser, store, make_settings())
    assert err is None and any("BLACK10" in c for c in coupons) and url.endswith("jsonld_sizes.html")


def test_aggregator_entry_uses_its_own_label_and_does_not_mix_with_the_store(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    entries = [{"name": "Loja (via agregadores)", "label": "Loja — via agregador", "urls": [f"{base_url}/jsonld_sizes.html"]}]
    monkeypatch.setattr(store_coupons, "load_stores", lambda path=None: entries)
    settings = make_settings(tmp_path, store_coupons_enabled=True)
    db.init_db(settings.db_path)
    n = Notifier()
    store_coupons.run_store_coupons(browser, settings, n, main.send_coupon_digest)
    assert "Cupons — Loja — via agregador" in n.sent[0]


def test_loader_keeps_label(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('[{"name":"A","label":"A — via x","urls":["https://a.com/"]},{"name":"B","url":"https://b.com/"}]')
    got = store_coupons.load_stores(str(f))
    assert got[0]["label"] == "A — via x" and "label" not in got[1]


def test_debug_report_explains_empty_pages(browser, base_url, monkeypatch):  # noqa: F811
    monkeypatch.setattr(store_coupons, "load_stores",
                        lambda path=None: [{"name": "Sem cupom", "urls": [f"{base_url}/no_price.html"]}])
    text = "\n".join(store_coupons.debug_stores(browser, make_settings()))
    assert "nenhum cupom publicado" in text and "↳" in text and "título:" in text and "texto:" in text


def test_aggregator_keeps_only_lines_with_a_real_code():
    keep = ["Use o cupom PRIMEIRA10 e ganhe 10% OFF", "cupom: VOUDEAPP", "35% OFF - Cupom 11DOZE5"]
    drop = ["Cupom Mercado Livre oferece 10% OFF", "Use cupom Mercado Livre e tenha desconto de 10%",
            "Código promocional Netshoes concede 10% OFF", "Cupom de desconto Centauro concede 5% OFF"]
    assert all(store_coupons.has_real_code(c) for c in keep)
    assert not any(store_coupons.has_real_code(c) for c in drop)


def test_aggregator_403_does_not_stop_the_next_site(browser, base_url, monkeypatch):  # noqa: F811
    store = {"name": "X (via agregadores)", "label": "X — via agregador",
             "urls": [f"{base_url}/nao-existe.html", f"{base_url}/jsonld_sizes.html"]}
    monkeypatch.setattr(store_coupons, "scrape_store_coupons",
                        lambda b, u, s, d=None: ([], "HTTP 403") if "nao-existe" in u else (["use o cupom BLACK10 e ganhe 10%"], None))
    coupons, err, _ = store_coupons.read_store(browser, store, make_settings())
    assert err is None and coupons == ["use o cupom BLACK10 e ganhe 10%"]
