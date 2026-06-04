# External Integrations

**Analysis Date:** 2026-06-04

## Overview

All integrations are `vol.Optional` in the config schema (`config_flow.py`). The coordinator degrades gracefully when any is absent — it never hard-fails on a missing external entity. The `sensor.control_mode` entity reports the effective operating mode.

---

## OCPP Charger Service

**Purpose:** Set the EV charger's current limit and install a charging profile on the physical charger.

**Wiring:**
- Config keys: `CONF_OCPP_SET_RATE_SERVICE` (default `"ocpp.set_charge_rate"`), `CONF_OCPP_DEVID` (required alongside service to activate)
- Both must be set for OCPP calls to fire; missing either puts the integration into advisory mode.

**Call site:** `coordinator.py` → `_apply_to_charger()`, invoked on every coordinator cycle when `dynamic_current` changes.

**Payload sent:**
```yaml
action: <ocpp_set_rate_service>        # e.g. ocpp.set_charge_rate
data:
  devid: <ocpp_devid>
  limit_amps: <int>                    # computed dynamic_target_current
  limit_watts: <int>                   # amps × voltage × √3 (3-phase) or × 1 (1-phase)
  custom_profile:
    chargingProfileId: <int>           # default 8 (CONF_CHARGING_PROFILE_ID); reused to replace, not stack
    stackLevel: 0
    chargingProfileKind: Relative
    chargingProfilePurpose: ChargePointMaxProfile
    chargingSchedule:
      chargingRateUnit: A
      chargingSchedulePeriod:
        - startPeriod: 0
          limit: <int>
```

**Idempotency:** The coordinator tracks `_last_applied_current` and only sends when the value changes. The `chargingProfileId` is reused on every push so the charger replaces rather than stacks profiles.

**Stop behaviour:**
- With a charger switch configured: 0-amp push is suppressed; switch `turn_off` stops the transaction.
- Without a charger switch (`current_only` mode): a `limit: 0` push is sent on session end.

**Degradation:** If OCPP service or devid is absent, `_apply_to_charger` skips the service call entirely. `sensor.control_mode` reports `advisory` (neither wired), `switch_only` (only switch), or `current_only` (only OCPP). No error is raised.

---

## Charger Switch Entity

**Purpose:** Start and stop the OCPP charging transaction by toggling a HA switch entity (the charger's on/off switch).

**Wiring:**
- Config key: `CONF_CHARGER_SWITCH` — any HA `switch` domain entity ID.

**Call site:** `coordinator.py` → `_apply_to_charger()` — calls `hass.services.async_call("switch", "turn_on"|"turn_off", target={"entity_id": switch_entity})` when `should_run` (dynamic_current > 0) changes.

**Idempotency:** Coordinator tracks `_last_applied_running` and only calls the service when the on/off state changes.

**Degradation:** If absent, the coordinator skips turn_on/turn_off entirely. Charging is controlled solely through the OCPP current-limit profile. `sensor.control_mode` reports `current_only` if OCPP is wired, `advisory` if nothing is wired.

---

## Tariff Sensor

**Purpose:** Live night/day tariff state. Drives `is_night_now`, which the planner uses to decide whether to charge (`asap` mode) or wait.

**Wiring:**
- Config key: `CONF_TARIFF_SENSOR` — any HA `sensor` entity whose state equals `CONF_NIGHT_TARIFF_VALUE` (default `"night"`) during the off-peak window.

**Read pattern:** `coordinator.py` → `_async_update_data()` — reads `self.hass.states.get(tariff_sensor_id).state` on every 30-second cycle. Also triggers an immediate coordinator refresh via `async_track_state_change_event` whenever the tariff sensor state changes (subscribed in `async_config_entry_first_refresh`).

**Degradation:** If absent, `is_night_now` is derived from the configured `night_start`/`night_end` clock window using `_is_in_night_window()`. `sensor.tariff_source` reports `schedule` instead of `sensor`.

---

## HA Recorder History API

**Purpose:** Learn the real night-tariff window and typical electricity prices from historical sensor data, so the planner uses accurate windows rather than static configured clock values.

**Wiring:** No config key — uses the HA recorder component if available. Recorder queries run in the recorder executor (blocking I/O), never inline in `_async_update_data`.

**Call pattern:**
```python
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period

changes = await get_instance(hass).async_add_executor_job(
    state_changes_during_period, hass, start, end, sensor_id,
    True, False, None, True
)
```

**Night-window learning (`_async_learn_night_window`):**
- Triggered: once at startup and every 24 hours (`async_track_time_interval`)
- Source: 14 days of tariff sensor history (`NIGHT_WINDOW_LEARN_DAYS = 14`)
- Method: collects day→night and night→day transition times-of-day, computes circular median of each, stores as `_learned_night_start` / `_learned_night_end`
- Threshold: requires ≥2 transitions per side; otherwise stays `None`
- Effect: `_async_update_data` prefers learned values over configured clock for all night-hours math
- Surfaced via: `sensor.night_window_source` (`learned` / `configured`), `sensor.learned_night_start`, `sensor.learned_night_end`

**Price learning (`_async_learn_prices`):**
- Triggered: immediately after night-window learning in `_async_daily_learn`
- Source: 14 days of price sensor history, bucketed by learned/configured night window
- Method: median of each bucket stored as `_learned_night_price` / `_learned_day_price`
- Threshold: both buckets must be non-empty; otherwise prices stay `None`
- Effect: enables cost-aware slow charging in `end_of_night` mode
- Surfaced via: `sensor.price_source` (`learned` / `pending` / `none`), `sensor.learned_night_price`, `sensor.learned_day_price`

**Degradation:** Wrapped in `try/except ImportError` (recorder may be disabled) and `try/except Exception` (recorder not ready, sensor excluded from recording, insufficient history). All failures leave learned values as `None` and are logged at `debug` level only. Falls back to configured clock values.

---

## Apartment Current Sensor

**Purpose:** Real-time apartment power draw (amps). Used by the load balancer to throttle or pause EV charging so total draw stays below the main-breaker limit.

**Wiring:**
- Config key: `CONF_APARTMENT_CURRENT_SENSOR` — any HA `sensor` entity reporting amperes.

**Read pattern:** `coordinator.py` → `_async_update_data()` — reads `self.hass.states.get(apt_sensor_id).state` on every cycle. Also subscribed via `async_track_state_change_event` for immediate recompute on change.

**Load-balancing formula:**
```
available_current = max(0, total_limit - headroom - apartment_current)
```

**Degradation:** If absent, `apartment_current = 0.0` and `load_balancing_active = False`. Charger uses the full configured limit. `sensor.throttle_reason` will never report `apartment_load_too_high` or `throttled_by_apartment`.

**Assumption:** The sensor must exclude the EV charger's own draw. If it includes the charger (main meter), the math double-counts and requires manual adjustment.

---

## External SoC Sensor

**Purpose:** Automatic current state-of-charge reading from an external source (e.g., a car integration or OCPP SoC reading), eliminating the need for manual SoC entry.

**Wiring:**
- Config key: `CONF_SOC_SENSOR` — any HA `sensor` entity reporting SoC as a percentage (0–100).

**Read pattern:** `coordinator.py` → `_async_update_data()` — reads `self.hass.states.get(soc_sensor_id).state` on every cycle and passes it directly to the energy calculation. Also subscribed via `async_track_state_change_event` for immediate recompute.

**Degradation:** If absent, the coordinator falls back to `self.current_soc` (set by the manual `number.current_soc` entity). The manual number entity is only created when no SoC sensor is configured. `coordinator.async_end_session()` clears `current_soc` at session end (stale after charging).

---

## Price Sensor

**Purpose:** Historical electricity price data used to learn typical day and night tariff rates, enabling cost-aware slow charging (`end_of_night` mode can spill into day tariff within a cost budget).

**Wiring:**
- Config key: `CONF_PRICE_SENSOR` — any HA `sensor` entity recording electricity price over time.

**Read pattern:** History-only (no live read). Consumed exclusively by `_async_learn_prices` in the daily recorder-learn job. Price values are bucketed into night vs day using the learned/configured night window.

**Effect on planning:** With prices learned and `cost_tolerance_pct > 0`, `_compute_gentle_plan` calls `_gentle_current_within_budget` which scans `min_a → max_a` to find the slowest current that finishes by departure and stays within the cost budget. The `number.cost_tolerance` entity (only created when price sensor is configured) exposes the budget (default 15%).

**Degradation:** If absent, `sensor.price_source` reports `none` and the `number.cost_tolerance` entity is not created. `end_of_night` spreads over the night window only, never into day tariff. If configured but one price bucket is empty or history is insufficient, `price_source` reports `pending` and the cost-aware path is not taken.

---

## Data Storage

**State persistence:**
- HA config entries store all configuration (data + options merged in `coordinator._config`)
- `RestoreEntity` (used by `binary_sensor.py` for `session_active`) persists session state across HA restarts, allowing mid-charge restarts to resume
- No external database, file storage, or caching layer

**HA Recorder:**
- Read-only consumer of recorder data (never writes to recorder directly)
- Recorder queries are the only blocking I/O; all executed via `async_add_executor_job`

---

## Authentication & Identity

**Auth Provider:** Home Assistant's own auth layer — users access the integration through the HA UI. No separate authentication for the integration itself.

**OCPP identity:** The `ocpp_devid` string identifies the charger to the OCPP backend service. No API keys or tokens managed by this integration directly.

---

## Monitoring & Observability

**Error Tracking:** None (no Sentry, Bugsnag, etc.)

**Logging:**
- Standard Python `logging` via `_LOGGER = logging.getLogger(__name__)` in every module
- OCPP call errors: `_LOGGER.error(...)` in `_apply_to_charger`
- Learning failures: `_LOGGER.debug(...)` (intentionally quiet — degradation is expected)
- Successful OCPP pushes: `_LOGGER.debug(...)`

**Observability via HA sensors:** ~23 read-only sensors expose internal planner state (see `sensor.py`). Key diagnostic sensors: `sensor.control_mode`, `sensor.tariff_source`, `sensor.night_window_source`, `sensor.price_source`, `sensor.throttle_reason`, `sensor.plan_status`, `sensor.recommended_action`.

---

## CI/CD & Deployment

**Hosting:** GitHub repository (self-hosted by user)

**CI Pipeline:** None detected (no `.github/workflows/`, no CI config files)

**Distribution:** HACS custom integration
- `hacs.json` at repo root declares `homeassistant: "2024.6.0"` minimum
- `content_in_root: false` — component lives under `custom_components/evpoint_charge_scheduler/`
- Brand icons in `brands/` (PNG files must be submitted to `home-assistant/brands` to appear in HA UI)

---

## Webhooks & Callbacks

**Incoming:** None (no HTTP endpoints)

**Outgoing HA event subscriptions:**
- `async_track_state_change_event` on tariff sensor, apartment current sensor, and SoC sensor (if configured) — triggers immediate coordinator refresh
- `async_track_time_interval` (24h) — triggers daily recorder-learn job

All subscriptions are unsubscribed in `coordinator.async_shutdown()` to prevent leaks on integration unload.

---

*Integration audit: 2026-06-04*
