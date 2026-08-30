# Segurança

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
  `_RiskApprovalToken`, classe privada do módulo. Qualquer tentativa de
  construir um `ApprovedOrder` fora de `RiskEngine.evaluate()` (por exemplo,
  passando `token=object()`) levanta `TypeError` — ver
  `tests/test_risk_engine.py::test_execution_requires_risk_approval`.
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
- `app/execution/reconciliation.py::reconcile_positions()` compara posições
  locais com posições reportadas pela exchange; qualquer divergência é
  reportada (não corrigida silenciosamente) — o comportamento padrão
  recomendado é bloquear novas operações (`TRADING_BLOCKED`) até revisão
  manual.
