"""Select platform: runtime finish-mode dropdown.

Lets the user switch finish_mode (asap / end_of_night / departure) from the
HA UI without re-running the options flow. The selected value overrides the
config-seeded value and persists across restarts via RestoreEntity.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_FINISH_MODE,
    DEFAULT_FINISH_MODE,
    DOMAIN,
    FINISH_MODES,
)
from .coordinator import SmartEVChargingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FinishModeSelect(coordinator, entry)])


class FinishModeSelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-cog-outline"
    _attr_translation_key = "finish_mode"
    _attr_options = FINISH_MODES

    def __init__(
        self, coordinator: SmartEVChargingCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_finish_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EVPoint Charge Scheduler",
            "manufacturer": "EVPoint Charge Scheduler",
        }
        self._attr_current_option: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in FINISH_MODES:
            self._attr_current_option = last_state.state
        else:
            seed = (
                self._entry.options.get(CONF_FINISH_MODE)
                or self._entry.data.get(CONF_FINISH_MODE)
                or DEFAULT_FINISH_MODE
            )
            self._attr_current_option = (
                seed if seed in FINISH_MODES else DEFAULT_FINISH_MODE
            )
        await self._coordinator.async_set_finish_mode(self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        if option not in FINISH_MODES:
            return
        self._attr_current_option = option
        await self._coordinator.async_set_finish_mode(option)
        self.async_write_ha_state()
