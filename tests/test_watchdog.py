"""Charger-reboot recovery watchdog test — REL-01 on injected power + a frozen clock.

A silent charger reboot leaves the session "active and commanding charge" while
the charger sits idle: the idempotent apply path sees no change in the target
current and pushes nothing, so charging never resumes. The watchdog detects the
fingerprint of that stall — commanding charge (``dynamic_target_current > 0``)
but the configured charger power sensor reads ~0 W for ``WATCHDOG_ZERO_CYCLES``
consecutive cycles — and breaks the stalemate by resetting the
``_last_applied_*`` idempotency trackers so the EXISTING ``_apply_to_charger``
path re-pushes the OCPP profile and re-issues switch turn_on on the same cycle.

These tests drive the watchdog with HA power states injected via
``hass.states.async_set`` and a clock advanced with ``freezegun``, with the OCPP
service and the charger switch mocked via ``async_mock_service`` so the re-assert
is directly observable as service calls. They assert:

* the re-assert fires on the threshold cycle (OCPP re-push + switch turn_on) and
  the consecutive-zero counter resets;
* healthy power -> the counter stays 0, no re-assert;
* a plan-commanded pause (``dynamic_current == 0``) with 0 power never counts
  (the watchdog never fights a deliberate stop);
* no power sensor configured -> the watchdog is inert;
* an UNAVAILABLE read is treated as "unknown", never the ~0 reboot fingerprint,
  so it never triggers a spurious re-assert.

Deterministic, no live HA loop. ``asyncio_mode = auto`` runs every ``async def
test_*`` automatically — no decorator needed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.evpoint_charge_scheduler.const import (
    DOMAIN,
    WATCHDOG_ZERO_CYCLES,
)

POWER_ENTITY = "sensor.charger_power"
SWITCH_ENTITY = "switch.charger"
OCPP_DOMAIN = "ocpp"
OCPP_SERVICE = "set_charge_rate"


def _make_entry(*, with_power_sensor: bool = True) -> MockConfigEntry:
    """Active-mode config (OCPP + switch wired) so the re-assert is observable."""
    data = {
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
        # Wire the charger controls so a re-assert issues real service calls.
        "ocpp_set_rate_service": f"{OCPP_DOMAIN}.{OCPP_SERVICE}",
        "ocpp_devid": "charger-1",
        "charging_profile_id": 8,
        "charger_switch": SWITCH_ENTITY,
    }
    if with_power_sensor:
        data["charger_power_sensor"] = POWER_ENTITY
    return MockConfigEntry(domain=DOMAIN, data=data)


async def _setup(hass: HomeAssistant, *, with_power_sensor: bool = True):
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))
    entry = _make_entry(with_power_sensor=with_power_sensor)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # A running session at night so the planner commands charge, with a
    # departure far out so no auto-end fires.
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)
    return coordinator


def _set_power(hass: HomeAssistant, value, unit: str = "W") -> None:
    hass.states.async_set(POWER_ENTITY, value, {"unit_of_measurement": unit})


def _mock_charger(hass: HomeAssistant):
    """Mock the OCPP set-rate service + the switch turn_on/turn_off services."""
    ocpp_calls = async_mock_service(hass, OCPP_DOMAIN, OCPP_SERVICE)
    on_calls = async_mock_service(hass, "switch", "turn_on")
    off_calls = async_mock_service(hass, "switch", "turn_off")
    return ocpp_calls, on_calls, off_calls


# A frozen night moment so the planner commands charge (asap night-tariff).
NIGHT = "2026-06-04T23:30:00+03:00"


async def test_reassert_after_n_zero_power_cycles(hass: HomeAssistant) -> None:
    """Commanding charge but 0 W for N cycles -> re-push OCPP + switch turn_on."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass)
        ocpp_calls, on_calls, off_calls = _mock_charger(hass)

        # Establish a healthy charging baseline so the trackers are "applied".
        _set_power(hass, "6500", "W")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["dynamic_target_current"] > 0
        baseline_ocpp = len(ocpp_calls)
        baseline_on = len(on_calls)
        assert coordinator._zero_power_cycles == 0

        # The charger silently reboots: power collapses to ~0 while we still
        # command a charge. Drive WATCHDOG_ZERO_CYCLES - 1 cycles below the
        # threshold — no re-assert yet (debounced).
        _set_power(hass, "0", "W")
        for _ in range(WATCHDOG_ZERO_CYCLES - 1):
            frozen.tick(timedelta(seconds=30))
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert len(ocpp_calls) == baseline_ocpp, "re-asserted before the threshold"
        assert len(on_calls) == baseline_on
        assert coordinator._zero_power_cycles == WATCHDOG_ZERO_CYCLES - 1

        # Threshold cycle: the watchdog fires — re-pushes the OCPP profile and
        # re-issues switch turn_on — and the counter resets.
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert len(ocpp_calls) == baseline_ocpp + 1, "OCPP profile was not re-pushed"
        assert len(on_calls) == baseline_on + 1, "switch turn_on was not re-issued"
        assert coordinator._zero_power_cycles == 0, "counter did not reset after firing"


async def test_no_reassert_with_healthy_power(hass: HomeAssistant) -> None:
    """Power present (6500 W) -> counter stays 0, no re-assert."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass)
        ocpp_calls, on_calls, _ = _mock_charger(hass)

        _set_power(hass, "6500", "W")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["dynamic_target_current"] > 0
        baseline_ocpp = len(ocpp_calls)
        baseline_on = len(on_calls)

        for _ in range(WATCHDOG_ZERO_CYCLES + 2):
            frozen.tick(timedelta(seconds=30))
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert coordinator._zero_power_cycles == 0
        assert len(ocpp_calls) == baseline_ocpp, "spurious OCPP re-push on healthy power"
        assert len(on_calls) == baseline_on


async def test_no_reassert_on_deliberate_pause(hass: HomeAssistant) -> None:
    """Plan commands 0 (pause) with 0 power -> never counts, never re-asserts."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass)
        ocpp_calls, on_calls, off_calls = _mock_charger(hass)

        # Force a deliberate pause: an apartment load high enough that the load
        # balancer throttles the EV to 0 A. With total_limit 32 and headroom 0,
        # an apartment draw of 32 A leaves 0 A available -> dynamic_current 0.
        hass.states.async_set("sensor.apartment", "40", {"unit_of_measurement": "A"})
        coordinator._config["apartment_current_sensor"] = "sensor.apartment"

        _set_power(hass, "0", "W")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["dynamic_target_current"] == 0, (
            "test precondition: the plan must be commanding 0 A (a deliberate pause)"
        )
        baseline_ocpp = len(ocpp_calls)
        baseline_on = len(on_calls)

        for _ in range(WATCHDOG_ZERO_CYCLES + 2):
            frozen.tick(timedelta(seconds=30))
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert coordinator._zero_power_cycles == 0, (
            "the watchdog counted a deliberate pause as a stall"
        )
        assert len(ocpp_calls) == baseline_ocpp
        assert len(on_calls) == baseline_on


async def test_inert_without_power_sensor(hass: HomeAssistant) -> None:
    """No power sensor configured -> watchdog inert, counter never increments."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass, with_power_sensor=False)
        ocpp_calls, on_calls, _ = _mock_charger(hass)

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["dynamic_target_current"] > 0
        baseline_ocpp = len(ocpp_calls)
        baseline_on = len(on_calls)

        for _ in range(WATCHDOG_ZERO_CYCLES + 2):
            frozen.tick(timedelta(seconds=30))
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert coordinator._zero_power_cycles == 0
        assert len(ocpp_calls) == baseline_ocpp, "watchdog fired without a power sensor"
        assert len(on_calls) == baseline_on
        assert coordinator.data["charger_heartbeat"] == "no_sensor"


async def test_unavailable_read_never_triggers_reassert(hass: HomeAssistant) -> None:
    """UNAVAILABLE read is 'unknown', not ~0 -> never counts, never re-asserts."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass)
        ocpp_calls, on_calls, _ = _mock_charger(hass)

        _set_power(hass, "6500", "W")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["dynamic_target_current"] > 0
        baseline_ocpp = len(ocpp_calls)
        baseline_on = len(on_calls)

        # The sensor goes UNAVAILABLE while we still command a charge. A missing
        # read is NOT proof the charger is off — it must not count.
        _set_power(hass, STATE_UNAVAILABLE, "W")
        for _ in range(WATCHDOG_ZERO_CYCLES + 2):
            frozen.tick(timedelta(seconds=30))
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert coordinator._zero_power_cycles == 0, (
            "an UNAVAILABLE read was counted as the zero-power reboot fingerprint"
        )
        assert len(ocpp_calls) == baseline_ocpp, "spurious re-push on an UNAVAILABLE read"
        assert len(on_calls) == baseline_on


async def test_single_blip_does_not_reassert(hass: HomeAssistant) -> None:
    """One zero-power cycle below the threshold does NOT re-assert (debounced)."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass)
        ocpp_calls, on_calls, _ = _mock_charger(hass)

        _set_power(hass, "6500", "W")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        baseline_ocpp = len(ocpp_calls)
        baseline_on = len(on_calls)

        # A single zero blip, then power recovers -> counter resets, no re-assert.
        _set_power(hass, "0", "W")
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator._zero_power_cycles == 1

        _set_power(hass, "6500", "W")
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator._zero_power_cycles == 0
        assert len(ocpp_calls) == baseline_ocpp
        assert len(on_calls) == baseline_on


async def test_charger_heartbeat_surfaced(hass: HomeAssistant) -> None:
    """charger_heartbeat reports ok while drawing, stalled while counting."""
    with freeze_time(NIGHT) as frozen:
        coordinator = await _setup(hass)
        _mock_charger(hass)

        _set_power(hass, "6500", "W")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["charger_heartbeat"] == "ok"

        _set_power(hass, "0", "W")
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data["charger_heartbeat"] == "stalled"
