# Roteiro de Teste Manual no Bybit Demo Trading

**Este documento é apenas um roteiro. Nenhum passo aqui é executado
automaticamente por este agente ou por qualquer teste da suíte.** Testar
contra o ambiente real Bybit Demo Trading — mesmo sendo dinheiro fictício —
exige rede real e credenciais reais, e por isso só pode ser feito depois de
autorização explícita e separada do Diego, em uma mensagem posterior a este
roteiro. Nenhuma correção ou fase deste projeto autoriza, por si só, a
execução deste roteiro.

## Pré-requisitos

- Suíte completa de testes com fakes passando (zero rede real).
- Leitura de `docs/SEGURANCA.md` e `docs/OPERACAO_DEMO.md` completa.
- Autorização explícita do Diego para o teste manual, especificamente.

## Passos

1. **Criar uma chave de API exclusivamente Demo, sem permissão de saque.**
   Acesse o ambiente Bybit Demo Trading (nunca a conta principal/produção).
   Marque apenas as permissões "Read" e "Trade". **Nunca** habilite
   "Withdraw". Se o plano permitir, restrinja a chave por IP.

2. **Confirmar visualmente o host Demo.** Antes de colar a chave em
   qualquer lugar, confirme que `BYBIT_BASE_URL`/`BYBIT_WS_URL` no `.env`
   continuam exatamente `api-demo.bybit.com`/`stream-demo.bybit.com` — a
   aplicação recusa iniciar com qualquer outro host
   (`app/core/config.py::assert_demo_host`), mas confirme manualmente
   antes de prosseguir mesmo assim.

3. **Confirmar saldo exclusivamente fictício.** No painel da própria
   Bybit (fora desta aplicação), confirme que a conta Demo mostra saldo
   fictício, nunca saldo real.

4. **Configurar quantidade mínima e limites conservadores.** Ajuste
   `RISK_MAX_POSITION_USD`, `RISK_MAX_TOTAL_EXPOSURE_USD` e
   `RISK_MAX_CONCURRENT_POSITIONS=1` no `.env` para os menores valores que
   a Bybit Demo aceitar para o símbolo escolhido — o objetivo é uma única
   ordem de teste mínima, não uma simulação de operação real.

5. **Iniciar em `OBSERVANDO`.** Suba o processo (`python -m app.run`) com
   `MODE=BYBIT_DEMO`. Confirme no painel que `ESTADO OPERACIONAL` mostra
   `OBSERVANDO` — **nunca** `ATIVO` automaticamente (item 7.8). Não clique
   em "Ativar novas entradas" ainda.

6. **Reconciliação inicial.** Confirme no painel ("Estado Operacional e
   Causas de Bloqueio") que `Última reconciliação` está preenchida e que
   nenhuma causa de bloqueio está marcada `SIM`. Se `Reconciliação
   divergente` ou qualquer outra causa aparecer, **pare aqui** e investigue
   antes de continuar — não force ativação.

7. **Uma única ordem de teste.** Só agora clique em "Ativar novas
   entradas" (confirmação explícita é exigida pelo próprio painel).
   Aguarde a estratégia gerar e a ordem ser aprovada e enviada
   naturalmente — não force uma ordem fora do fluxo normal do sistema.
   Assim que uma ordem for enviada, clique em "Pausar novas entradas"
   imediatamente para impedir uma segunda ordem automática.

8. **Confirmar fill, posição e taxa.** No painel ("Ordens e Máquina de
   Estados"), confirme que a ordem chegou a `FILLED` ou
   `PARTIALLY_FILLED`, com `Qtd Preenchida`/`Preço Médio` reais. Confirme
   a posição aberta correspondente e a taxa cobrada em "Custos e
   Slippage".

9. **Cancelamento ou fechamento controlado.** Se a posição ainda estiver
   aberta, force o fechamento manualmente (aguarde o stop/take-profit ou
   ative o kill switch, que cancela pendências e permite fechamento
   controlado) em vez de deixá-la correndo sem supervisão.

10. **Reconciliação final.** Após o fechamento, aguarde o próximo ciclo de
    reconciliação (ou force reiniciando o processo) e confirme no painel
    que nenhuma causa de bloqueio ficou ativa.

11. **Ativação do kill switch.** Clique em "Ativar bloqueio de emergência"
    e confirme que `OPERAÇÕES: BLOQUEADAS` aparece imediatamente e que
    qualquer ordem pendente foi cancelada (campo `ordens_canceladas` na
    resposta).

12. **Remoção da chave do ambiente após o teste.** Ao final, remova
    `BYBIT_API_KEY`/`BYBIT_API_SECRET` do `.env` (ou revogue a chave no
    painel da Bybit). Nunca deixe uma chave Demo válida configurada além
    do necessário para o teste.

## Em caso de qualquer comportamento inesperado

Pare imediatamente, ative o kill switch, capture os logs
(`LOG_DIR`) e o conteúdo das tabelas `security_events`/
`failures_reconciliations`/`order_events` relevantes, e reporte antes de
tentar novamente.
