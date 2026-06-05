"""Sensor platform for EVPoint Charge Scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartEVChargingCoordinator


@dataclass(frozen=True, kw_only=True)
class EVSensorDescription(SensorEntityDescription):
    """A sensor backed by a key in the coordinator's data dict."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[EVSensorDescription, ...] = (
    EVSensorDescription(
        key="energy_needed",
        name="Energy needed",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-charging",
        value_fn=lambda d: d.get("energy_needed"),
    ),
    EVSensorDescription(
        key="hours_to_departure",
        name="Hours to departure",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:clock-outline",
        value_fn=lambda d: d.get("hours_to_departure"),
    ),
    EVSensorDescription(
        key="night_hours_available",
        name="Night hours available",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:weather-night",
        value_fn=lambda d: d.get("night_hours_available"),
    ),
    EVSensorDescription(
        key="day_hours_available",
        name="Day hours available",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:weather-sunny",
        value_fn=lambda d: d.get("day_hours_available"),
    ),
    EVSensorDescription(
        key="max_charge_power",
        name="Max charge power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        icon="mdi:flash",
        value_fn=lambda d: d.get("max_charge_power"),
    ),
    EVSensorDescription(
        key="slack_hours",
        name="Slack hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:clock-alert-outline",
        value_fn=lambda d: d.get("slack_hours"),
    ),
    EVSensorDescription(
        key="day_energy_deficit",
        name="Day energy deficit",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-alert",
        value_fn=lambda d: d.get("day_energy_deficit"),
    ),
    EVSensorDescription(
        key="day_charging_current",
        name="Day charging current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        value_fn=lambda d: d.get("day_charging_current"),
    ),
    EVSensorDescription(
        key="available_current",
        name="Available current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        value_fn=lambda d: d.get("available_current"),
    ),
    EVSensorDescription(
        key="dynamic_target_current",
        name="Dynamic target current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:ev-station",
        value_fn=lambda d: d.get("dynamic_target_current"),
    ),
    EVSensorDescription(
        key="recommended_action",
        name="Recommended action",
        icon="mdi:robot",
        value_fn=lambda d: d.get("recommended_action"),
    ),
    EVSensorDescription(
        key="throttle_reason",
        name="Throttle reason",
        icon="mdi:speedometer-slow",
        value_fn=lambda d: d.get("throttle_reason"),
    ),
    EVSensorDescription(
        key="plan_status",
        name="Plan status",
        icon="mdi:clipboard-check-outline",
        value_fn=lambda d: d.get("plan_status"),
    ),
    EVSensorDescription(
        key="control_mode",
        name="Control mode",
        icon="mdi:cog-outline",
        value_fn=lambda d: d.get("control_mode"),
    ),
    EVSensorDescription(
        key="tariff_source",
        name="Tariff source",
        icon="mdi:swap-horizontal",
        value_fn=lambda d: d.get("tariff_source"),
    ),
    EVSensorDescription(
        key="finish_mode",
        name="Finish mode",
        icon="mdi:timer-cog-outline",
        value_fn=lambda d: d.get("finish_mode"),
    ),
    EVSensorDescription(
        key="latest_start_time",
        name="Latest start time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:timer-play-outline",
        value_fn=lambda d: d.get("latest_start_time"),
    ),
    EVSensorDescription(
        key="gentle_target_current",
        name="Gentle target current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        value_fn=lambda d: d.get("gentle_target_current"),
    ),
    EVSensorDescription(
        key="learned_night_start",
        name="Learned night start",
        icon="mdi:weather-night",
        value_fn=lambda d: d.get("learned_night_start"),
    ),
    EVSensorDescription(
        key="learned_night_end",
        name="Learned night end",
        icon="mdi:weather-sunset-up",
        value_fn=lambda d: d.get("learned_night_end"),
    ),
    EVSensorDescription(
        key="night_window_source",
        name="Night window source",
        icon="mdi:swap-horizontal",
        value_fn=lambda d: d.get("night_window_source"),
    ),
    EVSensorDescription(
        key="learned_night_price",
        name="Learned night price",
        icon="mdi:cash",
        value_fn=lambda d: d.get("learned_night_price"),
    ),
    EVSensorDescription(
        key="learned_day_price",
        name="Learned day price",
        icon="mdi:cash",
        value_fn=lambda d: d.get("learned_day_price"),
    ),
    EVSensorDescription(
        key="price_source",
        name="Price source",
        icon="mdi:tag-text-outline",
        value_fn=lambda d: d.get("price_source"),
    ),
    EVSensorDescription(
        key="energy_source",
        name="Energy source",
        icon="mdi:counter",
        value_fn=lambda d: d.get("energy_source"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [EVSensor(coordinator, entry, desc) for desc in SENSORS]
    # The delivered-energy accumulator persists across HA restarts via
    # RestoreEntity, so it can't be a plain coordinator-data sensor.
    entities.append(DeliveredEnergyRestoreSensor(coordinator, entry))
    async_add_entities(entities)


class EVSensor(CoordinatorEntity[SmartEVChargingCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartEVChargingCoordinator,
        entry: ConfigEntry,
        description: EVSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class DeliveredEnergyRestoreSensor(
    CoordinatorEntity[SmartEVChargingCoordinator],
    SensorEntity,
    RestoreEntity,
):
    """Delivered-energy accumulator, persisted across HA restarts.

    Mirrors the binary_sensor.SessionActiveBinarySensor RestoreEntity pattern:
    on add, the last stored numeric state is pushed back into the coordinator
    (via async_set_delivered_energy) so a mid-charge restart resumes progress;
    the live value is read straight from coordinator.delivered_energy_kwh.
    """

    _attr_has_entity_name = True
    _attr_name = "Delivered energy"
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    # Resets to 0 on each session start, so it's a per-session total, not a
    # monotonically-increasing meter — TOTAL, not TOTAL_INCREASING.
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_delivered_energy_kwh"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        try:
            restored = float(last_state.state)
        except (TypeError, ValueError):
            return  # non-numeric / unavailable -> leave accumulator at 0
        if restored >= 0:
            await self.coordinator.async_set_delivered_energy(restored)

    @property
    def native_value(self) -> Any:
        return round(self.coordinator.delivered_energy_kwh, 3)
