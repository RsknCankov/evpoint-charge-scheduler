"""Pure decision core — the 8-branch decision tree + planned-current selection.

Lifted verbatim from ``coordinator.py._async_update_data`` (Blocks A+B). Operates
only on resolved scalars carried in ``PlanInputs``; imports nothing from the
Home Assistant framework. The precedence ladder (session -> done -> too_late -> deficit
-> safety -> mode, with ASAP as the bare ``else``) is preserved exactly.
"""
from __future__ import annotations

from .const import (
    ACTION_CHARGE_DAY_SUPPLEMENT,
    ACTION_CHARGE_GENTLE,
    ACTION_CHARGE_MAX,
    ACTION_DONE,
    ACTION_IDLE,
    ACTION_TOO_LATE,
    ACTION_WAIT_FOR_NIGHT,
    ACTION_WAIT_FOR_START_TIME,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
)
from .models import Decision, PlanInputs


def plan(i: PlanInputs) -> Decision:
    # Decide recommended action. No session = idle: we still compute the
    # plan for the dashboard, but never push to the charger.
    if not i.session_active:
        action = ACTION_IDLE
    elif i.energy_needed <= 0:
        action = ACTION_DONE
    elif i.hours_to_dep <= 0:
        action = ACTION_TOO_LATE
    elif i.deficit_kwh > 0:
        # Always honour the deficit — applies in every finish mode
        action = ACTION_CHARGE_DAY_SUPPLEMENT
    elif i.slack < i.safety_margin:
        # Safety: too tight to wait around
        action = ACTION_CHARGE_MAX
    elif i.finish_mode == FINISH_MODE_DEPARTURE:
        # Tariff irrelevant; spread gently up to the departure deadline.
        if i.gentle_should_start:
            action = ACTION_CHARGE_GENTLE
        else:
            action = ACTION_WAIT_FOR_START_TIME
    elif i.finish_mode == FINISH_MODE_END_OF_NIGHT:
        # Wait for night, then charge gently across the rest of the night
        # window so it finishes around night-end instead of bursting at max.
        if not i.is_night_now:
            action = ACTION_WAIT_FOR_NIGHT
        elif i.gentle_should_start:
            action = ACTION_CHARGE_GENTLE
        else:
            action = ACTION_WAIT_FOR_START_TIME
    else:  # FINISH_MODE_ASAP — burst at max as soon as tariff allows
        if i.is_night_now:
            action = ACTION_CHARGE_MAX
        else:
            action = ACTION_WAIT_FOR_NIGHT

    # Plan-level target current (before load balancing)
    if action == ACTION_CHARGE_MAX:
        planned_current = i.max_a
    elif action == ACTION_CHARGE_GENTLE:
        planned_current = i.gentle_current
    elif action == ACTION_CHARGE_DAY_SUPPLEMENT:
        planned_current = i.day_current
    else:
        planned_current = 0

    # executed_finish_mode = the mode the decision actually branched on. The
    # dashboard read-back sources finish_mode from here (via the coordinator)
    # so it can never contradict the recommended action — both come from this
    # one Decision.
    return Decision(
        action=action,
        planned_current=planned_current,
        executed_finish_mode=i.finish_mode,
    )
