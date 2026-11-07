"""Extração de cupons a partir de JSON que a própria loja entrega à página.

Muitas lojas carregam promoções/cupons em chamadas internas (XHR/fetch) ou no estado embutido da
página (ex.: __NEXT_DATA__). Ler isso é bem mais confiável do que caçar texto na tela: vem com código,
descrição e validade. Só lemos o que a loja PUBLICA; não tentamos adivinhar códigos.

Para evitar falsos positivos (ex.: {"code": "BR"} de país), um campo genérico "code"/"codigo" só vale se
algum ancestral do objeto tiver nome ligado a cupom/promo/voucher.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

HINT_RE = re.compile(r"(cupom|cupon|coupon|voucher|promo)", re.I)
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,23}[A-Za-z0-9]$")  # 4 a 25 caracteres
STOP_CODES = {"null", "none", "true", "false", "undefined", "coupon", "cupom", "voucher", "promo", "code"}

SPECIFIC_CODE_KEYS = {
    "couponcode", "cupomcode", "codigocupom", "codigocupon", "vouchercode", "promocode",
    "discountcode", "codigopromocional", "cupom", "coupon", "voucher",
}
GENERIC_CODE_KEYS = {"code", "codigo"}
DESC_KEYS = ("description", "descricao", "title", "titulo", "name", "nome", "label", "message", "mensagem")
PERCENT_KEYS = {"percentage", "percent", "porcentagem", "discountpercentage", "discountpercent"}
VALUE_KEYS = {"discountvalue", "valordesconto"}
VALID_KEYS = {"validuntil", "expiresat", "expirationdate", "expiration", "enddate", "datafim",
              "validade", "expiry", "expirydate", "validto", "dateend"}
ACTIVE_KEYS = {"active", "isactive", "enabled", "ativo"}
BAD_STATUS_RE = re.compile(r"(expir|inativ|inactive|disabled|cancel)", re.I)


@dataclass
class Coupon:
    code: str
    description: Optional[str] = None
    valid_until: Optional[date] = None
    source: str = ""

    def format(self) -> str:
        text = self.code
        if self.description:
            text += f" — {self.description}"
        if self.valid_until:
            text += f" (válido até {self.valid_until.strftime('%d/%m/%Y')})"
        return text[:180]


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _valid_code(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not CODE_RE.match(code) or code.lower() in STOP_CODES or not re.search(r"[A-Za-z]", code):
        return None
    return code


def _parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", value)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})", value)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def _coupon_from_dict(d: dict, hinted: bool, today: date, source: str) -> Optional[Coupon]:
    code = None
    for k, v in d.items():
        nk = _norm(k)
        if nk in SPECIFIC_CODE_KEYS or (hinted and nk in GENERIC_CODE_KEYS):
            code = _valid_code(v)
            if code:
                break
    if not code:
        return None

    description = percent = value = valid_until = None
    for k, v in d.items():
        nk = _norm(k)
        if nk in ACTIVE_KEYS and v is False:
            return None
        if nk == "status" and isinstance(v, str) and BAD_STATUS_RE.search(v):
            return None
        if nk in VALID_KEYS:
            valid_until = _parse_date(v) or valid_until
        elif nk in PERCENT_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            percent = v
        elif nk in VALUE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            value = v
    if valid_until and valid_until < today:
        return None  # vencido
    for key in DESC_KEYS:
        for k, v in d.items():
            if _norm(k) == key and isinstance(v, str) and 3 <= len(v.strip()) <= 120 and v.strip() != code:
                description = " ".join(v.split())
                break
        if description:
            break
    if not description:
        if percent:
            description = f"{percent:g}% de desconto"
        elif value:
            description = f"R$ {value:.2f} de desconto".replace(".", ",")
    return Coupon(code=code, description=description, valid_until=valid_until, source=source)


def extract_coupons(data: Any, source: str = "", today: Optional[date] = None) -> list[Coupon]:
    """Percorre um JSON já carregado e devolve os cupons válidos (sem repetição, vencidos/inativos fora)."""
    today = today or datetime.now(timezone.utc).date()
    found: dict[str, Coupon] = {}

    def add(c: Optional[Coupon]) -> None:
        if not c:
            return
        old = found.get(c.code.upper())
        if old is None:
            found[c.code.upper()] = c
        else:  # mantém a versão mais completa
            old.description = old.description or c.description
            old.valid_until = old.valid_until or c.valid_until

    def walk(obj: Any, hinted: bool, depth: int) -> None:
        if depth > 12:
            return
        if isinstance(obj, dict):
            add(_coupon_from_dict(obj, hinted, today, source))
            for k, v in obj.items():
                h = hinted or bool(HINT_RE.search(str(k)))
                if h and isinstance(v, list):  # "coupons": ["A", "B"]
                    for item in v:
                        code = _valid_code(item)
                        if code:
                            add(Coupon(code=code, source=source))
                walk(v, h, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, hinted, depth + 1)

    walk(data, False, 0)
    return list(found.values())


def extract_from_json_text(text: str, source: str = "", today: Optional[date] = None) -> list[Coupon]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    return extract_coupons(data, source, today)


def dedupe(coupons: list[Coupon]) -> list[Coupon]:
    seen: dict[str, Coupon] = {}
    for c in coupons:
        old = seen.get(c.code.upper())
        if old is None:
            seen[c.code.upper()] = c
        else:
            old.description = old.description or c.description
            old.valid_until = old.valid_until or c.valid_until
    return list(seen.values())
