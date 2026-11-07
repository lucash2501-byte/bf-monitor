"""Envio de mensagens pelo Telegram Bot API."""
from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger("notifier")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id and "COLE_AQUI" not in self.token + self.chat_id)

    def send(self, text: str, retries: int = 3) -> bool:
        if not self.configured:
            log.error("Telegram não configurado (.env).")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, json=payload, timeout=20)
                if r.ok:
                    return True
                log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as exc:
                log.warning("Telegram erro de rede: %s", exc)
            time.sleep(2 * attempt)
        return False


def _brl(v) -> str:
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_alert_message(product: dict, snap, decision) -> str:
    e = html.escape
    lines = [f"🔥 <b>{e(product['name'])}</b>"]
    if product.get("variations"):
        lines.append(f"📐 Variação: <b>{e(' / '.join(product['variations']))}</b> — ✅ em estoque")
    lines.append("")
    lines.append(f"💰 À vista: <b>{_brl(snap.price_cash)}</b>  (alvo: {_brl(product['target_price'])})")
    if snap.price_installment:
        lines.append(f"💳 Parcelado: {_brl(snap.price_installment)} ({e(snap.installment_text or '')})")
    if decision.avg_price:
        lines.append(
            f"📊 Média {decision.samples} leituras: {_brl(decision.avg_price)} | "
            f"mínima: {_brl(decision.min_price)}"
        )
    if decision.discount_vs_avg_pct is not None:
        lines.append(f"📉 <b>{decision.discount_vs_avg_pct:.1f}% abaixo da média</b> (desconto real)")
    if decision.suspicious_drop:
        lines.append("⚠️ Queda muito grande: confira se não é erro de leitura (ou erro de preço da loja!)")
    if snap.coupons:
        lines.append("")
        lines.append("🎟️ <b>Cupons/banners na página:</b>")
        for c in snap.coupons:
            lines.append(f"• {e(c)}")
    lines.append("")
    lines.append(f'🔗 <a href="{e(product["url"], quote=True)}">Abrir produto</a>')
    return "\n".join(lines)
