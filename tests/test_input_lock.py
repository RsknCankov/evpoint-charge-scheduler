"""Input-lock feedback tests — the UX-01 / SESS-03 guard on the real entities.

Proves the silent-revert defect is replaced by *visible* feedback across all six
writable entities (D-06 / TEST-02): editing any input while a session is active
raises a ``HomeAssistantError`` (surfaced as a UI toast). Two lock flavours,
both established in Phase 3 and pinned here:

* **raise-and-revert** (``battery_capacity``, ``target_soc``, ``current_soc``,
  ``cost_tolerance``, ``departure_time``) — the write method snaps the UI back to
  the running value and does NOT call the coordinator setter, so the coordinator
  attribute is UNCHANGED after a locked edit.
* **raise-and-allow** (``finish_mode``) — the pick reaches the coordinator FIRST
  (so the running session honours the user's intent), THEN raises. The "lost
  night" input must never silently vanish.

Each entity also has an edit-while-idle case that succeeds with NO error and
updates the coordinator. Plus session start/stop/auto-end lock transitions and
the unguarded restart-restore resume (SESS-03) on the hass fixture.

These exercise the production write paths
(``number.async_set_native_value`` / ``datetime.async_set_value`` /
``select.async_select_option``) through the service registry under
``session_active=True`` — no live HA, frozen clock, deterministic. The single
``coordinator.inputs_locked`` predicate is the one source of truth all writable
entities consult.

No ``@pytest.mark.asyncio`` decorator: ``asyncio_mode = auto`` runs every
``async def test_*`` automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import (
    CONF_PRICE_SENSOR,
    DOMAIN,
)

FROZEN = "2026-06-04T14:00:00+03:00"
PRICE_ENTITY = "sensor.electricity_price"


def _make_entry(*, with_price_sensor: bool = False) -> MockConfigEntry:
    """Minimal advisory-mode config, mirroring tests/test_planner_baseline.py.

    No SoC sensor is ever configured, so the manual ``current_soc`` number is
    always created. A price sensor is added only when ``with_price_sensor`` so
    the ``cost_tolerance`` number exists for that entity's matrix row (mirrors
    the conditional-data idiom in tests/test_session_end.py).
    """
    data: dict[str, Any] = {
        "voltage": 230,
        "phases": 3,
        "min_current": 6,
        "max_current": 16,
        "total_current_limit": 32,
        "safety_headroom": 0,
        "night_start": "23:00",
        "night_end": "07:00",
        "battery_capacity": 60,
        "charging_loss": 1.1,
        "safety_margin_hours": 0.5,
    }
    if with_price_sensor:
        data[CONF_PRICE_SENSOR] = PRICE_ENTITY
    return MockConfigEntry(domain=DOMAIN, data=data)


async def _setup(hass: HomeAssistant, *, with_price_sensor: bool = False):
    """Pin Europe/Sofia, set up the entry, return the coordinator."""
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))
    entry = _make_entry(with_price_sensor=with_price_sensor)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return entry, coordinator


def _seed_running(coordinator) -> None:
    """Put the coordinator in a lingering-session, ample-slack state."""
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)


# --------------------------------------------------------------------------- #
# The six-entity matrix.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Case:
    """One writable entity's matrix row."""

    entity_id: str  # human label / pytest id
    platform: str  # number / datetime / select
    service: str  # set_value / set_value / select_option
    suffix: str  # unique_id suffix after entry_id
    attr: str  # coordinator attribute read back
    flavour: str  # "revert" or "allow"
    with_price_sensor: bool
    # given the current running attr value, build (active_payload, new_value)
    payload: Callable[[Any], tuple[dict[str, Any], Any]]


def _number_payload(running: Any) -> tuple[dict[str, Any], Any]:
    """A new numeric value distinct from the running one."""
    new = float(running) + 5.0 if running is not None else 25.0
    return {"value": new}, new


def _departure_payload(running: datetime) -> tuple[dict[str, Any], datetime]:
    new = running + timedelta(hours=1)
    # HA's datetime.set_value accepts an ISO string or datetime; pass datetime.
    return {"datetime": new}, new


def _finish_payload(_running: Any) -> tuple[dict[str, Any], Any]:
    # Distinct from the config-seeded default ("asap").
    return {"option": "departure"}, "departure"


CASES: list[_Case] = [
    _Case(
        entity_id="battery_capacity",
        platform="number",
        service="set_value",
        suffix="_battery_capacity",
        attr="battery_capacity",
        flavour="revert",
        with_price_sensor=False,
        payload=_number_payload,
    ),
    _Case(
        entity_id="target_soc",
        platform="number",
        service="set_value",
        suffix="_target_soc",
        attr="target_soc",
        flavour="revert",
        with_price_sensor=False,
        payload=_number_payload,
    ),
    _Case(
        entity_id="current_soc",
        platform="number",
        service="set_value",
        suffix="_current_soc",
        attr="current_soc",
        flavour="revert",
        with_price_sensor=False,
        payload=_number_payload,
    ),
    _Case(
        entity_id="cost_tolerance",
        platform="number",
        service="set_value",
        suffix="_cost_tolerance",
        attr="cost_tolerance_pct",
        flavour="revert",
        with_price_sensor=True,
        payload=_number_payload,
    ),
    _Case(
        entity_id="departure_time",
        platform="datetime",
        service="set_value",
        suffix="_departure_time",
        attr="departure_time",
        flavour="revert",
        with_price_sensor=False,
        payload=_departure_payload,
    ),
    _Case(
        entity_id="finish_mode",
        platform="select",
        service="select_option",
        suffix="_finish_mode",
        attr="finish_mode",
        flavour="allow",
        with_price_sensor=False,
        payload=_finish_payload,
    ),
]

_CASE_IDS = [c.entity_id for c in CASES]


def _lookup(hass: HomeAssistant, entry, case: _Case) -> str:
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        case.platform, DOMAIN, f"{entry.entry_id}{case.suffix}"
    )
    assert entity_id is not None, f"{case.entity_id} entity was not created"
    return entity_id


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
@freeze_time(FROZEN)
async def test_locked_edit_is_rejected_with_feedback(
    hass: HomeAssistant, case: _Case
) -> None:
    """Edit-while-active raises a visible HomeAssistantError on every entity.

    raise-and-revert (number/datetime): the coordinator attribute is UNCHANGED.
    raise-and-allow (finish_mode): coordinator.finish_mode IS the new value.
    """
    entry, coordinator = await _setup(
        hass, with_price_sensor=case.with_price_sensor
    )
    _seed_running(coordinator)

    entity_id = _lookup(hass, entry, case)
    running = getattr(coordinator, case.attr)
    data, new_value = case.payload(running)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            case.platform,
            case.service,
            {"entity_id": entity_id, **data},
            blocking=True,
        )
    await hass.async_block_till_done()

    after = getattr(coordinator, case.attr)
    if case.flavour == "revert":
        # The locked edit was rejected and the coordinator value snapped back.
        assert after == running, (
            f"{case.entity_id}: a locked number/datetime edit must leave the "
            f"coordinator value UNCHANGED (raise-and-revert), got {after!r} "
            f"(was {running!r})"
        )
    else:
        # raise-and-allow: the user's intent reached the coordinator.
        assert after == new_value, (
            f"{case.entity_id}: a locked finish-mode pick must be honoured "
            f"(reach the coordinator), got {after!r} (expected {new_value!r})"
        )


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
@freeze_time(FROZEN)
async def test_idle_edit_succeeds(hass: HomeAssistant, case: _Case) -> None:
    """Edit-while-idle succeeds with NO error and updates the coordinator."""
    entry, coordinator = await _setup(
        hass, with_price_sensor=case.with_price_sensor
    )
    # Seed running values but keep the session INACTIVE so inputs are unlocked.
    coordinator.session_active = False
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)
    assert coordinator.inputs_locked is False

    entity_id = _lookup(hass, entry, case)
    running = getattr(coordinator, case.attr)
    data, new_value = case.payload(running)

    # No pytest.raises — an idle edit must NOT raise.
    await hass.services.async_call(
        case.platform,
        case.service,
        {"entity_id": entity_id, **data},
        blocking=True,
    )
    await hass.async_block_till_done()

    after = getattr(coordinator, case.attr)
    assert after == new_value, (
        f"{case.entity_id}: an idle edit must update the coordinator, got "
        f"{after!r} (expected {new_value!r})"
    )


# --------------------------------------------------------------------------- #
# Existing finish-mode focus tests (kept verbatim — the matrix template).
# --------------------------------------------------------------------------- #


@freeze_time(FROZEN)
async def test_locked_select_raises_but_honours_the_pick(hass: HomeAssistant) -> None:
    """Editing finish_mode under an active session honours the pick AND raises.

    Finish mode is the "lost night" input: the Phase-1 defect was the pick
    vanishing silently. The fix is raise-and-allow — the new mode reaches the
    coordinator (so the running session uses the user's intent) and a visible
    HomeAssistantError tells them it took effect. NEVER silent.
    """
    entry, coordinator = await _setup(hass)
    _seed_running(coordinator)

    # The running mode is the config-seeded default (asap).
    assert coordinator.finish_mode == "asap"

    ent_reg = er.async_get(hass)
    select_entity_id = ent_reg.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_finish_mode"
    )
    assert select_entity_id is not None, "finish-mode select entity was not created"

    # A locked finish-mode edit must surface a visible error — NOT silently vanish.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select_entity_id, "option": "departure"},
            blocking=True,
        )
    await hass.async_block_till_done()

    # raise-AND-allow: the user's intent reached the coordinator.
    assert coordinator.finish_mode == "departure", (
        "a locked finish-mode pick must be honoured (reach the coordinator), "
        "not silently swallowed"
    )


@freeze_time(FROZEN)
async def test_ending_session_releases_the_lock(hass: HomeAssistant) -> None:
    """After async_end_session() the same edit succeeds (lock released)."""
    entry, coordinator = await _setup(hass)
    _seed_running(coordinator)

    ent_reg = er.async_get(hass)
    select_entity_id = ent_reg.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_finish_mode"
    )
    assert select_entity_id is not None

    # Locked: the edit raises a visible error (raise-and-allow for finish mode).
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select_entity_id, "option": "end_of_night"},
            blocking=True,
        )
    await hass.async_block_till_done()

    # End the session -> lock released.
    await coordinator.async_end_session()
    await hass.async_block_till_done()
    assert coordinator.inputs_locked is False

    # The same edit now succeeds with NO error raised (lock released).
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_entity_id, "option": "departure"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator.finish_mode == "departure", (
        "after ending the session the edit must succeed silently (lock released)"
    )


# --------------------------------------------------------------------------- #
# Session lifecycle: start / stop / auto-end + restart-restore resume (SESS-03).
# --------------------------------------------------------------------------- #


@freeze_time(FROZEN)
async def test_start_session_locks_inputs(hass: HomeAssistant) -> None:
    """async_start_session flips session_active True and locks the inputs."""
    _entry, coordinator = await _setup(hass)
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)

    assert coordinator.session_active is False
    assert coordinator.inputs_locked is False

    await coordinator.async_start_session()
    await hass.async_block_till_done()

    assert coordinator.session_active is True
    assert coordinator.inputs_locked is True


@freeze_time(FROZEN)
async def test_manual_stop_releases_the_lock(hass: HomeAssistant) -> None:
    """async_end_session (manual stop) releases the lock so the next pick lands."""
    _entry, coordinator = await _setup(hass)
    _seed_running(coordinator)
    assert coordinator.inputs_locked is True

    await coordinator.async_end_session()
    await hass.async_block_till_done()

    assert coordinator.session_active is False
    assert coordinator.inputs_locked is False

    # The next mode select must now apply with no error.
    await coordinator.async_set_finish_mode("departure")
    assert coordinator.finish_mode == "departure"


async def test_auto_end_releases_the_lock(hass: HomeAssistant) -> None:
    """The departure-passed backstop auto-ends the session and releases the lock.

    Reuses the test_session_end.py auto-end driving (a passed departure with
    delivered < needed) so the next mode select succeeds (SESS-02/SESS-03).
    """
    with freeze_time("2026-06-04T23:30:00+03:00"):
        _entry, coordinator = await _setup(hass)
        _seed_running(coordinator)
        # Drive the auto-end backstop: departure is in the past with progress.
        coordinator.departure_time = dt_util.now() - timedelta(minutes=5)
        coordinator.delivered_energy_kwh = 1.0

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is False
        assert coordinator.inputs_locked is False

        # A stale lingering session must never block the next mode select.
        await coordinator.async_set_finish_mode("departure")
        assert coordinator.finish_mode == "departure"


@freeze_time(FROZEN)
async def test_restart_restore_resumes_without_start_session(
    hass: HomeAssistant,
) -> None:
    """The unguarded restore path resumes a mid-charge restart (SESS-03).

    A mid-charge HA restart must resume the session WITHOUT calling
    async_start_session — the binary_sensor RestoreEntity drives the restore-path
    setter async_set_session_active, which deliberately does NOT consult
    inputs_locked. Assert the resumed session is active and inputs are locked.
    """
    _entry, coordinator = await _setup(hass)
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)

    # Fresh boot: no session yet, inputs unlocked.
    assert coordinator.session_active is False
    assert coordinator.inputs_locked is False

    # Drive the restore path (NOT async_start_session) — a mid-charge restart.
    await coordinator.async_set_session_active(True)
    await hass.async_block_till_done()

    assert coordinator.session_active is True, (
        "the restore path must resume a mid-charge restart"
    )
    assert coordinator.inputs_locked is True, (
        "a resumed session must re-lock the inputs"
    )
