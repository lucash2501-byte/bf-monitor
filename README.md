# 🔥 Monitor de Preços — Black Friday

Monitora produtos, valida se o desconto é real (vs. média do histórico), confere o estoque da sua
variação/tamanho e avisa no Telegram com os cupons encontrados na página.

## Estrutura

```
.
├── main.py          # loop principal / CLI
├── scraper.py       # Playwright: preço, parcelado, estoque da variação, cupons
├── analyzer.py      # regras de alerta (alvo + estoque + desconto real)
├── notifier.py      # Telegram
├── db.py            # SQLite (histórico de preços e alertas)
├── config.py        # lê o .env
├── products.json    # SUA LISTA de produtos
├── .env.example     # copie para .env
└── requirements.txt
```

## 1. Criar o Bot no Telegram

1. No Telegram, procure **@BotFather** e envie `/newbot`.
2. Escolha um nome e um username (terminando em `bot`).
3. O BotFather devolve o **TOKEN** (ex.: `123456789:AAH...`). Guarde.
4. Abra a conversa com o SEU bot (link que o BotFather mostra) e envie **qualquer mensagem** (ex.: "oi").
5. No navegador, abra (troque `SEU_TOKEN`):
   `https://api.telegram.org/botSEU_TOKEN/getUpdates`
6. Procure `"chat":{"id": 123456789, ...}` — esse número é o seu **CHAT_ID**.
   (Alternativa: fale com **@userinfobot**, que responde o seu ID.)

## 2. Instalação

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env             # Windows: copy .env.example .env
```

Edite o `.env` e preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`.

## 3. Cadastrar produtos (`products.json`)

```json
{
  "name": "Tênis Exemplo Corrida",
  "url": "https://loja.com.br/produto/p",
  "target_price": 299.90,
  "variations": ["41", "Preto"],
  "selectors": { "price": "span.preco-a-vista" }
}
```

- `variations`: todas precisam estar em estoque (tamanho, cor, capacidade...). Use o texto exato do botão.
- `selectors` (opcional, recomendado para sites teimosos): `price`, `installment`, `coupon` (CSS).
  Dica: no Chrome, clique com o direito no preço > Inspecionar > copiar seletor.
- Você pode editar o arquivo com o loop rodando: ele é relido a cada rodada.

## 4. Rodar

```bash
python main.py --test-telegram   # confirma que o bot funciona
python main.py --once            # uma rodada de teste (veja o log)
python main.py                   # loop contínuo (Ctrl+C para parar)
python main.py --stats           # resumo do histórico coletado
```

Rodar em segundo plano: Linux/macOS `nohup python main.py &` ou `tmux`; Windows: Agendador de Tarefas
ou `pythonw main.py`. Logs em `monitor.log`.

## Como o alerta decide

Só dispara se TODAS forem verdadeiras:

1. Preço à vista <= preço alvo.
2. Todas as variações estão confirmadas em estoque (se o botão não for encontrado, o estoque é
   "desconhecido" e **não** alerta).
3. Preço pelo menos `MIN_DISCOUNT_PCT`% abaixo da média dos últimos `HISTORY_DAYS` dias, com no mínimo
   `MIN_HISTORY_SAMPLES` leituras.

⚠️ **Comece a rodar agora!** A Black Friday 2026 é em 27/11. Sem histórico anterior o app não
consegue desmascarar a "metade do dobro" — por isso o alerta só dispara depois de coletar leituras
suficientes (ajustável no `.env`).

## Limitações honestas

- Cada loja tem um HTML diferente; a extração genérica (JSON-LD/meta/heurística) funciona em muitas,
  mas pode exigir `selectors` em outras.
- Sites que exigem clicar na variação para mostrar o estoque, ou que usam captcha, podem exigir ajustes.
- Uso pessoal e com intervalos razoáveis (15–30 min); respeite os termos das lojas.
