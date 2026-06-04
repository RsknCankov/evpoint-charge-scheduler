# Testing Patterns

**Analysis Date:** 2026-06-04

## Automated Test Framework

**There are no automated tests in this repository.**

A search for `test_*.py`, `*_test.py`, `*.spec.*`, `pytest.ini`, `pyproject.toml`, `conftest.py`, `setup.cfg`, and `tox.ini` returned no results. There is no `tests/` or `test/` directory. There is no `.github/workflows/` directory, so no CI pipeline exists.

This is the current reality. Any future testing work would be building from scratch.

## Manual Validation Workflow

The pre-commit validation workflow documented in `CLAUDE.md` serves as the substitute for automated tests. It must be run manually before every commit.

### Step 1 — Python syntax check

```bash
python3 -m py_compile custom_components/evpoint_charge_scheduler/*.py
```

Catches syntax errors in all eight Python files:
- `custom_components/evpoint_charge_scheduler/__init__.py`
- `custom_components/evpoint_charge_scheduler/coordinator.py`
- `custom_components/evpoint_charge_scheduler/config_flow.py`
- `custom_components/evpoint_charge_scheduler/const.py`
- `custom_components/evpoint_charge_scheduler/sensor.py`
- `custom_components/evpoint_charge_scheduler/number.py`
- `custom_components/evpoint_charge_scheduler/datetime.py`
- `custom_components/evpoint_charge_scheduler/select.py`
- `custom_components/evpoint_charge_scheduler/button.py`
- `custom_components/evpoint_charge_scheduler/binary_sensor.py`

Silent on success; prints a `SyntaxError` and exits non-zero on failure.

### Step 2 — JSON validation

```bash
python3 -c "import json, sys; [json.load(open(f)) for f in sys.argv[1:]]" \
  custom_components/evpoint_charge_scheduler/manifest.json \
  custom_components/evpoint_charge_scheduler/strings.json \
  custom_components/evpoint_charge_scheduler/translations/en.json \
  hacs.json
```

Catches malformed JSON in all four JSON files. Silent on success.

### Step 3 — strings.json mirror check

```bash
diff custom_components/evpoint_charge_scheduler/strings.json \
     custom_components/evpoint_charge_scheduler/translations/en.json
```

Verifies `translations/en.json` is a byte-for-byte copy of `strings.json`. Silent on success; prints a diff and exits non-zero if they differ.

### Fix for strings.json drift

```bash
cp custom_components/evpoint_charge_scheduler/strings.json \
   custom_components/evpoint_charge_scheduler/translations/en.json
```

### Step 4 — Local HA install test

For behaviour verification, the integration is installed into a live Home Assistant instance via HACS:

1. HACS → Integrations → ⋮ → Custom repositories → add the GitHub URL → category Integration
2. Install via HACS, restart HA
3. Settings → Devices & Services → Add Integration → search "EVPoint Charge Scheduler"
4. To pick up code changes: `Settings → Devices & Services → ⋮ → Reload` (or restart HA for new platforms/sensors)

This is the only way to verify runtime behaviour — coordinator logic, charger service calls, entity state restoration, session lifecycle, and load balancing.

## Coverage Reality

**No automated coverage measurement exists.** The following areas are entirely unverified by automated tests:

### Planner logic (highest risk)

- `coordinator.py` `_async_update_data` — the full decision tree (idle/done/too_late/deficit/safety/gentle/asap branches)
- `_compute_gentle_plan` — the gentle current and start time calculations for all three finish modes
- `_gentle_current_within_budget` — cost-aware slow charging budget scan
- `_night_hours_between` — 5-minute-step night window iterator (wraps midnight)
- `_latest_night_end_before` — boundary conditions at target/now edge cases
- `_circular_median_time` — circular statistics for learned night window

### Edge cases with no coverage

- Midnight-crossing night windows (e.g., `night_start=22:00`, `night_end=06:00`)
- Departure time in the past (`hours_to_dep <= 0`)
- `energy_needed <= 0` auto-end path
- Deficit calculation when `day_hours = 0`
- `_gentle_current_within_budget` when no current fits budget — falls back to `max_a`
- Session restore across HA restart
- Config entry with every optional field omitted (pure advisory mode)
- Options flow round-trip (verify merged data+options displayed correctly)

### Entity locking

- Writable inputs (`number.py`, `datetime.py`, `select.py`) reject changes during active sessions and revert the UI — this is untested

### Load balancer

- All four throttle branches (`unrestricted`, `smart_charging_pause`, `apartment_load_too_high`, `throttled_by_apartment`)

### Learning subsystem

- `_async_learn_night_window` with insufficient history (<2 transitions)
- `_async_learn_prices` with one bucket empty
- Recorder import failure path

## What Would Be Needed to Add Tests

**Framework:** pytest with `pytest-homeassistant-custom-component` (the standard HA custom integration test harness)

**Key dependencies to add:**
- `pytest`
- `pytest-asyncio`
- `pytest-homeassistant-custom-component`
- `homeassistant` (dev/test version)

**High-value test targets (in priority order):**

1. **Pure calculation helpers** — no HA mocking required, can be tested directly:
   - `SmartEVChargingCoordinator._kw_to_current`
   - `SmartEVChargingCoordinator._night_hours_between`
   - `SmartEVChargingCoordinator._is_in_night_window`
   - `SmartEVChargingCoordinator._latest_night_end_before`
   - `SmartEVChargingCoordinator._circular_median_time`
   - `_parse_time`, `_safe_float` module-level helpers

2. **Decision tree branches** — mock `hass`, `coordinator.data`, and sensor states:
   - Each of the 8 action states with minimal fixture data
   - Deficit override superseding `finish_mode`
   - Safety margin override

3. **`_compute_gentle_plan` / `_gentle_current_within_budget`** — pure-ish math with datetime mocking

4. **Config flow** — HA test helpers for config entry creation and options round-trip

**Suggested file layout when tests are added:**
```
tests/
├── conftest.py              # coordinator fixture, mock hass
├── test_coordinator.py      # decision tree, calculations
├── test_helpers.py          # _kw_to_current, _night_hours_between, etc.
├── test_config_flow.py      # config/options flow
└── test_entities.py         # entity locking, RestoreEntity restore paths
```

---

*Testing analysis: 2026-06-04*
