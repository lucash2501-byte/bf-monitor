from datetime import date

from coupons import Coupon, dedupe, extract_coupons, extract_from_json_text

TODAY = date(2026, 11, 7)


def codes(cs):
    return sorted(c.code for c in cs)


def test_valid_expired_inactive_and_alt_keys():
    data = {"coupons": [
        {"code": "BLACK10", "description": "10% OFF", "validUntil": "2099-12-31"},
        {"code": "VELHO20", "validUntil": "2020-01-01"},
        {"code": "INATIVO1", "active": False},
        {"code": "CANCEL01", "status": "EXPIRED"},
        {"couponCode": "FRETE15", "percentage": 15},
    ]}
    result = {c.code: c for c in extract_coupons(data, today=TODAY)}
    assert sorted(result) == ["BLACK10", "FRETE15"]
    assert result["FRETE15"].description == "15% de desconto"
    assert result["BLACK10"].valid_until == date(2099, 12, 31)


def test_generic_code_needs_coupon_context():
    """{"code": "BRASIL"} de país não é cupom; dentro de 'coupons' é."""
    assert extract_coupons({"country": {"code": "BRASIL"}}, today=TODAY) == []
    assert extract_coupons({"user": {"codigo": "XYZ12345"}}, today=TODAY) == []
    assert codes(extract_coupons({"cupons": [{"codigo": "BEMVINDO"}]}, today=TODAY)) == ["BEMVINDO"]


def test_plain_string_lists_and_invalid_codes():
    data = {"vouchers": ["PIX5OFF", "ab", "null", "1234567", "tem espaço"]}
    assert codes(extract_coupons(data, today=TODAY)) == ["PIX5OFF"]  # curto, palavra reservada, só número e com espaço: fora


def test_date_formats_and_formatting():
    c = extract_coupons({"coupons": [{"code": "NATAL25", "title": "Natal", "validade": "25/12/2099"}]},
                        today=TODAY)[0]
    assert c.valid_until == date(2099, 12, 25)
    assert c.format() == "NATAL25 — Natal (válido até 25/12/2099)"
    assert Coupon(code="ABCD").format() == "ABCD"


def test_dedupe_merges_case_insensitively_keeping_most_complete():
    merged = dedupe([Coupon("black10"), Coupon("BLACK10", "10% OFF", date(2099, 1, 1))])
    assert len(merged) == 1 and merged[0].description == "10% OFF" and merged[0].valid_until


def test_garbage_input_is_safe():
    assert extract_from_json_text("{não é json") == []
    assert extract_from_json_text("") == []
    assert extract_coupons(None) == [] and extract_coupons([1, "x", None]) == []
    deep = current = {}
    for _ in range(50):
        current["coupons"] = {}
        current = current["coupons"]
    assert extract_coupons(deep) == []  # profundidade limitada, sem estourar a pilha
