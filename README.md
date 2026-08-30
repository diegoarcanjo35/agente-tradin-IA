# Agente de Trading IA — Fase 1 (MVP Bybit Demo)

Sistema automatizado de trading conectado **exclusivamente** ao ambiente
oficial Bybit Demo/Testnet. Não opera dinheiro real, não aceita configuração
de produção, e não promete rentabilidade. Ver `docs/SEGURANCA.md` para as
garantias de segurança e `docs/ARQUITETURA.md` para a arquitetura completa.

**AMBIENTE DEMO — SEM DINHEIRO REAL.**

## Instalação

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt      # Linux/Mac
```

Nenhuma credencial é necessária para o modo padrão (`REPLAY`).

## Execução

Use **sempre** o launcher oficial — é o único ponto de entrada que garante
que `API_HOST`/`API_PORT` validados (ver `docs/SEGURANCA.md`) sejam
realmente o host/porta usados pelo servidor:

```bash
.venv/Scripts/python -m app.run
```

Abra `http://127.0.0.1:8000` (ou a porta definida em `API_PORT`). O painel
mostra o estado do sistema, posições, sinais, decisões do Risk Engine,
recomendações da IA em shadow mode, curva de equity e histórico de erros.

Não inicie o servidor com `uvicorn app.api.main:app --host ...` diretamente
— esse caminho contorna a validação de host feita pelo launcher.

Para os demais modos (`PAPER_LOCAL`, `BYBIT_DEMO`), copie `.env.example`
para `.env` e siga `docs/OPERACAO_DEMO.md`.

## Testes

```bash
.venv/Scripts/python -m pytest -v
```

## Modos disponíveis

| Modo | Dados | Execução | Credenciais |
|---|---|---|---|
| `REPLAY` (padrão) | fixture local | simulada | nenhuma |
| `PAPER_LOCAL` | fixture local | simulada (taxa/slippage configuráveis) | nenhuma |
| `BYBIT_DEMO` | Bybit Demo/Testnet | Bybit Demo/Testnet | API key/secret demo, sem saque |

## Como gerar credenciais Demo/Testnet

Ver `docs/OPERACAO_DEMO.md`.

## Como iniciar sem credenciais

O modo padrão é `REPLAY` — basta rodar o comando de execução acima, sem
`.env`.

## Como ativar e testar o kill switch

Ver seção correspondente em `docs/OPERACAO_DEMO.md`. Resumo: botão "Ativar
bloqueio de emergência" no painel, ou `POST /api/kill-switch/engage`.

## Limitações conhecidas

- Estratégia simples (SMA cross + filtro ATR), sem promessa de
  rentabilidade.
- `BYBIT_DEMO` está implementado e testado com transporte falso, mas requer
  configuração de credenciais reais antes do primeiro uso real (ver
  `docs/OPERACAO_DEMO.md`).
- Funding fee não é coletado nesta fase (métrica reportada como
  "indisponível").
- Sem garantia de rentabilidade, sem promessa de retorno, sem gestão de
  dinheiro de terceiros. Fase 1 é um MVP de observação e validação de
  arquitetura, não um produto de investimento.

## Documentação

- `docs/ARQUITETURA.md`
- `docs/SEGURANCA.md`
- `docs/METRICAS.md`
- `docs/OPERACAO_DEMO.md`
- `docs/MODELO_DE_DADOS.md`
- `docs/MIGRACOES.md`

---

**ENTREGA PRONTA PARA AUDITORIA INDEPENDENTE**
