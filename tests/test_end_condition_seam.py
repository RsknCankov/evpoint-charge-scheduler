"""Pure end-condition matrix over should_end() — proving SOC-01/SESS-01 on the seam.

``should_end(EndInputs) -> EndDecision`` imports only ``.const`` and ``.models``,
so the matrix runs with no hass fixture and no clock. It pins the deterministic
manual-SoC end decision the coordinator wires real values into:

* PRIMARY success stop — ``delivered >= needed`` (or ``needed <= 0``) -> END_SUCCESS;
* DEPARTURE-PASSED backstop — ``has_departure_time and hours_to_departure <= 0`` ->
  END_BACKSTOP (covers both the power-sensor "departure passed before target" and
  the no-power-sensor departure-time hard-stop fallback, D-03);
* NO-DEPARTURE GUARD — ``has_departure_time=False`` with the coordinator's
  ``hours_to_departure=0.0`` sentinel -> END_CONTINUE, NEVER END_BACKSTOP (no
  silent early end vs today's ACTION_TOO_LATE-stays-active behaviour);
* CRITERION 2 — an undercharged session with time remaining stays END_CONTINUE;
  success is never manufactured from a non-advancing (UNAVAILABLE) read because
  the comparison requires ``delivered >= needed`` which such a read cannot reach.

No ``@pytest.mark.asyncio``: these are plain sync unit tests.
"""

from __future__ import annotations

import pytest

from custom_components.evpoint_charge_scheduler.const import (
    END_BACKSTOP,
    END_CONTINUE,
    END_SUCCESS,
)
from custom_components.evpoint_charge_scheduler.models import EndInputs
from custom_components.evpoint_charge_scheduler.planner import should_end


def _decide(
    *,
    delivered: float,
    needed: float,
    hours_to_dep: float,
    has_power_sensor: bool,
    has_departure_time: bool,
) -> str:
    return should_end(
        EndInputs(
            delivered_energy_kwh=delivered,
            energy_needed=needed,
            hours_to_departure=hours_to_dep,
            has_power_sensor=has_power_sensor,
            has_departure_time=has_departure_time,
        )
    ).outcome


@pytest.mark.parametrize(
    "delivered,needed,hours_to_dep,has_power_sensor,has_departure_time,expected",
    [
        # PRIMARY success: delivered crosses needed.
        (27.0, 26.4, 5.0, True, True, END_SUCCESS),
        # Not yet at target, departure not passed -> continue.
        (10.0, 26.4, 5.0, True, True, END_CONTINUE),
        # Departure passed before target reached -> backstop.
        (10.0, 26.4, 0.0, True, True, END_BACKSTOP),
        # No power sensor, time remaining -> continue (no energy counting).
        (0.0, 26.4, 5.0, False, True, END_CONTINUE),
        # No power sensor, departure passed -> departure-time hard stop (D-03).
        (0.0, 26.4, 0.0, False, True, END_BACKSTOP),
        # NO-DEPARTURE GUARD (power sensor): sentinel 0.0 must NOT backstop.
        (0.0, 26.4, 0.0, True, False, END_CONTINUE),
        # NO-DEPARTURE GUARD (no power sensor): sentinel 0.0 must NOT backstop.
        (0.0, 26.4, 0.0, False, False, END_CONTINUE),
        # Already at target (needed <= 0) -> success, preserves done semantics.
        (0.0, 0.0, 5.0, True, True, END_SUCCESS),
        (0.0, -1.0, 5.0, False, True, END_SUCCESS),
    ],
)
def test_should_end_matrix(
    delivered,
    needed,
    hours_to_dep,
    has_power_sensor,
    has_departure_time,
    expected,
) -> None:
    assert (
        _decide(
            delivered=delivered,
            needed=needed,
            hours_to_dep=hours_to_dep,
            has_power_sensor=has_power_sensor,
            has_departure_time=has_departure_time,
        )
        == expected
    )


def test_criterion_2_never_premature_success() -> None:
    """An undercharged session with time left never ends; with departure passed it backstops.

    Mirrors the runtime guard: an UNAVAILABLE read does not advance ``delivered``
    upstream (03-02), so ``delivered`` stays below ``needed`` and should_end can
    only return CONTINUE (time remaining) or BACKSTOP (departure passed) — never
    a manufactured SUCCESS.
    """
    # Time remaining, undercharged -> continue (never premature success).
    assert (
        _decide(
            delivered=5.0,
            needed=26.4,
            hours_to_dep=3.0,
            has_power_sensor=True,
            has_departure_time=True,
        )
        == END_CONTINUE
    )
    # Departure passed, still undercharged -> backstop, not success.
    assert (
        _decide(
            delivered=5.0,
            needed=26.4,
            hours_to_dep=0.0,
            has_power_sensor=True,
            has_departure_time=True,
        )
        == END_BACKSTOP
    )


def test_no_departure_guard_never_backstops() -> None:
    """has_departure_time=False with the 0.0 sentinel stays CONTINUE either way."""
    for has_power_sensor in (True, False):
        assert (
            _decide(
                delivered=0.0,
                needed=26.4,
                hours_to_dep=0.0,
                has_power_sensor=has_power_sensor,
                has_departure_time=False,
            )
            == END_CONTINUE
        )
