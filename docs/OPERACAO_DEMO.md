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
.venv/Scripts/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8034
```

Abra `http://127.0.0.1:8034`. O sistema roda inteiramente sobre
`fixtures/replay_btcusdt.json`, sem qualquer chamada de rede.

## Modos disponíveis

- `REPLAY` (padrão): dados de um arquivo local, execução simulada, zero rede.
- `PAPER_LOCAL`: mesma fonte de dados de `REPLAY` nesta fase (pode ser trocada
  por um feed ao vivo somente-leitura no futuro), execução simulada local
  com taxa e slippage configuráveis, sem enviar ordens à Bybit.
- `BYBIT_DEMO`: dados e ordens reais contra o ambiente Demo/Testnet oficial.
  Exige `BYBIT_API_KEY`/`BYBIT_API_SECRET` e recusa iniciar se o host
  configurado não estiver na allowlist demo/testnet.

Defina `MODE=` no `.env` (ou variável de ambiente) e reinicie o processo —
não há troca de modo em tempo de execução.

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
