"""Regras de decisão: quando vale a pena alertar.

A ideia central é desmascarar a "metade do dobro": o desconto só é considerado real se o preço
atual estiver abaixo de uma REFERÊNCIA construída com o histórico que o próprio monitor coletou.

Referência = o MENOR entre
  - a mediana de todas as leituras da janela (robusta a leituras erradas pontuais), e
  - a mediana da metade mais antiga da janela (pega o caso em que a loja infla o preço dias
    antes da Black Friday e depois "desconta" de volta ao nível normal).
Isso é de propósito conservador: exige que o desconto seja real contra o preço mais baixo
que a loja praticava antes da promoção.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

PUMP_THRESHOLD = 0.10      # preço recente > 10% acima do período anterior = suspeita de inflação
SUSPICIOUS_RATIO = 0.30    # preço < 30% da referência = queda absurda (erro de leitura ou erro da loja)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class Decision:
    alert: bool
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ref_price: Optional[float] = None       # referência usada para medir o desconto
    median_price: Optional[float] = None
    min_price: Optional[float] = None
    samples: int = 0
    span_hours: float = 0.0
    discount_vs_ref_pct: Optional[float] = None
    suspicious_drop: bool = False           # queda enorme: pode ser erro de leitura (ou erro de preço da loja!)
    pump_detected: bool = False             # preço subiu antes do "desconto"
    stock_unknown: bool = False


def _span_hours(history: list[tuple[str, float]]) -> float:
    if len(history) < 2:
        return 0.0
    try:
        first = datetime.strptime(history[0][0], _TS_FORMAT).replace(tzinfo=timezone.utc)
        last = datetime.strptime(history[-1][0], _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return max((last - first).total_seconds() / 3600, 0.0)


def evaluate(product: dict, snap, history: list[tuple[str, float]], settings) -> Decision:
    """`history`: lista de (timestamp UTC 'YYYY-MM-DD HH:MM:SS', preço), da mais antiga para a mais nova."""
    d = Decision(alert=False)
    price = snap.price_cash
    if price is not None and price <= 0:
        price = None  # leitura inválida
    target = float(product["target_price"])

    # a) preço alvo
    if price is not None and price <= target:
        d.passed.append(f"Preço R$ {price:.2f} <= alvo R$ {target:.2f}")
    else:
        d.failed.append(f"Preço acima do alvo (alvo R$ {target:.2f})")

    # b) estoque da variação
    if snap.in_stock is True:
        d.passed.append("Variação em estoque")
    elif snap.in_stock is False:
        d.failed.append("Variação sem estoque")
    elif settings.alert_on_unknown_stock:
        d.stock_unknown = True
        d.passed.append("Estoque não confirmado (permitido por configuração)")
        d.warnings.append("Não consegui confirmar o estoque da variação: confira o tamanho/cor na página.")
    else:
        d.failed.append("Estoque não confirmado (botão da variação não encontrado)")

    # c) desconto genuíno vs. histórico
    prices = [p for _, p in history]
    d.samples = len(prices)
    if prices:
        d.median_price = statistics.median(prices)
        d.min_price = min(prices)
        d.span_hours = _span_hours(history)
        d.ref_price = d.median_price
        half = len(prices) // 2
        if half >= 3:
            median_old = statistics.median(prices[:half])
            median_recent = statistics.median(prices[half:])
            if median_recent > median_old * (1 + PUMP_THRESHOLD):
                d.pump_detected = True
            d.ref_price = min(d.median_price, median_old)

    enough = (
        d.samples >= settings.min_history_samples
        and d.span_hours >= settings.min_history_span_hours
        and d.ref_price
    )
    if enough and price is not None:
        threshold = d.ref_price * (1 - settings.min_discount_pct / 100)
        d.discount_vs_ref_pct = (1 - price / d.ref_price) * 100
        if price <= threshold:
            d.passed.append(
                f"{d.discount_vs_ref_pct:.1f}% abaixo da referência dos últimos {settings.history_days} dias "
                f"(R$ {d.ref_price:.2f})"
            )
        else:
            d.failed.append(
                f"Desconto não é real: só {d.discount_vs_ref_pct:.1f}% vs. referência "
                f"R$ {d.ref_price:.2f} (exigido {settings.min_discount_pct:.0f}%)"
            )
        if price < d.ref_price * SUSPICIOUS_RATIO:
            d.suspicious_drop = True
            d.warnings.append("Queda muito grande: confira se não é erro de leitura (ou erro de preço da loja!).")
        if d.pump_detected:
            d.warnings.append(
                "O preço subiu antes dessa queda (possível inflação pré-Black Friday); "
                "o desconto foi medido contra o preço mais baixo anterior."
            )
    elif settings.allow_alert_without_history:
        d.passed.append(f"Histórico curto ({d.samples} leituras) — permitido por configuração")
        d.warnings.append("Histórico curto: não dá para garantir que o desconto é real.")
    else:
        d.failed.append(
            f"Histórico insuficiente ({d.samples}/{settings.min_history_samples} leituras, "
            f"{d.span_hours:.0f}h/{settings.min_history_span_hours:.0f}h)"
        )

    d.alert = not d.failed
    return d
