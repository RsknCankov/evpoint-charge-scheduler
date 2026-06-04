"""Pure load-balancer core — the apartment-priority throttle ladder.

Lifted verbatim from ``coordinator.py._async_update_data`` (Block C). Operates
only on resolved scalars carried in ``LoadInputs``; imports nothing from the
Home Assistant framework. ``available_current`` is computed first, copying the
``max(0, total_limit - headroom - int(apartment_current))`` expression
character-for-character (the ``int()`` truncation and ``max(0, ...)`` order are
load-bearing for parity).
"""
from __future__ import annotations

from .const import (
    THROTTLE_APARTMENT_HIGH,
    THROTTLE_BY_APARTMENT,
    THROTTLE_SMART_PAUSE,
    THROTTLE_UNRESTRICTED,
)
from .models import LoadInputs, Output


def balance(i: LoadInputs) -> Output:
    available_current = max(0, i.total_limit - i.headroom - int(i.apartment_current))

    if i.planned_current <= 0:
        dynamic_current = 0
        throttle_reason = THROTTLE_SMART_PAUSE
    elif available_current < i.min_a:
        dynamic_current = 0
        throttle_reason = THROTTLE_APARTMENT_HIGH
    elif available_current < i.planned_current:
        dynamic_current = available_current
        throttle_reason = THROTTLE_BY_APARTMENT
    else:
        dynamic_current = i.planned_current
        throttle_reason = THROTTLE_UNRESTRICTED

    return Output(
        dynamic_current=dynamic_current,
        throttle_reason=throttle_reason,
        available_current=available_current,
    )
