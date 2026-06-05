"""Input-lock feedback test — the UX-01 / SESS-03 guard on the real entity.

Proves the silent-revert defect is replaced by *visible* feedback: editing the
finish-mode select while a session is active raises a ``HomeAssistantError``
(surfaced as a UI toast), snaps the dropdown back to the running value, and
leaves ``coordinator.finish_mode`` unchanged. After ``async_end_session()`` the
lock is released and the same edit succeeds.

This exercises the production ``FinishModeSelect.async_select_option`` path
through the service registry under ``session_active=True`` — no live HA, frozen
clock, deterministic. The single ``coordinator.inputs_locked`` predicate is the
one source of truth all writable entities consult; this test pins its behaviour
on the select (the entity the Phase-1 "lost night" bug was reported through).

No ``@pytest.mark.asyncio`` decorator: ``asyncio_mode = auto`` runs every
``async def test_*`` automatically.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import DOMAIN


def _make_entry() -> MockConfigEntry:
    """Minimal advisory-mode config, mirroring tests/test_planner_baseline.py."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
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
        },
    )


@freeze_time("2026-06-04T14:00:00+03:00")
async def test_locked_select_raises_and_snaps_back(hass: HomeAssistant) -> None:
    """Editing finish_mode under an active session raises + reverts the pick."""
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))

    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Lingering-session state, no deficit / ample slack (matches the baseline).
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)

    # The running mode is the config-seeded default (asap).
    assert coordinator.finish_mode == "asap"

    ent_reg = er.async_get(hass)
    select_entity_id = ent_reg.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_finish_mode"
    )
    assert select_entity_id is not None, "finish-mode select entity was not created"

    # A locked edit must surface a visible error — NOT silently vanish.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select_entity_id, "option": "departure"},
            blocking=True,
        )
    await hass.async_block_till_done()

    # The pick was rejected: the running mode is unchanged.
    assert coordinator.finish_mode == "asap", (
        "a locked-session edit must leave coordinator.finish_mode unchanged"
    )


@freeze_time("2026-06-04T14:00:00+03:00")
async def test_ending_session_releases_the_lock(hass: HomeAssistant) -> None:
    """After async_end_session() the same edit succeeds (lock released)."""
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))

    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)

    ent_reg = er.async_get(hass)
    select_entity_id = ent_reg.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_finish_mode"
    )
    assert select_entity_id is not None

    # Locked: rejected.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select_entity_id, "option": "departure"},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert coordinator.finish_mode == "asap"

    # End the session -> lock released.
    await coordinator.async_end_session()
    await hass.async_block_till_done()
    assert coordinator.inputs_locked is False

    # The same edit now succeeds and reaches the coordinator.
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_entity_id, "option": "departure"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator.finish_mode == "departure", (
        "after ending the session the edit must be honoured (lock released)"
    )
