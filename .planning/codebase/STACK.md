# Technology Stack

**Analysis Date:** 2026-06-04

## Languages

**Primary:**
- Python 3 - All integration logic. Uses `from __future__ import annotations` throughout for PEP 563 deferred evaluation.

**Secondary:**
- JSON - Manifests, UI strings, translations (`manifest.json`, `strings.json`, `translations/en.json`, `hacs.json`)
- SVG/PNG - Brand icon assets in `brands/`

## Runtime

**Environment:**
- Home Assistant core (custom component, loaded by HA's package loader at startup)
- Python 3.12+ (implicit — HA 2024.6.0 minimum requires Python 3.12)
- Single asyncio event loop (HA's event loop — all coordinator logic is `async`)

**Package Manager:**
- No standalone package manager — distribution is via HACS (Home Assistant Community Store)
- No `requirements.txt`, `pyproject.toml`, or `setup.py`
- Lockfile: Not applicable (HACS/HA manages the Python environment)

## Frameworks

**Core:**
- Home Assistant `DataUpdateCoordinator` (`homeassistant.helpers.update_coordinator`) - Central polling and push-on-change orchestration, 30-second interval, defined in `custom_components/evpoint_charge_scheduler/coordinator.py`
- Home Assistant config entries (`homeassistant.config_entries`) - Config/options flow, singleton entry enforcement (`async_set_unique_id(DOMAIN)`), defined in `custom_components/evpoint_charge_scheduler/config_flow.py`
- Home Assistant entity platforms (`homeassistant.components.sensor`, `.number`, `.datetime`, `.select`, `.button`, `.binary_sensor`) - Six HA platforms registered in `custom_components/evpoint_charge_scheduler/const.py` (`PLATFORMS` list)
- `voluptuous` - Config schema validation and coercion in `custom_components/evpoint_charge_scheduler/config_flow.py`
- Home Assistant `selector` helpers (`homeassistant.helpers.selector`) - Entity/select UI widgets in the config flow

**Testing:**
- Not detected (no test files, no `pytest.ini`, `setup.cfg`, or `vitest.config.*`)

**Build/Dev:**
- No build step — Python source files are deployed directly as a HACS custom component
- `cairosvg` (dev-only, optional) - Regenerates PNG brand icons from `brands/icon.svg`; not shipped with the integration

## Key Dependencies

**Critical (HA built-ins, no extra install required):**
- `homeassistant.helpers.update_coordinator.DataUpdateCoordinator` - Coordinator base class
- `homeassistant.helpers.event.async_track_state_change_event` - Real-time sensor reactivity (tariff, apartment current, SoC sensor)
- `homeassistant.helpers.event.async_track_time_interval` - Daily recorder-learn timer
- `homeassistant.helpers.restore_state.RestoreEntity` - Session state persistence across HA restarts (`binary_sensor.py`)
- `homeassistant.util.dt` (`dt_util`) - Timezone-aware datetime operations throughout `coordinator.py`

**Infrastructure (HA optional component, no `requirements` entry needed):**
- `homeassistant.components.recorder` (`get_instance`, `state_changes_during_period`) - History queries for night-window learning and price learning. Imported with `try/except ImportError` so the integration works even if recorder is disabled. Used in `coordinator.py` methods `_async_learn_night_window` and `_async_learn_prices`.

**Declared in `manifest.json`:**
- `dependencies: []` - No HA integration dependencies declared
- `requirements: []` - No PyPI packages required; zero external installs

## Configuration

**Environment:**
- All configuration lives in the HA config entry (data + options), accessible via `entry.data` and `entry.options`
- No `.env` files or secrets files — secrets are managed by HA's credential store or entered directly in the UI
- Key config values: `ocpp_set_rate_service`, `ocpp_devid`, `tariff_sensor`, `apartment_current_sensor`, `soc_sensor`, `charger_switch`, `price_sensor`, `voltage`, `phases`, `min_current`, `max_current`, `total_current_limit`, `safety_headroom`, `night_start`, `night_end`, `charging_profile_id`
- All constants and defaults in `custom_components/evpoint_charge_scheduler/const.py` as `DEFAULT_*` and `CONF_*` names

**Build:**
- No build config files
- Validation script (manual, pre-commit):
  ```bash
  python3 -m py_compile custom_components/evpoint_charge_scheduler/*.py
  python3 -c "import json, sys; [json.load(open(f)) for f in sys.argv[1:]]" manifest.json strings.json translations/en.json hacs.json
  diff custom_components/evpoint_charge_scheduler/strings.json custom_components/evpoint_charge_scheduler/translations/en.json
  ```

## Platform Requirements

**Development:**
- Python 3.12+
- Home Assistant 2024.6.0+ (minimum declared in `hacs.json`)
- `cairosvg` for regenerating brand PNGs (optional, dev only)

**Production:**
- Distribution: HACS custom integration (add repo URL, category Integration)
- Installation path: `<HA config>/custom_components/evpoint_charge_scheduler/`
- `iot_class: "calculated"` (no outbound network calls of its own — all I/O is HA service calls and state reads)
- Singleton: one config entry per HA install, enforced by `async_set_unique_id(DOMAIN)` in `config_flow.py`

## Versioning

- `version` field in `custom_components/evpoint_charge_scheduler/manifest.json` — currently `0.7.1`
- Scheme: `MAJOR.MINOR.PATCH`
  - Patch (0.0.x): bug fixes, no behaviour change
  - Minor (0.x.0): new config options, new entities, user-visible behaviour changes
  - Major (x.0.0): reserved for breaking config migrations (not used pre-1.0)
- Release: bump `manifest.json`, commit, `git tag vX.Y.Z && git push --tags`

---

*Stack analysis: 2026-06-04*
