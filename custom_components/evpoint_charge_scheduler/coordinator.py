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

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_CHARGE_DAY_SUPPLEMENT,
    ACTION_CHARGE_GENTLE,
    ACTION_CHARGE_MAX,
    ACTION_DONE,
    ACTION_IDLE,
    ACTION_TOO_LATE,
    ACTION_WAIT_FOR_NIGHT,
    ACTION_WAIT_FOR_START_TIME,
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
    DEFAULT_COST_TOLERANCE_PCT,
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
    END_BACKSTOP,
    END_SUCCESS,
    FINISH_MODE_ASAP,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
    NIGHT_WINDOW_LEARN_DAYS,
    PLAN_ALREADY_AT_TARGET,
    PLAN_INSUFFICIENT_TIME,
    PLAN_OK,
    PLAN_TOO_LATE,
    THROTTLE_APARTMENT_HIGH,
    THROTTLE_BY_APARTMENT,
    THROTTLE_SMART_PAUSE,
    THROTTLE_UNRESTRICTED,
    UPDATE_INTERVAL,
    WATCHDOG_ZERO_CYCLES,
    WATCHDOG_ZERO_POWER_W,
)
from .load_balancer import balance
from .models import EndInputs, LoadInputs, PlanInputs
from .planner import plan, should_end

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
        self.current_soc: float | None = None  # None means "external sensor or unknown"
        self.departure_time: datetime | None = None
        # Seeded from the config entry; the writable entities override at runtime.
        self.finish_mode: str = self._config.get(CONF_FINISH_MODE, DEFAULT_FINISH_MODE)
        self.battery_capacity: float = float(
            self._config.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )

        # Session state. The integration only pushes commands to the charger
        # while a session is active; idle otherwise. The binary_sensor restores
        # session state across HA restarts.
        self.session_active: bool = False

        # Delivered-energy accumulator (SOC-01 / D-02). Each cycle integrates
        # the configured charger power sensor over the elapsed wall-clock
        # interval (Riemann sum) into delivered_energy_kwh, in kWh. Reset on
        # session start; restored across an HA restart by a RestoreEntity (in
        # sensor.py) so a mid-charge restart resumes progress. _last_power_ts is
        # the timestamp of the previous integration step; None re-anchors the
        # clock on the next cycle so no phantom cross-downtime interval is
        # credited.
        self.delivered_energy_kwh: float = 0.0
        self._last_power_ts: datetime | None = None

        # Reference to the manual current-SoC entity, set when that entity
        # is registered. Allows the coordinator to clear it on session end.
        self._current_soc_entity = None

        # Last applied charger state, used to avoid spamming the charger.
        self._last_applied_current: int | None = None
        self._last_applied_running: bool | None = None
        self._unsub_state_listener = None

        # Charger-reboot recovery watchdog (REL-01). Counts CONSECUTIVE update
        # cycles where we are commanding a charge (session_active and
        # dynamic_current > 0) but the charger power sensor reads ~0 W — the
        # fingerprint of a silent charger reboot that left the session
        # "commanding" while the charger sits idle. Once it reaches
        # WATCHDOG_ZERO_CYCLES the coordinator resets the _last_applied_*
        # trackers so the EXISTING _apply_to_charger path re-pushes the OCPP
        # profile + re-issues switch turn_on (an idempotent re-assert), then the
        # counter resets. Inert without a power sensor; never counts an
        # UNAVAILABLE read or a plan-commanded 0 (deliberate pause/throttle).
        self._zero_power_cycles: int = 0

        # Night-tariff window learned from the tariff sensor's recorder history.
        # None until enough history is available; the configured clock values
        # are used as the fallback in the meantime.
        self._learned_night_start: time | None = None
        self._learned_night_end: time | None = None
        self._learned_window_samples: int = 0
        self._unsub_window_timer = None

        # Day vs night electricity price, learned from the optional price
        # sensor's history. None until learned; gates cost-aware slow charging.
        self.cost_tolerance_pct: float = DEFAULT_COST_TOLERANCE_PCT
        self._learned_night_price: float | None = None
        self._learned_day_price: float | None = None

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

        # Learn the real night-tariff window and day/night prices from sensor
        # history once at startup, then refresh daily. Recorder queries are too
        # costly for the 30-second loop, so they live on their own slow timer.
        if self._config.get(CONF_TARIFF_SENSOR) or self._config.get(CONF_PRICE_SENSOR):
            self.hass.async_create_task(self._async_daily_learn())
            self._unsub_window_timer = async_track_time_interval(
                self.hass, self._async_daily_learn, timedelta(hours=24)
            )

    async def async_shutdown(self) -> None:
        if self._unsub_state_listener is not None:
            self._unsub_state_listener()
            self._unsub_state_listener = None
        if self._unsub_window_timer is not None:
            self._unsub_window_timer()
            self._unsub_window_timer = None

    @callback
    def _handle_external_change(self, event) -> None:
        """Recompute immediately when a watched sensor changes."""
        self.hass.async_create_task(self.async_refresh())

    async def _async_daily_learn(self, _now: datetime | None = None) -> None:
        """Refresh everything learned from recorder history.

        Night window first, then prices — the price classifier uses the
        freshly-learned window to bucket samples into night vs day.
        """
        await self._async_learn_night_window()
        await self._async_learn_prices()

    async def _async_learn_prices(self, _now: datetime | None = None) -> None:
        """Learn typical night vs day electricity price from the price sensor.

        Each recorded price sample is bucketed by whether its local time-of-day
        falls in the (learned or configured) night window; the median of each
        bucket becomes the learned night/day price. Falls back silently (prices
        stay None → no cost-aware slow charging) when there's no sensor, no
        recorder, or one of the buckets is empty.
        """
        sensor_id = self._config.get(CONF_PRICE_SENSOR)
        if not sensor_id:
            return
        night_start = self._learned_night_start or _parse_time(
            self._config.get(CONF_NIGHT_START), DEFAULT_NIGHT_START
        )
        night_end = self._learned_night_end or _parse_time(
            self._config.get(CONF_NIGHT_END), DEFAULT_NIGHT_END
        )
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                state_changes_during_period,
            )
        except ImportError:
            return

        end = dt_util.utcnow()
        start = end - timedelta(days=NIGHT_WINDOW_LEARN_DAYS)
        try:
            changes = await get_instance(self.hass).async_add_executor_job(
                state_changes_during_period,
                self.hass,
                start,
                end,
                sensor_id,
                True,
                False,
                None,
                True,
            )
        except Exception as err:
            _LOGGER.debug("Price learning skipped (recorder query failed): %s", err)
            return

        night_prices: list[float] = []
        day_prices: list[float] = []
        for st in changes.get(sensor_id, []):
            price = _safe_float(st.state, default=None)
            if price is None:
                continue
            local = dt_util.as_local(st.last_changed)
            if self._is_in_night_window(local, night_start, night_end):
                night_prices.append(price)
            else:
                day_prices.append(price)

        if night_prices and day_prices:
            self._learned_night_price = self._median(night_prices)
            self._learned_day_price = self._median(day_prices)
            _LOGGER.debug(
                "Learned prices: night=%.4f (%d samples) day=%.4f (%d samples)",
                self._learned_night_price,
                len(night_prices),
                self._learned_day_price,
                len(day_prices),
            )
            self.async_update_listeners()
        else:
            _LOGGER.debug(
                "Not enough price samples to learn (%d night, %d day)",
                len(night_prices),
                len(day_prices),
            )

    async def _async_learn_night_window(self, _now: datetime | None = None) -> None:
        """Learn the real night-tariff window from the tariff sensor's history.

        Walks the sensor's recorded state changes, collects the time-of-day of
        every day→night transition (night_start) and night→day transition
        (night_end), and stores the circular median of each. Falls back
        silently (leaves the learned values as None → configured clock is used)
        when there's no sensor, no recorder, or too little history.
        """
        sensor_id = self._config.get(CONF_TARIFF_SENSOR)
        if not sensor_id:
            return
        night_value = self._config.get(
            CONF_NIGHT_TARIFF_VALUE, DEFAULT_NIGHT_TARIFF_VALUE
        )
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                state_changes_during_period,
            )
        except ImportError:
            return

        end = dt_util.utcnow()
        start = end - timedelta(days=NIGHT_WINDOW_LEARN_DAYS)
        try:
            changes = await get_instance(self.hass).async_add_executor_job(
                state_changes_during_period,
                self.hass,
                start,
                end,
                sensor_id,
                True,   # no_attributes — we only need state + last_changed
                False,  # descending
                None,   # limit
                True,   # include_start_time_state
            )
        except Exception as err:  # recorder disabled / not ready / sensor unrecorded
            _LOGGER.debug("Night-window learning skipped (recorder query failed): %s", err)
            return

        start_samples: list[int] = []
        end_samples: list[int] = []
        prev_is_night: bool | None = None
        for st in changes.get(sensor_id, []):
            if st.state in (None, STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
                continue
            is_night = st.state == night_value
            if prev_is_night is not None and is_night != prev_is_night:
                local = dt_util.as_local(st.last_changed)
                minute = local.hour * 60 + local.minute
                (start_samples if is_night else end_samples).append(minute)
            prev_is_night = is_night

        if len(start_samples) >= 2 and len(end_samples) >= 2:
            self._learned_night_start = self._circular_median_time(start_samples)
            self._learned_night_end = self._circular_median_time(end_samples)
            self._learned_window_samples = min(len(start_samples), len(end_samples))
            _LOGGER.debug(
                "Learned night window %s–%s from %d start / %d end transitions",
                self._learned_night_start,
                self._learned_night_end,
                len(start_samples),
                len(end_samples),
            )
            self.async_update_listeners()
        else:
            _LOGGER.debug(
                "Not enough tariff transitions to learn night window "
                "(%d start, %d end); using configured clock",
                len(start_samples),
                len(end_samples),
            )

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

    async def async_set_finish_mode(self, value: str) -> None:
        self.finish_mode = value
        await self.async_refresh()

    async def async_set_battery_capacity(self, value: float) -> None:
        self.battery_capacity = float(value)
        await self.async_refresh()

    async def async_set_cost_tolerance(self, value: float) -> None:
        self.cost_tolerance_pct = float(value)
        await self.async_refresh()

    def register_current_soc_entity(self, entity) -> None:
        """Called by the manual current-SoC number entity once it is added."""
        self._current_soc_entity = entity

    @property
    def inputs_locked(self) -> bool:
        """Whether the writable inputs are locked against UI edits.

        Single source of truth consulted by every writable entity
        (select/number/datetime). Locked while a charging session is active so
        a running charge is never re-planned mid-session; a future condition can
        be added here once and all entities follow. The restore/lifecycle
        setters (``async_set_session_active``, ``async_start_session``,
        ``async_end_session``) and the entity ``async_added_to_hass`` restore
        paths deliberately do NOT consult this — a mid-charge restart must still
        resume (SESS-03).
        """
        return self.session_active

    async def async_set_session_active(self, value: bool) -> None:
        """Restore-path setter used by the session_active binary sensor.

        Deliberately does NOT reset the energy accumulator — a mid-charge
        restart must resume progress, so the RestoreEntity re-seeds
        delivered_energy_kwh separately via async_set_delivered_energy.
        """
        self.session_active = bool(value)
        await self.async_refresh()

    async def async_set_delivered_energy(self, value: float) -> None:
        """Restore-path setter for the delivered-energy accumulator.

        UNGUARDED (like async_set_session_active): used by the
        DeliveredEnergyRestoreSensor to re-seed accumulated progress after an HA
        restart. Bad/non-numeric restores are coerced to 0.0 by _safe_float at
        the call site, so the accumulator never starts corrupted.
        """
        self.delivered_energy_kwh = float(value)
        await self.async_refresh()

    async def async_start_session(self) -> None:
        """Begin a charging session using the current input values."""
        if self.session_active:
            return
        self.session_active = True
        # Fresh session -> start counting delivered energy from zero. Re-anchor
        # the integration timestamp so the first cycle credits nothing.
        self.delivered_energy_kwh = 0.0
        self._last_power_ts = None
        await self.async_refresh()

    async def async_end_session(self) -> None:
        """End the current session and clear the manual current-SoC input."""
        if not self.session_active:
            return
        self.session_active = False
        if self._current_soc_entity is not None:
            await self._current_soc_entity.async_reset()
        self.current_soc = None
        await self.async_refresh()

    @callback
    def _notify_undercharged(self, delivered: float, needed: float) -> None:
        """Fire an active HA persistent_notification that a session ended undercharged.

        Called on the departure-passed backstop (D-04): the charge stopped before
        reaching the target. The message names delivered vs needed kWh so the user
        knows how far short it ended. Sentence case per the project convention.
        Message content is the only outbound data — no secrets or internals
        (T-03-10). The notification_id is fixed so a repeat backstop replaces the
        prior note rather than stacking.
        """
        persistent_notification.async_create(
            self.hass,
            (
                f"The charging session ended before reaching the target. "
                f"Delivered {delivered:.1f} kWh of {needed:.1f} kWh needed."
            ),
            title="EV charging ended undercharged",
            notification_id=f"{DOMAIN}_undercharged",
        )

    # --- Main update loop ---

    # Cap a single integration interval to this many hours. A larger gap (e.g.
    # the machine was suspended) is treated as a missed window: re-anchor the
    # clock and credit 0 for that step rather than crediting a phantom spike
    # that could end a session early undercharged (T-03-05).
    _MAX_INTEGRATION_INTERVAL_H = 0.5

    def _read_charger_power_w(self) -> float | None:
        """Read the configured charger power sensor, normalised to watts.

        Returns the instantaneous power in WATTS, or ``None`` when there is no
        valid reading: no sensor configured, no state yet, or the state is
        UNAVAILABLE/UNKNOWN/""/non-numeric. ``None`` is deliberately distinct
        from a valid ``0.0`` — a missing read is "unknown", not "drawing no
        power". The watchdog (REL-01) relies on that distinction so an
        UNAVAILABLE sensor never looks like the ~0 reboot fingerprint; the
        energy accumulator treats both as "credit 0".

        Unit normalisation: the sensor may report watts or kilowatts. We read
        ``unit_of_measurement`` (default "W") and treat any "kW"-prefixed unit
        as kilowatts (×1000 to watts); everything else is taken as watts. So a
        "7.0"/"kW" sensor and a "7000"/"W" sensor report the same physical power.

        This is the SINGLE charger-power read path — both the delivered-energy
        accumulator and the watchdog consume it; there is no second read.
        """
        sensor_id = self._config.get(CONF_CHARGER_POWER_SENSOR)
        if not sensor_id:
            return None
        state = self.hass.states.get(sensor_id)
        raw = _safe_float(state.state if state else None, default=None)
        if raw is None:
            return None
        unit = (
            state.attributes.get("unit_of_measurement", "W")
            if state is not None
            else "W"
        )
        if isinstance(unit, str) and unit.strip().lower().startswith("kw"):
            return raw * 1000.0
        return raw

    def _integrate_delivered_energy(self, now: datetime) -> None:
        """Integrate the charger power sensor into delivered_energy_kwh.

        Riemann sum over the elapsed interval since the last cycle. Only
        accumulates while a session is active and the read is valid (>0). A bad
        read (UNAVAILABLE/UNKNOWN/""/non-numeric -> None, or negative)
        contributes 0 — the accumulator is monotonic and never corrupted
        (T-03-04). The clock is always advanced, so a bad read moves
        _last_power_ts forward without crediting energy.
        """
        sensor_id = self._config.get(CONF_CHARGER_POWER_SENSOR)
        if not sensor_id:
            # No sensor wired: accumulator stays inert. Don't advance the clock
            # — there's nothing to anchor.
            return

        power_w = self._read_charger_power_w()
        # A missing/invalid read credits 0 (monotonic accumulator, T-03-04).
        power_kw = (power_w / 1000.0) if power_w is not None else 0.0

        if (
            self.session_active
            and power_kw > 0
            and self._last_power_ts is not None
        ):
            interval_h = (now - self._last_power_ts).total_seconds() / 3600.0
            # Ignore negative/zero gaps and absurd jumps (suspend/clock skew).
            if 0 < interval_h <= self._MAX_INTEGRATION_INTERVAL_H:
                self.delivered_energy_kwh += power_kw * interval_h

        # Always advance the clock so the next interval is measured from now,
        # even after a bad read or while idle.
        self._last_power_ts = now

    async def _async_update_data(self) -> dict[str, Any]:
        cfg = self._config
        now = dt_util.now()

        # Integrate the charger power sensor into the delivered-energy
        # accumulator before anything else this cycle (SOC-01 / D-02).
        self._integrate_delivered_energy(now)

        # Inputs. battery_capacity is now a runtime-editable number entity;
        # the config value just seeded its initial value on first install.
        battery = float(self.battery_capacity)
        loss = float(cfg.get(CONF_CHARGING_LOSS, DEFAULT_CHARGING_LOSS))
        voltage = float(cfg.get(CONF_VOLTAGE, DEFAULT_VOLTAGE))
        phases = int(cfg.get(CONF_PHASES, DEFAULT_PHASES))
        min_a = int(cfg.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        max_a = int(cfg.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        total_limit = int(cfg.get(CONF_TOTAL_LIMIT, DEFAULT_TOTAL_LIMIT))
        headroom = int(cfg.get(CONF_HEADROOM, DEFAULT_HEADROOM))
        safety_margin = float(cfg.get(CONF_SAFETY_MARGIN_HOURS, DEFAULT_SAFETY_MARGIN_HOURS))
        # Prefer the window learned from the tariff sensor's history; fall back
        # to the configured clock values until enough history is available.
        night_start = self._learned_night_start or _parse_time(
            cfg.get(CONF_NIGHT_START), DEFAULT_NIGHT_START
        )
        night_end = self._learned_night_end or _parse_time(
            cfg.get(CONF_NIGHT_END), DEFAULT_NIGHT_END
        )
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
        # end_of_night:  target_finish = the next future occurrence of night_end at or before departure (falls back to departure if none fits)
        # departure:     target_finish = the departure time itself
        # The active value lives on the coordinator so the select entity can
        # change it without an integration reload.
        finish_mode = self.finish_mode
        charge_duration_hours = (energy_needed / max_kw) if max_kw > 0 else 0.0
        if finish_mode == FINISH_MODE_END_OF_NIGHT and self.departure_time is not None:
            target_finish = self._latest_night_end_before(
                self.departure_time, night_end, now
            )
            # No future night_end fits between now and departure — degrade
            # to finishing exactly at departure instead of aiming at a past
            # night_end (which would make latest_start nonsensical).
            if target_finish is None:
                target_finish = self.departure_time
        elif finish_mode == FINISH_MODE_DEPARTURE and self.departure_time is not None:
            target_finish = self.departure_time
        else:
            target_finish = None  # asap, or no departure set yet

        # Gentle plan for the non-asap finish modes: charge at the *slowest*
        # current that still finishes by the deadline. See _compute_gentle_plan.
        gentle_current, gentle_start = self._compute_gentle_plan(
            finish_mode,
            energy_needed,
            now,
            target_finish,
            night_start,
            night_end,
            voltage,
            factor,
            min_a,
            max_a,
            deficit_kwh,
        )
        gentle_should_start = now >= gentle_start

        # Decide recommended action + plan-level target current via the pure
        # planner seam. No session = idle: we still compute the plan for the
        # dashboard, but never push to the charger. The decision tree and
        # planned-current selection live in planner.plan() over PlanInputs;
        # the precedence (session > done > too_late > deficit > safety > mode,
        # with ASAP as the bare else) is preserved there verbatim.
        decision = plan(PlanInputs(
            session_active=self.session_active,
            energy_needed=energy_needed,
            hours_to_dep=hours_to_dep,
            deficit_kwh=deficit_kwh,
            slack=slack,
            safety_margin=safety_margin,
            finish_mode=finish_mode,
            is_night_now=is_night_now,
            gentle_should_start=gentle_should_start,
            gentle_current=gentle_current,
            day_current=day_current,
            max_a=max_a,
        ))
        action = decision.action
        planned_current = decision.planned_current

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
        # Apartment-priority throttle via the pure load-balancer seam. The
        # available_current math (max(0, total_limit - headroom -
        # int(apartment_current))) and the throttle ladder live in
        # load_balancer.balance() over LoadInputs, verbatim.
        output = balance(LoadInputs(
            planned_current=planned_current,
            total_limit=total_limit,
            headroom=headroom,
            apartment_current=apartment_current,
            min_a=min_a,
        ))
        available_current = output.available_current
        dynamic_current = output.dynamic_current
        throttle_reason = output.throttle_reason

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

        # Charger-reboot recovery watchdog (REL-01). When a power sensor is
        # configured, detect the fingerprint of a silent charger reboot:
        # session_active AND we are commanding a charge (dynamic_current > 0)
        # BUT the charger draws ~0 W. A valid read at/below WATCHDOG_ZERO_POWER_W
        # counts; an UNAVAILABLE/None read is "unknown" and never counts (T-03-13),
        # and a plan-commanded 0 (pause/throttle) never counts because
        # expecting_charge is False (T-03-14). After WATCHDOG_ZERO_CYCLES
        # consecutive such cycles, reset the _last_applied_* trackers so the
        # _apply_to_charger call below re-pushes the OCPP profile and re-issues
        # switch turn_on (an idempotent re-assert, T-03-15), then zero the
        # counter so it re-asserts once per stall, not every cycle (T-03-12).
        has_power_sensor = bool(cfg.get(CONF_CHARGER_POWER_SENSOR))
        charger_heartbeat = "no_sensor"
        if has_power_sensor:
            expecting_charge = self.session_active and dynamic_current > 0
            power_w = self._read_charger_power_w()
            # power_is_zero requires a VALID read at/below the threshold. A
            # None (UNAVAILABLE) read is "unknown" -> not zero.
            power_is_zero = power_w is not None and power_w <= WATCHDOG_ZERO_POWER_W
            if expecting_charge and power_is_zero:
                self._zero_power_cycles += 1
                charger_heartbeat = "stalled"
            else:
                self._zero_power_cycles = 0
                charger_heartbeat = "ok"
            if self._zero_power_cycles >= WATCHDOG_ZERO_CYCLES:
                _LOGGER.warning(
                    "Charger heartbeat watchdog: commanding %sA but power ~0 for "
                    "%s cycles — re-asserting charging command (likely a charger "
                    "reboot).",
                    dynamic_current,
                    self._zero_power_cycles,
                )
                # Force the existing idempotent apply path to re-push: a reboot
                # wiped the charger's profile/switch state but not our
                # last-applied memory, so the value-unchanged guard would
                # otherwise push nothing.
                self._last_applied_current = None
                self._last_applied_running = None
                self._zero_power_cycles = 0

        # Push commands to charger only on change (and only for the wired-up parts)
        await self._apply_to_charger(dynamic_current)

        # Auto-end the session when the target SoC is reached. Fire-and-forget
        # so we don't recurse into ourselves; the next cycle picks up IDLE.
        if self.session_active and action == ACTION_DONE:
            self.hass.async_create_task(self.async_end_session())

        # Deterministic manual-SoC session end (SOC-01 / SESS-01). Sibling to the
        # ACTION_DONE auto-end above. should_end() is the pure decision over
        # scalars; the coordinator only wires real values in and acts on the
        # outcome. has_departure_time MUST be sourced from whether a departure is
        # actually set — hours_to_dep collapses to 0.0 when departure_time is None,
        # and without the explicit flag the backstop would fire immediately for a
        # no-departure session (firing the undercharged notification at ~0 kWh) —
        # a silent regression vs today's ACTION_TOO_LATE-stays-active behaviour.
        if self.session_active:
            end_decision = should_end(EndInputs(
                delivered_energy_kwh=self.delivered_energy_kwh,
                energy_needed=energy_needed,
                hours_to_departure=hours_to_dep,
                has_power_sensor=bool(cfg.get(CONF_CHARGER_POWER_SENSOR)),
                has_departure_time=self.departure_time is not None,
            ))
            if end_decision.outcome == END_SUCCESS:
                # Target reached by energy counting — clean stop, no notification.
                # async_end_session clears current_soc (D-05) and is idempotent,
                # so a double-fire with the ACTION_DONE branch is harmless.
                self.hass.async_create_task(self.async_end_session())
            elif end_decision.outcome == END_BACKSTOP:
                # Departure passed before target — hard stop AND fire an active
                # undercharged notification (D-04). Both are fire-and-forget so we
                # never recurse into _async_update_data.
                self.hass.async_create_task(self.async_end_session())
                self._notify_undercharged(self.delivered_energy_kwh, energy_needed)
        # Note: should_end fires once — the first backstop cycle flips
        # session_active False via async_end_session, so subsequent cycles skip
        # this block entirely and the notification never spams (T-03-09).

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
            # Read back the finish mode the decision actually executed (not the
            # select's cache) so the dashboard can never disagree with
            # recommended_action — both derive from the same Decision.
            "finish_mode": decision.executed_finish_mode,
            "battery_capacity": round(battery, 2),
            "session_active": self.session_active,
            # Delivered-energy accumulator (SOC-01 / D-02) and whether a charger
            # power sensor is feeding it. energy_source = departure_only means
            # completion falls back to the departure-time hard stop.
            "delivered_energy_kwh": round(self.delivered_energy_kwh, 3),
            "energy_source": (
                "power_sensor" if cfg.get(CONF_CHARGER_POWER_SENSOR) else "departure_only"
            ),
            # Charger-reboot watchdog state (REL-01): "ok" while drawing power as
            # commanded, "stalled" while counting consecutive ~0-power cycles,
            # "no_sensor" when no charger power sensor is configured (watchdog
            # inert). Surfaced for transparency of the silent-reboot recovery.
            "charger_heartbeat": charger_heartbeat,
            "charge_duration_hours": round(charge_duration_hours, 3),
            "latest_start_time": gentle_start if target_finish is not None else None,
            "gentle_target_current": (
                gentle_current
                if finish_mode in (FINISH_MODE_END_OF_NIGHT, FINISH_MODE_DEPARTURE)
                else None
            ),
            "learned_night_start": (
                self._learned_night_start.strftime("%H:%M")
                if self._learned_night_start
                else None
            ),
            "learned_night_end": (
                self._learned_night_end.strftime("%H:%M")
                if self._learned_night_end
                else None
            ),
            "night_window_source": (
                "learned"
                if (self._learned_night_start and self._learned_night_end)
                else "configured"
            ),
            "learned_night_price": self._learned_night_price,
            "learned_day_price": self._learned_day_price,
            "cost_tolerance_pct": self.cost_tolerance_pct,
            "price_source": (
                "learned"
                if (
                    self._learned_night_price is not None
                    and self._learned_day_price is not None
                )
                else ("pending" if cfg.get(CONF_PRICE_SENSOR) else "none")
            ),
        }

    # --- Helpers ---

    def _compute_gentle_plan(
        self,
        finish_mode: str,
        energy_needed: float,
        now: datetime,
        target_finish: datetime | None,
        night_start: time,
        night_end: time,
        voltage: float,
        factor: float,
        min_a: int,
        max_a: int,
        deficit_kwh: float,
    ) -> tuple[int, datetime]:
        """Slowest current that finishes by the deadline, and when to start.

        - ``departure``: spread over all the time left to departure (tariff is
          irrelevant in this mode — the user already opted into day charging).
        - ``end_of_night`` *with* a learned price + a non-zero cost budget:
          allowed to spill past night-end toward departure, picking the slowest
          current whose total cost stays within ``cost_tolerance_pct`` of the
          cheapest plan (see ``_gentle_current_within_budget``).
        - ``end_of_night`` *without* prices: spread over the remaining
          night-tariff hours only, finishing around night-end (the v0.6.0
          behaviour).

        Returns ``(max_a, now)`` for asap or when there's nothing to plan.
        """
        if energy_needed <= 0 or target_finish is None:
            return max_a, now

        prices_ready = (
            self._learned_night_price is not None
            and self._learned_day_price is not None
        )
        if (
            finish_mode == FINISH_MODE_END_OF_NIGHT
            and prices_ready
            and self.cost_tolerance_pct > 0
            and self.departure_time is not None
        ):
            return self._gentle_current_within_budget(
                energy_needed,
                now,
                self.departure_time,
                night_start,
                night_end,
                voltage,
                factor,
                min_a,
                max_a,
                self._learned_night_price,
                self._learned_day_price,
                self.cost_tolerance_pct / 100.0,
                deficit_kwh,
            )

        # Plain spread to the mode's deadline.
        if finish_mode == FINISH_MODE_END_OF_NIGHT:
            window_hours = self._night_hours_between(
                now, target_finish, night_start, night_end
            )
        elif finish_mode == FINISH_MODE_DEPARTURE:
            window_hours = max(0.0, (target_finish - now).total_seconds() / 3600.0)
        else:  # asap — no gentle plan
            return max_a, now

        if window_hours <= 0:
            return max_a, now
        gentle_current = self._kw_to_current(
            energy_needed / window_hours, voltage, factor, min_a, max_a
        )
        power_kw = factor * voltage * gentle_current / 1000.0
        duration = energy_needed / power_kw if power_kw > 0 else 0.0
        return gentle_current, target_finish - timedelta(hours=duration)

    def _gentle_current_within_budget(
        self,
        energy_needed: float,
        now: datetime,
        departure: datetime,
        night_start: time,
        night_end: time,
        voltage: float,
        factor: float,
        min_a: int,
        max_a: int,
        night_price: float,
        day_price: float,
        tol_frac: float,
        deficit_kwh: float,
    ) -> tuple[int, datetime]:
        """Slowest current finishing by departure whose cost stays in budget.

        Baseline (cheapest) cost charges the unavoidable ``deficit_kwh`` in day
        tariff and the rest at night. The budget is ``baseline * (1 + tol)``.
        Charging starts now (night-open) and a slower current pushes the finish
        later — past night-end into day tariff — so cost rises as the current
        drops. Scanning from min_a up, the first current that finishes by
        departure *and* stays within budget is the slowest acceptable one.
        """
        total_window_h = (departure - now).total_seconds() / 3600.0
        if total_window_h <= 0:
            return max_a, now

        baseline_cost = (
            max(0.0, energy_needed - deficit_kwh) * night_price
            + deficit_kwh * day_price
        )
        budget = baseline_cost * (1.0 + tol_frac)

        for amps in range(min_a, max_a + 1):
            power_kw = factor * voltage * amps / 1000.0
            if power_kw <= 0:
                continue
            duration = energy_needed / power_kw
            if duration > total_window_h + 1e-6:
                continue  # too slow to finish by departure
            finish = now + timedelta(hours=duration)
            night_h = self._night_hours_between(
                now, finish, night_start, night_end
            )
            night_energy = min(energy_needed, night_h * power_kw)
            day_energy = energy_needed - night_energy
            cost = night_energy * night_price + day_energy * day_price
            if cost <= budget * (1.0 + 1e-9):
                return amps, now

        # Nothing fit the budget and the deadline — charge at max and let the
        # deficit / safety overrides handle it.
        return max_a, now

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
    def _circular_median_time(minutes: list[int]) -> time | None:
        """Circular median of a list of minutes-of-day, as a `time`.

        Times-of-day are circular (23:58 and 00:02 are 4 minutes apart, not
        ~24h), so we anchor on the circular mean, unwrap each sample to within
        ±12h of that anchor, take the ordinary median, and wrap back. Robust to
        the occasional stray transition without being thrown off by midnight.
        """
        if not minutes:
            return None
        angles = [m / 1440.0 * 2 * math.pi for m in minutes]
        mean_angle = math.atan2(
            sum(math.sin(a) for a in angles) / len(angles),
            sum(math.cos(a) for a in angles) / len(angles),
        )
        anchor = (mean_angle / (2 * math.pi) * 1440.0) % 1440.0
        unwrapped = sorted(
            anchor + ((m - anchor + 720) % 1440 - 720) for m in minutes
        )
        n = len(unwrapped)
        med = (
            unwrapped[n // 2]
            if n % 2
            else (unwrapped[n // 2 - 1] + unwrapped[n // 2]) / 2
        )
        med = int(round(med)) % 1440
        return time(hour=med // 60, minute=med % 60)

    @staticmethod
    def _median(values: list[float]) -> float:
        s = sorted(values)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

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
    def _latest_night_end_before(
        target: datetime, night_end_time: time, now: datetime
    ) -> datetime | None:
        """Latest occurrence of `night_end_time` in the open interval (now, target].

        Used by finish_mode = end_of_night to compute when the charge should
        wrap up — typically the morning of the departure. Returns None if no
        such occurrence exists (i.e. the next night_end is after departure),
        so callers can fall back to a different target.
        """
        candidate = target.replace(
            hour=night_end_time.hour,
            minute=night_end_time.minute,
            second=0,
            microsecond=0,
        )
        if candidate > target:
            candidate -= timedelta(days=1)
        if candidate <= now:
            return None
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

        Stopping depends on whether a charger switch is configured. If a
        switch is wired, charging is stopped by the switch turn_off and the
        redundant `limit: 0` push is suppressed. If no switch is configured
        (current_only mode), the profile is pushed with `limit: 0` when the
        session ends so the charger stops drawing immediately rather than
        holding the last non-zero limit.

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

        # When a charger switch is wired, the switch turn_off stops charging,
        # so the redundant limit_amps:0 push is suppressed — the switch is the
        # stop mechanism. Without a switch, the 0-amp push is the only way to
        # stop (current_only mode), so it still goes out.
        stop_via_switch = dynamic_current == 0 and bool(switch_entity)

        # Push the charging profile on every change in the dynamic current,
        # including transitions to 0 — so the charger stops as soon as the
        # session ends. Skip the startup no-op (None → 0): nothing to say
        # when we've never pushed and aren't asking for current. Also skip the
        # drop-to-0 push when a switch will handle the stop.
        push_ocpp = (
            service_call
            and devid
            and dynamic_current != self._last_applied_current
            and not (dynamic_current == 0 and self._last_applied_current is None)
            and not stop_via_switch
        )
        if push_ocpp:
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
        elif stop_via_switch and self._last_applied_current not in (None, 0):
            # We let the switch stop charging instead of pushing limit:0.
            # Record the effective stop so the next non-zero target re-pushes
            # the profile when charging resumes.
            self._last_applied_current = 0

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
