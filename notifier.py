# Trecho a colocar no loop de verificação do main.py:
for product in products:
    snapshot = scrape_product(browser, product, settings)
    
    if snapshot and snapshot.price_cash:
        # Busca o menor preço gravado até hoje no banco de dados SQLite
        min_price_history = db.get_min_price(product["id"])
        
        should_alert = False
        discount_pct = 0.0

        # Regra 1: Notificar se cair 10% ou mais em relação ao histórico
        if min_price_history and min_price_history > 0:
            discount_pct = ((min_price_history - snapshot.price_cash) / min_price_history) * 100
            if discount_pct >= 10.0:
                should_alert = True

        # Regra 2: Notificar se estiver igual ou abaixo do preço-alvo cadastrado
        if snapshot.price_cash <= product.get("target_price", 0):
            should_alert = True

        # Envia o alerta se cumpriu alguma das regras acima
        if should_alert:
            send_telegram_alert(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                snapshot,
                product,
                discount_pct=discount_pct
            )
