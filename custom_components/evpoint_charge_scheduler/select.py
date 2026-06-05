"""Select platform: runtime finish-mode dropdown.

Lets the user switch finish_mode (asap / end_of_night / departure) from the
HA UI without re-running the options flow. The selected value overrides the
config-seeded value and persists across restarts via RestoreEntity.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
        # Finish mode is the "lost night" input: the Phase-1 defect was a locked
        # pick vanishing silently (no error, no effect). The fix is raise-AND-allow
        # — the pick always reaches the coordinator so the running session uses the
        # user's intent, and when inputs are locked a visible HomeAssistantError
        # surfaces that it took effect instead of swallowing it. The single
        # coordinator.inputs_locked predicate is the one source of truth every
        # writable entity consults; the dropdown read-back (UX-02) reflects the
        # executed mode so the dashboard can never disagree with recommended_action.
        self._attr_current_option = option
        await self._coordinator.async_set_finish_mode(option)
        self.async_write_ha_state()
        if self._coordinator.inputs_locked:
            raise HomeAssistantError(
                "Finish mode is locked while a charging session is active. "
                "The new mode was applied to the running session — stop the "
                "session to change inputs without this warning."
            )
