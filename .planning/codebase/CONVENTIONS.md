# Coding Conventions

**Analysis Date:** 2026-06-04

## Naming Patterns

**Files:**
- Python platform files match the HA platform name exactly: `sensor.py`, `number.py`, `datetime.py`, `select.py`, `button.py`, `binary_sensor.py`, `coordinator.py`, `config_flow.py`, `const.py`
- `datetime.py` intentionally clashes with the Python stdlib module — HA's package loader resolves this correctly; do not rename it
- JSON files use lowercase with underscores: `strings.json`, `manifest.json`, `hacs.json`

**Classes:**
- HA entity classes use PascalCase describing what they are: `SmartEVChargingCoordinator`, `FinishModeSelect`, `DepartureDateTime`, `SessionActiveBinarySensor`, `BatteryCapacityNumber`, `CostToleranceNumber`
- Internal base classes prefix with `_`: `_EVPercentBase`, `_SessionButtonBase`
- Description dataclasses extend HA description types: `EVSensorDescription(SensorEntityDescription)`

**Functions and Methods:**
- Public async HA lifecycle methods follow the HA `async_` convention: `async_setup_entry`, `async_added_to_hass`, `async_set_native_value`, `async_press`
- Public coordinator setters are `async_set_<field>`: `async_set_target_soc`, `async_set_departure`, `async_set_finish_mode`
- Private methods prefix with `_`: `_parse_time`, `_safe_float`, `_kw_to_current`, `_night_hours_between`
- Private async methods prefix with `_async_`: `_async_update_data`, `_async_learn_night_window`, `_async_learn_prices`, `_async_daily_learn`
- Internal event handlers follow HA callback naming: `_handle_external_change`

**Constants:**
- All SCREAMING_SNAKE_CASE in `custom_components/evpoint_charge_scheduler/const.py`
- Configuration keys: `CONF_<FIELD>` (e.g., `CONF_TARIFF_SENSOR`, `CONF_NIGHT_START`)
- Defaults: `DEFAULT_<FIELD>` (e.g., `DEFAULT_BATTERY_CAPACITY`, `DEFAULT_FINISH_MODE`)
- Action/state strings: `ACTION_<STATE>` (e.g., `ACTION_IDLE`, `ACTION_CHARGE_MAX`)
- Throttle reasons: `THROTTLE_<REASON>`
- Plan statuses: `PLAN_<STATUS>`
- Finish mode values: `FINISH_MODE_<MODE>`, listed in `FINISH_MODES`

**Variables:**
- snake_case throughout
- Module-level logger: `_LOGGER = logging.getLogger(__name__)` in every file that logs
- Internal coordinator state fields prefixed with `_` when not for external use: `_last_applied_current`, `_learned_night_start`, `_unsub_state_listener`
- Public coordinator state (readable by entities) uses no prefix: `session_active`, `target_soc`, `finish_mode`, `battery_capacity`

**Entity attribute fields:**
- HA entity class attributes use the `_attr_` prefix convention: `_attr_unique_id`, `_attr_device_info`, `_attr_native_value`, `_attr_has_entity_name`, `_attr_name`
- Per-entity class-level keys stored as class attributes: `_key = "field_name"`

## UI Label Conventions

**Sentence case everywhere.** Never Title Case in sensor names, config flow labels, dropdown options, or button names:
- Correct: `"Energy needed"`, `"Start charging session"`, `"Night hours available"`
- Correct: `"ASAP — charge as soon as tariff allows"` (acronym ASAP is acceptable)
- Wrong: `"Night Hours Available"`, `"Start Charging Session"`

This applies to:
- Sensor `_attr_name` values in `sensor.py`, `number.py`, `datetime.py`, `select.py`, `button.py`, `binary_sensor.py`
- Config flow field labels in `strings.json` (`"data"` section)
- Dropdown option labels in `strings.json` (`"selector"` and `"entity.select"` sections)

## Defaults Pattern

All defaults live exclusively in `custom_components/evpoint_charge_scheduler/const.py` as `DEFAULT_*` constants. They are imported and referenced from two places:

1. **`config_flow.py`** — form field defaults (e.g., `default=d.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT)`)
2. **`coordinator.py`** — runtime fallbacks in `_async_update_data` and `__init__` (e.g., `cfg.get(CONF_CHARGING_LOSS, DEFAULT_CHARGING_LOSS)`)

Never hardcode a numeric or string default in entity files or in `coordinator.py` without a corresponding `DEFAULT_*` in `const.py`.

## Graceful Degradation / Optional Entities

**All external entity selectors in `config_flow.py` are `vol.Optional`.** This is a firm convention — the integration must work with any subset of optional integrations wired up.

External entities (sensors, switches, services) are all optional:
```python
vol.Optional(CONF_TARIFF_SENSOR, default=d.get(CONF_TARIFF_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
vol.Optional(CONF_PRICE_SENSOR, default=d.get(CONF_PRICE_SENSOR, vol.UNDEFINED)): _entity_selector("sensor"),
vol.Optional(CONF_APARTMENT_CURRENT_SENSOR, ...): _entity_selector("sensor"),
vol.Optional(CONF_SOC_SENSOR, ...): _entity_selector("sensor"),
vol.Optional(CONF_CHARGER_SWITCH, ...): _entity_selector("switch"),
vol.Optional(CONF_OCPP_DEVID, ...): str,
```

Only physical charger specs and schedule values use `vol.Required` (battery capacity, voltage, phases, min/max current, etc.).

Conditional entity creation: some entities are only created when the relevant sensor is configured:
```python
# number.py — only when no external SoC sensor is wired
if not entry.data.get(CONF_SOC_SENSOR) and not entry.options.get(CONF_SOC_SENSOR):
    entities.append(CurrentSoCNumber(coordinator, entry))
# Only when price sensor is configured
if entry.data.get(CONF_PRICE_SENSOR) or entry.options.get(CONF_PRICE_SENSOR):
    entities.append(CostToleranceNumber(coordinator, entry))
```

## strings.json Mirror Rule

`custom_components/evpoint_charge_scheduler/strings.json` is the **canonical source** for all UI labels. `custom_components/evpoint_charge_scheduler/translations/en.json` must be a byte-for-byte copy.

After any edit to `strings.json`:
```bash
cp custom_components/evpoint_charge_scheduler/strings.json \
   custom_components/evpoint_charge_scheduler/translations/en.json
```

Never edit `translations/en.json` directly. Verify identity with:
```bash
diff custom_components/evpoint_charge_scheduler/strings.json \
     custom_components/evpoint_charge_scheduler/translations/en.json
```

## Device Info Pattern

Every entity across all platform files uses an identical `_attr_device_info` dict:
```python
self._attr_device_info = {
    "identifiers": {(DOMAIN, entry.entry_id)},
    "name": "EVPoint Charge Scheduler",
    "manufacturer": "EVPoint Charge Scheduler",
}
```

This groups all entities under a single HA device. Every new entity must include this exact block.

## Idempotent Service Calls Pattern

The coordinator (`coordinator.py`) never spams the charger. Two guard fields track the last pushed state:

- `self._last_applied_current: int | None` — last OCPP limit pushed
- `self._last_applied_running: bool | None` — last switch state pushed

In `_apply_to_charger`, calls are gated by change detection:
```python
push_ocpp = (
    service_call
    and devid
    and dynamic_current != self._last_applied_current
    and not (dynamic_current == 0 and self._last_applied_current is None)
    and not stop_via_switch
)
```

Special case: when a charger switch is configured and the target drops to 0, the OCPP push is suppressed (`stop_via_switch = True`) and the switch `turn_off` handles stopping — but `_last_applied_current` is still reset to 0 so the profile is re-pushed on the next non-zero current. Without a switch (`current_only` mode), the `limit: 0` push is sent to stop the charger.

## Session Input Locking Pattern

All writable entity classes lock their write method when a session is active. Canonical pattern (from `number.py`, `select.py`, `datetime.py`):
```python
async def async_set_native_value(self, value: float) -> None:
    if self._coordinator.session_active:
        self.async_write_ha_state()  # locked — revert UI back to running value
        return
    self._attr_native_value = value
    await self._push_to_coordinator()
    self.async_write_ha_state()
```

The restore path in `async_added_to_hass` is **not** guarded — needed so mid-charge restarts resume correctly.

## RestoreEntity Pattern

All writable input entities (`number.py`, `datetime.py`, `select.py`, `binary_sensor.py`) extend `RestoreEntity` so their values survive HA restarts:
```python
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
```

After restoring, always push the recovered value to the coordinator immediately.

## Import Organization

**Order observed across all files:**
1. `from __future__ import annotations` (always first)
2. Standard library (`import logging`, `import math`, `from datetime import ...`, `from typing import ...`)
3. Third-party / HA framework (`from homeassistant...`, `import voluptuous as vol`)
4. Relative integration imports (`from .const import ...`, `from .coordinator import ...`)

No blank lines within each group; one blank line between groups.

## Type Annotations

All functions include return type annotations. `from __future__ import annotations` is in every file, enabling PEP 604-style `X | Y` union syntax. Common patterns:
- `-> None` for setters and lifecycle methods
- `-> bool` for `available` properties
- `-> dict[str, Any]` for `_async_update_data`
- `str | None`, `float | None`, `datetime | None` for optional values
- `tuple[int, datetime]` for `_compute_gentle_plan` return

## Error Handling

**Philosophy:** silent degradation over raising exceptions in the update loop.

`coordinator.py` uses two patterns:

1. **`_safe_float`** helper for sensor state parsing — returns a default value on any parse failure, never raises:
   ```python
   def _safe_float(state_value: Any, default: float = 0.0) -> float:
       if state_value in (None, STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
           return default
       try:
           return float(state_value)
       except (TypeError, ValueError):
           return default
   ```

2. **`try/except Exception`** around recorder queries and charger service calls — logs at `debug` for expected fallbacks (recorder not ready, no history), `error` for unexpected failures (failed OCPP push):
   ```python
   try:
       changes = await get_instance(self.hass).async_add_executor_job(...)
   except Exception as err:
       _LOGGER.debug("Night-window learning skipped (recorder query failed): %s", err)
       return
   ```

`__init__.py` wraps first-refresh in a `try/except` and raises `ConfigEntryNotReady` to let HA retry setup.

## Logging

Module-level logger in every file that logs:
```python
_LOGGER = logging.getLogger(__name__)
```

Level guidelines (from observed usage):
- `_LOGGER.debug(...)` — normal operation details (learned values, pushed profile, fallback decisions)
- `_LOGGER.error(...)` — unexpected failures in service calls: `"Failed to set charge rate: %s"`, `"Failed to switch charger: %s"`

Uses `%s` format strings (not f-strings) in all log calls.

## Coordinator Data Dict

`_async_update_data` returns a flat `dict[str, Any]`. Sensors read from it via `value_fn` lambdas:
```python
value_fn=lambda d: d.get("energy_needed"),
```

All numeric values in the returned dict are `round()`ed to a consistent number of decimal places (2 for kWh/kW, 3 for hours, integers for currents).

## Versioning

- **Patch** (0.0.x): bug fixes, icon changes, README updates, refactors with no behaviour change
- **Minor** (0.x.0): new config options, new entities, any behaviour change users would notice in automations
- **Major** (x.0.0): not used pre-1.0; reserved for breaking config migrations

Version is the single source of truth in `custom_components/evpoint_charge_scheduler/manifest.json` (`"version"` field). Commits and tags follow `vX.Y.Z: <description>` format.

## README Sync Rule

The **"Configuration"** and **"Entities created"** sections of `README.md` must stay in sync with:
- `config_flow.py` schema (`_build_schema`)
- `sensor.py` `SENSORS` tuple

When adding a new config field or sensor, update `README.md` in the same commit.

---

*Convention analysis: 2026-06-04*
