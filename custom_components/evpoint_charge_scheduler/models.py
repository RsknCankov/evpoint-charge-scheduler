"""Frozen dataclasses for the pure planning/load-balancing seam.

These are the immutable input/output contracts crossing the boundary between the
Home Assistant ``coordinator`` shell (which does all I/O, clock reads, and
``coordinator.data`` assembly) and the pure decision core (``planner.plan`` and
``load_balancer.balance``). They carry only resolved scalars — never a Home
Assistant runtime object, an entity id, or a clock — so the core stays free of
external framework imports and trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanInputs:
    session_active: bool
    energy_needed: float
    hours_to_dep: float
    deficit_kwh: float
    slack: float
    safety_margin: float
    finish_mode: str
    is_night_now: bool
    gentle_should_start: bool
    gentle_current: int
    day_current: int
    max_a: int


@dataclass(frozen=True)
class Decision:
    action: str
    planned_current: int
    executed_finish_mode: str


@dataclass(frozen=True)
class LoadInputs:
    planned_current: int
    total_limit: int
    headroom: int
    apartment_current: float
    min_a: int


@dataclass(frozen=True)
class Output:
    dynamic_current: int
    throttle_reason: str
    available_current: int
