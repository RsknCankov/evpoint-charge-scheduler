"""Edge-triggered day-supplement notification tests — NIGHT-03.

These tests verify that when the coordinator transitions into
ACTION_CHARGE_DAY_SUPPLEMENT for the first time in a session it calls the
configured HA notify service exactly once, never spams on subsequent cycles,
re-fires after session reset, and degrades gracefully when the service is not
configured or is malformed.

Setup: departure at 18:00 today with clock frozen at 15:00 so there are zero
night-tariff hours before departure (night_start=23:00, night_end=07:00) —
forcing deficit_kwh > 0 → charge_day_supplement on every cycle.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.evpoint_charge_scheduler.const import DOMAIN

NOTIFY_DOMAIN = "notify"
NOTIFY_SERVICE = "mobile_app_phone"

# A daytime moment — no night-tariff hours between 15:00 and departure 18:00.
DAY = "2026-06-04T15:00:00+03:00"


def _make_entry(*, notify_service: str | None = "notify.mobile_app_phone") -> MockConfigEntry:
    """Minimal config that drives charge_day_supplement every cycle."""
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
    if notify_service is not None:
        data["notify_service"] = notify_service
    return MockConfigEntry(domain=DOMAIN, data=data)


async def _setup(
    hass: HomeAssistant,
    *,
    notify_service: str | None = "notify.mobile_app_phone",
    frozen_time: str = DAY,
):
    """Set up coordinator with session active and a near departure that forces day-supplement."""
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))
    entry = _make_entry(notify_service=notify_service)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Session active; departure at 18:00 so there are 3 hours but zero night hours
    # → deficit_kwh > 0 → charge_day_supplement every cycle.
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 20.0
    # Departure 3 hours from the frozen 15:00 moment.
    coordinator.departure_time = dt_util.now() + timedelta(hours=3)
    return coordinator


async def test_notification_fires_once_on_first_day_supplement(hass: HomeAssistant) -> None:
    """Notification fires on the first refresh and NOT on the second."""
    with freeze_time(DAY):
        coordinator = await _setup(hass)
        notify_calls = async_mock_service(hass, NOTIFY_DOMAIN, NOTIFY_SERVICE)

        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert len(notify_calls) == 1, (
            f"Expected exactly 1 notify call on first day-supplement cycle; got {len(notify_calls)}"
        )


async def test_notification_does_not_repeat_on_continued_day_supplement(hass: HomeAssistant) -> None:
    """Five refreshes in charge_day_supplement → still exactly one notify call."""
    with freeze_time(DAY):
        coordinator = await _setup(hass)
        notify_calls = async_mock_service(hass, NOTIFY_DOMAIN, NOTIFY_SERVICE)

        for _ in range(5):
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()

        assert len(notify_calls) == 1, (
            f"Notification spammed: expected 1, got {len(notify_calls)}"
        )


async def test_notification_fires_again_after_session_reset(hass: HomeAssistant) -> None:
    """After session end the tracker resets so the notification fires again in the next session."""
    with freeze_time(DAY):
        coordinator = await _setup(hass)
        notify_calls = async_mock_service(hass, NOTIFY_DOMAIN, NOTIFY_SERVICE)

        # First session — notification fires once.
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert len(notify_calls) == 1

        # End the session.  async_end_session resets _last_notified_action to None.
        await coordinator.async_end_session()
        await hass.async_block_till_done()

        # Start a fresh session.
        coordinator.session_active = True
        coordinator.current_soc = 20.0
        coordinator.departure_time = dt_util.now() + timedelta(hours=3)

        # Second session — notification must fire again.
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert len(notify_calls) == 2, (
            f"Notification did not re-fire after session reset; got {len(notify_calls)}"
        )


async def test_notification_skipped_when_not_configured(hass: HomeAssistant) -> None:
    """No notify_service in config → zero service calls, no exception."""
    with freeze_time(DAY):
        coordinator = await _setup(hass, notify_service=None)
        notify_calls = async_mock_service(hass, NOTIFY_DOMAIN, NOTIFY_SERVICE)

        for _ in range(3):
            await coordinator.async_request_refresh()
            await hass.async_block_till_done()

        assert len(notify_calls) == 0, (
            f"Unexpected notify call with no service configured; got {len(notify_calls)}"
        )


async def test_notification_skipped_for_malformed_service(hass: HomeAssistant) -> None:
    """Malformed service string (no dot) → no call, no ValueError raised."""
    with freeze_time(DAY):
        coordinator = await _setup(hass, notify_service="mobilepush")
        notify_calls = async_mock_service(hass, NOTIFY_DOMAIN, NOTIFY_SERVICE)

        # Must not raise even though the service string has no dot.
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert len(notify_calls) == 0, (
            f"Malformed service should produce 0 calls; got {len(notify_calls)}"
        )
