"""Coordinator-level session-end wiring — SESS-01/02/03, D-04 under a frozen clock.

Full ``async_setup`` of a MockConfigEntry (with a charger power sensor) drives the
deterministic manual-SoC end the coordinator builds on top of plan 03-02's
accumulator and plan 03-01's ``inputs_locked`` predicate:

* SUCCESS — ``delivered_energy_kwh >= energy_needed`` auto-ends the session and
  clears ``current_soc`` with NO notification;
* BACKSTOP — a passed departure with delivered < needed auto-ends AND fires an
  active persistent_notification naming delivered vs needed kWh (D-04);
* NO-POWER-SENSOR FALLBACK — a passed departure with no power sensor still
  backstops + notifies (the departure-time hard stop, D-03);
* NO-DEPARTURE GUARD — ``departure_time = None`` (the coordinator's
  ``hours_to_dep=0.0`` sentinel) keeps the session ACTIVE with no notification
  (no silent early end);
* CRITERION 2 — an UNAVAILABLE read with a future departure keeps the session
  active (never premature end);
* LOCK RELEASE — after any auto-end ``inputs_locked`` is False so the next
  session's mode select succeeds (SESS-02/SESS-03).

The auto-end is fire-and-forget (``hass.async_create_task``); every assertion
follows ``await hass.async_block_till_done()``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import DOMAIN

POWER_ENTITY = "sensor.charger_power"


def _make_entry(*, with_power_sensor: bool = True) -> MockConfigEntry:
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
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)
    return coordinator


def _notifications(hass: HomeAssistant) -> dict:
    return dict(_async_get_or_create_notifications(hass))


async def test_success_ends_session_clears_soc_no_notification(
    hass: HomeAssistant,
) -> None:
    """delivered >= needed -> session ends, current_soc cleared, no notification."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass)
        # Drive delivered above whatever energy_needed resolved to.
        await coordinator.async_refresh()
        needed = coordinator.data["energy_needed"]
        assert needed > 0
        coordinator.delivered_energy_kwh = needed + 5.0

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is False
        assert coordinator.current_soc is None
        assert _notifications(hass) == {}


async def test_backstop_ends_session_fires_notification(hass: HomeAssistant) -> None:
    """Departure passed, delivered < needed -> end + undercharged notification."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass)
        coordinator.departure_time = dt_util.now() - timedelta(minutes=5)
        coordinator.delivered_energy_kwh = 1.0  # well below needed

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is False
        notes = _notifications(hass)
        assert len(notes) == 1
        msg = next(iter(notes.values()))["message"].lower()
        assert "kwh" in msg


async def test_no_power_sensor_departure_fallback_backstops(
    hass: HomeAssistant,
) -> None:
    """No power sensor + departure passed -> backstop + notification (D-03)."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass, with_power_sensor=False)
        coordinator.departure_time = dt_util.now() - timedelta(minutes=5)

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is False
        assert len(_notifications(hass)) == 1


async def test_no_departure_guard_keeps_session_active(hass: HomeAssistant) -> None:
    """departure_time=None (hours_to_dep=0.0 sentinel) -> stays active, no notify."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass)
        coordinator.departure_time = None
        coordinator.delivered_energy_kwh = 1.0  # below needed

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is True
        assert _notifications(hass) == {}


async def test_unavailable_read_future_departure_stays_active(
    hass: HomeAssistant,
) -> None:
    """UNAVAILABLE read, delivered not advanced, departure in the future -> active."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass)
        coordinator.delivered_energy_kwh = 0.0
        hass.states.async_set(
            POWER_ENTITY, STATE_UNAVAILABLE, {"unit_of_measurement": "W"}
        )

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is True
        assert _notifications(hass) == {}


async def test_after_end_lock_releases_and_mode_select_succeeds(
    hass: HomeAssistant,
) -> None:
    """After auto-end, inputs_locked is False and a mode change applies (SESS-02/03)."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass)
        coordinator.departure_time = dt_util.now() - timedelta(minutes=5)
        coordinator.delivered_energy_kwh = 1.0

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.session_active is False
        assert coordinator.inputs_locked is False

        # A stale lingering session must never block the next mode select.
        await coordinator.async_set_finish_mode("departure")
        assert coordinator.finish_mode == "departure"
