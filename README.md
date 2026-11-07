# 🔥 Monitor de Preços — Black Friday

Monitora produtos, valida se o desconto é **real** (vs. o histórico que o próprio monitor coletou),
confere o estoque da sua variação/tamanho e avisa no Telegram com os cupons encontrados na página.

## Estrutura

```
.
├── main.py          # loop principal / CLI
├── scraper.py       # Playwright: preço, parcelado, estoque da variação, cupons
├── coupons.py       # lê cupons publicados pela loja (API interna / estado da página)
├── analyzer.py      # regras de alerta (alvo + estoque + desconto real)
├── notifier.py      # Telegram
├── db.py            # SQLite (histórico de preços e alertas)
├── config.py        # lê o .env
├── products.json    # SUA LISTA de produtos
├── .env.example     # copie para .env
├── deploy/          # serviço systemd (VPS) e script .bat (Windows)
├── tests/           # testes automáticos (lojas falsas locais + Chromium real)
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
  "selectors": {
    "price": "span.preco-a-vista",
    "variation": "button[data-size='{v}']"
  }
}
```

- `target_price`: só alerta se o preço à vista ficar nesse valor ou menos. Aceita `299.9` ou `"R$ 299,90"`.
- `variations`: todas precisam estar em estoque (tamanho, cor, capacidade...). Use o texto exato do botão.
- `selectors` (opcional, recomendado para sites teimosos), todos em CSS:
  - `price`: o preço à vista. `installment`: o parcelamento. `coupon`: banner/área de cupom.
  - `variation`: como achar o botão de cada variação; `{v}` vira o valor de `variations`
    (ex.: `"button[data-size='{v}']"`). Use quando o monitor não encontrar o botão sozinho.
  - Dica: no Chrome, clique com o direito no preço > Inspecionar > copiar seletor.
- Você pode editar o arquivo com o loop rodando: ele é relido a cada rodada. Se algum item estiver
  inválido (preço não numérico, URL sem `http`, id repetido...), ele é ignorado com uma mensagem clara no log.

## 4. Rodar

```bash
python main.py --test-telegram        # confirma que o bot funciona
python main.py --once                 # uma rodada de teste (veja o log)
python main.py --once --only mizuno   # só os produtos cujo id/nome contém "mizuno" (ótimo p/ ajustar seletores)
python main.py                        # loop contínuo (Ctrl+C para parar)
python main.py --stats                # resumo do histórico coletado (e erros nas últimas 24h)
```

Rodar em segundo plano: Linux/macOS `nohup python main.py &` ou `tmux`; Windows: Agendador de Tarefas
ou `pythonw main.py`. Logs em `monitor.log` (rotativo, não cresce sem limite).

**Produto que não lê?** Rode `--once --only <nome>` com `DEBUG_DIR=debug` no `.env`: o monitor salva
`debug/<id>.png` e `.html` da página que falhou, e você descobre o seletor certo. Além disso, se um produto
falhar `FAIL_NOTICE_AFTER` leituras seguidas, o Telegram avisa (uma mensagem só, agrupando os produtos).

## Como o alerta decide

Só dispara se TODAS forem verdadeiras:

1. Preço à vista <= preço alvo.
2. Variação em estoque. Se o botão da variação não for encontrado, o estoque fica "desconhecido": por
   padrão (`ALERT_ON_UNKNOWN_STOCK=true`) o alerta vai **com aviso** "estoque não confirmado"; ponha `false`
   para exigir confirmação.
3. **Desconto real**: preço pelo menos `MIN_DISCOUNT_PCT`% abaixo do preço de **referência**, calculado
   sobre os últimos `HISTORY_DAYS` dias, exigindo `MIN_HISTORY_SAMPLES` leituras espalhadas por pelo menos
   `MIN_HISTORY_SPAN_HOURS` horas.
4. Passa na **confirmação**: o monitor lê a página de novo e só alerta se o preço e as regras acima se mantêm
   (evita alerta por leitura errada). Desligue com `CONFIRM_ALERTS=false`.

**O preço de referência** é o *menor* entre a mediana de toda a janela e a mediana da metade mais antiga dela.
Isso pega a "metade do dobro": se a loja deixou o produto a R$ 200, subiu para R$ 400 e agora "desconta" para
R$ 300, o desconto é medido contra os R$ 200 e não é considerado real (o alerta avisa quando detecta essa subida).
A mediana (em vez da média) também protege de uma leitura errada isolada.

Depois de alertar, o mesmo produto só alerta de novo após `ALERT_COOLDOWN_HOURS` — a menos que o preço caia mais
de 1% desde o último alerta.

⚠️ **Comece a rodar agora!** A Black Friday 2026 é em 27/11. Sem histórico anterior o app não consegue
desmascarar a "metade do dobro" — por isso o alerta só dispara depois de coletar leituras suficientes.

## Cupons

O alerta lista até 6 cupons encontrados na página do produto, nesta ordem de confiança:

1. **Dados estruturados da loja**: respostas JSON (XHR/fetch) que a própria página recebe e o estado
   embutido (`__NEXT_DATA__`, `__INITIAL_STATE__`...). Trazem código, descrição e validade. Cupons vencidos
   ou inativos são descartados, e um campo genérico `code` só conta dentro de algo ligado a cupom/promo,
   para não confundir com código de país ou de tracking.
2. **Seletor `selectors.coupon`**, se você configurou.
3. **Texto da página** (linhas com "cupom" + percentual, valor ou código; links de menu são ignorados).

**Aviso no Telegram:** além de aparecerem nos alertas de preço, os cupons **novos** de cada loja são enviados numa
mensagem própria (uma por loja, por rodada, até 8 cupons), sem repetir o mesmo cupom. Ele só reaparece depois de
`COUPON_RENOTIFY_HOURS` (padrão 168h = 7 dias; `0` = nunca repete). Para desligar, `NOTIFY_COUPONS=false`.
Para mandar para um **canal** em vez da conversa privada: adicione o bot como administrador do canal e use como
`TELEGRAM_CHAT_ID` o id do canal (começa com `-100`).

O monitor só lê o que a loja **publica**. Ele não adivinha códigos nem testa cupons no carrinho. Cupons de
e-mail, de primeira compra ou ligados à sua conta só aparecem se a loja os mostrar na página. Trate a lista como
sugestão: confirme no carrinho antes de contar com o desconto.

## Rodar online (GitHub Actions) — sem instalar nada no seu computador

O workflow `.github/workflows/monitor.yml` roda o monitor sozinho nos servidores do GitHub, a cada ~30 minutos
(e na hora, pelo botão "Run workflow"). É grátis em repositório público.

**Configuração (tudo pelo site do GitHub):**

1. No repositório: **Settings → Secrets and variables → Actions → New repository secret**. Crie dois:
   - `TELEGRAM_BOT_TOKEN`: o token do BotFather
   - `TELEGRAM_CHAT_ID`: o seu chat id
2. **Settings → Actions → General → Workflow permissions**: marque **Read and write permissions** e salve.
3. Aba **Actions**: se pedir, habilite os workflows. Abra **Monitor de preços → Run workflow** para a primeira rodada.
4. Abra a rodada para ver o log. No fim, o passo **Resumo do histórico** mostra, por produto, quantas leituras
   válidas e erros houve.

**Como o histórico se mantém:** o `prices.db` fica na branch `data` (um único commit, sobrescrito a cada rodada
para o repositório não crescer). Cada rodada baixa, acrescenta e salva de volta.

**Limitações importantes:**
- Os servidores do GitHub são "datacenter": algumas lojas bloqueiam esses IPs (captcha/403). Veja no log do
  primeiro "Run workflow" quais produtos leram. Os que falharem avisam no Telegram depois de
  `FAIL_NOTICE_AFTER` tentativas e podem exigir ajuste, ou rodar em outro lugar (ver abaixo).
- O agendamento do GitHub não é exato: o intervalo real pode variar alguns minutos.
- O repositório é público, então o log e o histórico de preços também são. Tokens ficam só nos Secrets
  (o GitHub os esconde dos logs). Não coloque o token no código.
- Repositórios sem atividade por 60 dias têm o agendamento pausado pelo GitHub; é só reativar na aba Actions.
- Os produtos monitorados (URLs e preços-alvo do `products.json`) ficam visíveis no repositório público.

## Deixar rodando 24h (alternativa: seu PC ou uma VPS)

O histórico só existe enquanto o monitor está ligado, então ele precisa ficar ativo até a Black Friday.

- **Linux / VPS / Raspberry Pi:** `deploy/bf-monitor.service` (systemd). Ajuste `User` e os caminhos, copie para
  `/etc/systemd/system/` e rode `sudo systemctl enable --now bf-monitor`. Reinicia sozinho se cair e volta com a máquina.
- **Windows:** `deploy/run_windows.bat` reinicia o monitor se ele parar. Ponha um atalho em `shell:startup` e
  desative a suspensão do PC (Configurações > Energia) para o notebook não dormir.
- **macOS:** `caffeinate -i python main.py` impede o Mac de dormir enquanto o monitor roda.

## Rodando em servidor / Docker

- O **histórico é o coração do monitor**. Em Docker, monte um volume em `/data` (o `Dockerfile` já grava
  `prices.db` e `monitor.log` lá). Sem volume/disco persistente, cada novo deploy zera o histórico.
- Se definir a variável `PORT`, o monitor abre um endpoint HTTP de saúde (útil em "Web Service" do Render).
  Atenção: planos gratuitos que "dormem" por inatividade pausam o loop e não têm disco persistente; o Chromium
  também pede memória (≥ 1 GB é mais seguro que 512 MB). Para uma máquina sua ou VPS pequena, é mais simples.

## Testes

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Os testes sobem lojas falsas locais e usam o Chromium real do Playwright (preço, estoque por tamanho, cupons,
captcha, 404, rodada completa com alerta). Eles não acessam as lojas de verdade.

## Limitações honestas

- Cada loja tem um HTML diferente; a extração genérica (JSON-LD/meta/heurística) funciona em muitas,
  mas pode exigir `selectors` em outras.
- Lojas com proteção anti-bot forte (Amazon, Shopee, Zara, Adidas e similares costumam ser as mais difíceis)
  podem mostrar captcha. O monitor tenta de novo, registra o erro e avisa no Telegram, mas não consegue
  "furar" o bloqueio. Nesses casos, tente `selectors`, um intervalo maior ou acompanhe o produto manualmente.
- Mercado Livre/Shopee/Amazon podem mostrar preços diferentes por vendedor ou por variação (cor/tamanho
  mudam o preço); o monitor lê o preço que a página exibe ao abrir a URL.
- Uso pessoal e com intervalos razoáveis (15–30 min); respeite os termos das lojas.
