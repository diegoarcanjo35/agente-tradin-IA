# Fase 2 — Operação Contínua e Validação Controlada (Bybit Demo)

## Objetivo

Transformar o MVP arquitetural da Fase 1 num agente capaz de permanecer em
execução contínua, com dados reais do ambiente **Bybit Demo Trading**,
operando exclusivamente em simulação local alimentada por mercado ao vivo
(`PAPER_LIVE`) ou com ordens reais no ambiente Demo (`BYBIT_DEMO`). A Fase 2
**não** promete rentabilidade nem otimiza estratégia — o foco é resiliência,
recuperação, rastreabilidade e segurança operacional.

## O que muda em relação à Fase 1

| Área | Fase 1 | Fase 2 |
|---|---|---|
| Status de ordem | string livre, nunca avançada | máquina de estados explícita (`app/execution/order_state.py`) — ver `docs/ORDEM_E_FILLS.md` |
| Reconciliação | só no startup ou após erro | também periódica, configurável (`RECONCILIATION_INTERVAL_SECONDS`) |
| Causas de bloqueio | `kill_switch`, `state_ambiguous`, `clock_out_of_sync`, `api_failure_count` | + `reconciliation_diverged`, `reconciliation_stale` (só bloqueia aberturas), `order_state_unknown` |
| Controle de entradas | implícito (qualquer ordem aprovada era enviada) | separado em `operational_state` — ver abaixo |
| Sessões | não existiam | `operational_sessions` — ver `docs/SESSOES_OPERACIONAIS.md` |
| Modos | `REPLAY`, `PAPER_LOCAL`, `BYBIT_DEMO` | + `PAPER_LIVE` (dados reais, execução simulada) |
| Cancelamento | não existia | `ExecutionEngine.cancel()`, kill switch cancela pendências |
| Custos | taxas, PnL bruto/líquido | + slippage realizado vs. preço de referência (`app/metrics/engine.py::compute_cost_metrics`) |
| AI Shadow | contexto básico | + sessão/posição/risco/métricas no contexto; métricas de concordância (`app/metrics/ai_shadow_metrics.py`) |

## Estado operacional (item 7.8)

Separa "processo está rodando" de "estratégia está autorizada a abrir novas
posições":

```
INICIALIZANDO → OBSERVANDO → ATIVO (ação explícita do operador)
                     ↑              ↓
                  PAUSADO ←── (ação explícita do operador)
                     ↑
                 BLOQUEADO (espelha trading_blocked; nunca por ação manual)
```

- Todo processo/sessão nasce em `OBSERVANDO` — **nunca** `ATIVO`. Novas
  aberturas exigem `POST /api/operational-state/activate` (origem local ou
  `CONTROL_API_TOKEN`, mesma política do kill switch).
- `POST /api/operational-state/pause` sempre permitido (como engajar o kill
  switch) — pausa impede novas aberturas, mas mantém dados, monitoramento,
  reconciliação e saídas redutoras de risco.
- `BLOQUEADO` espelha `trading_blocked` automaticamente
  (`repo.recompute_trading_blocked`) — nunca setado manualmente. Ao
  desbloquear, volta para `OBSERVANDO`, nunca direto para `ATIVO` (reforça
  "ativação sempre explícita").
- `RiskEngine.evaluate()` (só abertura) rejeita qualquer sinal quando
  `operational_state != "ATIVO"`. `evaluate_close()` nunca verifica isso —
  fechar/reduzir posição nunca é bloqueado pelo estado operacional.
- Nenhum endpoint permite trocar `REPLAY`/`PAPER_LOCAL`/`PAPER_LIVE`/
  `BYBIT_DEMO` em tempo de execução — essa restrição da Fase 1 continua
  intacta; `operational_state` é um eixo completamente separado do `mode`.

## Modo `PAPER_LIVE`

Reaproveita `BybitDemoMarketDataProvider`/`BybitServerTimeProvider` (dados
reais, só endpoints públicos, sem credenciais) pareado com
`PaperLocalExecutionEngine` (execução 100% local). Nunca constrói
`BybitDemoExecutionEngine`, nunca chama `require_bybit_credentials()`, nunca
usa `http_post` — `PaperLocalExecutionEngine` simplesmente não tem nenhum
caminho de código que envie uma requisição à corretora. Banner do painel:
`"PAPER AO VIVO — SIMULAÇÃO, SEM ORDEM NA CORRETORA"`. Testado em
`tests/test_paper_live_mode.py`.

## Migração de esquema

Nova versão `3` do sistema de migrações (`app/persistence/migrations.py`) —
ver `docs/MIGRACOES.md`. Upgrade obrigatório e testado a partir do banco
aprovado da Fase 1; histórico continua contíguo e validado
cumulativamente; nenhuma versão futura/desconhecida é aceita.

## Limitações desta fase

- `PAPER_LIVE` usa os mesmos limites de risco configurados para os demais
  modos — não há um perfil de risco dedicado por modo nesta fase.
- Funding real do Bybit Demo não está conectado (endpoint não wired ainda)
  — `MetricsResult.funding` continua reportando `"indisponível"`, nunca um
  zero fabricado.
- O roteiro de teste manual (`docs/ROTEIRO_TESTE_MANUAL_DEMO.md`) é apenas
  documentação — não é executado automaticamente e exige autorização
  separada do Diego.
