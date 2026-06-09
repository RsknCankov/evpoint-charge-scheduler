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
    END_BACKSTOP,
    END_CONTINUE,
    END_SUCCESS,
    FINISH_MODE_ASAP,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
)
from .models import Decision, EndDecision, EndInputs, PlanInputs


def plan(i: PlanInputs) -> Decision:
    # Decide recommended action. No session = idle: we still compute the
    # plan for the dashboard, but never push to the charger.
    if not i.session_active:
        action = ACTION_IDLE
    elif i.energy_needed <= 0:
        action = ACTION_DONE
    elif i.hours_to_dep <= 0:
        action = ACTION_TOO_LATE
    elif i.finish_mode == FINISH_MODE_ASAP:
        # ASAP: charge immediately at asap_current regardless of tariff; deficit
        # and safety overrides are suppressed — the user asked to charge now.
        action = ACTION_CHARGE_MAX
    elif i.deficit_kwh > 0:
        # Always honour the deficit — applies in every finish mode except ASAP
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
    else:
        # Defensive fallback — all three modes are explicitly matched above;
        # this branch is unreachable with valid finish_mode values.
        action = ACTION_WAIT_FOR_NIGHT

    # Plan-level target current (before load balancing)
    if action == ACTION_CHARGE_MAX and i.finish_mode == FINISH_MODE_ASAP:
        planned_current = i.asap_current
    elif action == ACTION_CHARGE_MAX:
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


def should_end(i: EndInputs) -> EndDecision:
    """Deterministic manual-SoC session-end decision over scalars (SOC-01 / SESS-01).

    Precedence:
      (1) PRIMARY success — ``energy_needed <= 0`` (already at target) OR a power
          sensor whose accumulated ``delivered_energy_kwh >= energy_needed``.
          The comparison uses ``delivered_energy_kwh``, which upstream (03-02)
          ONLY advances on valid reads: an UNAVAILABLE/degenerate read does not
          advance it, so it can never reach ``needed`` and can never manufacture
          a premature success (criterion 2).
      (2) DEPARTURE-PASSED backstop — fires ONLY when a departure_time is set and
          has passed. The ``has_departure_time`` guard is REQUIRED: the
          coordinator passes ``hours_to_departure=0.0`` as a sentinel when no
          departure_time is set, and without this guard the backstop would fire
          immediately on every cycle (firing the undercharged notification with
          ~0 kWh) — a silent regression vs today's ACTION_TOO_LATE-stays-active
          behaviour. This single branch covers BOTH the power-sensor
          "departure passed before target" case and the no-power-sensor
          departure-time hard-stop fallback (D-03).
      (3) CONTINUE — everything else, including the no-departure-time case: the
          session stays active until target is reached or a departure is set.

    Pure and framework-free — no homeassistant import. The coordinator wires real
    values in and acts on the outcome.
    """
    if i.energy_needed <= 0 or (
        i.has_power_sensor and i.delivered_energy_kwh >= i.energy_needed
    ):
        return EndDecision(outcome=END_SUCCESS)
    if i.has_departure_time and i.hours_to_departure <= 0:
        return EndDecision(outcome=END_BACKSTOP)
    return EndDecision(outcome=END_CONTINUE)
