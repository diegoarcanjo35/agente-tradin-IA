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
