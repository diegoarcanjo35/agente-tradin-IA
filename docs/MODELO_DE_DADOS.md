# Modelo de Dados

Definido em `app/persistence/models.py` (SQLAlchemy). Toda tabela usa
timestamps em UTC (`app/core/clock.py::utcnow()`). Cadeia de rastreabilidade:
`candles → strategy_signals → (ai_recommendations | risk_evaluations) → orders → executions → positions`.

| Tabela | Propósito | Campos-chave |
|---|---|---|
| `candles` | Velas recebidas (replay ou Bybit) | `symbol`, `open_time`, OHLCV, `source` |
| `strategy_signals` | Sinal produzido pela estratégia determinística | `direction`, `justification`, `observed_price`, `atr`, `params_json` |
| `ai_recommendations` | Saída do AI Shadow Agent (validada ou rejeitada) | `signal_id` (FK), `recommendation`, `confidence`, `is_valid`, `rejection_reason` |
| `risk_evaluations` | Decisão do Risk Engine para um sinal | `signal_id` (FK), `approved`, `reason`, `checks_json` (cada check individual) |
| `orders` | Ordem submetida (PAPER_LOCAL ou BYBIT_DEMO) | `idempotency_key` (único), `risk_evaluation_id` (FK), `status`, `exchange_order_id`, `is_close` |
| `executions` | Preenchimentos (fills), incluindo parciais | `order_id` (FK), `fill_qty`, `fill_price`, `fee`, `is_partial` |
| `positions` | Posições abertas/fechadas | `status`, `realized_pnl`, `fees_paid` (acumula taxa de abertura + parciais + fechamento — nunca só a última), `opened_at`, `closed_at` |
| `account_snapshots` | Fotos periódicas de saldo/equity | `balance`, `equity`, `unrealized_pnl`, `mode` |
| `security_events` | Eventos de segurança (kill switch, bloqueio de endpoint de produção, drift de relógio, divergência de reconciliação) | `event_type`, `detail` |
| `failures_reconciliations` | Falhas de execução e resultados de reconciliação (integrada ao Orchestrator, não só função isolada) | `kind` (FAILURE\|RECONCILIATION), `detail`, `resolved` |
| `system_state` | Estado vivo (linha única, id=1) | `trading_blocked`, `kill_switch_engaged`, `consecutive_losses`, `cooldown_until`, `api_failure_count`, `state_ambiguous` |

## Rastreabilidade ponta a ponta

Toda ordem tem um caminho auditável: `orders.risk_evaluation_id` →
`risk_evaluations.signal_id` → `strategy_signals.id`. Uma recomendação de IA
para o mesmo instante é linkada por `ai_recommendations.signal_id`, mas nunca
aparece na cadeia de aprovação — ela é sempre um ramo paralelo, nunca
consumida pelo Risk Engine ou pelo Execution Engine.

Isso vale igualmente para fechamentos (`orders.is_close = true`): um
fechamento por sinal oposto ou por stop-loss/take-profit sempre gera sua
própria linha em `strategy_signals` (direção = lado de fechamento,
justificativa explica o gatilho) antes de `risk_evaluations` ser criada —
nunca existe uma ordem de fechamento com `signal_id` sintético/zero.
