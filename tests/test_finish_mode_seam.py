"""Pure finish-mode matrix over plan() — proving MODE-02/03/04 on the seam.

These are homeassistant-free unit tests: ``plan(PlanInputs) -> Decision`` imports
only ``.const`` and ``.models``, so the matrix runs with no hass fixture and no
clock. It parametrises ``(finish_mode x is_night_now x deficit x slack)`` to pin:

* MODE-02 — ``departure`` NEVER emits ``wait_for_night`` (the impossible state
  the Phase-1 "lost night" bug produced); it gentles when due, else waits for
  its start time.
* MODE-03 — ``end_of_night`` waits for night when it is day, gentles in night.
* MODE-04 — ``asap`` charges immediately at asap_current regardless of tariff;
  deficit and safety overrides are bypassed (ASAP-01, ASAP-03, D-01).
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
    ACTION_DONE,
    ACTION_WAIT_FOR_NIGHT,
    ACTION_WAIT_FOR_START_TIME,
    FINISH_MODE_ASAP,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
    FINISH_MODES,
)
from custom_components.evpoint_charge_scheduler.models import PlanInputs
from custom_components.evpoint_charge_scheduler.planner import plan

# Modes for which deficit and safety overrides apply (ASAP bypasses them per D-01)
_NON_ASAP_MODES = [m for m in FINISH_MODES if m != FINISH_MODE_ASAP]


def _inputs(
    *,
    finish_mode: str,
    is_night_now: bool = False,
    deficit_kwh: float = 0.0,
    slack: float = 13.0,
    gentle_should_start: bool = True,
    safety_margin: float = 0.5,
    asap_current: int = 0,
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
        asap_current=asap_current,
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


# --- MODE-04: asap charges immediately regardless of tariff (ASAP-01) -------


@pytest.mark.parametrize("is_night_now", [True, False])
def test_asap_charges_immediately_regardless_of_tariff(is_night_now: bool) -> None:
    """ASAP charges now — tariff state is irrelevant (ASAP-01, D-02)."""
    d = plan(_inputs(finish_mode=FINISH_MODE_ASAP, is_night_now=is_night_now))
    assert d.action == ACTION_CHARGE_MAX
    assert d.action != ACTION_WAIT_FOR_NIGHT


def test_asap_uses_asap_current_not_max_a() -> None:
    """planned_current == asap_current when asap_current != max_a (D-02, ASAP-03)."""
    d = plan(_inputs(finish_mode=FINISH_MODE_ASAP, asap_current=12))
    assert d.action == ACTION_CHARGE_MAX
    assert d.planned_current == 12   # asap_current, not max_a (16)


def test_asap_done_when_energy_needed_zero() -> None:
    """ASAP auto-ends when energy_needed <= 0 — done guard precedes ASAP branch (ASAP-04)."""
    inputs = PlanInputs(
        session_active=True,
        energy_needed=0.0,
        hours_to_dep=18.0,
        deficit_kwh=0.0,
        slack=13.0,
        safety_margin=0.5,
        finish_mode=FINISH_MODE_ASAP,
        is_night_now=True,
        gentle_should_start=True,
        gentle_current=10,
        day_current=8,
        max_a=16,
        asap_current=12,
    )
    d = plan(inputs)
    assert d.action == ACTION_DONE


# --- Override precedence: deficit/safety win for non-ASAP modes (D-01) ------
# ASAP bypasses both overrides — the user explicitly asked to charge now.
# Separate ASAP-specific assertions are in the MODE-04 section above.


@pytest.mark.parametrize("finish_mode", _NON_ASAP_MODES)
@pytest.mark.parametrize("is_night_now", [True, False])
def test_deficit_overrides_non_asap_modes(finish_mode: str, is_night_now: bool) -> None:
    d = plan(
        _inputs(finish_mode=finish_mode, is_night_now=is_night_now, deficit_kwh=5.0)
    )
    assert d.action == ACTION_CHARGE_DAY_SUPPLEMENT
    assert d.planned_current == 8  # day_current


@pytest.mark.parametrize("finish_mode", _NON_ASAP_MODES)
@pytest.mark.parametrize("is_night_now", [True, False])
def test_safety_override_engages_non_asap_modes(
    finish_mode: str, is_night_now: bool
) -> None:
    # slack below safety_margin and no deficit -> charge_max regardless of (non-ASAP) mode
    d = plan(
        _inputs(
            finish_mode=finish_mode,
            is_night_now=is_night_now,
            slack=0.1,
            safety_margin=0.5,
        )
    )
    assert d.action == ACTION_CHARGE_MAX
    assert d.planned_current == 16  # max_a (safety override uses max_a, not asap_current)


@pytest.mark.parametrize("is_night_now", [True, False])
def test_asap_deficit_bypassed(is_night_now: bool) -> None:
    """ASAP with deficit still gives ACTION_CHARGE_MAX, not CHARGE_DAY_SUPPLEMENT (D-01)."""
    d = plan(
        _inputs(finish_mode=FINISH_MODE_ASAP, is_night_now=is_night_now, deficit_kwh=5.0)
    )
    assert d.action == ACTION_CHARGE_MAX


@pytest.mark.parametrize("is_night_now", [True, False])
def test_asap_safety_override_bypassed(is_night_now: bool) -> None:
    """ASAP with tight slack still gives ACTION_CHARGE_MAX at asap_current (D-01)."""
    d = plan(
        _inputs(
            finish_mode=FINISH_MODE_ASAP,
            is_night_now=is_night_now,
            slack=0.1,
            safety_margin=0.5,
            asap_current=12,
        )
    )
    assert d.action == ACTION_CHARGE_MAX
    assert d.planned_current == 12  # asap_current, not max_a via safety override


# --- Honor direction: with slack, the SELECTED mode is honored (TEST-01) -----
#
# The override tests above pin the OVERRIDE direction (deficit/safety bypass the
# mode). This block pins the complementary HONOR direction across ALL three modes
# in one parametrization: when deficit_kwh == 0 AND slack >= safety_margin, the
# action is the mode-specific expectation — proving the selected mode is honored
# when there is genuine slack (the Phase-1 "lost night" bug direction). The
# single-mode tests above assert each mode individually; this consolidates the
# both-directions contract into one all-modes case so the honor direction is an
# explicit peer of test_deficit_overrides_every_mode / test_safety_override_*.


@pytest.mark.parametrize(
    ("finish_mode", "is_night_now", "expected_action"),
    [
        # departure: tariff-irrelevant; gentles when due, NEVER waits for night.
        (FINISH_MODE_DEPARTURE, True, ACTION_CHARGE_GENTLE),
        (FINISH_MODE_DEPARTURE, False, ACTION_CHARGE_GENTLE),
        # end_of_night: waits for night in day, gentles in night.
        (FINISH_MODE_END_OF_NIGHT, False, ACTION_WAIT_FOR_NIGHT),
        (FINISH_MODE_END_OF_NIGHT, True, ACTION_CHARGE_GENTLE),
        # asap: charges immediately regardless of tariff (ASAP-01).
        (FINISH_MODE_ASAP, True, ACTION_CHARGE_MAX),
        (FINISH_MODE_ASAP, False, ACTION_CHARGE_MAX),
    ],
)
def test_mode_honored_when_slack_and_no_deficit(
    finish_mode: str, is_night_now: bool, expected_action: str
) -> None:
    """deficit==0 & slack>=safety_margin -> the mode-specific action is honored."""
    d = plan(
        _inputs(
            finish_mode=finish_mode,
            is_night_now=is_night_now,
            deficit_kwh=0.0,
            slack=13.0,  # >> safety_margin 0.5
            safety_margin=0.5,
            gentle_should_start=True,
        )
    )
    assert d.action == expected_action
    # departure NEVER emits wait_for_night — the impossible "lost night" state.
    if finish_mode == FINISH_MODE_DEPARTURE:
        assert d.action != ACTION_WAIT_FOR_NIGHT
    # asap NEVER waits for night — it charges immediately (ASAP-01).
    if finish_mode == FINISH_MODE_ASAP:
        assert d.action != ACTION_WAIT_FOR_NIGHT


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


# --- NIGHT-04/05: Night Only cost-spread and no-departure guard (Phase 06) ---


def test_end_of_night_gentle_fires_with_cost_spread_active() -> None:
    # NIGHT-04: planner produces CHARGE_GENTLE in Night Only when gentle window is active
    d = plan(_inputs(finish_mode=FINISH_MODE_END_OF_NIGHT, is_night_now=True, gentle_should_start=True))
    assert d.action == ACTION_CHARGE_GENTLE


def test_end_of_night_no_departure_still_gentles() -> None:
    # NIGHT-05: Night Only with small slack but gentle window due still produces CHARGE_GENTLE
    d = plan(_inputs(finish_mode=FINISH_MODE_END_OF_NIGHT, is_night_now=True, gentle_should_start=True, slack=2.0))
    assert d.action == ACTION_CHARGE_GENTLE
    assert d.executed_finish_mode == FINISH_MODE_END_OF_NIGHT


def test_end_of_night_waits_for_night_in_day_regression() -> None:
    # Regression: end-of-night still waits for night in day after Phase 06 guard change
    d = plan(_inputs(finish_mode=FINISH_MODE_END_OF_NIGHT, is_night_now=False))
    assert d.action == ACTION_WAIT_FOR_NIGHT
