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
    target = product.get("target_price", 0.0)

    # Destaque de Desconto no Cabeçalho
    header_tag = "🚨 *OFERTA DETECTADA!* 🚨"
    discount_tag = ""
    if discount_pct >= 10.0:
        header_tag = f"🚨 *QUEDA DE PREÇO DETECTADA! (-{discount_pct:.1f}% OFF)* 🚨"
        discount_tag = f"\n📉 *Queda:* {discount_pct:.1f}% abaixo do menor preço registrado!"

    # Formatação de Cupons
    coupons_text = ""
    if coupons:
        coupons_formatted = "\n".join([f"• 🎟️ `{c}`" for c in coupons])
        coupons_text = f"\n\n*Cupons / Promoções Encontradas:*\n{coupons_formatted}"

    # Formatação de Parcelamento
    installment_info = ""
    if price_inst and inst_text:
        installment_info = f"\n💳 *Parcelado:* {inst_text} (Total: R$ {price_inst:.2f})".replace(".", ",")

    message = (
        f"{header_tag}\n\n"
        f"📦 *{title}*\n"
        f"🏪 *Loja:* {product.get('name')}\n"
        f"💰 *À vista:* R$ {price_cash:.2f}".replace(".", ",") + f"{installment_info}"
        f"{discount_tag}\n"
        f"🎯 *Preço Alvo:* R$ {target:.2f}".replace(".", ",") +
        f"{coupons_text}\n\n"
        f"🔗 [Ir para a Oferta]({url})"
    )

    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }

    try:
        resp = requests.post(telegram_url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Alerta enviado com sucesso para %s", product.get("name"))
            return True
        log.error("Falha ao enviar mensagem no Telegram: %s", resp.text)
    except Exception as exc:
        log.error("Erro ao conectar com a API do Telegram: %s", exc)

    return False
