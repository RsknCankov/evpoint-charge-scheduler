"""Binary sensor platform: surfaces whether a charging session is active.

Backed by RestoreEntity so an in-progress session survives HA restarts —
without this, the coordinator would come back as idle every time HA reloads.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartEVChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SessionActiveBinarySensor(coordinator, entry)])


class SessionActiveBinarySensor(
    CoordinatorEntity[SmartEVChargingCoordinator],
    BinarySensorEntity,
    RestoreEntity,
):
    _attr_has_entity_name = True
    _attr_name = "Charging session active"
    _attr_icon = "mdi:flash"
    _attr_translation_key = "session_active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_session_active"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            await self.coordinator.async_set_session_active(last_state.state == "on")

    @property
    def is_on(self) -> bool:
        return self.coordinator.session_active
