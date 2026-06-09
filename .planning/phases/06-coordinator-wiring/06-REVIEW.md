---
phase: 06-coordinator-wiring
padded_phase: "06"
review_depth: standard
status: clean
critical: 0
warning: 0
info: 2
reviewed_files:
  - custom_components/evpoint_charge_scheduler/const.py
  - custom_components/evpoint_charge_scheduler/coordinator.py
  - tests/test_coordinator_notification.py
  - tests/test_finish_mode_seam.py
reviewed_at: "2026-06-09"
---

# Phase 06: Coordinator Wiring — Code Review

**Status: CLEAN** — 0 critical, 0 warning, 2 info (non-actionable notes)

## Summary

Phase 06 added the day-supplement notification edge-trigger (NIGHT-03) and fixed the Night Only cost-spread guard (NIGHT-04/05). The implementation is minimal and correct. No correctness bugs, security issues, or code quality problems found.

## Findings

### [INFO-01] Redundant `target_finish is not None` guard in `_compute_gentle_plan`

**File:** `custom_components/evpoint_charge_scheduler/coordinator.py`
**Lines:** 1103–1107

**Detail:**
The guard at line 1096 already returns early when `target_finish is None`:
```python
if energy_needed <= 0 or target_finish is None:
    return max_a, now
```
The additional `and target_finish is not None` check at line 1107 is therefore unreachable (target_finish is guaranteed non-None at that point). The check is harmless and adds clarity about intent, but is logically redundant.

**Verdict:** Non-actionable. The redundancy makes the guard's intent explicit and aligns with how the original code read before the fix. No change needed.

---

### [INFO-02] `_setup` in test_coordinator_notification.py always uses frozen departure

**File:** `tests/test_coordinator_notification.py`
**Lines:** 63–72

**Detail:**
`coordinator.departure_time = dt_util.now() + timedelta(hours=3)` inside `_setup` is called within the `freeze_time(DAY)` context, so `dt_util.now()` returns the frozen 15:00 moment and departure is reliably 18:00. If `_setup` were ever called outside a `freeze_time` block, the departure time would be relative to wall clock. The current test structure calls `_setup` inside the `with freeze_time(DAY):` block in every test, so this is safe.

**Verdict:** Non-actionable. Pattern is consistent with `test_watchdog.py`. No change needed.

---

## Files Reviewed

| File | Finding | Notes |
|------|---------|-------|
| `const.py` | Clean | `CONF_NOTIFY_SERVICE` placement and naming follow project conventions |
| `coordinator.py` | INFO-01 | Redundant guard in `_compute_gentle_plan`, harmless |
| `test_coordinator_notification.py` | INFO-02 | Freeze-time coupling in _setup, safe as written |
| `test_finish_mode_seam.py` | Clean | 3 new tests follow exact existing style, correct assertions |

## Security Assessment

- **T-06-01**: Notification message contains only `day_current` (int) — no user input, no PII, no secrets. Consistent with T-03-10 pattern documented in RESEARCH.md.
- **T-06-02**: `_last_notified_action` level-change detector prevents notification spam. Confirmed by edge-trigger semantics.
- **T-06-03**: `"." in notify_svc` guard prevents ValueError on dotless strings. Empty string check (`if notify_svc and`) prevents call on unconfigured entry.
- **T-06-04**: `target_finish is not None` guard correctly handles ASAP/Departure modes passing `target_finish=None`.
- **T-06-05**: `_gentle_current_within_budget` `total_window_h <= 0` fallback still applies with `target_finish` as the horizon.

All threat model items from the plan's `<threat_model>` section are mitigated as specified.
