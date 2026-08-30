# Sessões Operacionais

Fase 2, item 7.7 — `app/sessions.py`, tabela `operational_sessions`.

## Ciclo de vida

`app/api/main.py::build_orchestrator()` chama
`start_or_resume_session(session, settings, strategy_version, risk_limits)`
logo na inicialização, **antes** da reconciliação de startup (para que essa
primeira reconciliação já seja contabilizada nos contadores da sessão):

- Se já existe uma sessão **não encerrada** (`ended_at IS NULL`) para o
  mesmo `mode` + `symbol` **e** cujo `config_fingerprint` bate exatamente
  com o fingerprint atual (correção v1.1 #8, ver abaixo), ela é
  **retomada** — nenhuma linha nova é criada. Um reinício de processo com a
  mesma configuração nunca gera uma segunda sessão para o mesmo contexto.
- Se existe uma sessão não encerrada para o mesmo `mode`+`symbol` mas com
  um fingerprint **diferente** (ou sem fingerprint algum — uma linha
  legada, tratada como divergente por padrão, nunca confiada
  implicitamente), essa sessão antiga é encerrada
  (`end_session(..., "Configuração alterada...")`) e uma nova é criada —
  uma sessão retomada nunca opera silenciosamente sob um snapshot
  desatualizado.
- Caso contrário (nenhuma sessão anterior), cria uma nova sessão:
  `session_uid` (UUID), `mode`, `symbol`, `timeframe`, `strategy_version`,
  um snapshot da configuração de risco (`risk_config_json`, via
  `dataclasses.asdict(RiskLimits)`) e um **snapshot sanitizado** da
  configuração geral (`config_snapshot_json`) — `_sanitized_config_snapshot()`
  usa uma **lista de permissão explícita** de campos (nunca uma lista de
  bloqueio), então nenhum campo novo adicionado a `Settings` no futuro vaza
  para o snapshot por acidente; `bybit_api_key`/`bybit_api_secret` nunca são
  incluídos.

### Fingerprint de configuração (correção v1.1 #8)

`app.sessions._config_fingerprint(settings, strategy_version, risk_limits)`
calcula um SHA-256 sobre `json.dumps(sort_keys=True)` de
`{strategy_version, timeframe, risk_config: asdict(risk_limits),
config_snapshot: _sanitized_config_snapshot(settings)}` — os mesmos campos
já sanitizados usados em `config_snapshot_json`, nunca um segredo. Uma
mudança em qualquer um desses campos (versão de estratégia, limites de
risco, timeframe, ou qualquer campo do snapshot sanitizado) produz um
fingerprint diferente e força uma sessão nova em vez de uma retomada
silenciosa sob configuração desatualizada.

`SystemState.active_session_id` sempre aponta para a sessão em uso; o
estado da sessão (`OperationalSession.status`) é mantido em sincronia com
`SystemState.operational_state` em todo endpoint que muda esse último
(`/kill-switch/engage`, `/operational-state/activate`,
`/operational-state/pause`, e `recompute_trading_blocked` quando entra ou
sai de `BLOQUEADO`).

`end_session(session, op_session, reason)` marca `ended_at`, `end_reason` e
`status="ENCERRANDO"`. Desde a correção v1.1 #7, é chamado de fato pelo
desligamento gracioso do processo — ver "Desligamento gracioso" abaixo.

## Contadores

Incrementados exatamente no ponto em que cada evento já é sabido ter
acontecido dentro de `Orchestrator.tick()`/`reconcile()` — nunca
recalculados por uma query separada, então não há risco de dupla contagem:

| Contador | Incrementado em |
|---|---|
| `candles_count` | candle novo persistido com sucesso |
| `signals_count` | sinal da estratégia salvo |
| `approvals_count` / `rejections_count` | resultado de `RiskEngine.evaluate()` |
| `orders_count` | `repo.save_order` (abertura ou fechamento) |
| `fills_count` | fill FILLED/PARTIALLY_FILLED registrado |
| `failures_count` | `repo.record_failure(..., "FAILURE", ...)` |
| `reconciliations_count` | toda chamada a `Orchestrator.reconcile()` |

`app.sessions.increment(op_session, field)` é um no-op seguro quando
`op_session is None` (nenhuma sessão ativa ainda) — usado por testes que
constroem um `Orchestrator` diretamente, sem passar por
`build_orchestrator()`.

## Gates de ativação de sessão

Uma sessão só pode chegar a `operational_state="ATIVO"` (via
`POST /api/operational-state/activate`) depois de:

1. ambiente validado (host allowlist — já garantido na construção do
   `Settings`, antes de qualquer wiring);
2. credenciais validadas sem exposição (BYBIT_DEMO: `require_bybit_credentials()`
   já rodou; REPLAY/PAPER_LOCAL/PAPER_LIVE não precisam de credenciais);
3. relógio sincronizado (refletido em `trading_blocked` via
   `clock_out_of_sync`);
4. cursor de mercado definido (implícito: o provider já processou pelo
   menos um candle antes de qualquer sinal existir);
5. reconciliação inicial concluída
   (`SystemState.initialization_not_reconciled == False`, limpo por
   `Orchestrator.reconcile()` na primeira vez que uma reconciliação
   realmente completa — sucesso ou divergência, não apenas uma falha de
   rede que nem chegou a comparar);
6. estado remoto não ambíguo (`not state.trading_blocked` no momento da
   ativação).

O endpoint recusa explicitamente com mensagem em português quando (5) ou
(6) não estão satisfeitos, ou quando o estado operacional atual não é
`OBSERVANDO`/`PAUSADO`.

## Endpoints do painel

`GET /api/session` devolve a sessão ativa (ou `null`) com todos os
contadores — o que alimenta a seção "Sessão Atual" do painel.

## Desligamento gracioso (correção v1.1 #7)

`app/api/main.py::_graceful_shutdown` roda no `finally` do lifespan do
FastAPI (`_lifespan`):

1. marca `SystemState.operational_state = "ENCERRANDO"` (bloqueia novas
   entradas imediatamente);
2. cancela a tarefa de polling (`loop_task.cancel()`) e aguarda o
   `CancelledError`, sem deixá-la solta;
3. roda uma última `Orchestrator.reconcile()` — uma falha aqui nunca trava
   nem derruba o desligamento, apenas é registrada;
4. se há uma sessão ativa não encerrada, chama `end_session(...)` com um
   motivo em português;
5. persiste tudo antes do processo terminar.

Um **crash real** (que nunca alcança `_lifespan`) nunca encerra a sessão —
ela continua com `ended_at IS NULL` e é retomada normalmente no próximo
boot (sujeita ao mesmo gate de fingerprint acima). Só um desligamento que
efetivamente passa por `_lifespan` produz uma sessão encerrada, e só uma
sessão encerrada libera o próximo boot para criar uma sessão nova em vez
de retomar.

## Testes

`tests/test_operational_sessions_and_states.py` (retomada de sessão,
gates de ativação, pausa, causas de bloqueio independentes, contadores
reais via `Orchestrator.tick()`), `tests/test_session_fingerprint.py`
(retomada só com fingerprint idêntico, sessão nova em mudança de
estratégia/risco, nenhum segredo no fingerprint/snapshot),
`tests/test_graceful_shutdown.py` (sessão encerrada no desligamento real via
`TestClient`, ordem pendente nunca finalizada por adivinhação, próximo boot
cria sessão nova, crash continua retomando a sessão não encerrada).
