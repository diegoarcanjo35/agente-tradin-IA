# Máquina de Estados de Ordens e Fills

Fase 2, item 7.2. Antes da Fase 2, `orders.status` era uma string livre
(`String(16)`), gravada uma única vez como `"PENDING"` em `repo.save_order`
e nunca mais alterada em nenhum lugar do código — não havia máquina de
estados de fato.

**Correção da Fase 2 v1.1** reescreveu o ciclo real de vida da ordem: o
fluxo v1.0 pulava direto de `PENDING_SUBMIT` para o estado terminal, sem
nunca persistir `SUBMITTED`/`CANCEL_PENDING` de fato, sem acompanhamento
persistente pós-`submit()`, e com fills aplicados por sobrescrita cumulativa
em vez de por delta idempotente. O que está documentado abaixo é o desenho
corrigido.

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
- `UNKNOWN` — não foi possível confirmar o estado real (ex.: uma resposta de
  `poll_order()` fora de ordem que não corresponde a nenhuma transição
  válida a partir do estado atual). **Nenhuma ordem `UNKNOWN` libera nova
  exposição** — `SystemState.order_state_unknown` é recalculado a cada
  transição (`repo.has_unknown_orders`) e entra em
  `recompute_trading_blocked` como causa independente de bloqueio.

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

## Submissão separada de acompanhamento (correção v1.1 #1)

`ExecutionEngine` (`app/execution/base.py`) não tem mais um `submit()`
bloqueante que só retorna depois de confirmar o resultado. O contrato agora
é:

- `submit(order, idempotency_key, reference_price) -> SubmitAck` — envia o
  `POST /order/create` **uma única vez** e retorna imediatamente com
  `exchange_order_id` + `status` (`SUBMITTED`, `REJECTED` ou `UNKNOWN`).
  Nunca confirma fill.
- `poll_order(exchange_order_id) -> OrderStatusSnapshot` — consulta o status
  atual **e** o histórico completo de fills individuais (nunca um delta —
  a deduplicação é responsabilidade de quem persiste, não de quem consulta).
  É a única forma de saber que algo foi preenchido.
- `request_cancel(exchange_order_id) -> None` — dispara o pedido de
  cancelamento (fire-and-forget); o chamador **sempre** persiste
  `CANCEL_PENDING` antes de chamar isso. A confirmação real (cancelado, ou
  um fill que venceu a corrida) só vem de um `poll_order()` posterior.
- `list_open_orders(symbol) -> list[dict]` — toda ordem que a corretora
  considera aberta para `symbol`, usado pela reconciliação (ver
  `docs/FASE_2.md`) para detectar uma ordem remota sem registro local.

`Orchestrator._submit_and_track` (usado por `_submit_and_record` e
`_close_position_via_risk`) sempre chama `poll_order()` **uma vez,
imediatamente**, logo após um `submit()` que retornou `SUBMITTED` — isso
preserva o comportamento de preenchimento-no-mesmo-tick que os motores
`PAPER_*` (síncronos) sempre tiveram. Além disso,
`Orchestrator._maybe_poll_open_orders` roda no topo de todo `tick()`,
gated por `OPEN_ORDER_POLL_INTERVAL_SECONDS`, e reconsulta **toda** ordem
ainda não-terminal — inclusive uma que ficou pendente entre um reinício do
processo e o próximo tick. Nenhuma ordem é reenviada: a deduplicação por
`idempotency_key` (`repo.find_order_by_idempotency_key`, checada **antes**
de qualquer `submit()`) é o que impede um envio duplicado, nunca um
dicionário em memória dentro do motor.

## Fills: ledger idempotente por delta (correção v1.1 #2)

`app/execution/fill_ledger.py::record_new_fills(session, order, fills)` é o
único lugar que persiste `Execution` rows: deduplica por
`exchange_fill_id` (índice único `(order_id, exchange_fill_id)`, migração
v4) e, para cada fill novo, insere a linha e **recalcula**
`order.filled_qty`/`avg_fill_price`/`fees_total` a partir do conjunto
completo de `Execution` já gravadas — nunca soma ad hoc, nunca sobrescreve
com um total cumulativo que a corretora reportou. Como `poll_order()`
sempre devolve o histórico completo de fills (nunca um delta), chamar
`record_new_fills` de novo com os mesmos fills (ou um superconjunto) é
sempre um no-op seguro para o que já foi registrado.

`app/execution/fill_service.py::apply_order_snapshot(...)` é o **único**
ponto que aplica um `OrderStatusSnapshot` de ponta a ponta: transiciona o
status (se mudou), grava os fills novos via o ledger, aplica o delta de
cada fill à posição (abre/soma para entrada; reduz/fecha com PnL realizado
para saída), atualiza `SystemState.order_state_unknown` e recomputa
`trading_blocked`. É usado por **todos** os quatro caminhos que podem
aprender sobre um fill: o poll imediato pós-submit, o poller periódico, o
kill switch (`POST /api/kill-switch/engage`, que persiste `CANCEL_PENDING`
antes de `request_cancel()`) e a reconciliação (`Orchestrator.reconcile()`,
ver `docs/FASE_2.md`) — nunca existe um segundo caminho que toque
`Execution`/`Position`/contadores de sessão de outra forma.

## Política de fill parcial (correção v1.1 #2/#5)

`PARTIAL_FILL_POLICY` (`WAIT` | `CANCEL_REMAINDER` | `EXPIRE_AND_CANCEL`) e
`PARTIAL_FILL_TIMEOUT_SECONDS` controlam o que acontece com uma ordem presa
em `PARTIALLY_FILLED` por tempo demais:

- `WAIT` (padrão) nunca expira — sempre seguro.
- `CANCEL_REMAINDER`/`EXPIRE_AND_CANCEL` transicionam para
  `CANCEL_PENDING`, chamam `request_cancel()` e aplicam o resultado via
  `fill_service` — o mesmo caminho de cancelamento usado em todo o resto do
  sistema, nunca um atalho paralelo.

## Reinício com ordem em voo

Toda ordem não-terminal (`SUBMITTED`, `PARTIALLY_FILLED`, `CANCEL_PENDING`,
`UNKNOWN`) com `exchange_order_id` já persistido é retomada pelo poller
periódico logo no primeiro `tick()` após o reinício — nunca reenviada. Um
crash entre `repo.save_order` (que cria a linha em `PENDING_SUBMIT`, sem
`exchange_order_id` ainda) e o retorno de `submit()` é a única janela onde a
ordem fica sem rastro no lado da corretora; a deduplicação por
`idempotency_key` garante que o próximo tick nunca a reenvie, e a
reconciliação (que também consulta `list_open_orders()`) é o mecanismo que
eventualmente resolve a ambiguidade.

## Testes

`tests/test_order_state_machine.py` (transições válidas/inválidas),
`tests/test_order_lifecycle_repo.py` (ledger de fills por delta,
idempotência), `tests/test_order_lifecycle_real_flow.py` (fluxo real
completo: SUBMITTED → fills parciais por delta → CANCEL_PENDING →
CANCELLED, reinício sem reenvio, `POST /order/create` uma única vez),
`tests/test_order_cancellation.py` (`request_cancel`/`poll_order`,
`list_open_orders`), `tests/test_partial_fill_policy.py` (WAIT/
CANCEL_REMAINDER/EXPIRE_AND_CANCEL), `tests/test_reconciliation_periodic_and_order_lifecycle.py`
(fiação real via `Orchestrator`, ordem `UNKNOWN` bloqueando entradas, kill
switch cancelando pendências pelo caminho centralizado).
