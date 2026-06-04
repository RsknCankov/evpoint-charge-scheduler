# Codebase Concerns

**Analysis Date:** 2026-06-04

---

## 1. No Automated Test Suite

**Severity: HIGH — headline concern**

- Issue: Zero test files exist anywhere in the repository. No `tests/` directory, no `test_*.py` files, no CI pipeline (no `.github/` directory, no `Makefile`). The entire coordinator logic — planner decision tree, deficit calculation, gentle-current math, circular-median night-window learning, cost-aware budget scanning, load-balancer — is validated exclusively by manual testing against a live HA instance.
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (1051 lines, all untested)
- Impact: Regressions in time/tariff/cost arithmetic are invisible until they cause a real under- or over-charge. The decision tree (8 branches) and `_gentle_current_within_budget` (scan loop with budget constraint) are particularly high-risk. A unit test suite with mocked `dt_util.now()` and scenario fixtures could cover all branches in minutes.
- Fix approach: Add `tests/test_coordinator.py` using `pytest` + `unittest.mock`. Pure functions like `_kw_to_current`, `_night_hours_between`, `_is_in_night_window`, `_circular_median_time`, `_latest_night_end_before`, and the full `_async_update_data` decision tree are all testable without a live HA instance. The `homeassistant.core.HomeAssistant` dependency can be mocked.

---

## 2. Stale Root-Level Python Files Tracked in Git

**Severity: MEDIUM — silent confusion risk**

- Issue: Three Python files at the repository root are tracked in git: `coordinator.py`, `config_flow.py`, and `const.py`. These are outdated copies of the actual package files under `custom_components/evpoint_charge_scheduler/`. `config_flow.py` is missing `CONF_FINISH_MODE`, `CONF_PRICE_SENSOR`, `FINISH_MODES`, and the finish-mode selector added in v0.6.0/v0.7.0. `const.py` is missing `NIGHT_WINDOW_LEARN_DAYS`, `CONF_PRICE_SENSOR`, and `CONF_FINISH_MODE`. `coordinator.py` diverges at the import block.
- Files: `/coordinator.py`, `/config_flow.py`, `/const.py` (root) vs `custom_components/evpoint_charge_scheduler/coordinator.py`, `config_flow.py`, `const.py` (canonical)
- Impact: Any contributor who edits the root copies instead of the package copies silently ships a no-op change. A future Claude instance following a "edit coordinator.py" instruction might touch the wrong file.
- Fix approach: `git rm coordinator.py config_flow.py const.py` at the repo root and add `*.py` (or the specific names) to `.gitignore` at root level. The `.gitignore` currently only excludes `.DS_Store`.

---

## 3. `__pycache__` Artifacts Committed to Git

**Severity: LOW — cosmetic but messy**

- Issue: All 10 `.pyc` files under `custom_components/evpoint_charge_scheduler/__pycache__/` are tracked in git (confirmed via `git ls-files`). The commit `9b837b8 chore: refresh __pycache__ artifacts` explicitly updated them. The `.gitignore` does not contain `__pycache__/` or `*.pyc`.
- Files: `custom_components/evpoint_charge_scheduler/__pycache__/*.cpython-39.pyc` (10 files)
- Impact: Every Python edit causes a spurious git-tracked change when HA re-compiles. It also pins the bytecode to Python 3.9, making the repo appear to require that version even though `manifest.json` has no `python_requires` field.
- Fix approach: Add `__pycache__/` and `*.pyc` to `.gitignore`, then `git rm -r --cached custom_components/evpoint_charge_scheduler/__pycache__/`.

---

## 4. Options-Flow Save Triggers Full Integration Reload — Resets Learned State

**Severity: MEDIUM — user-visible latency after reconfiguration**

- Issue: `_async_update_listener` in `custom_components/evpoint_charge_scheduler/__init__.py` (line 43) calls `hass.config_entries.async_reload(entry.entry_id)` on every options save. This destroys the coordinator instance, including `_learned_night_start`, `_learned_night_end`, `_learned_night_price`, and `_learned_day_price`. The re-learning task (`_async_daily_learn`) fires once at startup, making a recorder query that takes several seconds. Until it resolves, `night_window_source` shows `configured` and cost-aware charging is disabled.
- Files: `custom_components/evpoint_charge_scheduler/__init__.py` (line 41–43), `custom_components/evpoint_charge_scheduler/coordinator.py` (lines 155–164, 186–189)
- Impact: Any options change resets learned state. Not a bug per se — it recovers within the startup learn cycle — but users with slow HA instances may briefly get suboptimal scheduling after every config change.
- Fix approach: Implement `async_update_entry` instead of a full reload, or persist learned values to `entry.runtime_data` / HA storage so they survive the reload.

---

## 5. `blocking=False` Service Calls with Optimistic State Tracking

**Severity: MEDIUM — silent mismatch when OCPP call is dropped**

- Issue: Both OCPP and switch service calls in `_apply_to_charger` use `blocking=False` (`coordinator.py` lines 1023, 1046). The state trackers `_last_applied_current` and `_last_applied_running` are updated immediately after the `async_call` returns — before the service has actually executed. If the OCPP integration is temporarily unavailable and the call is silently dropped (no exception raised), the coordinator believes the charger received the command and will not re-send it on the next 30-second cycle.
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (lines 1022–1036, 1039–1051)
- Impact: The charger may be stuck at a stale current cap (e.g., previous session's max-amp profile), while the integration reports `dynamic_target_current = 0`. The next change in `dynamic_current` will trigger a retry, but if the current is stable (e.g., parked overnight at target current) the mismatch can persist for hours.
- Fix approach: Use `blocking=True` for the OCPP call (it is already `async_call`, so it awaits the service dispatcher), or only update `_last_applied_current` inside the `try` block after confirming delivery. Note: `_last_applied_current` is already inside the `try`, so the real risk is silent drop without exception.

---

## 6. `_night_hours_between` Has 5-Minute Quantization Error

**Severity: LOW — planning inaccuracy up to 5 minutes**

- Issue: `_night_hours_between` in `coordinator.py` (lines 909–938) walks time in 5-minute steps. The last partial step is not counted. For a night window of exactly 7:57, this returns 7:55. The comment acknowledges "accurate to ~5 min, plenty for planning purposes."
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (lines 909–938)
- Impact: Could cause a false positive on `deficit_kwh > 0` by under-counting available night hours by up to 5 minutes. At 7.36 kW max (32A × 230V × √3), 5 minutes = 0.61 kWh. In practice this widens the `slack` slightly in the "does night cover it?" check, which means the integration might add a small unnecessary day-supplement at the margin.
- Fix approach: Replace the step loop with a direct calculation using window boundary arithmetic. Low priority — the existing error is always conservative (counts fewer night minutes, never over-plans).

---

## 7. `_safe_float` Called With `default=None` — Type Mismatch

**Severity: LOW — latent type error**

- Issue: `_safe_float` is declared `def _safe_float(state_value: Any, default: float = 0.0) -> float`. In `_async_learn_prices` (coordinator.py line 261) it is called as `_safe_float(st.state, default=None)`. This passes `None` as the `default` and the function returns `None` when the state is invalid — which is not a `float`. The result is assigned to `price` and then `if price is None: continue` handles it, so there is no runtime crash. But the type annotation is wrong and type checkers would flag it.
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (lines 104–110, 261)
- Impact: No runtime bug. Misleads type checkers and static analysis.
- Fix approach: Add an overload or change the signature to `Optional[float]`, or introduce a separate `_safe_float_or_none` helper for the price-learning path.

---

## 8. Night-Window Learning Requires ≥2 Clean Transitions Per Side — Silently Falls Back

**Severity: LOW — operational risk on new installs**

- Issue: `_async_learn_night_window` (coordinator.py line 342) requires `len(start_samples) >= 2 and len(end_samples) >= 2` — i.e., at least 2 confirmed day→night and 2 night→day transitions in the past 14 days. A new install with a tariff sensor sees only `night_window_source = configured` until this threshold is met, but there is no user-facing warning that learning is in progress. The `sensor.night_window_source` distinguishes `learned` vs `configured` but the dashboard does not surface "still learning."
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (lines 342–360), `custom_components/evpoint_charge_scheduler/sensor.py` (`night_window_source` sensor)
- Impact: On a new install, if the configured clock times differ from the real tariff window, the integration uses the wrong window for 1–2 days. Could cause premature start or missed night window on first use. Not catastrophic — falls back to configured values rather than failing.
- Fix approach: Add a third `night_window_source` value (e.g., `learning`) and expose it when `len(start_samples) < 2`. Low priority if the configured defaults match the user's actual tariff window.

---

## 9. `chargingProfileId` Reuse Silently Overwrites Other OCPP Automations

**Severity: LOW — inter-automation conflict**

- Issue: Every OCPP push uses the same `chargingProfileId` (default `8`, configurable via `CONF_CHARGING_PROFILE_ID`). This is intentional — it replaces the previous profile. However, if another HA automation or OCPP integration installs a profile with id `8`, this integration silently replaces it, and vice versa. No logging or conflict detection.
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (line 975, 1009), `custom_components/evpoint_charge_scheduler/const.py` (`DEFAULT_CHARGING_PROFILE_ID = 8`)
- Impact: Silent conflict with other OCPP automations. The default `8` is arbitrary and undocumented in the HA OCPP integration's own defaults. Users running multiple OCPP profiles need to manually pick non-colliding IDs.
- Fix approach: Document the default ID prominently in README. No code change needed; the conflict is expected OCPP behavior, not a bug.

---

## 10. `manifest.json` Contains Placeholder `@yourname` URLs

**Severity: LOW — cosmetic / HACS listing**

- Issue: `custom_components/evpoint_charge_scheduler/manifest.json` has `"codeowners": ["@yourname"]`, `"documentation": "https://github.com/yourname/evpoint-charge-scheduler"`, and `"issue_tracker": "https://github.com/yourname/evpoint-charge-scheduler/issues"`. These are unfilled template placeholders.
- Files: `custom_components/evpoint_charge_scheduler/manifest.json` (lines 5, 8, 10)
- Impact: HACS displays broken codeowner and documentation links. Issue tracker link is dead. No functional impact.
- Fix approach: Replace `yourname` with the actual GitHub username/org.

---

## 11. `datetime.py` Filename Clashes With Python stdlib `datetime` Module

**Severity: LOW — known gotcha, currently safe**

- Issue: `custom_components/evpoint_charge_scheduler/datetime.py` shadows Python's `datetime` stdlib module within the package namespace. The file itself uses `from datetime import datetime, timedelta` (absolute import), which works because HA's package loader resolves it correctly.
- Files: `custom_components/evpoint_charge_scheduler/datetime.py`
- Impact: Safe today. Could break if HA's import machinery changes, or if a future contributor adds `from .datetime import ...` thinking it refers to the stdlib. The CLAUDE.md gotcha section documents this explicitly.
- Fix approach: Cannot be renamed without changing HA platform discovery. No action needed beyond keeping the warning in CLAUDE.md.

---

## 12. `safety_margin_hours` Unit is Hours, Not Minutes

**Severity: LOW — misread risk in future edits**

- Issue: `CONF_SAFETY_MARGIN_HOURS` / `DEFAULT_SAFETY_MARGIN_HOURS = 0.5` (const.py line 55) is in hours (= 30 minutes). The variable name is accurate but the small float value can mislead someone editing the safety-margin logic into treating it as minutes.
- Files: `custom_components/evpoint_charge_scheduler/const.py` (line 55), `custom_components/evpoint_charge_scheduler/coordinator.py` (line 430, 553)
- Impact: If a future edit multiplies by 60 thinking it needs conversion, the safety margin would balloon to 30 hours and always trigger `charge_max_now`.
- Fix approach: No change needed. The name `_hours` is the documentation. The CLAUDE.md gotcha section documents it.

---

## 13. Per-Phase vs Total Amp Limit Ambiguity

**Severity: LOW — documentation and user education**

- Issue: `CONF_TOTAL_LIMIT` (default 60) is interpreted as per-phase amps in 3-phase mode. The load-balancer formula (`available_current = total_limit - headroom - apartment_current`) does not multiply by phase count. A user who enters "60A total" expecting 20A per phase will configure the integration incorrectly.
- Files: `custom_components/evpoint_charge_scheduler/coordinator.py` (line 598), `custom_components/evpoint_charge_scheduler/const.py` (`CONF_TOTAL_LIMIT`)
- Impact: Could result in the charger being allowed more amps than the physical breaker permits (if user provides total amps), tripping the breaker. Or over-conservative load balancing (if user provides per-phase correctly).
- Fix approach: Clarify the label and description in `strings.json`/`translations/en.json` and README. Consider renaming to `total_current_limit_per_phase`.

---

## 14. Single-Entry Singleton — No Multi-Instance Support

**Severity: LOW — by design, but limits reach**

- Issue: `ConfigFlow.async_step_user` (config_flow.py line 140) calls `await self.async_set_unique_id(DOMAIN)` and `self._abort_if_unique_id_configured()`. This enforces exactly one config entry per HA instance. A user with two EV chargers cannot run two instances.
- Files: `custom_components/evpoint_charge_scheduler/config_flow.py` (lines 140–142)
- Impact: One integration instance per HA install. Documented intentional design ("singleton"). Would require a meaningful architecture refactor to support multi-vehicle.
- Fix approach: Use a vehicle-specific unique ID (e.g., `ocpp_devid`) if multi-instance support is ever needed. No urgency for the current use case.

---

## 15. Brand Icons Not Submitted to `home-assistant/brands`

**Severity: COSMETIC — no functional impact**

- Issue: `brands/` directory contains `icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`, and `icon.svg`, but a PR to `home-assistant/brands` under `custom_integrations/evpoint_charge_scheduler/` has not been filed. HA and HACS display a generic placeholder icon.
- Files: `brands/` directory
- Impact: Visual only. Integration functions normally.
- Fix approach: Submit a PR to `home-assistant/brands` when the integration is published under a stable GitHub username.

---

## 16. Options Save Reloads Integration Mid-Session — No Guard

**Severity: MEDIUM — potential session interruption**

- Issue: `_async_update_listener` triggers `async_reload` on every options save (`__init__.py` line 43). Reloading tears down the coordinator and all platform entities. If a charging session is active when options are saved, the session flag (`binary_sensor.session_active`) is restored by `RestoreEntity` on re-setup, and the coordinator resume path works. However, during the reload window (seconds), the charger receives no stop command — it continues at the last pushed current. There is a brief period where the physical charger is running but the HA integration is unloaded.
- Files: `custom_components/evpoint_charge_scheduler/__init__.py` (lines 41–43), `custom_components/evpoint_charge_scheduler/binary_sensor.py` (lines 53–57)
- Impact: Charger keeps charging at the last OCPP profile limit for the duration of the reload. On resume, the restore path re-activates the session, and the coordinator pushes the correct current on the first 30-second cycle. Real-world impact is minimal but the charger is briefly "unmanaged."
- Fix approach: Add a guard in `_async_update_listener` that declines to reload if `coordinator.session_active` is True, prompting the user to stop the session first.

---

## Test Coverage Gaps

**Core logic untested:**
- All 8 branches of the decision tree in `_async_update_data`
- `_compute_gentle_plan` under all three finish modes
- `_gentle_current_within_budget` budget-scan loop
- `_night_hours_between` boundary conditions (midnight wrap, zero window)
- `_circular_median_time` with various clusterings and midnight crossings
- `_latest_night_end_before` edge cases (departure before next night_end)
- Load-balancer all four throttle states
- Session lifecycle (start, auto-end on done, mid-session input lock, restart recovery)

Files: `custom_components/evpoint_charge_scheduler/coordinator.py` — entire file
Risk: Regressions ship silently
Priority: **High**

---

*Concerns audit: 2026-06-04*
