"""Envio de mensagens pelo Telegram Bot API."""
from __future__ import annotations

import html
import logging
import re
import time

import requests

log = logging.getLogger("notifier")

MAX_LEN = 4096


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id and "COLE_AQUI" not in self.token + self.chat_id)

    def _scrub(self, value) -> str:
        """Remove o token de qualquer texto que vá para o log (as exceções do requests trazem a URL)."""
        text = str(value)
        return text.replace(self.token, "***") if self.token else text

    def send(self, text: str, retries: int = 3) -> bool:
        if not self.configured:
            log.error("Telegram não configurado (.env).")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        plain_fallback_used = False
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, json=payload, timeout=20)
            except requests.RequestException as exc:
                log.warning("Telegram erro de rede: %s", self._scrub(exc))
            else:
                if r.ok:
                    return True
                log.warning("Telegram HTTP %s: %s", r.status_code, self._scrub(r.text[:200]))
                if r.status_code == 429:  # rate limit: respeita o tempo pedido pelo Telegram
                    time.sleep(min(self._retry_after(r), 60))
                    continue
                if r.status_code == 400 and "parse" in r.text.lower() and not plain_fallback_used:
                    # HTML inválido (ex.: mensagem cortada no meio de uma tag): reenvia como texto puro
                    plain_fallback_used = True
                    payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))[:MAX_LEN]
                    payload.pop("parse_mode", None)
                    continue
                if 400 <= r.status_code < 500:
                    log.error("Telegram recusou a mensagem: verifique TOKEN e CHAT_ID "
                              "(e se você já mandou /start para o bot).")
                    return False
            if attempt < retries:
                time.sleep(2 * attempt)
        return False

    @staticmethod
    def _retry_after(response) -> float:
        try:
            return float(response.json()["parameters"]["retry_after"])
        except Exception:  # noqa: BLE001
            return 5.0


def _brl(v) -> str:
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_span(hours: float) -> str:
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.1f} dias"


def build_alert_message(product: dict, snap, decision) -> str:
    e = html.escape
    lines = [f"🔥 <b>{e(product['name'])}</b>"]
    if product.get("variations"):
        stock = "❓ estoque não confirmado" if decision.stock_unknown else "✅ em estoque"
        lines.append(f"📐 Variação: <b>{e(' / '.join(product['variations']))}</b> — {stock}")
    lines.append("")
    lines.append(f"💰 À vista: <b>{_brl(snap.price_cash)}</b>  (alvo: {_brl(product['target_price'])})")
    if snap.price_installment:
        lines.append(f"💳 Parcelado: {_brl(snap.price_installment)} ({e(snap.installment_text or '')})")
    if decision.ref_price:
        lines.append(
            f"📊 Histórico ({decision.samples} leituras em {_fmt_span(decision.span_hours)}): "
            f"referência {_brl(decision.ref_price)} | mínima: {_brl(decision.min_price)}"
        )
    if decision.discount_vs_ref_pct is not None:
        lines.append(f"📉 <b>{decision.discount_vs_ref_pct:.1f}% abaixo da referência</b> (desconto real)")
    for w in decision.warnings:
        lines.append(f"⚠️ {e(w)}")
    if snap.coupons:
        lines.append("")
        lines.append("🎟️ <b>Cupons/banners na página:</b>")
        for c in snap.coupons:
            lines.append(f"• {e(c)}")
    lines.append("")
    lines.append(f'🔗 <a href="{e(product["url"], quote=True)}">Abrir produto</a>')
    return "\n".join(lines)
