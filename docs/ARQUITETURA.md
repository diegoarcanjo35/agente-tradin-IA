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
pontos que chamam `ExecutionEngine.submit(...)` — há exatamente um caminho de
código, e ele só aceita um `ApprovedOrder`.

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
- **app/risk** — `RiskEngine`: única fonte de aprovação de ordens. Produz um
  `ApprovedOrder`, cuja construção exige um token opaco (`_RiskApprovalToken`)
  que só existe dentro de `app/risk/engine.py`.
- **app/execution** — `PaperLocalExecutionEngine` (simulação local) e
  `BybitDemoExecutionEngine` (Bybit Demo/Testnet real, via cliente HTTP
  injetado). Nenhum dos dois aceita nada além de um `ApprovedOrder`. Um HTTP
  200 na criação da ordem nunca é tratado como execução confirmada — o motor
  sempre consulta o status da ordem antes de reportar `FILLED`.
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
5. Se houver uma posição aberta na direção oposta ao sinal, o Orchestrator
   fecha essa posição (reduzir risco é sempre permitido, exceto sob kill
   switch/TRADING_BLOCKED/dados obsoletos) e a tick termina aqui.
6. Caso contrário, o `RiskEngine.evaluate()` roda todos os checks (seção
   "Risk Engine" do README) e retorna aprovação ou rejeição — sempre
   persistido em `risk_evaluations`, com o motivo exato.
7. Se aprovado, o `ExecutionEngine.submit()` é chamado com uma chave de
   idempotência derivada do sinal; o resultado (fill total, parcial, erro)
   é persistido em `orders`/`executions`, e uma posição é aberta/atualizada.

## Modos de execução

`REPLAY` (padrão) → `PAPER_LOCAL` → `BYBIT_DEMO`. Detalhes em
`docs/OPERACAO_DEMO.md`. Não existe endpoint ou variável de ambiente que
alterne modo em tempo de execução — é configuração de processo, definida uma
vez na inicialização.
