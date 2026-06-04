<!-- refreshed: 2026-06-04 -->
# Architecture

**Analysis Date:** 2026-06-04

## System Overview

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Home Assistant UI / Automations               │
│  Writable entities: number, datetime, select, button, binary_sensor │
│  Read-only entities: sensor (23 sensors)                         │
└──────┬─────────────────────────────────────────────────────────┘
       │  set_*() / async_start_session / async_end_session
       ▼
┌─────────────────────────────────────────────────────────────────┐
│           SmartEVChargingCoordinator  (coordinator.py)          │
│                                                                  │
│  ┌──────────────────────────────────────┐                        │
│  │  Smart Planner  (_async_update_data) │                        │
│  │  · energy_needed, night_hours,       │                        │
│  │    deficit, slack                    │                        │
│  │  · decision tree → action            │                        │
│  │  · _compute_gentle_plan              │                        │
│  │  · _gentle_current_within_budget     │                        │
│  └──────────────┬───────────────────────┘                        │
│                 │ planned_current                                 │
│  ┌──────────────▼───────────────────────┐                        │
│  │  Load Balancer  (_async_update_data) │                        │
│  │  available = total_limit - headroom  │                        │
│  │            - apartment_current       │                        │
│  │  → dynamic_target_current            │                        │
│  └──────────────┬───────────────────────┘                        │
│                 │ dynamic_current                                 │
│  ┌──────────────▼───────────────────────┐                        │
│  │  _apply_to_charger (idempotent)      │                        │
│  │  · OCPP set_charge_rate service      │                        │
│  │  · charger switch turn_on / turn_off │                        │
│  └──────────────────────────────────────┘                        │
│                                                                  │
│  Background learners (daily timer):                              │
│  · _async_learn_night_window (recorder history)                  │
│  · _async_learn_prices       (recorder history)                  │
└──────┬──────────────────────────────────────────────────────────┘
       │  hass.services.async_call / hass.states.get
       ▼
┌─────────────────────────────────────────────────────────────────┐
│  External HA Entities (all optional)                             │
│  · tariff sensor      · apartment current sensor                 │
│  · SoC sensor         · price sensor                             │
│  · OCPP service       · charger switch                           │
└─────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| SmartEVChargingCoordinator | All planner + load balancer math; pushes to charger | `custom_components/evpoint_charge_scheduler/coordinator.py` |
| async_setup_entry | Integration entry point; creates coordinator, forwards to platforms | `custom_components/evpoint_charge_scheduler/__init__.py` |
| EVSensor / SENSORS tuple | 23 read-only sensors exposing coordinator.data keys | `custom_components/evpoint_charge_scheduler/sensor.py` |
| BatteryCapacityNumber, TargetSoCNumber, CurrentSoCNumber, CostToleranceNumber | Writable inputs; RestoreEntity; locked during session | `custom_components/evpoint_charge_scheduler/number.py` |
| DepartureDateTime | Writable departure time; RestoreEntity; locked during session | `custom_components/evpoint_charge_scheduler/datetime.py` |
| FinishModeSelect | Writable finish mode dropdown; RestoreEntity; locked during session | `custom_components/evpoint_charge_scheduler/select.py` |
| StartSessionButton / StopSessionButton | Trigger async_start/end_session; availability inverted per session state | `custom_components/evpoint_charge_scheduler/button.py` |
| SessionActiveBinarySensor | Restores session_active across HA restarts | `custom_components/evpoint_charge_scheduler/binary_sensor.py` |
| ConfigFlow / OptionsFlow | HACS install wizard and reconfigure UI | `custom_components/evpoint_charge_scheduler/config_flow.py` |
| Constants | All CONF_*, DEFAULT_*, ACTION_*, THROTTLE_*, PLAN_* symbols | `custom_components/evpoint_charge_scheduler/const.py` |

## Pattern Overview

**Overall:** Single-coordinator Home Assistant integration (singleton config entry).

**Key Characteristics:**
- One `DataUpdateCoordinator` (`SmartEVChargingCoordinator`) is the sole source of truth; all entity platforms are thin wrappers around it.
- Entity platforms handle only UI concerns (RestoreEntity persistence, session-lock guard, HA state writes); all decision logic lives in `coordinator.py`.
- All external integrations (tariff sensor, OCPP service, charger switch, SoC sensor, apartment sensor, price sensor) are `vol.Optional`; the integration degrades gracefully when any are absent.
- The integration is a singleton: `async_step_user` calls `async_set_unique_id(DOMAIN)` and aborts if already configured.

## Layers

**Smart Planner:**
- Purpose: Decides when and at what rate to charge, producing `planned_current`.
- Location: `_async_update_data` method in `custom_components/evpoint_charge_scheduler/coordinator.py`
- Contains: Decision tree (8 branches), `_compute_gentle_plan`, `_gentle_current_within_budget`, helper math methods
- Depends on: User inputs (`target_soc`, `current_soc`, `departure_time`, `finish_mode`, `battery_capacity`), external sensor states, learned night window and prices
- Used by: Load balancer (same method, sequential)

**Load Balancer:**
- Purpose: Caps `planned_current` by available apartment headroom, producing `dynamic_target_current`.
- Location: `_async_update_data` (lines ~590–611) in `custom_components/evpoint_charge_scheduler/coordinator.py`
- Contains: `available_current = total_limit - headroom - apartment_current`; four throttle outcomes
- Depends on: `planned_current` from planner, optional apartment current sensor
- Used by: `_apply_to_charger`

**Charger Output:**
- Purpose: Idempotently sends OCPP charging profile and toggles charger switch.
- Location: `_apply_to_charger` method in `custom_components/evpoint_charge_scheduler/coordinator.py`
- Contains: OCPP service call logic, switch turn_on/turn_off, `_last_applied_current` / `_last_applied_running` guards
- Depends on: `dynamic_current`, `CONF_OCPP_SET_RATE_SERVICE`, `CONF_OCPP_DEVID`, `CONF_CHARGER_SWITCH`

**Background Learners:**
- Purpose: Sharpens night-window and price knowledge from recorder history; runs daily, not in the 30-second loop.
- Location: `_async_learn_night_window` and `_async_learn_prices` in `custom_components/evpoint_charge_scheduler/coordinator.py`
- Contains: Recorder `state_changes_during_period` queries (run via `async_add_executor_job`), `_circular_median_time`, `_median`
- Depends on: HA Recorder, tariff sensor history, price sensor history
- Used by: `_async_daily_learn` (called at startup + every 24 h)

**Entity Platforms (plumbing):**
- Purpose: Expose coordinator state to HA UI and accept user input.
- Location: `custom_components/evpoint_charge_scheduler/sensor.py`, `number.py`, `datetime.py`, `select.py`, `button.py`, `binary_sensor.py`
- Pattern: All extend `CoordinatorEntity` or hold a reference to the coordinator. Writable entities extend `RestoreEntity` and guard writes with `coordinator.session_active`.

## Data Flow

### Primary Charging Cycle (every 30 s or on sensor state change)

1. `async_track_state_change_event` fires → `_handle_external_change` → `async_refresh()` (`coordinator.py:201-203`)
2. OR: 30-second `UPDATE_INTERVAL` timer fires automatically
3. `_async_update_data` reads user inputs from coordinator attributes and external sensor states via `hass.states.get()` (`coordinator.py:416-`)
4. Smart planner computes `energy_needed`, `night_hours`, `deficit_kwh`, `slack`, `gentle_current/start` → selects `action` and `planned_current` (`coordinator.py:544-585`)
5. Load balancer reads apartment sensor, computes `available_current`, derives `dynamic_target_current` and `throttle_reason` (`coordinator.py:590-611`)
6. `_apply_to_charger(dynamic_current)` sends OCPP profile (if changed) and toggles switch (if changed) (`coordinator.py:940-1051`)
7. If `action == ACTION_DONE`, fire-and-forget `async_end_session()` task (`coordinator.py:632-633`)
8. Return data dict → `CoordinatorEntity` subclasses auto-update their HA state

### User Input Flow

1. User edits a writable entity (number, datetime, select) or presses a button in HA UI
2. Entity's `async_set_native_value` / `async_select_option` / `async_press` checks `coordinator.session_active`; if active, reverts UI via `async_write_ha_state()` and returns
3. Otherwise calls the appropriate `coordinator.async_set_*()` setter
4. Setter updates the coordinator attribute and calls `await self.async_refresh()`
5. Full `_async_update_data` cycle runs with the new value

### Session Restore on HA Restart

1. `SessionActiveBinarySensor.async_added_to_hass` calls `async_get_last_state()` (`binary_sensor.py:53-57`)
2. If last state was "on", calls `coordinator.async_set_session_active(True)` — this setter is not session-guarded
3. Similarly all number/datetime/select entities restore their last values via `RestoreEntity`
4. The coordinator resumes the active session from where it left off on the next cycle

### Background Learning (daily)

1. `async_config_entry_first_refresh` schedules `_async_daily_learn` via `async_track_time_interval(timedelta(hours=24))` (`coordinator.py:185-190`)
2. At startup and daily: `_async_learn_night_window` → queries recorder history → stores `_learned_night_start` / `_learned_night_end` as `time` objects
3. Then: `_async_learn_prices` → queries price sensor history → buckets by night/day window → stores `_learned_night_price` / `_learned_day_price`
4. Both run in the recorder executor via `async_add_executor_job` (blocking queries must not run inline)
5. `_async_update_data` prefers learned values over configured clock values in all look-ahead math

**State Management:**
- The coordinator holds all runtime state: `session_active`, `target_soc`, `current_soc`, `departure_time`, `finish_mode`, `battery_capacity`, `cost_tolerance_pct`, learned prices and night window.
- `coordinator.data` is the dict returned by `_async_update_data`; entity `native_value` properties read from it via `value_fn` lambdas.
- `_last_applied_current` and `_last_applied_running` track what was last pushed to the charger to avoid redundant commands.

## Key Abstractions

**SmartEVChargingCoordinator:**
- Purpose: Singleton brain; owns all charging logic and state.
- File: `custom_components/evpoint_charge_scheduler/coordinator.py`
- Pattern: Subclasses `DataUpdateCoordinator`; `_async_update_data` is the main loop.

**Action States:**
- Purpose: Named outcomes from the decision tree, used as sensor values and internal routing.
- Defined: `custom_components/evpoint_charge_scheduler/const.py` (`ACTION_*` constants)
- Values: `idle`, `done`, `too_late`, `charge_max_now`, `charge_gentle`, `charge_day_supplement`, `wait_for_night`, `wait_for_start_time`

**Throttle Reasons:**
- Purpose: Explain why `dynamic_target_current` differs from `planned_current`.
- Defined: `custom_components/evpoint_charge_scheduler/const.py` (`THROTTLE_*` constants)
- Values: `unrestricted`, `smart_charging_pause`, `apartment_load_too_high`, `throttled_by_apartment`

**SENSORS tuple:**
- Purpose: Declarative definition of all 23 read-only sensors with `value_fn` lambdas into `coordinator.data`.
- File: `custom_components/evpoint_charge_scheduler/sensor.py`
- Pattern: `EVSensorDescription(SensorEntityDescription)` dataclass with a `value_fn` field; `EVSensor.native_value` calls it.

**RestoreEntity pattern:**
- Purpose: All writable inputs persist their values across HA restarts.
- Used by: `number.py`, `datetime.py`, `select.py`, `binary_sensor.py`
- Pattern: `async_added_to_hass` calls `async_get_last_state()`, falls back to config-flow seed or default.

## Entry Points

**async_setup_entry:**
- Location: `custom_components/evpoint_charge_scheduler/__init__.py:16`
- Triggers: HA loads the config entry (first install, reload, or restart)
- Responsibilities: Instantiates `SmartEVChargingCoordinator`, calls `async_config_entry_first_refresh`, stores coordinator in `hass.data[DOMAIN][entry.entry_id]`, forwards setup to all six platforms.

**async_config_entry_first_refresh:**
- Location: `custom_components/evpoint_charge_scheduler/coordinator.py:166`
- Triggers: Called by `async_setup_entry`
- Responsibilities: Subscribes to external sensor state changes, schedules daily learning, performs the first `_async_update_data` call.

**_async_update_data:**
- Location: `custom_components/evpoint_charge_scheduler/coordinator.py:416`
- Triggers: Every 30 s (`UPDATE_INTERVAL`) or immediately on sensor state change
- Responsibilities: Full planner + load balancer + charger output cycle; returns `coordinator.data` dict.

## Architectural Constraints

- **Threading:** Single-threaded HA event loop. All coordinator methods are async. Blocking recorder queries run via `async_add_executor_job` to avoid blocking the loop.
- **Global state:** One coordinator instance per config entry stored in `hass.data[DOMAIN][entry.entry_id]`. Only one config entry is permitted (singleton enforced by `async_set_unique_id(DOMAIN)` in config flow).
- **Circular imports:** None. All platforms import from `coordinator.py` and `const.py` only.
- **Datetime awareness:** `SensorDeviceClass.TIMESTAMP` requires tz-aware datetimes. The coordinator exclusively uses `dt_util.now()` and preserves `tzinfo` in all datetime arithmetic. Never introduce naïve datetimes.
- **Session lock:** Writable entity setters guard on `coordinator.session_active`. The restore path (`async_added_to_hass`) is not guarded so restarts resume correctly. The `async_set_*` coordinator setters used by the restore path are also unguarded.

## Decision Tree (in `_async_update_data`)

Evaluated top to bottom; first match wins:

1. `not session_active` → `ACTION_IDLE` — planner still runs for advisory dashboard output; `_apply_to_charger` is still called but `dynamic_current` will be 0 because `planned_current` is 0.
2. `energy_needed <= 0` → `ACTION_DONE` — auto-end session via fire-and-forget task.
3. `hours_to_dep <= 0` → `ACTION_TOO_LATE`
4. `deficit_kwh > 0` → `ACTION_CHARGE_DAY_SUPPLEMENT` at `day_current` — overrides all finish modes; night-at-max cannot cover the required energy.
5. `slack < safety_margin` → `ACTION_CHARGE_MAX` — safety override; bypasses finish mode.
6. `finish_mode == departure` → `ACTION_CHARGE_GENTLE` if `now >= gentle_start`, else `ACTION_WAIT_FOR_START_TIME`.
7. `finish_mode == end_of_night` → `ACTION_WAIT_FOR_NIGHT` if day; `ACTION_CHARGE_GENTLE` if night and `now >= gentle_start`; else `ACTION_WAIT_FOR_START_TIME`.
8. `finish_mode == asap` → `ACTION_CHARGE_MAX` if night, else `ACTION_WAIT_FOR_NIGHT`.

## Anti-Patterns

### Setting coordinator state in entity constructors

**What happens:** Calling `coordinator.async_set_*()` or mutating coordinator attributes in `__init__` rather than `async_added_to_hass`.
**Why it's wrong:** The coordinator may not yet have completed its first refresh; mutations before `async_added_to_hass` bypass the restore path and may lose persisted values.
**Do this instead:** Perform all coordinator pushes inside `async_added_to_hass` after `async_get_last_state()`, as shown in `number.py:66-76`, `datetime.py:42-56`, `select.py:52-66`.

### Assigning `self.config_entry` in `OptionsFlow.__init__`

**What happens:** `self.config_entry = config_entry` in `OptionsFlow.__init__`.
**Why it's wrong:** Modern HA cores make `OptionsFlow.config_entry` a read-only property; assigning it raises and surfaces as a 500 Internal Server Error on the options form.
**Do this instead:** Return `OptionsFlow()` (no args) from `async_get_options_flow`; access `self.config_entry` via the inherited property, as in `config_flow.py:149`.

### Inline recorder queries

**What happens:** Calling `state_changes_during_period` directly inside `_async_update_data`.
**Why it's wrong:** Recorder queries are blocking and would freeze the HA event loop on every 30-second cycle.
**Do this instead:** Run them via `get_instance(hass).async_add_executor_job(...)` inside the dedicated `_async_learn_night_window` / `_async_learn_prices` methods, which run on their own daily timer (`coordinator.py:314`, `coordinator.py:243`).

## Error Handling

**Strategy:** Log and degrade gracefully; never raise from `_async_update_data`.

**Patterns:**
- Recorder queries wrapped in `try/except Exception` — failure logs at `debug` level and leaves learned values as `None`, falling back to configured clock values.
- `_apply_to_charger` wraps both the OCPP service call and switch toggle in `try/except Exception` — logs at `error`, does not re-raise.
- `_safe_float` helper converts any sensor state (including `STATE_UNAVAILABLE`, `STATE_UNKNOWN`, empty string) to a float with a configurable default, preventing crashes from bad sensor readings.

## Cross-Cutting Concerns

**Logging:** `_LOGGER = logging.getLogger(__name__)` in `coordinator.py`. Routine decisions log at `debug`; charger commands log at `debug`; failures log at `error`. No structured logging.
**Validation:** Config flow uses `voluptuous` schema with `vol.Coerce` and `vol.In`. Runtime sensor values are sanitised via `_safe_float`.
**Authentication:** Delegated entirely to HA — the integration calls HA services and reads HA entity states; no direct authentication to external systems.

---

*Architecture analysis: 2026-06-04*
