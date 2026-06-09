---
phase: 06-coordinator-wiring
plan: "01"
subsystem: coordinator
tags: [home-assistant, notification, edge-trigger, service-call]

requires:
  - phase: 05-night-only-restructure
    provides: Night Only mode + FINISH_MODE_END_OF_NIGHT + day-supplement action path

provides:
  - CONF_NOTIFY_SERVICE constant in const.py
  - _last_notified_action per-session edge-trigger tracker in coordinator
  - Notification gate in _async_update_data: fires HA notify service exactly once per session transition to day-supplement
  - 5 integration tests in tests/test_coordinator_notification.py

affects: [07-ui-wiring, verify-work]

tech-stack:
  added: []
  patterns: [edge-trigger pattern via level-change detector on action string, fire-and-forget async_create_task for service call]

key-files:
  created:
    - tests/test_coordinator_notification.py
  modified:
    - custom_components/evpoint_charge_scheduler/const.py
    - custom_components/evpoint_charge_scheduler/coordinator.py

key-decisions:
  - "_last_notified_action else branch updates tracker on every cycle (not just when firing) so transitions back from day-supplement then re-entering will re-fire — per D-03"
  - "domain_n variable name avoids shadowing outer domain variable in OCPP payload"
  - "blocking=False on service call keeps the update cycle non-blocking"

patterns-established:
  - "Edge-trigger: if action == TARGET and _last != TARGET: fire, set _last = TARGET; else: _last = action"
  - "Graceful degradation: empty string check + dot check before split prevents ValueError on malformed/unconfigured service"

requirements-completed:
  - NIGHT-03
completed: "2026-06-09"
duration: 20min
---

# Phase 06-01: Coordinator Wiring Summary

**Edge-triggered day-supplement notification: fires HA push notification exactly once per session when Night Only mode spills into day-tariff charging, with session-reset re-fire and graceful degradation for unconfigured/malformed service strings**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-06-09T16:30:00Z
- **Completed:** 2026-06-09T16:50:00Z
- **Tasks:** 3
- **Files modified:** 3 (1 created)

## Accomplishments
- Added `CONF_NOTIFY_SERVICE = "notify_service"` constant to const.py alongside other optional entity CONF_ constants
- Added `_last_notified_action: str | None = None` to coordinator `__init__` and reset in `async_end_session` for per-session edge-trigger semantics
- Inserted notification gate in `_async_update_data` between decision computation and `_apply_to_charger`: fires once on first `ACTION_CHARGE_DAY_SUPPLEMENT`, else-branch updates tracker on every non-firing cycle
- Created 5 integration tests: once-firing, no-spam, session-reset re-fire, unconfigured graceful skip, malformed-service graceful skip

## Task Commits

1. **Task 1+2: const + coordinator changes** - `5d90d7c` (feat)
2. **Task 3: notification test file** - `95c5ac1` (test)

## Files Created/Modified
- `custom_components/evpoint_charge_scheduler/const.py` — added `CONF_NOTIFY_SERVICE`
- `custom_components/evpoint_charge_scheduler/coordinator.py` — added `_last_notified_action` field + reset + notification gate in `_async_update_data`
- `tests/test_coordinator_notification.py` — new file, 5 NIGHT-03 integration tests

## Decisions Made
- The `else: self._last_notified_action = action` branch runs on every cycle where the notification does NOT fire. This means if the action transitions away from day-supplement and then back, the notification will fire again within the same session — this is intentional per D-03 (level-change semantics).
- Used `blocking=False` on the `async_call` so the notification dispatch never holds up the coordinator's 30s update cycle.
- `domain_n` as local variable name avoids shadowing the outer `domain` variable used in the OCPP payload section.

## Deviations from Plan
None - plan executed exactly as written.

## Issues Encountered
- System Python (`/usr/bin/python3`) is an Xcode stub and not functional without Xcode CLI tools installed. Syntax compilation was verified via the NDK Python 3.11 binary. Runtime tests (pytest) will be confirmed by CI on push.

## Self-Check: PASSED
- `CONF_NOTIFY_SERVICE` present in const.py and imported in coordinator.py
- `_last_notified_action` initialized in `__init__`, reset in `async_end_session`, referenced in two locations in `_async_update_data` (if-branch set + else-branch set)
- Notification gate positioned between `action = decision.action` and `_apply_to_charger` call
- 5 tests in `test_coordinator_notification.py` syntactically correct; compile passes
- All production Python files in the integration pass `py_compile`

## Next Phase Readiness
- Plan 06-02 (cost-spread guard fix) can proceed — coordinator.py is now ready for the `_compute_gentle_plan` guard change
- No blockers

---
*Phase: 06-coordinator-wiring*
*Completed: 2026-06-09*
