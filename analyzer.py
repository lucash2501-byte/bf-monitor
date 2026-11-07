"""Regras de decisão: quando vale a pena alertar."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Decision:
    alert: bool
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    avg_price: Optional[float] = None
    min_price: Optional[float] = None
    samples: int = 0
    discount_vs_avg_pct: Optional[float] = None
    suspicious_drop: bool = False  # queda enorme: pode ser erro de leitura (ou erro de preço da loja!)


def evaluate(product: dict, snap, history: list[float], settings) -> Decision:
    d = Decision(alert=False)
    price = snap.price_cash
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
    else:
        d.failed.append("Estoque não confirmado (botão da variação não encontrado)")

    # c) desconto genuíno vs. média histórica
    d.samples = len(history)
    if history:
        d.avg_price = statistics.mean(history)
        d.min_price = min(history)

    if d.samples >= settings.min_history_samples and d.avg_price and price is not None:
        threshold = d.avg_price * (1 - settings.min_discount_pct / 100)
        d.discount_vs_avg_pct = (1 - price / d.avg_price) * 100
        if price <= threshold:
            d.passed.append(
                f"{d.discount_vs_avg_pct:.1f}% abaixo da média de {settings.history_days} dias "
                f"(R$ {d.avg_price:.2f})"
            )
        else:
            d.failed.append(
                f"Desconto não é real: só {d.discount_vs_avg_pct:.1f}% vs. média "
                f"(exigido {settings.min_discount_pct:.0f}%)"
            )
        if price < d.avg_price * 0.3:
            d.suspicious_drop = True
    elif settings.allow_alert_without_history:
        d.passed.append(f"Histórico curto ({d.samples} leituras) — permitido por configuração")
    else:
        d.failed.append(
            f"Histórico insuficiente ({d.samples}/{settings.min_history_samples} leituras)"
        )

    d.alert = not d.failed
    return d
