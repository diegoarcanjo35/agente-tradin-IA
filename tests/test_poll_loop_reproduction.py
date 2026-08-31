"""Correção Operacional do Poll Loop v1.0: reprodução do defeito original.

O `_poll_loop` (app/api/main.py, versão pré-correção) era exatamente:

    while True:
        result = orch.tick()
        ...
        await asyncio.sleep(interval)

sem NENHUMA barreira de exceção em volta de `orch.tick()`. Este arquivo
recria esse padrão exato -- congelado aqui, isolado do código de produção
(que já foi substituído por `app/api/poll_engine.py`) -- para provar, de
forma independente, o defeito relatado em operação: uma exceção inesperada
mata a tarefa em segundo plano silenciosamente, sem processar o tick
seguinte (mesmo que ele fosse saudável), enquanto o servidor HTTP -- uma
tarefa asyncio inteiramente separada -- nunca percebe nada e continua
respondendo normalmente.

Este teste NÃO importa nada de app/api/poll_engine.py -- ele não pode
"espelhar a solução nova": prova o comportamento do padrão ANTIGO.
"""
from __future__ import annotations

import asyncio

import pytest


async def _naive_poll_loop_pre_correction(tick_fn, interval: float = 0.001, max_iterations: int = 10):
    """Cópia congelada do padrão de `_poll_loop` anterior à correção --
    NUNCA deve ser usada em produção. Existe apenas para provar o
    defeito."""
    result = None
    for _ in range(max_iterations):
        result = tick_fn()
        await asyncio.sleep(interval)
    return result


@pytest.mark.asyncio
async def test_unexpected_exception_silently_kills_the_old_unprotected_loop():
    """Reprodução exata do incidente: tick() funciona, depois levanta uma
    exceção inesperada, depois voltaria a funcionar -- mas o padrão antigo
    nunca alcança essa terceira chamada saudável."""
    calls: list[int] = []

    def tick_fn():
        calls.append(len(calls))
        if len(calls) == 2:
            raise RuntimeError("falha inesperada simulada (reprodução do incidente)")
        return {"status": "no_new_candle"}

    task = asyncio.create_task(_naive_poll_loop_pre_correction(tick_fn))

    with pytest.raises(RuntimeError):
        await task

    # Processou o 1º tick (saudável), morreu no 2º (a exceção), e NUNCA
    # chegou ao 3º -- que teria sido saudável de novo.
    assert len(calls) == 2
    assert task.done()
    assert task.exception() is not None
    assert isinstance(task.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_server_keeps_responding_while_the_old_loop_is_dead():
    """A parte mais perigosa do incidente: o servidor HTTP é uma tarefa
    asyncio completamente separada -- ela nunca sabe que o motor de
    mercado morreu, e continua respondendo normalmente, dando a falsa
    impressão de que o sistema está saudável."""
    calls: list[int] = []

    def tick_fn():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("falha inesperada simulada")
        return {"status": "no_new_candle"}

    poll_task = asyncio.create_task(_naive_poll_loop_pre_correction(tick_fn))

    async def fake_http_server():
        responses = []
        for _ in range(5):
            await asyncio.sleep(0.001)
            responses.append("200 OK")  # o servidor nunca percebe nada de errado
        return responses

    http_task = asyncio.create_task(fake_http_server())

    responses = await http_task
    assert responses == ["200 OK"] * 5  # "saudável" o tempo todo, do ponto de vista do servidor

    with pytest.raises(RuntimeError):
        await poll_task
    assert len(calls) == 1  # o motor de mercado morreu na primeira falha e nunca mais tickou
