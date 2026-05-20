"""Button platform: start / stop a single charging session.

Sessions are how the user tells the integration "use the values I just entered
and actually charge now". Outside of a session the coordinator still computes
the plan for the dashboard but doesn't push anything to the charger.

Only one session can run at a time: while a session is active the Start button
is unavailable, and the Stop button is unavailable when idle.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartEVChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            StartSessionButton(coordinator, entry),
            StopSessionButton(coordinator, entry),
        ]
    )


class _SessionButtonBase(
    CoordinatorEntity[SmartEVChargingCoordinator], ButtonEntity
):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }


class StartSessionButton(_SessionButtonBase):
    _key = "start_session"
    _attr_name = "Start charging session"
    _attr_icon = "mdi:play-circle"
    _attr_translation_key = "start_session"

    @property
    def available(self) -> bool:
        return not self.coordinator.session_active

    async def async_press(self) -> None:
        await self.coordinator.async_start_session()


class StopSessionButton(_SessionButtonBase):
    _key = "stop_session"
    _attr_name = "Stop charging session"
    _attr_icon = "mdi:stop-circle"
    _attr_translation_key = "stop_session"

    @property
    def available(self) -> bool:
        return self.coordinator.session_active

    async def async_press(self) -> None:
        await self.coordinator.async_end_session()
