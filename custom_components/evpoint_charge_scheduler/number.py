"""Number platform for EVPoint Charge Scheduler.

These are the writable per-session inputs (battery capacity, target SoC, and
the manual current-SoC fallback). All values are persisted across HA restarts
via RestoreEntity so the next session starts pre-filled with the previous
session's inputs.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_SOC_SENSOR,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_TARGET_SOC,
    DOMAIN,
)
from .coordinator import SmartEVChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        BatteryCapacityNumber(coordinator, entry),
        TargetSoCNumber(coordinator, entry),
    ]
    # Only show the manual current-SoC input if no external sensor is wired up
    if not entry.data.get(CONF_SOC_SENSOR) and not entry.options.get(CONF_SOC_SENSOR):
        entities.append(CurrentSoCNumber(coordinator, entry))
    async_add_entities(entities)


class _EVPercentBase(NumberEntity, RestoreEntity):
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


class TargetSoCNumber(_EVPercentBase):
    _key = "target_soc"
    _attr_name = "Target SoC"
    _attr_icon = "mdi:battery-high"
    _default_value: float = DEFAULT_TARGET_SOC
    _attr_native_min_value = 10

    async def async_set_native_value(self, value: float) -> None:
        if self._coordinator.session_active:
            self.async_write_ha_state()  # locked during a session — revert UI
            return
        self._attr_native_value = value
        await self._push_to_coordinator()
        self.async_write_ha_state()

    async def _push_to_coordinator(self) -> None:
        await self._coordinator.async_set_target_soc(self._attr_native_value or DEFAULT_TARGET_SOC)


class CurrentSoCNumber(_EVPercentBase):
    _key = "current_soc"
    _attr_name = "Current SoC"
    _attr_icon = "mdi:battery"
    _default_value: float = 50.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Register so the coordinator can clear us when a session ends.
        self._coordinator.register_current_soc_entity(self)

    async def async_set_native_value(self, value: float) -> None:
        if self._coordinator.session_active:
            self.async_write_ha_state()  # locked during a session — revert UI
            return
        self._attr_native_value = value
        await self._push_to_coordinator()
        self.async_write_ha_state()

    async def _push_to_coordinator(self) -> None:
        if self._attr_native_value is None:
            self._coordinator.current_soc = None
        else:
            await self._coordinator.async_set_current_soc(self._attr_native_value)

    async def async_reset(self) -> None:
        """Clear the value at session end; user re-enters before the next start."""
        self._attr_native_value = None
        self._coordinator.current_soc = None
        self.async_write_ha_state()


class BatteryCapacityNumber(NumberEntity, RestoreEntity):
    """Battery capacity in kWh. Pre-filled from the last value entered."""

    _attr_has_entity_name = True
    _attr_translation_key = "battery_capacity"
    _attr_name = "Battery capacity"
    _attr_icon = "mdi:car-battery"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 250
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_battery_capacity"
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
                self._attr_native_value = None
        if self._attr_native_value is None:
            seed = (
                self._entry.options.get(CONF_BATTERY_CAPACITY)
                or self._entry.data.get(CONF_BATTERY_CAPACITY)
                or DEFAULT_BATTERY_CAPACITY
            )
            self._attr_native_value = float(seed)
        await self._coordinator.async_set_battery_capacity(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        if self._coordinator.session_active:
            self.async_write_ha_state()  # locked during a session — revert UI
            return
        self._attr_native_value = value
        await self._coordinator.async_set_battery_capacity(value)
        self.async_write_ha_state()
