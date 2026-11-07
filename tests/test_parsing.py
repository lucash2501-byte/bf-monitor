import json

import pytest

from scraper import parse_installment, parse_jsonld, parse_price


@pytest.mark.parametrize("text,expected", [
    ("R$ 1.299,90", 1299.90),
    ("R$ 99,90", 99.90),
    ("1299,90", 1299.90),
    ("299.90", 299.90),
    ("1,299.90", 1299.90),            # formato US vindo de JSON-LD
    ("R$ 99", 99.0),                  # sem centavos
    ("R$ 1.299", 1299.0),
    ("de R$ 599,90 por R$ 349,90", 349.90),
    ("R$\n 349,90", 349.90),
    (399.9, 399.9),
    (350, 350.0),
    ("", None),
    (None, None),
    ("sem preço", None),
    (0, None),
    (True, None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_parse_installment():
    total, text = parse_installment("Em 10x de R$ 39,99 sem juros")
    assert total == 399.90
    assert text == "10x de R$ 39,99"
    assert parse_installment("sem parcelamento") == (None, None)


def jsonld(obj) -> list[str]:
    return [json.dumps(obj)]


def test_jsonld_simple_in_stock():
    price, stock, title = parse_jsonld(jsonld({
        "@type": "Product", "name": "X",
        "offers": {"price": "399.90", "availability": "https://schema.org/InStock"}}))
    assert (price, stock, title) == (399.90, True, "X")


def test_jsonld_out_of_stock_is_not_confused_with_in_stock():
    _, stock, _ = parse_jsonld(jsonld({
        "@type": "Product", "offers": {"price": 10, "availability": "https://schema.org/OutOfStock"}}))
    assert stock is False


def test_jsonld_graph_and_offer_list_takes_lowest_price():
    price, stock, _ = parse_jsonld(jsonld({"@graph": [
        {"@type": "WebSite"},
        {"@type": "Product", "name": "Y", "offers": [
            {"price": "120.00", "availability": "OutOfStock"},
            {"price": "100.00", "availability": "InStock"}]}]}))
    assert price == 100.0 and stock is True


def test_jsonld_aggregate_offer_and_price_specification():
    price, _, _ = parse_jsonld(jsonld({"@type": "Product", "offers": {
        "@type": "AggregateOffer", "lowPrice": "89.90", "offers": [{"price": "95.00"}]}}))
    assert price == 89.90
    price, _, _ = parse_jsonld(jsonld({"@type": "Product", "offers": {"priceSpecification": [
        {"price": "500.00", "priceType": "https://schema.org/ListPrice"},
        {"price": "350.00"}]}}))
    assert price == 350.0


def test_jsonld_ignores_garbage_and_non_products():
    assert parse_jsonld(["{not json", json.dumps({"@type": "Organization"})]) == (None, None, None)
