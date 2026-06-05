"""Config flow for EVPoint Charge Scheduler."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_APARTMENT_CURRENT_SENSOR,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGER_POWER_SENSOR,
    CONF_CHARGER_SWITCH,
    CONF_CHARGING_LOSS,
    CONF_FINISH_MODE,
    CONF_HEADROOM,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_NIGHT_END,
    CONF_NIGHT_START,
    CONF_NIGHT_TARIFF_VALUE,
    CONF_OCPP_DEVID,
    CONF_OCPP_SET_RATE_SERVICE,
    CONF_CHARGING_PROFILE_ID,
    CONF_PHASES,
    CONF_PRICE_SENSOR,
    CONF_SAFETY_MARGIN_HOURS,
    CONF_SOC_SENSOR,
    CONF_TARIFF_SENSOR,
    CONF_TOTAL_LIMIT,
    CONF_VOLTAGE,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGING_LOSS,
    DEFAULT_CHARGING_PROFILE_ID,
    DEFAULT_FINISH_MODE,
    DEFAULT_HEADROOM,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_NIGHT_TARIFF_VALUE,
    DEFAULT_OCPP_SERVICE,
    DEFAULT_PHASES,
    DEFAULT_SAFETY_MARGIN_HOURS,
    DEFAULT_TOTAL_LIMIT,
    DEFAULT_VOLTAGE,
    DOMAIN,
    FINISH_MODES,
)


def _entity_selector(domain: str | list[str] | None = None):
    cfg: dict[str, Any] = {}
    if domain:
        cfg["domain"] = domain
    return selector.EntitySelector(selector.EntitySelectorConfig(**cfg))


def _build_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            # External entities - all optional. The integration degrades
            # gracefully when any of these are left blank:
            #   - No tariff sensor       -> derive night/day from configured times
            #   - No apartment sensor    -> no load balancing (full charger limit available)
            #   - No SoC sensor          -> manual number entity is created instead
            #   - No charger switch      -> integration only sets the current limit
            #   - No OCPP service        -> "advisory mode": decisions exposed via sensors
            vol.Optional(CONF_TARIFF_SENSOR, default=d.get(CONF_TARIFF_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
            vol.Optional(CONF_NIGHT_TARIFF_VALUE, default=d.get(CONF_NIGHT_TARIFF_VALUE, DEFAULT_NIGHT_TARIFF_VALUE)): str,
            # Optional electricity price sensor. Used to learn day vs night
            # prices from history so end_of_night can trade a little extra cost
            # for gentler (slower) charging within the user's budget.
            vol.Optional(CONF_PRICE_SENSOR, default=d.get(CONF_PRICE_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
            vol.Optional(CONF_APARTMENT_CURRENT_SENSOR, default=d.get(CONF_APARTMENT_CURRENT_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
            vol.Optional(CONF_SOC_SENSOR, default=d.get(CONF_SOC_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
            # Optional charger power sensor (W or kW). When configured, the
            # coordinator integrates delivered energy over each cycle so a
            # session can finish on energy counted rather than the departure
            # backstop alone. Omitted -> energy counting is inert.
            vol.Optional(CONF_CHARGER_POWER_SENSOR, default=d.get(CONF_CHARGER_POWER_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
            vol.Optional(CONF_CHARGER_SWITCH, default=d.get(CONF_CHARGER_SWITCH, vol.UNDEFINED)): _entity_selector("switch"),

            # OCPP service to set current limit - optional.
            # Service is invoked with this full payload:
            #   data:
            #     devid: <ocpp_devid>
            #     limit_amps: <amps>
            #     limit_watts: <amps * voltage * (sqrt(3) if 3-phase else 1)>
            #     custom_profile:
            #       chargingProfileId: <charging_profile_id>
            #       stackLevel: 0
            #       chargingProfileKind: Relative
            #       chargingProfilePurpose: ChargePointMaxProfile
            #       chargingSchedule:
            #         chargingRateUnit: A
            #         chargingSchedulePeriod:
            #           - startPeriod: 0
            #             limit: <amps>
            vol.Optional(CONF_OCPP_SET_RATE_SERVICE, default=d.get(CONF_OCPP_SET_RATE_SERVICE, DEFAULT_OCPP_SERVICE)): str,
            vol.Optional(CONF_OCPP_DEVID, default=d.get(CONF_OCPP_DEVID, vol.UNDEFINED)): str,
            vol.Optional(CONF_CHARGING_PROFILE_ID, default=d.get(CONF_CHARGING_PROFILE_ID, DEFAULT_CHARGING_PROFILE_ID)): vol.Coerce(int),

            # Car / charger spec
            vol.Required(CONF_BATTERY_CAPACITY, default=d.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)): vol.Coerce(float),
            vol.Required(CONF_CHARGING_LOSS, default=d.get(CONF_CHARGING_LOSS, DEFAULT_CHARGING_LOSS)): vol.Coerce(float),
            vol.Required(CONF_VOLTAGE, default=d.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)): vol.Coerce(int),
            vol.Required(CONF_PHASES, default=d.get(CONF_PHASES, DEFAULT_PHASES)): vol.In([1, 3]),
            vol.Required(CONF_MIN_CURRENT, default=d.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT)): vol.Coerce(int),
            vol.Required(CONF_MAX_CURRENT, default=d.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT)): vol.Coerce(int),

            # Load balancing
            vol.Required(CONF_TOTAL_LIMIT, default=d.get(CONF_TOTAL_LIMIT, DEFAULT_TOTAL_LIMIT)): vol.Coerce(int),
            vol.Required(CONF_HEADROOM, default=d.get(CONF_HEADROOM, DEFAULT_HEADROOM)): vol.Coerce(int),

            # Tariff schedule
            vol.Required(CONF_NIGHT_START, default=d.get(CONF_NIGHT_START, DEFAULT_NIGHT_START)): str,
            vol.Required(CONF_NIGHT_END, default=d.get(CONF_NIGHT_END, DEFAULT_NIGHT_END)): str,
            vol.Required(CONF_SAFETY_MARGIN_HOURS, default=d.get(CONF_SAFETY_MARGIN_HOURS, DEFAULT_SAFETY_MARGIN_HOURS)): vol.Coerce(float),

            # When the charge should aim to finish:
            #   asap          — start as early as tariff allows; the original behaviour
            #   end_of_night  — finish just before night tariff ends (gentler on battery)
            #   departure     — finish exactly at departure (may pay some day-tariff energy)
            vol.Optional(CONF_FINISH_MODE, default=d.get(CONF_FINISH_MODE, DEFAULT_FINISH_MODE)): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=FINISH_MODES,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="finish_mode",
                )
            ),
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EVPoint Charge Scheduler."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="EVPoint Charge Scheduler", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_build_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    """Allow editing config after setup.

    `self.config_entry` is provided automatically by the base OptionsFlow in
    modern HA — do NOT set it in __init__ (recent cores made it a read-only
    property, and assigning it raises, which surfaces as a 500 when opening
    the options form).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Merge data + options so the user sees their current values
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_build_schema(defaults))
