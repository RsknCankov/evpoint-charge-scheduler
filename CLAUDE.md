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

The planner produces `dynamic_target_current` (an integer in amps); the coordinator's `_apply_to_charger` method sends it via an OCPP `set_charge_rate` service call and toggles the configured charger switch on/off. Both are idempotent — commands are only sent when the value changes.

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
        ├── sensor.py               ← read-only sensors (~16 of them)
        ├── number.py               ← writable battery_capacity, target_soc, current_soc inputs
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
6. `finish_mode == departure` → `charge_max_now` if `now >= latest_start`, else `wait_for_start_time`.
7. `finish_mode == end_of_night` → `wait_for_night` if currently day; `charge_max_now` if currently night and `now >= latest_start`; `wait_for_start_time` if currently night but too early.
8. `finish_mode == asap` (default) → `charge_max_now` if currently night, else `wait_for_night`.

`finish_mode` lives on the coordinator (`self.finish_mode`) and is driven by the writable `select.finish_mode` entity. The config-flow field only seeds the initial value at install time; once the select is set, it overrides on every cycle. `battery_capacity` follows the same pattern via `number.battery_capacity`.

### Session lifecycle

- `binary_sensor.session_active` (RestoreEntity) restores `coordinator.session_active` on startup, so a mid-charge HA restart resumes the session.
- `button.start_session` → `coordinator.async_start_session()` flips `session_active=True` and triggers a refresh. There's no input snapshot — the entities are authoritative on every cycle.
- `button.stop_session` and the auto-end on `ACTION_DONE` both call `coordinator.async_end_session()`, which clears `self.current_soc` and resets the manual current-SoC number entity (when present). Auto-end is fire-and-forget via `hass.async_create_task` to avoid recursing inside `_async_update_data`.

### Key derived values

| Value | Formula |
| --- | --- |
| `max_kw` | `√3 × voltage × max_current / 1000` for 3-phase, `× 1` for 1-phase |
| `energy_needed` | `(target_soc - current_soc) / 100 × battery_kwh × charging_loss` |
| `night_hours_available` | Iterates 5-min steps from now → departure, counts minutes in the night window. Wraps midnight correctly. |
| `deficit_kwh` | `max(0, energy_needed - night_hours_available × max_kw)` |
| `day_charging_current` | `_kw_to_current(deficit_kwh / day_hours_available, …)`, clamped to `[min_a, max_a]` |
| `charge_duration_hours` | `energy_needed / max_kw` |
| `latest_start` | `target_finish - timedelta(hours=charge_duration + safety_margin)`, where `target_finish` depends on finish_mode (see `_latest_night_end_before` for `end_of_night`) |

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
| Tariff sensor | Derive `is_night_now` from `night_start`/`night_end` clock window. `sensor.tariff_source` reports `schedule` instead of `sensor`. |
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
- **Idempotent service calls**: the coordinator tracks `_last_applied_current` and `_last_applied_running`. Only push to the charger when the value changes.

## Gotchas

- **`datetime.py` is the HA platform filename.** It clashes with Python's `datetime` stdlib module, but HA's package loader handles the namespace correctly — `from datetime import datetime, time, timedelta` inside the file works fine because it's a package-relative absolute import. Don't rename the file.
- **`safety_margin_hours` is in hours**, not minutes (e.g., `0.5` = 30 min). Easy to misread.
- **`_latest_night_end_before` only returns a future time** (within `(now, target]`) or `None`. The caller in `end_of_night` mode falls back to `departure_time` when there is no future night_end before departure, so `target_finish` and `latest_start` are never in the past.
- **`chargingProfileId` is reused across pushes.** This is intentional (replaces previous profile), but means colliding with other OCPP automations on the same charger silently overwrites their profile. Document this in your release notes if relevant.
- **Per-phase vs total amp limits.** The `total_current_limit` config value is assumed per-phase if the user is on 3-phase (most apartment main breakers trip per phase). The math doesn't multiply by phase count.
- **HACS structure error "Repository structure for main is not compliant"** = `hacs.json` is not at the actual repo root, or there's extra folder nesting. Common when someone commits the entire `evpoint-charge-scheduler/` directory as a subfolder instead of its contents.
- **Brand icons in `brands/` don't auto-show in HA.** They must be submitted via PR to `home-assistant/brands` under `custom_integrations/evpoint_charge_scheduler/` to appear in the HA UI and HACS listing. Until then, the integration shows a generic placeholder; functionality is unaffected.
- **`SensorDeviceClass.TIMESTAMP` requires timezone-aware datetimes.** The coordinator uses `dt_util.now()` and preserves tzinfo via `replace()`, so `latest_start_time` is always tz-aware. Don't introduce naïve datetimes.

## Quick reference: action states

| State | Meaning |
| --- | --- |
| `idle` | No active session — planner advisory only, no commands sent |
| `done` | Target SoC reached (session auto-ends on the next cycle) |
| `too_late` | Past departure time |
| `charge_max_now` | Push max current; happens in night tariff, or under safety override |
| `charge_day_supplement` | Charge at calculated minimum day rate because night-alone can't cover |
| `wait_for_night` | Day tariff, no urgency yet — sit idle |
| `wait_for_start_time` | Intentionally holding off under `end_of_night` or `departure` finish mode |
