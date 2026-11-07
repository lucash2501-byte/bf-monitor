"""Módulo de notificações via Telegram com suporte a destaque de percentagem de desconto."""

import logging
import requests

log = logging.getLogger("notifier")


def send_telegram_alert(bot_token: str, chat_id: str, snapshot, product: dict, discount_pct: float = 0.0):
    """Envia alerta formatado para o Telegram informando o preço e o desconto percentual."""
    if not bot_token or not chat_id:
        log.warning("Telegram token ou chat_id não configurados.")
        return False

    title = getattr(snapshot, 'title', None) or product.get("name")
    price_cash = getattr(snapshot, 'price_cash', 0.0)
    price_inst = getattr(snapshot, 'price_installment', None)
    inst_text = getattr(snapshot, 'installment_text', None)
    coupons = getattr(snapshot, 'coupons', [])
    url = product.get("url")
