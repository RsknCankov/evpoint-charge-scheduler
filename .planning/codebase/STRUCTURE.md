# Codebase Structure

**Analysis Date:** 2026-06-04

## Directory Layout

```
evpoint-charge-scheduler/           # repo root
├── CLAUDE.md                       # developer context (architecture, conventions, gotchas)
├── README.md                       # end-user documentation
├── hacs.json                       # HACS manifest — MUST be at repo root
├── brands/                         # icon assets for HA UI / HACS listing
│   ├── icon.svg                    # source SVG (edit this, regenerate PNGs)
│   ├── icon.png                    # 256 px
│   ├── icon@2x.png                 # 512 px
│   ├── logo.png                    # 256 px
│   └── logo@2x.png                 # 512 px
├── .planning/
│   └── codebase/                   # GSD codebase map documents
└── custom_components/
    └── evpoint_charge_scheduler/   # the actual HA integration package
        ├── __init__.py             # integration setup, coordinator registration
        ├── manifest.json           # domain, name, version (bump here to release)
        ├── const.py                # ALL constants, defaults, action/throttle/plan states
        ├── coordinator.py          # the brain — planner + load balancer + charger output
        ├── config_flow.py          # config wizard (ConfigFlow) + options flow (OptionsFlow)
        ├── sensor.py               # 23 read-only sensors via SENSORS tuple
        ├── number.py               # BatteryCapacityNumber, TargetSoCNumber,
        │                           #   CurrentSoCNumber, CostToleranceNumber
        ├── datetime.py             # DepartureDateTime (HA platform; clashes with stdlib — keep name)
        ├── select.py               # FinishModeSelect dropdown
        ├── button.py               # StartSessionButton, StopSessionButton
        ├── binary_sensor.py        # SessionActiveBinarySensor (RestoreEntity)
        ├── strings.json            # canonical UI labels — edit this file
        ├── translations/
        │   └── en.json             # byte-for-byte copy of strings.json — never edit directly
        └── __pycache__/            # Python bytecode cache — not committed
```

## Directory Purposes

**`custom_components/evpoint_charge_scheduler/`:**
- Purpose: The entire HA integration lives here. HA discovers it by domain name matching the directory name.
- Contains: All Python modules, JSON manifests, and translation files.
- Key files: `coordinator.py` (all logic), `const.py` (all symbols), `manifest.json` (version).

**`brands/`:**
- Purpose: Integration icon assets.
- Contains: SVG source and generated PNG sizes (256 px and 512 px variants).
- Generated: PNGs are generated from `icon.svg` using `cairosvg` (see CLAUDE.md). Committed to the repo but not auto-displayed in HA until submitted to `home-assistant/brands`.

**`custom_components/evpoint_charge_scheduler/translations/`:**
- Purpose: Locale files consumed by HA frontend.
- Contains: Only `en.json` (currently the sole language).
- Invariant: `en.json` MUST be a byte-for-byte copy of `strings.json`. After editing `strings.json`, run `cp strings.json translations/en.json`.

## Key File Locations

**Entry Points:**
- `custom_components/evpoint_charge_scheduler/__init__.py`: `async_setup_entry` — creates coordinator, forwards to platforms; `async_unload_entry` — tears down.
- `custom_components/evpoint_charge_scheduler/coordinator.py`: `SmartEVChargingCoordinator._async_update_data` — the 30-second charging logic loop.
- `custom_components/evpoint_charge_scheduler/config_flow.py`: `ConfigFlow.async_step_user` — initial setup; `OptionsFlow.async_step_init` — reconfigure.

**Configuration:**
- `custom_components/evpoint_charge_scheduler/manifest.json`: Domain name and version number.
- `hacs.json`: HACS integration category and HACS-specific metadata.
- `custom_components/evpoint_charge_scheduler/const.py`: All `CONF_*`, `DEFAULT_*`, `ACTION_*`, `THROTTLE_*`, `PLAN_*`, `FINISH_MODE_*` constants; `PLATFORMS` list; `UPDATE_INTERVAL`.

**Core Logic:**
- `custom_components/evpoint_charge_scheduler/coordinator.py`: Entire smart planner, load balancer, gentle-plan computation, cost-budget solver, night-window and price learners, charger output.

**UI Labels:**
- `custom_components/evpoint_charge_scheduler/strings.json`: Canonical source — edit here.
- `custom_components/evpoint_charge_scheduler/translations/en.json`: Copy of strings.json — never edit directly.

**Testing:**
- No test directory exists in this repository. Validation is done via `python3 -m py_compile` (syntax) and `python3 -c "import json ..."` (JSON validity) before each commit.

## Naming Conventions

**Files:**
- Snake_case Python module names matching HA platform names exactly: `sensor.py`, `number.py`, `datetime.py`, `select.py`, `button.py`, `binary_sensor.py`. Do not rename.
- `datetime.py` intentionally shadows the Python stdlib `datetime` module name; HA's package loader handles the namespace — `from datetime import datetime, time, timedelta` inside the file works correctly.

**Classes:**
- Entity classes: `PurposePlatform` format — e.g., `EVSensor`, `DepartureDateTime`, `FinishModeSelect`, `StartSessionButton`, `SessionActiveBinarySensor`.
- Private base classes prefixed with `_`: `_EVPercentBase`, `_SessionButtonBase`.

**Constants:**
- All in `const.py`.
- Config keys: `CONF_*` (e.g., `CONF_TARIFF_SENSOR`)
- Defaults: `DEFAULT_*` (e.g., `DEFAULT_MAX_CURRENT`)
- Action states: `ACTION_*` (e.g., `ACTION_CHARGE_GENTLE`)
- Throttle reasons: `THROTTLE_*` (e.g., `THROTTLE_BY_APARTMENT`)
- Plan status: `PLAN_*` (e.g., `PLAN_OK`)
- Finish modes: `FINISH_MODE_*` (e.g., `FINISH_MODE_END_OF_NIGHT`)

**UI Labels:**
- Sentence case in all labels, names, and dropdown options. Never Title Case.

**Entity unique IDs:**
- Pattern: `f"{entry.entry_id}_{key}"` — e.g., `f"{entry.entry_id}_battery_capacity"`.

**Device info:**
- Identical across all entity classes: `{"identifiers": {(DOMAIN, entry.entry_id)}, "name": "EVPoint Charge Scheduler", "manufacturer": "EVPoint Charge Scheduler"}`.

## Where to Add New Code

**New read-only sensor:**
- Add an `EVSensorDescription` entry to the `SENSORS` tuple in `custom_components/evpoint_charge_scheduler/sensor.py`.
- Add the corresponding key/value to the dict returned by `_async_update_data` in `custom_components/evpoint_charge_scheduler/coordinator.py`.
- Add a UI label in `custom_components/evpoint_charge_scheduler/strings.json`, then copy to `translations/en.json`.

**New writable input entity:**
- Implement in the appropriate platform file (`number.py`, `select.py`, `datetime.py`).
- Extend `RestoreEntity`; add session-lock guard in `async_set_native_value` / `async_select_option`.
- Add a `async_set_*` setter on `SmartEVChargingCoordinator` in `coordinator.py`.
- Use `vol.Optional` in the config schema in `config_flow.py` if it requires an external entity; `vol.Required` only for pure-numeric config that always has a sensible default.
- Add a new `DEFAULT_*` constant in `const.py`.
- Update `README.md` "Configuration" and "Entities created" sections.

**New external service integration:**
- Declare its config key as `vol.Optional` in `config_flow.py`'s `_build_schema`.
- Read it in `coordinator.py` via `cfg.get(CONF_*)` with a graceful fallback when absent.
- Add a `CONF_*` and `DEFAULT_*` constant in `const.py`.

**New finish mode:**
- Add the string to `FINISH_MODES` list in `const.py`.
- Add a `FINISH_MODE_*` constant in `const.py`.
- Add a branch in the decision tree in `_async_update_data` in `coordinator.py`.
- Add handling in `_compute_gentle_plan` in `coordinator.py`.
- Add the UI label in `strings.json` → copy to `translations/en.json`.

**New config option (non-entity):**
- Add `CONF_*` and `DEFAULT_*` to `const.py`.
- Add to `_build_schema` in `config_flow.py`.
- Read via `cfg.get(CONF_*, DEFAULT_*)` in `coordinator.py`.

## Special Directories

**`.planning/codebase/`:**
- Purpose: GSD codebase map documents (this file and siblings).
- Generated: Yes (by GSD tooling).
- Committed: Yes.

**`custom_components/evpoint_charge_scheduler/__pycache__/`:**
- Purpose: Python bytecode cache.
- Generated: Yes (by Python).
- Committed: Varies — recent commits include it; generally safe to gitignore.

**`brands/`:**
- Purpose: Icon assets.
- Generated: PNGs are generated from SVG source via `cairosvg`.
- Committed: Yes.

## Version Bump Checklist

1. Bump `version` in `custom_components/evpoint_charge_scheduler/manifest.json`.
2. Update `README.md` if behaviour changed.
3. Validate: `python3 -m py_compile custom_components/evpoint_charge_scheduler/*.py`
4. Validate JSON: `python3 -c "import json, sys; [json.load(open(f)) for f in sys.argv[1:]]" manifest.json strings.json translations/en.json hacs.json`
5. Verify strings mirror: `diff strings.json translations/en.json`
6. Commit: `git add -A && git commit -m "vX.Y.Z: <description>"`
7. Tag: `git tag vX.Y.Z && git push --tags`

---

*Structure analysis: 2026-06-04*
