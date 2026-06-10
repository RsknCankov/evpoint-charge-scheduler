"""Number platform for EVPoint Charge Scheduler.

These are the writable per-session inputs (battery capacity, target SoC, and
the manual current-SoC fallback). All values are persisted across HA restarts
via RestoreEntity so the next session starts pre-filled with the previous
session's inputs.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_PRICE_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_COST_TOLERANCE_PCT,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
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
        # ASAP charging current — always created; ASAP mode is always available.
        ASAPCurrentNumber(coordinator, entry),
    ]
    # Only show the manual current-SoC input if no external sensor is wired up
    if not entry.data.get(CONF_SOC_SENSOR) and not entry.options.get(CONF_SOC_SENSOR):
        entities.append(CurrentSoCNumber(coordinator, entry))
    # The cost-tolerance budget only matters when a price sensor is configured.
    if entry.data.get(CONF_PRICE_SENSOR) or entry.options.get(CONF_PRICE_SENSOR):
        entities.append(CostToleranceNumber(coordinator, entry))
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
        if self._coordinator.inputs_locked:
            self.async_write_ha_state()  # snap back to the running value
            raise HomeAssistantError(
                "Target SoC is locked while a charging session is active. "
                "Stop the session to change it."
            )
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
        if self._coordinator.inputs_locked:
            self.async_write_ha_state()  # snap back to the running value
            raise HomeAssistantError(
                "Current SoC is locked while a charging session is active. "
                "Stop the session to change it."
            )
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
        if self._coordinator.inputs_locked:
            self.async_write_ha_state()  # snap back to the running value
            raise HomeAssistantError(
                "Battery capacity is locked while a charging session is active. "
                "Stop the session to change it."
            )
        self._attr_native_value = value
        await self._coordinator.async_set_battery_capacity(value)
        self.async_write_ha_state()


class CostToleranceNumber(NumberEntity, RestoreEntity):
    """How much more than the cheapest plan to pay for gentler charging (%).

    Only created when a price sensor is configured. Like the other inputs, it
    persists across restarts and is locked while a session is active.
    """

    _attr_has_entity_name = True
    _attr_name = "Slow charging cost budget"
    _attr_icon = "mdi:cash-clock"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_cost_tolerance"
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
                self._attr_native_value = DEFAULT_COST_TOLERANCE_PCT
        if self._attr_native_value is None:
            self._attr_native_value = DEFAULT_COST_TOLERANCE_PCT
        await self._coordinator.async_set_cost_tolerance(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        if self._coordinator.inputs_locked:
            self.async_write_ha_state()  # snap back to the running value
            raise HomeAssistantError(
                "Slow charging cost budget is locked while a charging session "
                "is active. Stop the session to change it."
            )
        self._attr_native_value = value
        await self._coordinator.async_set_cost_tolerance(value)
        self.async_write_ha_state()


class ASAPCurrentNumber(NumberEntity, RestoreEntity):
    """ASAP charging current in amps.

    Sets the amperage used when finish_mode == ASAP. Persists across restarts
    via RestoreEntity and is locked during an active session (raise-and-revert).
    Always created — ASAP mode is always available regardless of other config.
    """

    _attr_has_entity_name = True
    _attr_name = "ASAP charging current"
    _attr_icon = "mdi:lightning-bolt"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_asap_current"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }
        cfg = {**entry.data, **entry.options}
        self._attr_native_min_value = float(cfg.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        self._attr_native_max_value = float(cfg.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last_state.state)
            except (TypeError, ValueError):
                self._attr_native_value = self._attr_native_max_value
        if self._attr_native_value is None:
            self._attr_native_value = self._attr_native_max_value
        await self._coordinator.async_set_asap_current(int(self._attr_native_value))

    async def async_set_native_value(self, value: float) -> None:
        if self._coordinator.inputs_locked:
            self.async_write_ha_state()  # snap back to the running value
            raise HomeAssistantError(
                "ASAP charging current is locked while a charging session is active. "
                "Stop the session to change it."
            )
        self._attr_native_value = value
        await self._coordinator.async_set_asap_current(int(value))
        self.async_write_ha_state()
