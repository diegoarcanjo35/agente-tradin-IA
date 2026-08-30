# Operação em Bybit Demo

## Gerando credenciais Demo/Testnet

1. Acesse o ambiente **Bybit Demo Trading** (não confundir com a conta
   principal/produção) — a Bybit disponibiliza um modo "Demo Trading"
   dedicado a partir da conta normal, com saldo fictício.
2. Crie uma API key exclusiva para este projeto.
3. Marque **apenas** as permissões de leitura (Read) e negociação (Trade).
   **Nunca** habilite "Withdraw" (saque).
4. Se seu plano permitir restrição por IP, restrinja a key ao IP da máquina
   que executará o agente.
5. Copie `.env.example` para `.env` e preencha `BYBIT_API_KEY` /
   `BYBIT_API_SECRET`. Confirme que `BYBIT_BASE_URL` e `BYBIT_WS_URL`
   continuam apontando para hosts demo/testnet — o sistema recusa iniciar
   caso contrário (ver `docs/SEGURANCA.md`).

## Iniciando sem credenciais (REPLAY)

O modo padrão é `REPLAY` e não exige `.env` nem qualquer credencial:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt      # Linux/Mac
.venv/Scripts/python -m app.run
```

Abra `http://127.0.0.1:8000`. O sistema roda inteiramente sobre
`fixtures/replay_btcusdt.json`, sem qualquer chamada de rede.

**Sempre inicie pelo launcher oficial (`python -m app.run`)** — é o único
código que efetivamente repassa `API_HOST`/`API_PORT` validados ao
`uvicorn` (correção v1.3 #3). Um `uvicorn app.api.main:app --host 0.0.0.0`
direto contorna essa validação por completo, já que a decisão do host de
bind acontece na camada do próprio `uvicorn`/CLI, fora do alcance do código
Python da aplicação. Como segunda camada de proteção, independente de como
o processo foi iniciado, os endpoints de bloqueio de emergência exigem
origem local ou `CONTROL_API_TOKEN` para *desativar* o bloqueio (ver
`docs/SEGURANCA.md`).

## Modos disponíveis

- `REPLAY` (padrão): dados de um arquivo local, execução simulada, zero rede.
- `PAPER_LOCAL`: mesma fonte de dados de `REPLAY` nesta fase (pode ser trocada
  por um feed ao vivo somente-leitura no futuro), execução simulada local
  com taxa e slippage configuráveis, sem enviar ordens à Bybit.
- `BYBIT_DEMO`: dados e ordens reais contra o ambiente oficial **Bybit Demo
  Trading**. Exige `BYBIT_API_KEY`/`BYBIT_API_SECRET` e recusa iniciar se o
  host configurado não estiver na allowlist.

Defina `MODE=` no `.env` (ou variável de ambiente) e reinicie o processo —
não há troca de modo em tempo de execução.

### Por que só Demo Trading, e não Testnet (correção v1.2 #6)

A allowlist de hosts Bybit (`app/core/config.py::ALLOWED_BYBIT_HOSTS`) aceita
apenas `api-demo.bybit.com`/`stream-demo.bybit.com`. O Testnet simples
(`api-testnet.bybit.com`) foi deliberadamente removido: o cliente oficial
`pybit`, quando configurado com `demo=True, testnet=True`, conecta-se a um
**terceiro** host (`api-demo-testnet.bybit.com`), diferente tanto do Demo
quanto do Testnet puro; e `testnet=True` sozinho reconectaria ao Testnet
puro mesmo que `BYBIT_BASE_URL` apontasse para outro lugar. Ou seja, o
`base_url` validado não determinava de fato o ambiente usado pelo cliente.
Em vez de construir lógica específica por host para suportar os dois
ambientes com segurança, esta fase suporta apenas Demo Trading — o modo do
cliente `pybit` é derivado do host validado
(`app/execution/bybit_pybit_client.py::build_pybit_client`), nunca
hard-coded. Um host desconhecido (incluindo Testnet puro) permanece
bloqueado, como qualquer host fora da allowlist.

### Resiliência do loop de dados de mercado (correções v1.2 #1 e #2)

`BybitDemoMarketDataProvider.next_candle()` nunca retorna um `None` simples.
Ele reporta um de cinco estados (`app/market_data/base.py::CandleFetchStatus`):
`CANDLE_AVAILABLE`, `NO_NEW_CANDLE` (candle repetido/ainda em formação),
`RETRYABLE_ERROR` (timeout/rate limit — aplica backoff e continua),
`REPLAY_FINISHED` (só ocorre em `REPLAY`; é o único status que encerra o
loop de polling) e `FATAL_ERROR`. O intervalo de polling é configurável via
`BYBIT_POLL_INTERVAL_SECONDS` (padrão 5s — conservador para não estourar
limites de requisição da Bybit num timeframe de 1 minuto); `REPLAY`/
`PAPER_LOCAL` usam `REPLAY_POLL_INTERVAL_SECONDS` (padrão 0.02s), já que
nunca tocam uma API real. Deduplicação ocorre em duas camadas: o provider
rastreia o último `open_time` processado, e a tabela `candles` tem uma
constraint única (`symbol, timeframe, open_time`) como defesa adicional —
`repo.save_candle()` nunca levanta exceção em caso de duplicata concorrente,
apenas retorna `None` e a tick é ignorada sem criar sinal/IA/avaliação de
risco duplicados.

### Seleção do candle fechado anterior (correção v1.3 #2)

A Bybit retorna as linhas de kline **da mais nova para a mais antiga**, e a
mais nova é frequentemente um candle ainda em formação. Consultar com
`limit=1` podia devolver só esse candle aberto repetidamente, deixando o
candle fechado anterior permanentemente fora da janela de resposta — o
processo nunca entregava candle algum à estratégia. `BybitDemoMarketDataProvider`
agora consulta `fetch_limit` linhas (padrão 5), interpreta todas
(independente da ordem), filtra apenas as fechadas (`open_time + intervalo
<= agora`) e ainda não processadas, e entrega sempre a **mais antiga
pendente** — nunca pulando à frente. Se várias tiverem se acumulado (ex.:
após um período de falhas), são drenadas uma por chamada, em ordem
cronológica, sem lacunas. Coberto por `tests/test_closed_candle_selection.py`.

## Testando o kill switch

1. Com o sistema rodando, abra o painel.
2. Clique em "Engage Kill Switch". O estado `TRADING_BLOCKED` é ativado
   imediatamente e um `security_events` é gravado.
3. Confirme que nenhum novo pedido é preenchido enquanto o kill switch está
   ativo (a aba "Decisões do Risk Engine" mostrará rejeições com o motivo
   `Kill switch is engaged`).
4. Clique em "Disengage" para retomar.
5. Reprodução automatizada: `pytest tests/test_orchestrator.py::test_kill_switch_blocks_new_orders`.

## Stop-loss / take-profit (REPLAY e PAPER_LOCAL)

A cada candle, se houver posição aberta no símbolo, o sistema verifica se a
faixa `[low, high]` do candle tocou o stop-loss ou o take-profit da posição
(`Orchestrator._check_stop_take`, `app/orchestrator.py`):

- Posição `BUY`: stop tocado se `low <= stop_loss`; alvo tocado se
  `high >= take_profit`.
- Posição `SELL`: stop tocado se `high >= stop_loss`; alvo tocado se
  `low <= take_profit`.
- **Regra conservadora**: como não há informação intrabar (sequência exata
  dos preços dentro do candle), se ambos forem tocados no mesmo candle o
  sistema assume que o stop-loss foi atingido primeiro (pior cenário). Isso é
  registrado explicitamente na justificativa do sinal de fechamento gerado.
- O fechamento por stop/take sempre passa por `RiskEngine.evaluate_close()`,
  aplica slippage configurável, gera ordem/execução/taxa persistidas, e
  atualiza a sequência de perdas consecutivas / cooldown como qualquer outro
  fechamento.

## Limitações conhecidas

- A estratégia (cruzamento de médias móveis + filtro ATR) é intencionalmente
  simples e não promete rentabilidade.
- `PAPER_LOCAL` reutiliza a fonte de dados de replay nesta fase; um feed de
  mercado ao vivo somente-leitura fica como extensão futura.
- O wiring completo de `BYBIT_DEMO` usa o cliente oficial `pybit`
  (`app/execution/bybit_pybit_client.py`) para dados de mercado, envio de
  ordens, confirmação de status e consulta de posições, sempre contra hosts
  da allowlist demo/testnet. Testado ponta a ponta com transporte falso em
  `tests/test_bybit_demo_wiring.py` (zero rede); o primeiro uso real ainda
  deve seguir este documento para gerar e configurar credenciais.
- O motor de risco só permite `RISK_MAX_CONCURRENT_POSITIONS=1` por padrão;
  o preenchimento parcial de uma abertura não é automaticamente
  complementado em ticks futuros (fica registrado como posição parcial, sem
  nova tentativa automática de completar o tamanho aprovado).
- Sem garantia de rentabilidade. Ambiente exclusivamente demonstrativo.
