"""Correção Operacional do Poll Loop v1.2, Bloqueio 2: reprodução auditada
-- `Settings(bybit_poll_interval_seconds=-1)` e
`Settings(replay_poll_interval_seconds=-1)` eram aceitos sem erro. Um
intervalo zero/negativo elimina a pausa entre ciclos do laço principal,
podendo bombardear CPU/API sem limite.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.parametrize("field", ["bybit_poll_interval_seconds", "replay_poll_interval_seconds"])
@pytest.mark.parametrize("bad_value", [0.0, -1.0, -0.001])
def test_reproduced_defect_negative_or_zero_poll_interval_is_now_rejected(field, bad_value):
    """Reprodução exata do defeito auditado: valores que antes eram
    aceitos silenciosamente agora levantam erro na construção."""
    with pytest.raises(Exception) as excinfo:
        Settings(**{field: bad_value}, database_url="sqlite:///:memory:")
    assert field.upper() in str(excinfo.value)


@pytest.mark.parametrize("field", ["bybit_poll_interval_seconds", "replay_poll_interval_seconds"])
@pytest.mark.parametrize("good_value", [0.001, 0.02, 5.0])
def test_small_positive_poll_intervals_remain_valid(field, good_value):
    """Preserva valores positivos pequenos, já usados extensivamente pela
    suíte de testes (ex.: replay_poll_interval_seconds=0.02 no padrão real
    de produção)."""
    settings = Settings(**{field: good_value}, database_url="sqlite:///:memory:")
    assert getattr(settings, field) == good_value


@pytest.mark.parametrize("field", ["reconciliation_max_delay_seconds", "partial_fill_timeout_seconds"])
@pytest.mark.parametrize("bad_value", [0.0, -1.0])
def test_other_reviewed_thresholds_reject_non_positive_values(field, bad_value):
    """Limites (não pausas de laço, mas ainda assim sem semântica segura
    em zero/negativo) revisados pela mesma correção."""
    with pytest.raises(Exception):
        Settings(**{field: bad_value}, database_url="sqlite:///:memory:")


@pytest.mark.parametrize("field,value", [
    ("reconciliation_interval_seconds", 0.0),
    ("open_order_poll_interval_seconds", 0.0),
    ("funding_poll_interval_seconds", 0.0),
])
def test_debounce_only_intervals_deliberately_still_accept_zero(field, value):
    """Diferente dos intervalos de PAUSA do laço principal, estes três são
    debounces de um ciclo já pausado por
    bybit_poll_interval_seconds/replay_poll_interval_seconds -- `0.0`
    ("sempre devido") é um valor deliberado e usado extensivamente pela
    suíte de testes; esta correção não pode quebrar esse padrão
    estabelecido."""
    settings = Settings(**{field: value}, database_url="sqlite:///:memory:")
    assert getattr(settings, field) == value


def test_negative_poll_interval_error_message_is_in_portuguese_and_names_the_field():
    with pytest.raises(Exception) as excinfo:
        Settings(bybit_poll_interval_seconds=-2.0, database_url="sqlite:///:memory:")
    message = str(excinfo.value)
    assert "BYBIT_POLL_INTERVAL_SECONDS" in message
    assert "positivo" in message.lower()
