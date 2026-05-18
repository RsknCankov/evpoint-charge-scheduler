"""DateTime platform: the departure time input."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SmartEVChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DepartureDateTime(coordinator, entry)])


class DepartureDateTime(DateTimeEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Departure time"
    _attr_icon = "mdi:car-clock"

    def __init__(self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_departure_time"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }
        self._attr_native_value: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = dt_util.parse_datetime(last_state.state)
            except Exception:
                self._attr_native_value = None
        if self._attr_native_value is None:
            # Default: tomorrow at 07:00 local time
            now = dt_util.now()
            tomorrow = (now + timedelta(days=1)).replace(
                hour=7, minute=0, second=0, microsecond=0
            )
            self._attr_native_value = tomorrow
        await self._coordinator.async_set_departure(self._attr_native_value)

    async def async_set_value(self, value: datetime) -> None:
        # HA passes UTC; keep as-is, coordinator uses dt_util.now() for comparison
        self._attr_native_value = value
        await self._coordinator.async_set_departure(value)
        self.async_write_ha_state()
