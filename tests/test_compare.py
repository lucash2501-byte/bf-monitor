"""Busca do mesmo modelo em outras lojas: casamento de nomes (unidade) e fluxo completo com lojas falsas."""
import json

import pytest

import compare
import db
from conftest import make_settings
from test_scraper import base_url, browser  # noqa: F401  (fixtures: servidor local + Chromium)

NAME = "Tênis Nike Pegasus 41 Preto Masculino"
PRODUCT = {"id": "peg", "name": NAME, "url": "https://www.loja-a.com/p", "target_price": 300.0, "variations": ["40"]}


def test_query_uses_model_number_and_color():
    assert compare.build_query(PRODUCT) == "nike pegasus 41 preto"
    assert compare.build_query({"name": "Tênis Asics Gel Nimbus 26", "color": "azul"}) == "asics gel nimbus 26 azul"
    assert compare.build_query({**PRODUCT, "search": "pegasus 41 black"}) == "pegasus 41 black"


@pytest.mark.parametrize("title,href,expected", [
    ("Tênis Nike Pegasus 41 Preto Masculino", "https://l.com/tenis-nike-pegasus-41/p", True),
    ("Tênis Nike Pegasus 40 Preto Masculino", "https://l.com/x/p", False),       # outro número de modelo
    ("Tênis Nike Pegasus 41 Branco Masculino", "https://l.com/x/p", False),      # outra cor
    ("Tênis Infantil Nike Pegasus 41 Preto", "https://l.com/x/p", False),        # infantil
    ("Tênis Nike Pegasus 41 Preto Feminino", "https://l.com/x/p", False),        # outro gênero
    ("Tênis Nike Revolution 7 Preto", "https://l.com/x/p", False),               # outro modelo
    ("Meia Nike Pegasus 41 Preto Masculino", "https://l.com/x/p", False),        # acessório, não o tênis
    ("Camiseta Nike Pegasus 41 Preto Masculino", "https://l.com/x/p", False),    # outra categoria
    ("Nike Pegasus 41", "https://l.com/nike-pegasus-41-black/p", True),          # cor só no link/inglês: aceita
])
def test_score_result_matches_same_model_only(title, href, expected):
    assert (compare.score_result(PRODUCT, title, href) is not None) is expected


def test_generic_names_are_not_compared():
    assert compare.score_result({"name": "Camiseta Preta"}, "Camiseta Preta Básica", "https://l.com/camiseta/p") is None


def test_color_unconfirmed_is_reported_as_none():
    score, color_ok = compare.score_result(PRODUCT, "Tênis Nike Pegasus 41 Masculino", "https://l.com/a-b-c-d/p")
    assert color_ok is None


def test_search_url_templates():
    q = "nike pegasus 41 preto"
    assert compare._search_url({"url": "https://a.com/?q={q}"}, q) == "https://a.com/?q=nike+pegasus+41+preto"
    assert compare._search_url({"url": "https://a.com/{q_path}?_q={q_path}"}, q).startswith("https://a.com/nike%20pegasus")
    assert compare._search_url({"url": "https://a.com/lista/{q_dash}"}, q) == "https://a.com/lista/nike-pegasus-41-preto"


def test_repo_store_list_is_valid():
    stores = compare.load_stores()
    assert len(stores) >= 10 and all("{q" in (s.get("url") or "".join(s["urls"])) for s in stores)


# ------------------------------------------------------------------ com Chromium e lojas falsas
def fake_store(base_url):
    return {"name": "loja-b.com", "url": f"{base_url}/search_results.html?q={{q}}"}


def test_search_store_keeps_only_the_same_model(browser, base_url):  # noqa: F811
    cands, err = compare.search_store(browser, fake_store(base_url), PRODUCT, make_settings())
    assert err is None
    assert [c.url.rsplit("/", 1)[1] for c in cands] == ["jsonld_sizes.html"]
    assert cands[0].color_ok is True


def test_search_store_reports_http_errors(browser, base_url):  # noqa: F811
    cands, err = compare.search_store(browser, {"name": "x", "url": f"{base_url}/nao-existe.html?q={{q}}"},
                                      PRODUCT, make_settings())
    assert cands == [] and err == "HTTP 404"


class Notifier:
    configured = True

    def __init__(self):
        self.sent = []

    def send(self, text, retries=3):
        self.sent.append(text)
        return True


def test_cheaper_elsewhere_alerts_once_and_searches_only_periodically(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    import main
    monkeypatch.setattr(compare, "load_stores", lambda path=None: [fake_store(base_url)])
    settings = make_settings(tmp_path, compare_enabled=True, compare_min_diff_pct=5.0,
                             compare_search_every_hours=6.0, compare_max_searches=4, compare_max_candidates=4)
    db.init_db(settings.db_path)
    # a loja principal está custando R$ 500 → a outra (R$ 399,90) é mais de 5% mais barata
    db.save_snapshot(settings.db_path, PRODUCT["id"], main_snap(500.0))
    notifier = Notifier()

    stats = compare.run_comparisons(browser, [PRODUCT], settings, notifier, main.process_product)
    assert stats == {"searched": 1, "read": 1, "alert": 1}
    msg = notifier.sent[0]
    assert "R$ 399,90" in msg and "loja-b.com" in msg and "Outra loja" in msg and "confira" in msg.lower()

    # segunda rodada: não pesquisa de novo (<6h), relê o candidato, e o cooldown evita alerta repetido
    stats = compare.run_comparisons(browser, [PRODUCT], settings, notifier, main.process_product)
    assert stats == {"searched": 0, "read": 1, "alert": 0} and len(notifier.sent) == 1


def test_not_cheaper_enough_does_not_alert(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    import main
    monkeypatch.setattr(compare, "load_stores", lambda path=None: [fake_store(base_url)])
    settings = make_settings(tmp_path, compare_enabled=True)
    db.init_db(settings.db_path)
    db.save_snapshot(settings.db_path, PRODUCT["id"], main_snap(405.0))   # 399,90 é só ~1% abaixo
    notifier = Notifier()
    stats = compare.run_comparisons(browser, [PRODUCT], settings, notifier, main.process_product)
    assert stats["read"] == 1 and stats["alert"] == 0 and not notifier.sent


def test_compare_false_skips_product(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    import main
    monkeypatch.setattr(compare, "load_stores", lambda path=None: [fake_store(base_url)])
    settings = make_settings(tmp_path, compare_enabled=True)
    db.init_db(settings.db_path)
    stats = compare.run_comparisons(browser, [{**PRODUCT, "compare": False}], settings, Notifier(), main.process_product)
    assert stats == {"searched": 0, "read": 0, "alert": 0}


def test_debug_search_report(browser, base_url, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(compare, "load_stores", lambda path=None: [
        fake_store(base_url), {"name": "morta.com", "url": f"{base_url}/nao-existe.html?q={{q}}"}])
    lines = compare.debug_search(browser, NAME, make_settings())
    text = "\n".join(lines)
    assert "✓ loja-b.com" in text and "✗ morta.com" in text and "HTTP 404" in text


def test_debug_search_explains_empty_results(browser, base_url, monkeypatch):  # noqa: F811
    monkeypatch.setattr(compare, "load_stores", lambda path=None: [fake_store(base_url)])
    text = "\n".join(compare.debug_search(browser, "Nike Air Zoom Vomero 99 Rosa", make_settings()))
    assert "nenhum resultado bate" in text and "a página mostrou" in text and "exemplos:" in text and "Pegasus" in text


def main_snap(price):
    from scraper import Snapshot
    return Snapshot(price_cash=price, in_stock=True)


def test_store_tries_next_link_only_on_404(browser, base_url):  # noqa: F811
    store = {"name": "loja-b.com", "urls": [f"{base_url}/nao-existe.html?q={{q}}", f"{base_url}/search_results.html?q={{q}}"]}
    cands, err = compare.search_store(browser, store, PRODUCT, make_settings())
    assert err is None and len(cands) == 1


def test_image_only_links_use_card_text_as_title():
    card = "Tênis Nike Pegasus 41 Preto\nR$ 399,90"
    assert compare.score_result(PRODUCT, "", "https://l.com/produto/12345/p", card) is not None
    other = "Tênis Nike Pegasus 40 Preto\nR$ 399,90"
    assert compare.score_result(PRODUCT, "", "https://l.com/produto/12345/p", other) is None


def test_search_store_reads_name_from_card_when_link_is_only_an_image(browser, base_url):  # noqa: F811
    store = {"name": "loja-c.com", "url": f"{base_url}/search_image_links.html?q={{q}}"}
    cands, err = compare.search_store(browser, store, PRODUCT, make_settings())
    assert err is None and [c.url.rsplit("/", 2)[1] for c in cands] == ["12345"]
    assert cands[0].title == "Tênis Nike Pegasus 41 Preto Masculino"


def test_other_category_allowed_when_the_product_itself_is_that_category():
    sock = {"name": "Meia Olympikus Corre 4 Preta", "url": "https://l.com/p", "target_price": 10}
    assert compare.score_result(sock, "Meia Corre 4 Olympikus Preta", "https://l.com/meia-corre-4/p") is not None
    shoe = {"name": "Tênis Olympikus Corre 4 Preto", "url": "https://l.com/p", "target_price": 10}
    assert compare.score_result(shoe, "Meia Corre 4 Olympikus", "https://l.com/meia-corre-4/p") is None
