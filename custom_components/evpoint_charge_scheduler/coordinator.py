"""DataUpdateCoordinator for EVPoint Charge Scheduler.

This is the brain of the integration. Every 30 seconds (and on relevant state
changes), it:

  1. Reads the user's inputs (target SoC, current SoC, departure time).
  2. Reads external sensors (electricity tariff, apartment current).
  3. Computes the energy needed, time available, night/day windows.
  4. Decides whether to charge, wait, or supplement during the day.
  5. Applies the apartment-priority load balancer.
  6. Sends the resulting current limit / start / stop to the OCPP charger.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_CHARGE_DAY_SUPPLEMENT,
    ACTION_CHARGE_MAX,
    ACTION_DISABLED,
    ACTION_DONE,
    ACTION_TOO_LATE,
    ACTION_WAIT_FOR_NIGHT,
    ACTION_WAIT_FOR_START_TIME,
    CONF_APARTMENT_CURRENT_SENSOR,
    CONF_BATTERY_CAPACITY,
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
    DEFAULT_PHASES,
    DEFAULT_SAFETY_MARGIN_HOURS,
    DEFAULT_TARGET_SOC,
    DEFAULT_TOTAL_LIMIT,
    DEFAULT_VOLTAGE,
    DOMAIN,
    FINISH_MODE_ASAP,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
    PLAN_ALREADY_AT_TARGET,
    PLAN_INSUFFICIENT_TIME,
    PLAN_OK,
    PLAN_TOO_LATE,
    THROTTLE_APARTMENT_HIGH,
    THROTTLE_BY_APARTMENT,
    THROTTLE_SMART_PAUSE,
    THROTTLE_UNRESTRICTED,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _parse_time(value: str | None, default: str) -> time:
    """Parse a HH:MM[:SS] string to a time, falling back to a default."""
    raw = value or default
    parts = raw.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(hour=h, minute=m)


def _safe_float(state_value: Any, default: float = 0.0) -> float:
    if state_value in (None, STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return default
    try:
        return float(state_value)
    except (TypeError, ValueError):
        return default


class SmartEVChargingCoordinator(DataUpdateCoordinator):
    """Coordinates the smart charging logic."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self._config = {**entry.data, **entry.options}

        # User-adjustable values held by our writable entities.
        # These are updated by the entity classes when the user changes them
        # in the UI, and persisted via RestoreEntity.
        self.target_soc: float = DEFAULT_TARGET_SOC
        self.current_soc: float | None = None  # None means "use external sensor"
        self.departure_time: datetime | None = None
        self.smart_charging_enabled: bool = True

        # Last applied charger state, used to avoid spamming the charger.
        self._last_applied_current: int | None = None
        self._last_applied_running: bool | None = None
        self._unsub_state_listener = None

    async def async_config_entry_first_refresh(self) -> None:
        """Subscribe to external sensors and do the first update."""
        await super().async_config_entry_first_refresh()
        # Trigger an immediate recompute when tariff or apartment current change
        watched = [
            self._config.get(CONF_TARIFF_SENSOR),
            self._config.get(CONF_APARTMENT_CURRENT_SENSOR),
        ]
        soc_sensor = self._config.get(CONF_SOC_SENSOR)
        if soc_sensor:
            watched.append(soc_sensor)
        watched = [e for e in watched if e]
        if watched:
            self._unsub_state_listener = async_track_state_change_event(
                self.hass, watched, self._handle_external_change
            )

    async def async_shutdown(self) -> None:
        if self._unsub_state_listener is not None:
            self._unsub_state_listener()
            self._unsub_state_listener = None

    @callback
    def _handle_external_change(self, event) -> None:
        """Recompute immediately when a watched sensor changes."""
        self.hass.async_create_task(self.async_refresh())

    # --- Public setters used by writable entity classes ---

    async def async_set_target_soc(self, value: float) -> None:
        self.target_soc = float(value)
        await self.async_refresh()

    async def async_set_current_soc(self, value: float) -> None:
        self.current_soc = float(value)
        await self.async_refresh()

    async def async_set_departure(self, value: datetime | None) -> None:
        self.departure_time = value
        await self.async_refresh()

    async def async_set_enabled(self, value: bool) -> None:
        self.smart_charging_enabled = bool(value)
        await self.async_refresh()

    # --- Main update loop ---

    async def _async_update_data(self) -> dict[str, Any]:
        cfg = self._config
        now = dt_util.now()

        # Inputs
        battery = float(cfg.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY))
        loss = float(cfg.get(CONF_CHARGING_LOSS, DEFAULT_CHARGING_LOSS))
        voltage = float(cfg.get(CONF_VOLTAGE, DEFAULT_VOLTAGE))
        phases = int(cfg.get(CONF_PHASES, DEFAULT_PHASES))
        min_a = int(cfg.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        max_a = int(cfg.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        total_limit = int(cfg.get(CONF_TOTAL_LIMIT, DEFAULT_TOTAL_LIMIT))
        headroom = int(cfg.get(CONF_HEADROOM, DEFAULT_HEADROOM))
        safety_margin = float(cfg.get(CONF_SAFETY_MARGIN_HOURS, DEFAULT_SAFETY_MARGIN_HOURS))
        night_start = _parse_time(cfg.get(CONF_NIGHT_START), DEFAULT_NIGHT_START)
        night_end = _parse_time(cfg.get(CONF_NIGHT_END), DEFAULT_NIGHT_END)
        night_value = cfg.get(CONF_NIGHT_TARIFF_VALUE, DEFAULT_NIGHT_TARIFF_VALUE)

        factor = math.sqrt(3) if phases >= 3 else 1.0
        max_kw = factor * voltage * max_a / 1000.0

        # Current SoC: external sensor takes priority if configured
        soc_sensor_id = cfg.get(CONF_SOC_SENSOR)
        if soc_sensor_id:
            soc_state = self.hass.states.get(soc_sensor_id)
            current_soc = _safe_float(soc_state.state if soc_state else None, default=0.0)
        else:
            current_soc = self.current_soc if self.current_soc is not None else 0.0

        # Energy needed
        delta_soc = max(0.0, self.target_soc - current_soc)
        energy_needed = delta_soc / 100.0 * battery * loss

        # Time to departure
        hours_to_dep = 0.0
        if self.departure_time is not None:
            delta = (self.departure_time - now).total_seconds() / 3600.0
            hours_to_dep = max(0.0, delta)

        # Night hours available between now and departure
        night_hours = self._night_hours_between(
            now, self.departure_time, night_start, night_end
        ) if self.departure_time else 0.0
        day_hours = max(0.0, hours_to_dep - night_hours)

        # Hours needed at max
        hours_needed_at_max = energy_needed / max_kw if max_kw > 0 else 0.0
        slack = hours_to_dep - hours_needed_at_max

        # Deficit: what night-at-max cannot cover
        deficit_kwh = max(0.0, energy_needed - night_hours * max_kw)

        # Plan status (informational)
        if energy_needed <= 0:
            plan_status = PLAN_ALREADY_AT_TARGET
        elif hours_to_dep <= 0:
            plan_status = PLAN_TOO_LATE
        elif hours_needed_at_max > hours_to_dep:
            plan_status = PLAN_INSUFFICIENT_TIME
        else:
            plan_status = PLAN_OK

        # Current tariff state. If no sensor is configured, derive from the
        # configured night window using the local clock.
        tariff_sensor_id = cfg.get(CONF_TARIFF_SENSOR)
        if tariff_sensor_id:
            tariff_state = self.hass.states.get(tariff_sensor_id)
            is_night_now = (
                tariff_state is not None
                and tariff_state.state == night_value
            )
            tariff_source = "sensor"
        else:
            is_night_now = self._is_in_night_window(now, night_start, night_end)
            tariff_source = "schedule"

        # Day charging rate needed (only if deficit > 0)
        day_kw = deficit_kwh / day_hours if (deficit_kwh > 0 and day_hours > 0) else 0.0
        day_current = self._kw_to_current(day_kw, voltage, factor, min_a, max_a) if day_kw > 0 else 0

        # Resolve the finish-mode strategy and compute the latest start time.
        # asap:          target_finish = now (charge immediately when allowed)
        # end_of_night:  target_finish = the latest occurrence of night_end before departure
        # departure:     target_finish = the departure time itself
        finish_mode = cfg.get(CONF_FINISH_MODE, DEFAULT_FINISH_MODE)
        charge_duration_hours = (energy_needed / max_kw) if max_kw > 0 else 0.0
        if finish_mode == FINISH_MODE_END_OF_NIGHT and self.departure_time is not None:
            target_finish = self._latest_night_end_before(self.departure_time, night_end)
        elif finish_mode == FINISH_MODE_DEPARTURE and self.departure_time is not None:
            target_finish = self.departure_time
        else:
            target_finish = None  # asap, or no departure set yet

        if target_finish is not None and charge_duration_hours > 0:
            latest_start = target_finish - timedelta(
                hours=charge_duration_hours + safety_margin
            )
        else:
            latest_start = now  # charge immediately under asap (or fallback)
        should_have_started = now >= latest_start

        # Decide recommended action
        if not self.smart_charging_enabled:
            action = ACTION_DISABLED
        elif energy_needed <= 0:
            action = ACTION_DONE
        elif hours_to_dep <= 0:
            action = ACTION_TOO_LATE
        elif deficit_kwh > 0:
            # Always honour the deficit — applies in every finish mode
            action = ACTION_CHARGE_DAY_SUPPLEMENT
        elif slack < safety_margin:
            # Safety: too tight to wait around
            action = ACTION_CHARGE_MAX
        elif finish_mode == FINISH_MODE_DEPARTURE:
            # Tariff irrelevant; just gate on time
            if should_have_started:
                action = ACTION_CHARGE_MAX
            else:
                action = ACTION_WAIT_FOR_START_TIME
        elif finish_mode == FINISH_MODE_END_OF_NIGHT:
            # Must wait for night, then hold until latest_start within night
            if not is_night_now:
                action = ACTION_WAIT_FOR_NIGHT
            elif should_have_started:
                action = ACTION_CHARGE_MAX
            else:
                action = ACTION_WAIT_FOR_START_TIME
        else:  # FINISH_MODE_ASAP — original behaviour
            if is_night_now:
                action = ACTION_CHARGE_MAX
            else:
                action = ACTION_WAIT_FOR_NIGHT

        # Plan-level target current (before load balancing)
        if action == ACTION_CHARGE_MAX:
            planned_current = max_a
        elif action == ACTION_CHARGE_DAY_SUPPLEMENT:
            planned_current = day_current
        else:
            planned_current = 0

        # Load balancing against apartment consumption. Without a sensor,
        # we assume the apartment isn't drawing anything and let the charger
        # use the full configured limit.
        apt_sensor_id = cfg.get(CONF_APARTMENT_CURRENT_SENSOR)
        if apt_sensor_id:
            apt_state = self.hass.states.get(apt_sensor_id)
            apartment_current = _safe_float(apt_state.state if apt_state else None)
            load_balancing_active = True
        else:
            apartment_current = 0.0
            load_balancing_active = False
        available_current = max(0, total_limit - headroom - int(apartment_current))

        if planned_current <= 0:
            dynamic_current = 0
            throttle_reason = THROTTLE_SMART_PAUSE
        elif available_current < min_a:
            dynamic_current = 0
            throttle_reason = THROTTLE_APARTMENT_HIGH
        elif available_current < planned_current:
            dynamic_current = available_current
            throttle_reason = THROTTLE_BY_APARTMENT
        else:
            dynamic_current = planned_current
            throttle_reason = THROTTLE_UNRESTRICTED

        # Determine the integration's effective control mode based on what's
        # actually wired up. Surfaces as a sensor so the user can see whether
        # commands are being sent to the charger or just computed.
        has_ocpp = bool(cfg.get(CONF_OCPP_SET_RATE_SERVICE)) and bool(cfg.get(CONF_OCPP_DEVID))
        has_switch = bool(cfg.get(CONF_CHARGER_SWITCH))
        if has_ocpp and has_switch:
            control_mode = "active"
        elif has_ocpp and not has_switch:
            control_mode = "current_only"
        elif has_switch and not has_ocpp:
            control_mode = "switch_only"
        else:
            control_mode = "advisory"

        # Push commands to charger only on change (and only for the wired-up parts)
        await self._apply_to_charger(dynamic_current)

        return {
            "energy_needed": round(energy_needed, 2),
            "hours_to_departure": round(hours_to_dep, 3),
            "night_hours_available": round(night_hours, 2),
            "day_hours_available": round(day_hours, 2),
            "max_charge_power": round(max_kw, 2),
            "hours_needed_at_max": round(hours_needed_at_max, 3),
            "slack_hours": round(slack, 3),
            "day_energy_deficit": round(deficit_kwh, 2),
            "day_charging_rate": round(day_kw, 2),
            "day_charging_current": day_current,
            "planned_current": planned_current,
            "available_current": available_current,
            "dynamic_target_current": dynamic_current,
            "recommended_action": action,
            "throttle_reason": throttle_reason,
            "plan_status": plan_status,
            "current_soc": current_soc,
            "is_night_now": is_night_now,
            "tariff_source": tariff_source,
            "apartment_current": int(apartment_current),
            "load_balancing_active": load_balancing_active,
            "control_mode": control_mode,
            "finish_mode": finish_mode,
            "charge_duration_hours": round(charge_duration_hours, 3),
            "latest_start_time": latest_start if target_finish is not None else None,
        }

    # --- Helpers ---

    @staticmethod
    def _kw_to_current(
        kw: float, voltage: float, factor: float, min_a: int, max_a: int
    ) -> int:
        if kw <= 0:
            return 0
        raw = (kw * 1000.0) / (factor * voltage)
        rounded = math.ceil(raw)
        return max(min_a, min(rounded, max_a))

    @staticmethod
    def _is_in_night_window(at: datetime, night_start: time, night_end: time) -> bool:
        """True if `at` is within the daily night-tariff window."""
        ns = night_start.hour * 60 + night_start.minute
        ne = night_end.hour * 60 + night_end.minute
        tod = at.hour * 60 + at.minute
        if ns < ne:
            return ns <= tod < ne
        return tod >= ns or tod < ne

    @staticmethod
    def _latest_night_end_before(target: datetime, night_end_time: time) -> datetime:
        """Most recent occurrence of `night_end_time` strictly before `target`.

        Used by finish_mode = end_of_night to compute when the charge should
        wrap up — typically the morning of the departure.
        """
        candidate = target.replace(
            hour=night_end_time.hour,
            minute=night_end_time.minute,
            second=0,
            microsecond=0,
        )
        if candidate >= target:
            candidate -= timedelta(days=1)
        return candidate

    @staticmethod
    def _night_hours_between(
        start: datetime,
        end: datetime | None,
        night_start: time,
        night_end: time,
    ) -> float:
        """Hours of night-tariff window contained in [start, end].

        Walks the range in 5-minute steps. Accurate to ~5 min, plenty for
        planning purposes.
        """
        if end is None or end <= start:
            return 0.0
        ns = night_start.hour * 60 + night_start.minute
        ne = night_end.hour * 60 + night_end.minute
        wraps = ns >= ne  # crosses midnight

        total_min = int(math.ceil((end - start).total_seconds() / 60))
        step = 5
        night_min = 0
        for i in range(0, total_min, step):
            t = start + timedelta(minutes=i)
            tod = t.hour * 60 + t.minute
            if wraps:
                in_window = tod >= ns or tod < ne
            else:
                in_window = ns <= tod < ne
            if in_window:
                night_min += step
        return night_min / 60.0

    async def _apply_to_charger(self, dynamic_current: int) -> None:
        """Send commands to the charger only when state has changed.

        The current limit is pushed via an OCPP charging profile. The full
        payload sent to the configured service is:

            data:
              devid: <ocpp_devid>
              limit_amps: <dynamic_current>
              limit_watts: <dynamic_current * voltage * (sqrt(3) if 3-phase else 1)>
              custom_profile:
                chargingProfileId: <charging_profile_id>
                stackLevel: 0
                chargingProfileKind: Relative
                chargingProfilePurpose: ChargePointMaxProfile
                chargingSchedule:
                  chargingRateUnit: A
                  chargingSchedulePeriod:
                    - startPeriod: 0
                      limit: <dynamic_current>

        Each side (current-limit service and start/stop switch) is independently
        optional. If neither is configured, the integration runs in pure
        advisory mode.
        """
        cfg = self._config
        service_call = cfg.get(CONF_OCPP_SET_RATE_SERVICE)
        devid = cfg.get(CONF_OCPP_DEVID)
        profile_id = int(cfg.get(CONF_CHARGING_PROFILE_ID, DEFAULT_CHARGING_PROFILE_ID))
        voltage = float(cfg.get(CONF_VOLTAGE, DEFAULT_VOLTAGE))
        phases = int(cfg.get(CONF_PHASES, DEFAULT_PHASES))
        switch_entity = cfg.get(CONF_CHARGER_SWITCH)

        factor = math.sqrt(3) if phases >= 3 else 1.0
        should_run = dynamic_current > 0

        # Push the charging profile (both service and devid required for this path)
        if (
            service_call
            and devid
            and should_run
            and dynamic_current != self._last_applied_current
        ):
            try:
                limit_watts = int(round(factor * voltage * dynamic_current))
                domain, service = service_call.split(".", 1)
                data: dict[str, Any] = {
                    "devid": devid,
                    "limit_amps": dynamic_current,
                    "limit_watts": limit_watts,
                    "custom_profile": {
                        "chargingProfileId": profile_id,
                        "stackLevel": 0,
                        "chargingProfileKind": "Relative",
                        "chargingProfilePurpose": "ChargePointMaxProfile",
                        "chargingSchedule": {
                            "chargingRateUnit": "A",
                            "chargingSchedulePeriod": [
                                {"startPeriod": 0, "limit": dynamic_current}
                            ],
                        },
                    },
                }
                await self.hass.services.async_call(
                    domain, service, data, blocking=False
                )
                self._last_applied_current = dynamic_current
                _LOGGER.debug(
                    "Pushed charging profile: devid=%s limit=%sA (%sW) profile_id=%s",
                    devid, dynamic_current, limit_watts, profile_id,
                )
            except Exception as err:
                _LOGGER.error("Failed to set charge rate: %s", err)

        # Start / stop the charger transaction if we have a switch to toggle
        if switch_entity and should_run != self._last_applied_running:
            try:
                service = "turn_on" if should_run else "turn_off"
                await self.hass.services.async_call(
                    "switch",
                    service,
                    target={"entity_id": switch_entity},
                    blocking=False,
                )
                self._last_applied_running = should_run
                _LOGGER.debug("Switched charger %s", "on" if should_run else "off")
            except Exception as err:
                _LOGGER.error("Failed to switch charger: %s", err)
