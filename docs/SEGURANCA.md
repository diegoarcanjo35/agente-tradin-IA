# Segurança

## Idioma da experiência do usuário (correção v1.2)

Toda a experiência destinada ao usuário — interface, botões, chips de
estado, justificativas exibidas, mensagens de erro, confirmações — é em
português do Brasil. Termos técnicos consolidados aparecem acompanhados da
tradução, por exemplo `Fator de lucro (Profit Factor)`. Identificadores
internos de código (nomes de classes, eventos como `KILL_SWITCH_ENGAGED`,
endpoints) permanecem em inglês, por não serem lidos diretamente pelo
usuário como frase. Verificado por `tests/test_frontend_i18n.py`.

## Proibição de dinheiro real

- `app/core/config.py` mantém um allowlist explícito de hosts Bybit
  demo/testnet (`ALLOWED_BYBIT_HOSTS`) e uma lista de hosts de produção
  conhecidos (`KNOWN_PRODUCTION_BYBIT_HOSTS`). `assert_demo_host()` é chamado
  na validação do `Settings` (portanto em qualquer modo, mesmo antes de saber
  se `BYBIT_DEMO` será usado) e novamente na construção de
  `BybitDemoMarketDataProvider`/`BybitDemoExecutionEngine`.
- Não existe endpoint HTTP, variável de ambiente em runtime, nem elemento de
  UI para trocar de ambiente. O modo é lido uma única vez na inicialização do
  processo (`get_settings()`, com `lru_cache`).
- Teste: `tests/test_security.py::test_assert_demo_host_rejects_known_production_host`
  e `test_settings_rejects_production_base_url_at_construction`.

## Credenciais

- `Settings` (Pydantic) só lê de variáveis de ambiente / `.env`. Nenhuma
  string de credencial está hard-coded em nenhum arquivo do repositório.
- `.env` está listado em `.gitignore`; `.env.example` traz apenas placeholders
  vazios (verificado por `tests/test_security.py::test_env_example_has_no_real_secret_values`).
- `app/core/logging.py::redact()` remove recursivamente qualquer valor cujo
  nome de campo contenha `key`, `secret`, `token`, `password` ou `signature`
  antes de qualquer linha ser emitida (console ou arquivo).
- O sistema nunca solicita nem usa permissão de saque. Ao gerar uma API key
  Bybit Demo, use **apenas** permissões de leitura + negociação (trade). Se o
  seu plano permitir, restrinja a key por IP (ver `docs/OPERACAO_DEMO.md`).

## IA sem autoridade de execução

- `app/ai_shadow/` não importa `app.execution` nem `pybit`, e não referencia
  nomes de campos de credenciais Bybit — verificado estruturalmente por
  `app/ai_shadow/guard.py` (varredura AST) e exercitado em
  `tests/test_ai_shadow.py::test_ai_module_has_no_execution_import`.
- `AIShadowAgent.observe()` retorna apenas dados (`AIShadowResult`); nunca
  recebe nem devolve uma referência a um `ExecutionEngine` ou `ApprovedOrder`.
- Saída validada contra um schema Pydantic estrito (`AIRecommendationOutput`);
  timeout e tamanho máximo de resposta são configuráveis e aplicados sempre.

## Autoridade soberana do Risk Engine

- `app/risk/engine.py::ApprovedOrder` só pode ser construído com um
  `_RiskApprovalToken`, classe privada do módulo, **nunca** importada ou
  instanciada fora de `app/risk/engine.py`. Qualquer tentativa de construir um
  `ApprovedOrder` com um token inválido levanta `TypeError` — ver
  `tests/test_risk_engine.py::test_execution_requires_risk_approval`.
- Isso vale tanto para abrir quanto para fechar/reduzir posição:
  `RiskEngine.evaluate()` aprova aberturas, `RiskEngine.evaluate_close()`
  aprova fechamentos. Nenhum outro módulo (incluindo `app/orchestrator.py`)
  fabrica um `ApprovedOrder` diretamente — ambos os fluxos passam pelo Risk
  Engine. Um teste estrutural (AST) falha o build se qualquer arquivo fora de
  `app/risk/engine.py` referenciar `_RiskApprovalToken` ou chamar
  `ApprovedOrder(...)`: `tests/test_risk_engine_boundary.py`. Testes usam as
  factories públicas e controladas `RiskEngine.make_test_approved_order()` e
  `RiskEngine.attempt_construct_with_invalid_token_for_testing()`.
- `evaluate_close()` dispensa apenas os limites que existem para conter
  *nova* exposição (perda diária, posições concorrentes, exposição total,
  stop-loss obrigatório) — fechar uma posição reduz risco, então nunca é
  bloqueado por eles. Continua exigindo: kill switch livre, não
  `TRADING_BLOCKED`, estado não ambíguo (ver reconciliação abaixo), dados
  frescos, relógio sincronizado, saúde da API, posição existente, lado
  correto (oposto ao da posição) e quantidade positiva não superior à
  posição aberta.
- `ExecutionEngine.submit()` (Protocol em `app/execution/base.py`) só aceita
  esse tipo. Não há sobrecarga, atalho ou segundo construtor.

## Segredos em logs

- `app/core/logging.py` usa um `JsonFormatter` que passa todo campo extra por
  `redact()` antes de serializar. Testado em
  `tests/test_security.py::test_redact_hides_secret_like_fields`.

## Idempotência e reconciliação

- `app/execution/idempotency.py::make_idempotency_key()` gera uma chave
  determinística por sinal; motores de execução deduplicam por essa chave
  (`tests/test_execution.py::*duplicate*`).
- `app/execution/reconciliation.py::reconcile_positions()` (função pura)
  compara posições locais com posições reportadas pela exchange; qualquer
  divergência é reportada, nunca corrigida silenciosamente.
- `Orchestrator.reconcile()` integra essa função pura ao sistema em execução:
  roda uma vez na inicialização de `build_orchestrator()` (cobre "primeira
  inicialização" e "depois de reinício", em qualquer modo), e novamente
  sempre que uma ordem termina em status não confirmado (`ERROR`) — timeout
  na criação, falha ao confirmar status via polling. Se a exchange não puder
  sequer ser consultada, ou se houver qualquer divergência, o sistema entra
  em `state_ambiguous=True` + `TRADING_BLOCKED=True`, registrado em
  `security_events` e `failures_reconciliations`. O Risk Engine (tanto
  `evaluate()` quanto `evaluate_close()`) recusa qualquer decisão enquanto
  `state_ambiguous` estiver ativo — só uma reconciliação bem-sucedida
  subsequente libera automaticamente um bloqueio causado por divergência
  (um bloqueio por kill switch nunca é limpo automaticamente). Ver
  `tests/test_reconciliation_integration.py`.

## Relógio (drift)

- `app/core/clock.py::compute_clock_sync()` nunca assume drift=0: se a fonte
  de tempo de referência (`RemoteTimeProvider`) não puder ser consultada,
  retorna `ok=False` com `drift_seconds=None`, e o Risk Engine trata drift
  desconhecido como reprovação. Em `REPLAY`/`PAPER_LOCAL` a referência é
  `ReplayClockProvider`, determinística e injetável (usada nos testes para
  simular sincronizado, fora do limite, e indisponível). Em `BYBIT_DEMO`, a
  referência é `BybitServerTimeProvider`, que consulta `/v5/market/time` real
  da Bybit. Ver `tests/test_clock_sync.py`.

## Bind da API de controle

- A API de controle (`/api/kill-switch/*`) **não possui autenticação** nesta
  fase. `Settings.api_host` tem padrão `127.0.0.1`; iniciar com um host
  diferente exige `API_ALLOW_EXTERNAL_BIND=true` explícito, ou o processo
  recusa iniciar (`UnsafeBindHostError`). Ver
  `tests/test_security.py::test_default_settings_bind_host_is_local`. Uma
  implementação completa de autenticação fica para uma fase futura.

## Proteção contra injeção de HTML no painel (correção v1.2 #7)

- `frontend/app.js` nunca usa `innerHTML` para inserir dado vindo do
  backend (justificativa da estratégia, motivo do Risk Engine, resumo da
  IA, detalhe de erro). Toda inserção de texto passa por `textContent` ou
  criação segura de elementos DOM (`buildRow`/`kvRow`), então uma string
  contendo marcação (ex.: `<img src=x onerror="...">`) é sempre exibida
  como texto literal, nunca interpretada como HTML executável.
- Prova estática: `tests/test_frontend_xss_safety.py::test_app_js_never_uses_innerhtml`.
- Prova dinâmica: o mesmo arquivo de teste executa as funções reais de
  construção de linha (`buildRow`/`kvRow`) de `app.js` num shim mínimo de
  DOM via Node.js, injeta um payload clássico de XSS e comprova que ele
  chega como `textContent` puro, sem gerar nenhum nó filho.
