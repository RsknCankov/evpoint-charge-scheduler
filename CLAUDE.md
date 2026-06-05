# CLAUDE.md

Context for Claude when working on this repository. Read it first, then check `README.md` for end-user documentation.

## Project

**EVPoint Charge Scheduler** — a HACS-installable Home Assistant custom integration that schedules EV charging around four inputs:

1. Departure time
2. Target state of charge
3. Electricity tariff (night vs day)
4. Apartment power draw (load balancing)

Built for an EVPoint OCPP charger in Bulgaria, but works with any OCPP backend that exposes a service to install a charging profile. Single config entry per HA install — it's a singleton.

Current version is set in `custom_components/evpoint_charge_scheduler/manifest.json`. See "Versioning" below for how to bump it.

## Architecture

One `DataUpdateCoordinator` (`coordinator.py`) running every 30 seconds + immediately on state changes from the configured tariff sensor, apartment current sensor, and external SoC sensor (if any). Two conceptual layers inside it:

1. **Smart planner** computes when and at what rate to charge. Decides based on energy needed, time to departure, night-tariff hours available, deficit, and the configured finish mode.
2. **Load balancer** caps the planner's target current by `total_limit - headroom - apartment_current`. Apartment always wins.

The planner produces `dynamic_target_current` (an integer in amps); the coordinator's `_apply_to_charger` method sends it via an OCPP `set_charge_rate` service call and toggles the configured charger switch on/off. Both are idempotent — commands are only sent when the value changes. How a session is stopped depends on whether a charger switch is configured: with a switch, the switch turn_off stops charging and the redundant 0-amp push is suppressed; without a switch (`current_only` mode), the OCPP profile is pushed with `limit: 0` so the charger stops as soon as a session ends instead of holding the previous non-zero cap.

Everything else in the integration (number/datetime/switch entities, sensors, config flow) is plumbing around the coordinator.

## File layout

```
evpoint-charge-scheduler/
├── CLAUDE.md                       ← this file
├── README.md                       ← end-user docs
├── hacs.json                       ← HACS manifest (must be at repo root)
├── brands/                         ← icon assets (icon.png, icon@2x.png, logo.png, logo@2x.png, icon.svg)
└── custom_components/
    └── evpoint_charge_scheduler/
        ├── __init__.py             ← integration setup, coordinator registration
        ├── manifest.json           ← domain, name, version
        ├── const.py                ← all constants, defaults, action states
        ├── coordinator.py          ← the brain — all planner + load balancer math
        ├── config_flow.py          ← config wizard + options flow
        ├── sensor.py               ← read-only sensors (~23 of them)
        ├── number.py               ← writable battery_capacity, target_soc, current_soc, cost_tolerance inputs
        ├── datetime.py             ← writable departure_time input
        ├── select.py               ← writable finish_mode dropdown
        ├── button.py               ← start/stop session buttons
        ├── binary_sensor.py        ← session_active (RestoreEntity)
        ├── strings.json            ← UI labels
        └── translations/en.json    ← MUST be a byte-for-byte copy of strings.json
```

## Domain logic

### Decision tree (in `_async_update_data`)

Checked top to bottom; first match wins:

1. `session_active == False` → `idle` (planner still computes for the dashboard; nothing is pushed to the charger)
2. `energy_needed <= 0` → `done` (and the session auto-ends; current_soc is cleared)
3. `hours_to_departure <= 0` → `too_late`
4. `deficit_kwh > 0` → `charge_day_supplement` at the minimum rate that, combined with night-at-max, hits target. This **always** overrides finish mode — deficit means night alone won't cover it.
5. `slack_hours < safety_margin` → `charge_max_now`. Safety override; also bypasses finish mode.
6. `finish_mode == departure` → `charge_gentle` if `now >= gentle_start`, else `wait_for_start_time`.
7. `finish_mode == end_of_night` → `wait_for_night` if currently day; `charge_gentle` if currently night and `now >= gentle_start`; `wait_for_start_time` if currently night but too early.
8. `finish_mode == asap` (default) → `charge_max_now` if currently night, else `wait_for_night`.

`charge_gentle` (steps 6–7) charges at the slowest current that still finishes by the mode's deadline (`gentle_current`), spreading across the window instead of bursting at max. `asap` is the only mode that still bursts at `max_a`. The deficit (4) and safety (5) overrides still win, so gentle charging only applies when there's genuine slack. `gentle_current`/`gentle_start` are produced by `_compute_gentle_plan`: `departure` spreads over all time to departure; `end_of_night` spreads over the remaining night hours only — **unless** a price is learned and `cost_tolerance_pct > 0`, in which case `_gentle_current_within_budget` lets it spill past night-end into day tariff for an even slower current, capped by the cost budget (see "Cost-aware slow charging").

`finish_mode` lives on the coordinator (`self.finish_mode`) and is driven by the writable `select.finish_mode` entity. The config-flow field only seeds the initial value at install time; once the select is set, it overrides on every cycle. `battery_capacity` follows the same pattern via `number.battery_capacity`.

### Session lifecycle

- `binary_sensor.session_active` (RestoreEntity) restores `coordinator.session_active` on startup, so a mid-charge HA restart resumes the session.
- `button.start_session` → `coordinator.async_start_session()` flips `session_active=True` and triggers a refresh. There's no input snapshot — the entities are authoritative on every cycle.
- `button.stop_session` and the auto-end on `ACTION_DONE` both call `coordinator.async_end_session()`, which clears `self.current_soc` and resets the manual current-SoC number entity (when present). Auto-end is fire-and-forget via `hass.async_create_task` to avoid recursing inside `_async_update_data`.
- **Inputs are locked while a session is active.** The writable entities (`number.battery_capacity`, `number.target_soc`, `number.current_soc`, `number.cost_tolerance`, `datetime.departure_time`, `select.finish_mode`) guard their write methods on the single `coordinator.inputs_locked` predicate (added in Phase 3 — every writable entity consults this one source of truth; no per-entity `session_active` checks remain). When locked, the lock surfaces **visible feedback instead of a silent revert** — but in two flavours by input type:
  - The five **number/datetime** inputs are *raise-and-revert*: the edit is rejected, `async_write_ha_state()` snaps the UI back to the running value, and the coordinator setter is **not** called. These genuinely cannot change mid-session.
  - The **finish-mode select** is *raise-and-allow*: the pick **is applied** to the running session (the coordinator setter runs so `finish_mode` reflects the user's intent) **and** a `HomeAssistantError` is raised as visible feedback. This is deliberate — finish mode is the "lost night" input, so silently reverting it is the exact Phase-1 bug; the running session honours the new mode. (Confirmed product decision, Phase 3.)

  The restore paths in `async_added_to_hass` and the coordinator's restore-path setters (`async_set_session_active`, `async_set_delivered_energy`) are *not* guarded, so a mid-charge restart still resumes correctly. Stop the session to change the number/datetime inputs.

### Key derived values

| Value | Formula |
| --- | --- |
| `max_kw` | `√3 × voltage × max_current / 1000` for 3-phase, `× 1` for 1-phase |
| `energy_needed` | `(target_soc - current_soc) / 100 × battery_kwh × charging_loss` |
| `night_hours_available` | Iterates 5-min steps from now → departure, counts minutes in the night window. Wraps midnight correctly. |
| `deficit_kwh` | `max(0, energy_needed - night_hours_available × max_kw)` |
| `day_charging_current` | `_kw_to_current(deficit_kwh / day_hours_available, …)`, clamped to `[min_a, max_a]` |
| `charge_duration_hours` | `energy_needed / max_kw` (informational only — the dashboard's "at max rate" estimate) |
| `gentle_current` | `_kw_to_current(energy_needed / gentle_window_hours, …)`, clamped to `[min_a, max_a]`. Window = night hours to `target_finish` for `end_of_night`, all hours to departure for `departure`. |
| `gentle_start` (→ `latest_start_time`) | `target_finish - timedelta(hours=energy_needed / gentle_power_kw)` — when charging at `gentle_current` must begin to finish by `target_finish`. Equals ~`night_start` in the normal case; slips later only when `gentle_current` floors at `min_a`. `target_finish` depends on finish_mode (see `_latest_night_end_before` for `end_of_night`). |

### Learned night window

When a tariff sensor is configured, `_async_learn_night_window` reads the sensor's recorder history (last `NIGHT_WINDOW_LEARN_DAYS` = 14 days) via `get_instance(hass).async_add_executor_job(state_changes_during_period, …)` — recorder queries are blocking, so they must run in the recorder executor, never inline in `_async_update_data`. It collects the local time-of-day of each day→night transition (`night_start`) and night→day transition (`night_end`), then stores the `_circular_median_time` of each (circular because times-of-day wrap at midnight). Runs once at startup and on a daily `async_track_time_interval`; unsubscribed in `async_shutdown`.

`_async_update_data` prefers `self._learned_night_start`/`_learned_night_end` over the configured clock values for **all** clock-based math (night_hours_available, deficit, gentle window, `_latest_night_end_before`). It does **not** affect `is_night_now` when a sensor is configured — the sensor stays authoritative for the live state; learning only sharpens the look-ahead. Needs ≥2 clean transitions per side or it stays `None` and falls back to config. Surfaced via `sensor.night_window_source` (`learned`/`configured`) and `sensor.learned_night_start`/`_end`.

### Cost-aware slow charging

When a price sensor (`CONF_PRICE_SENSOR`) is configured, `_async_learn_prices` (run after the window learner in the same daily `_async_daily_learn`) reads its history and buckets each sample into night vs day by the learned/configured window, storing the median of each as `_learned_night_price`/`_learned_day_price`. Both buckets must be non-empty or prices stay `None`.

With prices learned and `cost_tolerance_pct > 0`, `end_of_night` calls `_gentle_current_within_budget`: baseline cost charges the unavoidable `deficit_kwh` in day tariff and the rest at night; budget = `baseline × (1 + tol)`. Charging starts now (night-open) and a slower current pushes the finish later into day tariff, so cost rises as current drops. Scanning `min_a → max_a`, the first current that finishes by departure *and* stays within budget is the slowest acceptable one. **Only the day/night price ratio actually matters** (absolute prices cancel), but the code uses the learned prices directly for readability. The writable `number.cost_tolerance` (%, default `DEFAULT_COST_TOLERANCE_PCT = 15`) feeds `self.cost_tolerance_pct`; it's only created when a price sensor is configured and is locked during a session like the other inputs. Surfaced via `sensor.price_source` (`learned`/`pending`/`none`) and `sensor.learned_night_price`/`_day_price`.

### Load balancing

```
available_current = max(0, total_limit - headroom - apartment_current)

if planned_current == 0:           dynamic = 0,  reason = smart_charging_pause
elif available_current < min_a:    dynamic = 0,  reason = apartment_load_too_high
elif available_current < planned:  dynamic = available_current, reason = throttled_by_apartment
else:                              dynamic = planned_current,   reason = unrestricted
```

When no apartment sensor is configured, `apartment_current = 0` and load balancing effectively does nothing — the planner's target passes through unmodified.

### OCPP payload

When the dynamic current changes and both `ocpp_set_rate_service` and `ocpp_devid` are configured, the coordinator pushes:

```yaml
action: <ocpp_set_rate_service>     # default ocpp.set_charge_rate
data:
  devid: <ocpp_devid>
  limit_amps: <dynamic_current>
  limit_watts: <amps × voltage × √3 (3-phase) or × 1 (1-phase)>
  custom_profile:
    chargingProfileId: <charging_profile_id, default 8>
    stackLevel: 0
    chargingProfileKind: Relative
    chargingProfilePurpose: ChargePointMaxProfile
    chargingSchedule:
      chargingRateUnit: A
      chargingSchedulePeriod:
        - startPeriod: 0
          limit: <dynamic_current>
```

`chargingProfileId` is reused on every push — the charger replaces the existing profile rather than stacking new ones. If other OCPP automations on the same charger install profiles, pick a non-colliding ID via the config flow.

### Graceful degradation

Every external entity in the config flow is `vol.Optional`. The coordinator falls back as follows:

| Missing | Fallback |
| --- | --- |
| Tariff sensor | Derive `is_night_now` from `night_start`/`night_end` clock window. `sensor.tariff_source` reports `schedule` instead of `sensor`. No history to learn from, so the configured clock values are used directly. |
| Recorder / tariff sensor not recorded / <2 days history | The night-window learner (`_async_learn_night_window`) leaves `_learned_night_start`/`_learned_night_end` as `None`; the configured clock values are used. `sensor.night_window_source` reports `configured` instead of `learned`. Logged at `debug`. |
| Price sensor | No cost-aware slow charging. `end_of_night` spreads over the night window only (never into day). The `cost_tolerance` number entity isn't created. `sensor.price_source` reports `none`. If configured but one price bucket is empty / no history, `_learned_*_price` stay `None` and `price_source` reports `pending`. |
| Apartment current sensor | Assume 0 A apartment load; load balancing is effectively disabled. `load_balancing_active` reports `False`. |
| SoC sensor | The `current_soc` number entity is created instead; user updates it manually. |
| Charger switch | Skip turn_on/turn_off calls. Integration still pushes the OCPP profile. |
| OCPP service or devid | Skip the OCPP call entirely — pure advisory mode. `sensor.control_mode` reports `advisory`, `switch_only`, or `current_only` depending on what's wired. |

## Development workflow

### Validate before commit

```bash
# Syntax check all Python
python3 -m py_compile custom_components/evpoint_charge_scheduler/*.py

# Valid JSON
python3 -c "import json, sys; [json.load(open(f)) for f in sys.argv[1:]]" \
  custom_components/evpoint_charge_scheduler/manifest.json \
  custom_components/evpoint_charge_scheduler/strings.json \
  custom_components/evpoint_charge_scheduler/translations/en.json \
  hacs.json

# strings.json and translations/en.json must be identical
diff custom_components/evpoint_charge_scheduler/strings.json \
     custom_components/evpoint_charge_scheduler/translations/en.json
```

### After editing strings.json

```bash
cp custom_components/evpoint_charge_scheduler/strings.json \
   custom_components/evpoint_charge_scheduler/translations/en.json
```

### Regenerate brand icon PNGs

```bash
# Edit brands/icon.svg, then:
python3 <<'PY'
import cairosvg
svg = open("brands/icon.svg").read().encode()
for name, size in [("icon.png", 256), ("icon@2x.png", 512),
                   ("logo.png", 256), ("logo@2x.png", 512)]:
    cairosvg.svg2png(bytestring=svg, write_to=f"brands/{name}",
                     output_width=size, output_height=size)
PY
```

### Release a version

1. Bump `version` in `custom_components/evpoint_charge_scheduler/manifest.json`.
2. Update README if behaviour changed.
3. Validate (see above).
4. Commit and tag:

```bash
git add -A && git commit -m "vX.Y.Z: <description>"
git tag vX.Y.Z && git push --tags
```

### Install locally in HA for testing

1. HACS → Integrations → ⋮ → Custom repositories → add the GitHub URL → category Integration.
2. Install via HACS, restart HA.
3. Settings → Devices & Services → Add Integration → search "EVPoint Charge Scheduler".
4. To pick up code changes after install: `Settings → Devices & Services → ⋮ → Reload`, or restart HA for new platforms/sensors.

## Versioning

- **Patch** (0.0.x): bug fixes, icon swaps, README polish, refactors with no behaviour change.
- **Minor** (0.x.0): new config options, new entities, behaviour changes — anything users would notice in their automations.
- **Major** (x.0.0): not used pre-1.0. Reserve for breaking config changes that require migration.

## Conventions

- **Sentence case** in all UI labels (config flow, sensor names, dropdown options). Never Title Case.
- **Defaults** live in `const.py` as `DEFAULT_*` constants. Reference them from both `config_flow.py` (form defaults) and `coordinator.py` (runtime fallbacks).
- **All external entity IDs in config are `vol.Optional`.** New external integrations should follow the same pattern — graceful degradation, never required.
- **README "Configuration" and "Entities created" sections must stay in sync** with `config_flow.py`'s schema and `sensor.py`'s `SENSORS` tuple.
- **No `vol.Required` for entity selectors** — the user might want to use the integration with only a subset of features wired up.
- **`strings.json` is the canonical source**; `translations/en.json` is a copy. Edit the former, copy to the latter.
- **`device_info` is identical across all entities** — they all belong to one HA device named "EVPoint Charge Scheduler", identified by `(DOMAIN, entry.entry_id)`.
- **Idempotent service calls**: the coordinator tracks `_last_applied_current` and `_last_applied_running`. Only push to the charger when the value changes. On a drop to 0: if a charger switch is configured, the 0-amp push is suppressed (`stop_via_switch`) and the switch turn_off does the stopping — but `_last_applied_current` is still reset to 0 so the next non-zero target re-pushes the profile on resume. If no switch is wired (`current_only` mode), a push with `limit: 0` is sent on session end, since the charger would otherwise keep drawing at the last cap.

## Gotchas

- **`datetime.py` is the HA platform filename.** It clashes with Python's `datetime` stdlib module, but HA's package loader handles the namespace correctly — `from datetime import datetime, time, timedelta` inside the file works fine because it's a package-relative absolute import. Don't rename the file.
- **`safety_margin_hours` is in hours**, not minutes (e.g., `0.5` = 30 min). Easy to misread.
- **`_latest_night_end_before` only returns a future time** (within `(now, target]`) or `None`. The caller in `end_of_night` mode falls back to `departure_time` when there is no future night_end before departure, so `target_finish` and `latest_start` are never in the past.
- **`chargingProfileId` is reused across pushes.** This is intentional (replaces previous profile), but means colliding with other OCPP automations on the same charger silently overwrites their profile. Document this in your release notes if relevant.
- **Per-phase vs total amp limits.** The `total_current_limit` config value is assumed per-phase if the user is on 3-phase (most apartment main breakers trip per phase). The math doesn't multiply by phase count.
- **HACS structure error "Repository structure for main is not compliant"** = `hacs.json` is not at the actual repo root, or there's extra folder nesting. Common when someone commits the entire `evpoint-charge-scheduler/` directory as a subfolder instead of its contents.
- **Brand icons in `brands/` don't auto-show in HA.** They must be submitted via PR to `home-assistant/brands` under `custom_integrations/evpoint_charge_scheduler/` to appear in the HA UI and HACS listing. Until then, the integration shows a generic placeholder; functionality is unaffected.
- **Never set `self.config_entry` in `OptionsFlow.__init__`.** Modern HA cores make `OptionsFlow.config_entry` a read-only property; assigning it raises and surfaces as a **500 Internal Server Error** when the user opens *Configure*. `async_get_options_flow` should `return OptionsFlow()` (no arg) and the flow accesses `self.config_entry` via the inherited property. (Fixed in v0.7.1.)
- **`SensorDeviceClass.TIMESTAMP` requires timezone-aware datetimes.** The coordinator uses `dt_util.now()` and preserves tzinfo via `replace()`, so `latest_start_time` (now `gentle_start`, derived from `target_finish`) is always tz-aware. Don't introduce naïve datetimes.

## Quick reference: action states

| State | Meaning |
| --- | --- |
| `idle` | No active session — planner advisory only, no commands sent |
| `done` | Target SoC reached (session auto-ends on the next cycle) |
| `too_late` | Past departure time |
| `charge_max_now` | Push max current; happens in `asap` night tariff, or under deficit/safety override |
| `charge_gentle` | Charge at `gentle_current` — the slowest rate that finishes by the deadline. `end_of_night` / `departure` only |
| `charge_day_supplement` | Charge at calculated minimum day rate because night-alone can't cover |
| `wait_for_night` | Day tariff, no urgency yet — sit idle |
| `wait_for_start_time` | Intentionally holding off under `end_of_night` or `departure` finish mode |
