# Sessões Operacionais

Fase 2, item 7.7 — `app/sessions.py`, tabela `operational_sessions`.

## Ciclo de vida

`app/api/main.py::build_orchestrator()` chama
`start_or_resume_session(session, settings, strategy_version, risk_limits)`
logo na inicialização, **antes** da reconciliação de startup (para que essa
primeira reconciliação já seja contabilizada nos contadores da sessão):

- Se já existe uma sessão **não encerrada** (`ended_at IS NULL`) para o
  mesmo `mode` + `symbol`, ela é **retomada** — nenhuma linha nova é criada.
  Um reinício de processo nunca gera uma segunda sessão para o mesmo
  contexto.
- Caso contrário, cria uma nova sessão: `session_uid` (UUID), `mode`,
  `symbol`, `timeframe`, `strategy_version`, um snapshot da configuração de
  risco (`risk_config_json`, via `dataclasses.asdict(RiskLimits)`) e um
  **snapshot sanitizado** da configuração geral (`config_snapshot_json`) —
  `_sanitized_config_snapshot()` usa uma **lista de permissão explícita**
  de campos (nunca uma lista de bloqueio), então nenhum campo novo
  adicionado a `Settings` no futuro vaza para o snapshot por acidente;
  `bybit_api_key`/`bybit_api_secret` nunca são incluídos.

`SystemState.active_session_id` sempre aponta para a sessão em uso; o
estado da sessão (`OperationalSession.status`) é mantido em sincronia com
`SystemState.operational_state` em todo endpoint que muda esse último
(`/kill-switch/engage`, `/operational-state/activate`,
`/operational-state/pause`, e `recompute_trading_blocked` quando entra ou
sai de `BLOQUEADO`).

`end_session(session, op_session, reason)` marca `ended_at`, `end_reason` e
`status="ENCERRANDO"` — usado num desligamento gracioso explícito (não
chamado automaticamente hoje; ver limitações abaixo).

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

## Limitações desta fase

- Não há encerramento automático de sessão no desligamento do processo
  (`_lifespan` em `app/api/main.py` não chama `end_session`) — a sessão
  simplesmente é retomada no próximo `build_orchestrator()`. Um
  desligamento gracioso explícito fica como extensão futura.
- Uma troca de símbolo/modo sem reiniciar o processo criaria uma nova
  sessão automaticamente (por design — `start_or_resume_session` busca por
  `mode`+`symbol` exatos), mas como não há endpoint para trocar `mode` em
  tempo de execução, isso só ocorre naturalmente entre reinícios.

## Testes

`tests/test_operational_sessions_and_states.py` (retomada de sessão,
gates de ativação, pausa, causas de bloqueio independentes, contadores
reais via `Orchestrator.tick()`).
