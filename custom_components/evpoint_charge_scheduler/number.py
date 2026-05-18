"""Number platform for EVPoint Charge Scheduler.

These are the writable user inputs (target SoC, current SoC fallback).
Persisted across HA restarts via RestoreEntity.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_TARGET_SOC, DOMAIN
from .coordinator import SmartEVChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        TargetSoCNumber(coordinator, entry),
    ]
    # Only show the manual current-SoC input if no external sensor is wired up
    from .const import CONF_SOC_SENSOR
    if not entry.data.get(CONF_SOC_SENSOR) and not entry.options.get(CONF_SOC_SENSOR):
        entities.append(CurrentSoCNumber(coordinator, entry))
    async_add_entities(entities)


class _EVNumberBase(NumberEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last_state.state)
            except (TypeError, ValueError):
                self._attr_native_value = self._default_value
        else:
            self._attr_native_value = self._default_value
        await self._push_to_coordinator()


class TargetSoCNumber(_EVNumberBase):
    _key = "target_soc"
    _attr_name = "Target SoC"
    _attr_icon = "mdi:battery-high"
    _default_value: float = DEFAULT_TARGET_SOC
    _attr_native_min_value = 10

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        await self._push_to_coordinator()
        self.async_write_ha_state()

    async def _push_to_coordinator(self) -> None:
        await self._coordinator.async_set_target_soc(self._attr_native_value or DEFAULT_TARGET_SOC)


class CurrentSoCNumber(_EVNumberBase):
    _key = "current_soc"
    _attr_name = "Current SoC"
    _attr_icon = "mdi:battery"
    _default_value: float = 50.0

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        await self._push_to_coordinator()
        self.async_write_ha_state()

    async def _push_to_coordinator(self) -> None:
        await self._coordinator.async_set_current_soc(self._attr_native_value or 0.0)
