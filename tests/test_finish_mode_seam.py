"""Pure finish-mode matrix over plan() — proving MODE-02/03/04 on the seam.

These are homeassistant-free unit tests: ``plan(PlanInputs) -> Decision`` imports
only ``.const`` and ``.models``, so the matrix runs with no hass fixture and no
clock. It parametrises ``(finish_mode x is_night_now x deficit x slack)`` to pin:

* MODE-02 — ``departure`` NEVER emits ``wait_for_night`` (the impossible state
  the Phase-1 "lost night" bug produced); it gentles when due, else waits for
  its start time.
* MODE-03 — ``end_of_night`` waits for night when it is day, gentles in night.
* MODE-04 — ``asap`` bursts at max in night, waits for night in day; and the
  deficit / safety overrides engage regardless of mode when the night window is
  too short.
* The read-back contract — ``Decision.executed_finish_mode`` equals the mode the
  decision actually branched on, in every case.

No ``@pytest.mark.asyncio``: these are plain sync unit tests.
"""

from __future__ import annotations

import pytest

from custom_components.evpoint_charge_scheduler.const import (
    ACTION_CHARGE_DAY_SUPPLEMENT,
    ACTION_CHARGE_GENTLE,
    ACTION_CHARGE_MAX,
    ACTION_WAIT_FOR_NIGHT,
    ACTION_WAIT_FOR_START_TIME,
    FINISH_MODE_ASAP,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
    FINISH_MODES,
)
from custom_components.evpoint_charge_scheduler.models import PlanInputs
from custom_components.evpoint_charge_scheduler.planner import plan


def _inputs(
    *,
    finish_mode: str,
    is_night_now: bool = False,
    deficit_kwh: float = 0.0,
    slack: float = 13.0,
    gentle_should_start: bool = True,
    safety_margin: float = 0.5,
) -> PlanInputs:
    """A no-deficit, ample-slack PlanInputs unless overridden.

    energy_needed > 0 and hours_to_dep > 0 so the pre-mode guards (idle / done /
    too_late) never fire; the mode branch is what we are testing.
    """
    return PlanInputs(
        session_active=True,
        energy_needed=26.4,
        hours_to_dep=18.0,
        deficit_kwh=deficit_kwh,
        slack=slack,
        safety_margin=safety_margin,
        finish_mode=finish_mode,
        is_night_now=is_night_now,
        gentle_should_start=gentle_should_start,
        gentle_current=10,
        day_current=8,
        max_a=16,
    )


# --- MODE-02: departure NEVER waits for night --------------------------------


@pytest.mark.parametrize("is_night_now", [True, False])
def test_departure_never_waits_for_night_when_due(is_night_now: bool) -> None:
    d = plan(_inputs(finish_mode=FINISH_MODE_DEPARTURE, is_night_now=is_night_now))
    assert d.action == ACTION_CHARGE_GENTLE
    assert d.action != ACTION_WAIT_FOR_NIGHT


@pytest.mark.parametrize("is_night_now", [True, False])
def test_departure_waits_for_start_time_when_not_due(is_night_now: bool) -> None:
    d = plan(
        _inputs(
            finish_mode=FINISH_MODE_DEPARTURE,
            is_night_now=is_night_now,
            gentle_should_start=False,
        )
    )
    assert d.action == ACTION_WAIT_FOR_START_TIME
    assert d.action != ACTION_WAIT_FOR_NIGHT


# --- MODE-03: end_of_night branches on the live tariff -----------------------


def test_end_of_night_gentles_in_night() -> None:
    d = plan(_inputs(finish_mode=FINISH_MODE_END_OF_NIGHT, is_night_now=True))
    assert d.action == ACTION_CHARGE_GENTLE


def test_end_of_night_waits_for_night_in_day() -> None:
    d = plan(_inputs(finish_mode=FINISH_MODE_END_OF_NIGHT, is_night_now=False))
    assert d.action == ACTION_WAIT_FOR_NIGHT


def test_end_of_night_waits_for_start_time_too_early_in_night() -> None:
    d = plan(
        _inputs(
            finish_mode=FINISH_MODE_END_OF_NIGHT,
            is_night_now=True,
            gentle_should_start=False,
        )
    )
    assert d.action == ACTION_WAIT_FOR_START_TIME


# --- MODE-04: asap bursts at night, waits in day -----------------------------


def test_asap_bursts_at_max_in_night() -> None:
    d = plan(_inputs(finish_mode=FINISH_MODE_ASAP, is_night_now=True))
    assert d.action == ACTION_CHARGE_MAX
    assert d.planned_current == 16  # max_a


def test_asap_waits_for_night_in_day() -> None:
    d = plan(_inputs(finish_mode=FINISH_MODE_ASAP, is_night_now=False))
    assert d.action == ACTION_WAIT_FOR_NIGHT


# --- Override precedence: deficit/safety win regardless of mode --------------


@pytest.mark.parametrize("finish_mode", FINISH_MODES)
@pytest.mark.parametrize("is_night_now", [True, False])
def test_deficit_overrides_every_mode(finish_mode: str, is_night_now: bool) -> None:
    d = plan(
        _inputs(finish_mode=finish_mode, is_night_now=is_night_now, deficit_kwh=5.0)
    )
    assert d.action == ACTION_CHARGE_DAY_SUPPLEMENT
    assert d.planned_current == 8  # day_current


@pytest.mark.parametrize("finish_mode", FINISH_MODES)
@pytest.mark.parametrize("is_night_now", [True, False])
def test_safety_override_engages_every_mode(
    finish_mode: str, is_night_now: bool
) -> None:
    # slack below safety_margin and no deficit -> charge_max regardless of mode
    d = plan(
        _inputs(
            finish_mode=finish_mode,
            is_night_now=is_night_now,
            slack=0.1,
            safety_margin=0.5,
        )
    )
    assert d.action == ACTION_CHARGE_MAX
    assert d.planned_current == 16  # max_a


# --- Read-back contract: executed_finish_mode == branched mode ---------------


@pytest.mark.parametrize("finish_mode", FINISH_MODES)
@pytest.mark.parametrize("is_night_now", [True, False])
@pytest.mark.parametrize("deficit_kwh", [0.0, 5.0])
@pytest.mark.parametrize("slack", [0.1, 13.0])
def test_executed_finish_mode_always_equals_input(
    finish_mode: str, is_night_now: bool, deficit_kwh: float, slack: float
) -> None:
    d = plan(
        _inputs(
            finish_mode=finish_mode,
            is_night_now=is_night_now,
            deficit_kwh=deficit_kwh,
            slack=slack,
        )
    )
    assert d.executed_finish_mode == finish_mode
