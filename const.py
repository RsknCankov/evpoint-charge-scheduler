"""Constants for the EVPoint Charge Scheduler integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "evpoint_charge_scheduler"
DEFAULT_NAME = "EVPoint Charge Scheduler"

UPDATE_INTERVAL = timedelta(seconds=30)

# --- Configuration keys (set in config flow) ---
CONF_TARIFF_SENSOR = "tariff_sensor"
CONF_NIGHT_TARIFF_VALUE = "night_tariff_value"
CONF_APARTMENT_CURRENT_SENSOR = "apartment_current_sensor"
CONF_SOC_SENSOR = "soc_sensor"  # optional external SoC source
CONF_OCPP_SET_RATE_SERVICE = "ocpp_set_rate_service"
CONF_OCPP_DEVID = "ocpp_devid"
CONF_CHARGING_PROFILE_ID = "charging_profile_id"
CONF_CHARGER_SWITCH = "charger_switch"

CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_VOLTAGE = "voltage"
CONF_PHASES = "phases"
CONF_MIN_CURRENT = "min_current"
CONF_MAX_CURRENT = "max_current"
CONF_TOTAL_LIMIT = "total_current_limit"
CONF_HEADROOM = "safety_headroom"
CONF_CHARGING_LOSS = "charging_loss"
CONF_NIGHT_START = "night_start"
CONF_NIGHT_END = "night_end"
CONF_SAFETY_MARGIN_HOURS = "safety_margin_hours"

# --- Defaults ---
DEFAULT_BATTERY_CAPACITY = 60.0
DEFAULT_VOLTAGE = 230
DEFAULT_PHASES = 3
DEFAULT_MIN_CURRENT = 6
DEFAULT_MAX_CURRENT = 32
DEFAULT_TOTAL_LIMIT = 60
DEFAULT_HEADROOM = 4
DEFAULT_CHARGING_LOSS = 1.10
DEFAULT_TARGET_SOC = 80
DEFAULT_NIGHT_START = "22:00"
DEFAULT_NIGHT_END = "06:00"
DEFAULT_SAFETY_MARGIN_HOURS = 0.5
DEFAULT_NIGHT_TARIFF_VALUE = "night"
DEFAULT_OCPP_SERVICE = "ocpp.set_charge_rate"
DEFAULT_CHARGING_PROFILE_ID = 8

# --- Internal entity keys (the integration's own writable inputs) ---
KEY_TARGET_SOC = "target_soc"
KEY_CURRENT_SOC = "current_soc"
KEY_DEPARTURE = "departure_time"
KEY_ENABLED = "smart_charging_enabled"

# --- Recommended actions (sensor states) ---
ACTION_DISABLED = "disabled"
ACTION_DONE = "done"
ACTION_TOO_LATE = "too_late"
ACTION_CHARGE_MAX = "charge_max_now"
ACTION_CHARGE_DAY_SUPPLEMENT = "charge_day_supplement"
ACTION_WAIT_FOR_NIGHT = "wait_for_night"

# --- Throttle reasons ---
THROTTLE_UNRESTRICTED = "unrestricted"
THROTTLE_SMART_PAUSE = "smart_charging_pause"
THROTTLE_APARTMENT_HIGH = "apartment_load_too_high"
THROTTLE_BY_APARTMENT = "throttled_by_apartment"

# --- Plan status ---
PLAN_OK = "ok"
PLAN_ALREADY_AT_TARGET = "already_at_target"
PLAN_TOO_LATE = "too_late"
PLAN_INSUFFICIENT_TIME = "insufficient_time"

# Platforms to set up
PLATFORMS = ["sensor", "number", "switch", "datetime"]
