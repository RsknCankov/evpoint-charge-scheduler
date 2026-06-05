"""Delivered-energy accumulator test — SOC-01 / D-02 on injected power + a frozen clock.

The coordinator integrates the configured charger power sensor over each cycle's
elapsed wall-clock interval (Riemann sum) into ``delivered_energy_kwh``. These
tests drive that integration with HA states injected via ``hass.states.async_set``
and a clock advanced with ``freezegun``, asserting:

* constant-power integration matches the expected kWh (and the first cycle only
  establishes the baseline timestamp, crediting ~0);
* a kW-reporting sensor and a W-reporting sensor of the same physical power
  integrate to the SAME delivered kWh (unit normalisation);
* UNAVAILABLE / negative reads contribute 0 and never decrease the accumulator;
* ``async_start_session`` resets the accumulator to 0;
* a simulated restore (``async_set_delivered_energy`` then continuing) resumes
  from the restored value without crediting a phantom cross-downtime interval.

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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import DOMAIN

POWER_ENTITY = "sensor.charger_power"


def _make_entry(*, with_power_sensor: bool = True) -> MockConfigEntry:
    """Advisory-mode config with (optionally) the charger power sensor wired."""
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
    # A running session with a departure far out so no auto-end fires.
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)
    return coordinator


def _set_power(hass: HomeAssistant, value, unit: str = "W") -> None:
    hass.states.async_set(
        POWER_ENTITY, value, {"unit_of_measurement": unit}
    )


async def test_constant_power_integrates_over_interval(hass: HomeAssistant) -> None:
    """Two cycles 30s apart at 7000 W -> ~0.0583 kWh; first cycle credits ~0."""
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass)
        coordinator.delivered_energy_kwh = 0.0
        coordinator._last_power_ts = None

        _set_power(hass, "7000", "W")
        await coordinator.async_refresh()
        # First cycle only anchors the timestamp.
        assert coordinator.delivered_energy_kwh == pytest.approx(0.0, abs=1e-9)

        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        # 7 kW * (30/3600) h = 0.058333... kWh
        assert coordinator.delivered_energy_kwh == pytest.approx(7.0 * 30 / 3600, abs=1e-4)


async def test_kw_and_w_sensors_yield_same_energy(hass: HomeAssistant) -> None:
    """A "7.0" kW sensor and a "7000" W sensor integrate to identical kWh."""
    # W-reporting run
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coord_w = await _setup(hass)
        coord_w.delivered_energy_kwh = 0.0
        coord_w._last_power_ts = None
        _set_power(hass, "7000", "W")
        await coord_w.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coord_w.async_refresh()
        w_energy = coord_w.delivered_energy_kwh

    # kW-reporting run (fresh hass state)
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coord_kw = await _setup(hass)
        coord_kw.delivered_energy_kwh = 0.0
        coord_kw._last_power_ts = None
        _set_power(hass, "7.0", "kW")
        await coord_kw.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coord_kw.async_refresh()
        kw_energy = coord_kw.delivered_energy_kwh

    assert kw_energy == pytest.approx(w_energy, abs=1e-6)
    assert kw_energy == pytest.approx(7.0 * 30 / 3600, abs=1e-4)


async def test_unavailable_read_contributes_zero(hass: HomeAssistant) -> None:
    """An UNAVAILABLE read over an interval credits 0 and never decreases."""
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass)
        coordinator.delivered_energy_kwh = 0.0
        coordinator._last_power_ts = None

        _set_power(hass, "7000", "W")
        await coordinator.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        good = coordinator.delivered_energy_kwh
        assert good > 0

        # Now the sensor goes unavailable for the next interval.
        _set_power(hass, STATE_UNAVAILABLE, "W")
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        assert coordinator.delivered_energy_kwh == pytest.approx(good, abs=1e-9)


async def test_negative_read_contributes_zero(hass: HomeAssistant) -> None:
    """A negative power read credits 0 (the accumulator is monotonic)."""
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass)
        coordinator.delivered_energy_kwh = 0.0
        coordinator._last_power_ts = None

        _set_power(hass, "-5000", "W")
        await coordinator.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        assert coordinator.delivered_energy_kwh == pytest.approx(0.0, abs=1e-9)


async def test_session_start_resets_accumulator(hass: HomeAssistant) -> None:
    """async_start_session resets delivered_energy_kwh to 0 and re-anchors ts."""
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass)
        coordinator.delivered_energy_kwh = 0.0
        coordinator._last_power_ts = None

        _set_power(hass, "7000", "W")
        await coordinator.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        assert coordinator.delivered_energy_kwh > 0

        # End then start a fresh session -> accumulator reset.
        await coordinator.async_end_session()
        await coordinator.async_start_session()
        assert coordinator.delivered_energy_kwh == pytest.approx(0.0, abs=1e-9)
        assert coordinator._last_power_ts is None


async def test_restore_resumes_from_restored_value(hass: HomeAssistant) -> None:
    """A simulated restore seeds the accumulator; integration continues from it.

    The post-restart cycle re-anchors the timestamp (no phantom cross-downtime
    interval is credited), then the following interval adds on top of the
    restored base.
    """
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass)

        # Simulate the RestoreEntity handing back a prior value across restart.
        await coordinator.async_set_delivered_energy(3.5)
        coordinator._last_power_ts = None
        assert coordinator.delivered_energy_kwh == pytest.approx(3.5)

        _set_power(hass, "7000", "W")
        # First post-restart cycle only re-anchors the timestamp.
        await coordinator.async_refresh()
        assert coordinator.delivered_energy_kwh == pytest.approx(3.5, abs=1e-4)

        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        assert coordinator.delivered_energy_kwh == pytest.approx(
            3.5 + 7.0 * 30 / 3600, abs=1e-4
        )


async def test_inert_without_power_sensor(hass: HomeAssistant) -> None:
    """No power sensor configured -> accumulator stays 0, energy_source reports it."""
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass, with_power_sensor=False)
        coordinator.delivered_energy_kwh = 0.0
        coordinator._last_power_ts = None

        await coordinator.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        assert coordinator.delivered_energy_kwh == pytest.approx(0.0, abs=1e-9)
        assert coordinator.data["delivered_energy_kwh"] == pytest.approx(0.0)
        assert coordinator.data["energy_source"] == "departure_only"


async def test_surfaced_in_coordinator_data(hass: HomeAssistant) -> None:
    """delivered_energy_kwh + energy_source appear in coordinator.data."""
    with freeze_time("2026-06-04T23:30:00+03:00") as frozen:
        coordinator = await _setup(hass)
        coordinator.delivered_energy_kwh = 0.0
        coordinator._last_power_ts = None

        _set_power(hass, "7000", "W")
        await coordinator.async_refresh()
        frozen.tick(timedelta(seconds=30))
        await coordinator.async_refresh()
        assert "delivered_energy_kwh" in coordinator.data
        assert coordinator.data["energy_source"] == "power_sensor"
        assert coordinator.data["delivered_energy_kwh"] == pytest.approx(
            7.0 * 30 / 3600, abs=1e-3
        )
