"""Cache de cupons por loja e o coupons.json que o bot do Telegram consulta."""
import json

import db
import store_coupons
from conftest import make_settings

STORES = [
    {"name": "Renner", "urls": ["https://www.lojasrenner.com.br/"], "aliases": ["renner"]},
    {"name": "Netshoes", "urls": ["https://www.netshoes.com.br/"]},
    {"name": "Netshoes (via agregadores)", "store": "Netshoes", "label": "Netshoes — via agregador",
     "urls": ["https://www.cuponomia.com.br/desconto/netshoes"]},
]


def test_defaults_do_not_push_coupons_and_discount_is_10(monkeypatch):
    import importlib
    import config
    for k in ("NOTIFY_COUPONS", "MIN_DISCOUNT_PCT"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(config)
    s = config.load_settings()
    assert s.notify_coupons is False and s.min_discount_pct == 10


def test_store_for_url_matches_subdomains_and_unknown_hosts():
    assert store_for_url("https://www.lojasrenner.com.br/p/camisa/-/A-1") == "Renner"
    assert store_for_url("https://clube.netshoes.com.br/p/tenis") == "Netshoes"
    assert store_for_url("https://www.cuponomia.com.br/x") == "www.cuponomia.com.br".removeprefix("www.")
    assert store_for_url("https://www.loja-nova.com/p") == "loja-nova.com"


def store_for_url(url):
    return store_coupons.store_for_url(url, STORES)


def test_export_lists_every_store_with_coupons_status_and_source(tmp_path):
    settings = make_settings(tmp_path)
    db.init_db(settings.db_path)
    db.cache_coupons(settings.db_path, "Renner", ["use o cupom: VOUDEAPP"], "loja")
    db.cache_coupons(settings.db_path, "Renner", ["use o cupom: VOUDEAPP", "BLACK10 — 10% OFF"], "produto")
    db.cache_coupons(settings.db_path, "Netshoes", ["NETS20 — 20% OFF"], "agregador")
    db.set_store_status(settings.db_path, "Renner", None)
    db.set_store_status(settings.db_path, "Netshoes", "HTTP 403")
    out = store_coupons.build_export(settings, STORES)
    by = {s["name"]: s for s in out["stores"]}
    assert set(by) == {"Renner", "Netshoes"}                       # a entrada de agregador não vira loja própria
    assert [c["text"] for c in by["Renner"]["coupons"]].count("use o cupom: VOUDEAPP") == 1   # sem duplicar
    assert by["Netshoes"]["error"] == "HTTP 403" and by["Netshoes"]["coupons"][0]["source"] == "agregador"
    assert "renner" in by["Renner"]["aliases"] and by["Renner"]["url"].startswith("https://www.lojasrenner")
    assert out["generated_at"].endswith("Z")


def test_old_coupons_stop_being_exported(tmp_path):
    settings = make_settings(tmp_path)
    db.init_db(settings.db_path)
    db.cache_coupons(settings.db_path, "Renner", ["ANTIGO10"], "loja")
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE coupon_cache SET last_seen = datetime('now', '-2 days')")
    assert store_coupons.build_export(settings, STORES)["stores"][0]["coupons"] == []


def test_export_json_writes_file_next_to_db(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db.init_db(settings.db_path)
    monkeypatch.setattr(store_coupons, "load_stores", lambda path=None: STORES)
    path = store_coupons.export_json(settings)
    assert path.endswith("coupons.json") and json.load(open(path, encoding="utf-8"))["stores"]


def test_product_page_coupons_go_to_the_store_cache(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db.init_db(settings.db_path)
    monkeypatch.setattr(store_coupons, "load_stores", lambda path=None: STORES)
    store_coupons.cache_product_coupons(settings, {"url": "https://www.lojasrenner.com.br/p/x"}, ["RENNER15"])
    assert db.get_cached_coupons(settings.db_path, 1)[0][:3] == ("Renner", "RENNER15", "produto")
