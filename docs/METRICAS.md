# Métricas

Todas as métricas são calculadas por `app/metrics/engine.py::compute_metrics()`,
uma função pura que recebe apenas posições **fechadas** (`ClosedTrade`) e o
saldo inicial. Quando um valor não pode ser calculado (ex.: nenhuma operação
perdedora, para `profit_factor`), o campo retorna a string `"indisponível"` —
nunca `0`.

O teste `tests/test_reproducible_fixture.py` recalcula cada fórmula abaixo à
mão a partir de `fixtures/reproducible_trades.json` e compara com a saída do
motor. Um auditor pode reproduzir os mesmos números manualmente.

## Fórmulas

- **Lucro bruto** = soma dos `realized_pnl` positivos.
- **Prejuízo bruto** = soma dos `realized_pnl` negativos (valor negativo).
- **Comissões** = soma de `fees_paid` de todas as operações fechadas.
- **Lucro líquido** = lucro bruto + prejuízo bruto − comissões.
- **Funding** = `"indisponível"` nesta fase (feed de funding da Bybit não
  integrado no Fase 1).
- **Taxa de acerto** = nº de operações vencedoras / nº total de operações
  fechadas.
- **Ganho médio** = lucro bruto / nº de operações vencedoras.
- **Perda média** = prejuízo bruto / nº de operações perdedoras.
- **Payoff** = |ganho médio / perda média|.
- **Profit factor** = lucro bruto / |prejuízo bruto| (indisponível se não
  houver prejuízo).
- **Expectativa matemática** = taxa de acerto × ganho médio + (1 − taxa de
  acerto) × perda média.
- **Maior sequência de ganhos / perdas** = maior run consecutivo de
  `realized_pnl > 0` / `< 0`, na ordem de fechamento.
- **Curva de equity** = saldo inicial + soma cumulativa de
  (`realized_pnl − fees_paid`) por operação fechada, em ordem cronológica.
- **Drawdown máximo (dinheiro)** = maior queda (pico − vale) observada na
  curva de equity.
- **Drawdown máximo (%)** = drawdown máximo em dinheiro / valor do pico
  correspondente × 100.
- **Retorno sobre capital (%)** = lucro líquido / saldo inicial × 100.
- **Retorno/Drawdown** = lucro líquido / drawdown máximo em dinheiro
  (indisponível se não houve drawdown).
- **Exposição** = soma de `qty × avg_entry_price` das posições **abertas**
  no momento da consulta — nunca misturada com o P&L realizado.

## Separação realizado vs. não realizado

`compute_metrics()` só enxerga posições fechadas (`status = CLOSED`); uma
posição aberta não contribui com nenhum valor de P&L realizado, apenas com
`exposure_usd` (opcional, informativo). O painel consulta posições abertas
por um endpoint separado (`/api/positions`), nunca fundido com os números de
`/api/metrics`.

## Custos e slippage (Fase 2, item 7.6)

`app/metrics/engine.py::compute_cost_metrics()` — função pura separada de
`compute_metrics()`, recebe `OrderFillView` (visão mínima de uma ordem
preenchida: `side`, `reference_price`, `avg_fill_price`, `fees_total`),
alimentada por `repo.filled_orders()` via `GET /api/costs`:

- **Taxas acumuladas** = soma de `fees_total` de toda ordem preenchida —
  sempre um valor real (0.0 para um conjunto vazio é um total genuíno, não
  fabricado).
- **Slippage realizado** = diferença entre o preço de execução
  (`avg_fill_price`) e o preço de referência (`reference_price` — o preço
  do candle/gatilho que originou a decisão, gravado em `orders.reference_price`
  no momento do envio). Para `BUY`, pagar MAIS que a referência é
  desfavorável (valor positivo); para `SELL`, receber MENOS é desfavorável.
  Slippage favorável aparece como valor negativo — nunca truncado a zero,
  para não esconder informação real do operador.
- Só é calculado sobre ordens com `reference_price` conhecido
  (`priced_orders_count`); se nenhuma ordem tiver essa informação (ex.:
  ordens migradas de antes da Fase 2), `slippage_avg_usd`/`slippage_total_usd`
  retornam `"indisponível"` — nunca um zero fabricado. `unpriced_orders_count`
  reporta quantas ordens ficaram de fora desse cálculo.

## AI Shadow: concordância e contrafactual (Fase 2, item 7.10)

`app/metrics/ai_shadow_metrics.py::compute_ai_shadow_metrics()` — pura,
compara cada recomendação da IA com a decisão real e autoritativa da
estratégia (nunca o contrário: a IA nunca influencia a decisão real).
`hypothetical_hit_rate` e `counterfactual_pnl` são **explicitamente
retrospectivos** (só sobre operações que a estratégia já fechou de verdade
e onde a IA concordou com a direção real) e devem ser sempre rotulados como
**SIMULAÇÃO** em qualquer lugar que apareçam — nunca apresentados como um
histórico real de desempenho da IA, já que ela nunca teve autoridade de
execução.
