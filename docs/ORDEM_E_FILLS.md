# Máquina de Estados de Ordens e Fills

Fase 2, item 7.2. Antes desta fase, `orders.status` era uma string livre
(`String(16)`), gravada uma única vez como `"PENDING"` em `repo.save_order`
e nunca mais alterada em nenhum lugar do código — não havia máquina de
estados de fato.

## Estados

`app/execution/order_state.py::OrderStatus`:

- `PENDING_SUBMIT` — ordem criada localmente, ainda não confirmada pela
  corretora.
- `SUBMITTED` — aceita pela API (existe `exchange_order_id`), ainda sem
  confirmação de execução.
- `PARTIALLY_FILLED` — pelo menos um fill parcial confirmado.
- `FILLED` — totalmente preenchida (terminal).
- `CANCEL_PENDING` — cancelamento solicitado, ainda não confirmado.
- `CANCELLED` — cancelamento confirmado pela corretora (terminal).
- `REJECTED` — rejeitada pela corretora (terminal).
- `UNKNOWN` — não foi possível confirmar o estado real após todas as
  tentativas de polling. **Nenhuma ordem `UNKNOWN` libera nova exposição** —
  `SystemState.order_state_unknown` é recalculado a cada transição
  (`repo.has_unknown_orders`) e entra em `recompute_trading_blocked` como
  causa independente de bloqueio.

`FILLED`, `CANCELLED` e `REJECTED` são terminais — nenhuma transição sai
deles. Todas as demais transições permitidas estão explicitadas na tabela
`_ALLOWED_TRANSITIONS` do módulo; qualquer transição fora dessa tabela
levanta `IllegalOrderTransitionError` em português.

## Transição centralizada

`app/persistence/repo.py::transition_order_status(session, order, new_status,
detail=None)` é o **único** ponto sancionado para mudar `Order.status`:
valida a transição, grava uma linha de auditoria em `order_events`
(`from_status`, `to_status`, `detail`, `created_at`) e só então atualiza o
campo. Nenhum outro código (orchestrator, endpoints) escreve
`order.status = ...` diretamente.

`repo.record_fill(session, order, new_status, cumulative_filled_qty,
avg_fill_price, fees_total, detail=None)` é usado especificamente para
`PARTIALLY_FILLED`/`FILLED`: grava a transição (via
`transition_order_status`) e atualiza `filled_qty`/`avg_fill_price`/
`fees_total` com **semântica de conjunto** (SET), nunca soma — os mesmos
valores que a Bybit já reporta como acumulados (`cumExecQty`, `avgPrice`,
`cumExecFee`). Isso torna seguro registrar um segundo fill parcial, ou a
confirmação final, sem nunca contar quantidade/taxa em dobro. Chamar
`record_fill` de novo com o **mesmo** status terminal já registrado é
corretamente rejeitado (`FILLED -> FILLED` não está na tabela de
transições) — quem faz polling de status deve checar
`is_terminal(order.status)` antes de tentar de novo.

## Cancelamento (item 7.3)

`ExecutionEngine.cancel(exchange_order_id) -> CancelResult`:

- `PaperLocalExecutionEngine.cancel()`: ordens locais preenchem
  sincronamente dentro de `submit()` — nunca existe uma janela não-terminal
  para cancelar. É um no-op documentado que devolve o estado terminal já
  registrado (nunca fabrica um `CANCELLED`).
- `BybitDemoExecutionEngine.cancel()`: `POST /v5/order/cancel`, depois
  **sempre** confirma o estado final via o mesmo padrão de polling de
  `submit()` — nunca assume `CANCELLED` sem confirmar. Se um fill venceu a
  corrida antes do cancelamento ser processado, devolve
  `FILLED`/`PARTIALLY_FILLED` com os dados reais do fill, para o chamador
  registrar corretamente em vez de assumir cancelamento.

O kill switch (`POST /api/kill-switch/engage`) cancela toda ordem não
terminal (`repo.non_terminal_orders`) que já tenha `exchange_order_id`
antes de considerar o sistema estabilizado; se algo permanecer não-terminal
ou uma corrida de fill for detectada, `Orchestrator.reconcile()` roda em
seguida para assentar qualquer divergência de posição.

## Reinício com ordem em voo

Como `ExecutionEngine.submit()` é uma chamada bloqueante que só retorna
depois de tentar confirmar o resultado, hoje não existe uma janela real de
"aceita mas ainda não confirmada" que sobreviva a um crash do processo —
`_apply_fill_to_order` sempre transiciona a ordem para seu estado final
antes de `tick()` retornar. Um crash **entre** `repo.save_order` (que cria
a linha em `PENDING_SUBMIT`) e a chamada a `submit()` é a única janela
real; nesse caso a ordem fica presa em `PENDING_SUBMIT` sem
`exchange_order_id`, e a reconciliação periódica/no próximo tick é o
mecanismo que resolve isso — não há reenvio automático da mesma ordem, e a
deduplicação por `idempotency_key` (`find_order_by_idempotency_key`) impede
qualquer novo envio duplicado para o mesmo sinal/candle.

## Testes

`tests/test_order_state_machine.py` (transições válidas/inválidas),
`tests/test_order_lifecycle_repo.py` (transição + fill idempotente),
`tests/test_order_cancellation.py` (cancelamento, corrida de fill),
`tests/test_reconciliation_periodic_and_order_lifecycle.py` (fiação real via
`Orchestrator`, ordem `UNKNOWN` bloqueando entradas, kill switch cancelando
pendências).
