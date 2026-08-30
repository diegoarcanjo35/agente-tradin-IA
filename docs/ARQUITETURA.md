# Arquitetura — Agente de Trading IA (Fase 1)

## Visão geral

```
MarketDataProvider ──► StrategyEngine ──► RiskEngine ──► ExecutionEngine ──► Persistence
        │                                                                        ▲
        └──────────────────────► AIShadowAgent (observação, sem autoridade) ────┘
```

O `Orchestrator` (`app/orchestrator.py`) é o único módulo que conhece tanto o
`RiskEngine` quanto um `ExecutionEngine`. Isso torna a garantia "o Risk Engine
tem autoridade soberana" auditável por inspeção: basta localizar todos os
pontos que chamam `ExecutionEngine.submit(...)` — há exatamente dois
caminhos de código (abertura via `RiskEngine.evaluate()`, fechamento via
`RiskEngine.evaluate_close()`), e ambos só produzem um `ApprovedOrder`.
`tests/test_risk_engine_boundary.py` prova isso por AST: nenhum arquivo fora
de `app/risk/engine.py` referencia o token privado de aprovação ou constrói
`ApprovedOrder` diretamente.

## Módulos

- **app/core** — configuração (`config.py`, com `RunMode` e allowlist de hosts
  Bybit demo/testnet), relógio UTC (`clock.py`), logging estruturado com
  redação de segredos (`logging.py`), exceções de domínio (`errors.py`).
- **app/persistence** — modelos SQLAlchemy (`models.py`) e um repositório
  fino de funções tipadas (`repo.py`) sobre uma `Session`.
- **app/market_data** — `ReplayMarketDataProvider` (fixture local, zero rede)
  e `BybitDemoMarketDataProvider` (REST polling com backoff exponencial,
  válido apenas contra hosts demo/testnet).
- **app/strategy** — `StrategyEngine`: cruzamento de médias móveis (SMA
  rápida/lenta) com filtro de volatilidade por ATR. Determinístico, sem
  aprendizado de máquina.
- **app/risk** — `RiskEngine`: única fonte de aprovação de ordens, tanto para
  abrir (`evaluate()`) quanto para fechar/reduzir (`evaluate_close()`).
  Produz um `ApprovedOrder`, cuja construção exige um token opaco
  (`_RiskApprovalToken`) que só existe dentro de `app/risk/engine.py`.
- **app/execution** — `PaperLocalExecutionEngine` (simulação local, preenche
  usando o `reference_price` explícito passado pelo chamador — o preço do
  candle que gerou a decisão, ou o preço de gatilho de um stop/take) e
  `BybitDemoExecutionEngine` (Bybit Demo/Testnet real, via cliente HTTP
  injetado; produção usa `app/execution/bybit_pybit_client.py`, um adaptador
  sobre o cliente oficial `pybit`). Nenhum dos dois aceita nada além de um
  `ApprovedOrder`. Um HTTP 200 na criação da ordem nunca é tratado como
  execução confirmada — o motor sempre consulta o status da ordem antes de
  reportar `FILLED`. `app/execution/reconciliation.py` compara posições
  locais com as reportadas pela exchange; `Orchestrator.reconcile()` integra
  essa checagem à inicialização e a qualquer ordem que termine em erro.
- **app/ai_shadow** — `AIShadowAgent` + `SimulatedProvider` (determinístico,
  offline, provedor padrão). Produz apenas dados estruturados validados;
  nunca referencia `app.execution` nem credenciais Bybit (ver
  `app/ai_shadow/guard.py` e `tests/test_ai_shadow.py`).
- **app/metrics** — funções puras que recebem posições fechadas e retornam
  métricas; nunca inventam zero quando o dado é insuficiente (retornam a
  string `"indisponível"`).
- **app/api** — FastAPI: rotas de leitura (`routes_dashboard.py`) e de
  controle (`routes_control.py`, apenas kill switch — não há endpoint de
  troca de modo/ambiente).
- **app/orchestrator.py** — conecta tudo por tick.
- **frontend/** — painel single-page estático servido pelo próprio FastAPI.

## Fluxo de uma vela (tick)

1. `MarketDataProvider.next_candle()` retorna a próxima vela (ou `None`).
2. A vela é persistida (`candles`).
3. `StrategyEngine.on_candle()` produz um `Signal` (BUY/SELL/HOLD +
   justificativa + parâmetros usados). Persistido em `strategy_signals`.
4. Em paralelo (mesma tick, não bloqueante), o `AIShadowAgent` observa o
   mesmo contexto de mercado e produz uma recomendação estruturada,
   persistida em `ai_recommendations` — nunca influencia os passos
   seguintes.
5. O relógio é verificado (`compute_clock_sync`); drift desconhecido ou acima
   do limite bloqueia o sistema (`TRADING_BLOCKED`) em vez de assumir zero.
6. Se houver posição aberta no símbolo, o candle é checado contra o
   stop-loss/take-profit da posição (`_check_stop_take`); se tocado, o
   fechamento segue direto para o passo 8 via `RiskEngine.evaluate_close()`.
7. Senão, se houver posição aberta na direção oposta ao sinal da estratégia,
   o Orchestrator tenta fechá-la (reduzir risco é sempre permitido, exceto
   sob kill switch/TRADING_BLOCKED/dados obsoletos/estado ambíguo) via
   `RiskEngine.evaluate_close()` — passo 8, e a tick termina aí.
8. Fechamento: `evaluate_close()` aprova ou rejeita (sempre persistido em
   `risk_evaluations`, ligado ao sinal originador — nunca `signal_id=0`); se
   aprovado, `ExecutionEngine.submit(approved, key, reference_price=...)` é
   chamado com o preço de gatilho correto; ordem, execução, taxas (somadas
   às já acumuladas na posição) e o fechamento/redução da posição são
   persistidos; se a ordem terminar em erro, `Orchestrator.reconcile()` roda
   antes de retornar.
9. Caso contrário (sem posição a fechar), `RiskEngine.evaluate()` roda todos
   os checks de abertura e retorna aprovação ou rejeição — sempre persistido
   em `risk_evaluations`.
10. Se aprovado, `ExecutionEngine.submit(..., reference_price=candle.close)`
    é chamado com uma chave de idempotência derivada do sinal; o resultado
    (fill total, parcial, erro) é persistido em `orders`/`executions`, e uma
    posição é aberta com a taxa de abertura já contabilizada.

## Modos de execução

`REPLAY` (padrão) → `PAPER_LOCAL` → `BYBIT_DEMO`. Detalhes em
`docs/OPERACAO_DEMO.md`. Não existe endpoint ou variável de ambiente que
alterne modo em tempo de execução — é configuração de processo, definida uma
vez na inicialização.
