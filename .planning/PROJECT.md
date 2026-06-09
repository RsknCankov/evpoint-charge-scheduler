# EVPoint Charge Scheduler

## What This Is

A HACS-installable Home Assistant custom integration that schedules EV charging around departure time, target SoC, electricity tariff (night vs. day), and apartment power draw. Built for an EVPoint OCPP charger in Bulgaria, but works with any OCPP backend that exposes a `set_charge_rate` service. Single config entry per HA install — singleton integration.

## Core Value

Automatically charge the car cheaply overnight without ever missing a departure — the scheduler always ensures the target SoC is reached by departure time even when the night window alone isn't enough.

## Current Milestone: v2.0.0 Charging Mode Overhaul

**Goal:** Replace all three finish modes with purpose-built scheduling algorithms that are tariff-aware, cost-optimizing, and predictable.

**Target features:**
- Night Only mode — charge exclusively in the next night window; supplement day tariff when night isn't enough + notify
- Departure mode — complete charging exactly at departure time; tariff-aware optimal scheduling
- ASAP mode — user-set amperage, charge immediately, no tariff logic
- 5% cost-spread mechanism across Night Only and Departure (prefer slower current when within budget)
- Configurable mobile notify service; new ASAP current entity; select labels renamed

## Requirements

### Validated

- [x] **SCHED-01**: Smart planner computes charging current based on energy needed, night hours, deficit, and finish mode
- [x] **SCHED-02**: Three finish modes: `asap`, `end_of_night`, `departure` — each with distinct scheduling logic
- [x] **SCHED-03**: Gentle charging (`charge_gentle`) spreads load across available window instead of bursting
- [x] **SCHED-04**: Deficit detection overrides finish mode and supplements with day charging when night alone can't cover
- [x] **LOAD-01**: Load balancer caps planner current by `total_limit - headroom - apartment_current`
- [x] **LOAD-02**: Apartment load wins — charger throttles or stops when apartment draw is too high
- [x] **SESSION-01**: Session lifecycle with start/stop buttons and auto-end on `done`
- [x] **SESSION-02**: Inputs locked while session active; finish-mode is the one exception (apply-and-raise)
- [x] **SESSION-03**: Session state restored on HA restart via RestoreEntity
- [x] **OCPP-01**: OCPP `set_charge_rate` service push on current change — idempotent (only send on change)
- [x] **OCPP-02**: Charger switch turn_off stops charging; `current_only` mode sends 0-amp profile instead
- [x] **NIGHT-01**: Learned night window from tariff-sensor recorder history (14-day circular median)
- [x] **NIGHT-02**: Graceful fallback to configured clock when <2 days of history
- [x] **COST-01**: Cost-aware slow charging — `end_of_night` can spill past night-end within a cost budget
- [x] **COST-02**: Learned day/night price from price-sensor history; budget = `baseline × (1 + tol%)`
- [x] **RELIABLE-01**: Charger-reboot recovery watchdog
- [x] **RELIABLE-02**: Regression harness with 77 tests covering planner matrix, load balancer, input locking, session lifecycle, time boundaries

### Validated (v2.0.0)

- [x] **ASAP-01**: ASAP mode charges immediately at configured amperage, bypassing tariff/deficit/safety overrides — Phase 05
- [x] **ASAP-03**: ASAP deficit/safety bypass implemented via early-return branch positioned after `too_late`, before deficit gate — Phase 05
- [x] **ASAP-04**: `done` and `too_late` guards still fire for ASAP mode — Phase 05
- [x] **CONF-03**: Cost tolerance default changed from 15% to 5% — Phase 05
- [x] **NIGHT-01**: Night Only mode anchors on next night-window end; single-window night hours; day supplement via deficit gate — Phase 05
- [x] **NIGHT-02**: Night Only synthetic `hours_to_dep` from `night_only_target` when no departure set (prevents spurious `too_late`) — Phase 05
- [x] **DEPART-01**: Departure gentle-window uses `_night_hours_between` (not wall-clock hours) — Phase 05
- [x] **DEPART-02**: Departure falls through to deficit/day charging when no night window exists before departure — Phase 05

- [x] **NIGHT-03**: Edge-triggered day-supplement notification — fires HA notify service exactly once per session transition to `charge_day_supplement`, resets on session end — Phase 06
- [x] **NIGHT-04**: Night Only cost-spread guard fix — `_compute_gentle_plan` uses `target_finish is not None` instead of `self.departure_time is not None`, enabling `_gentle_current_within_budget` for Night Only without departure — Phase 06
- [x] **NIGHT-05**: Night Only operates correctly when no departure_time is set — no false deficit, no crash, correct `hours_to_dep` from `night_only_target` — Phase 06

### Active

<!-- Remaining v2.0.0 requirements — ASAP-02, CONF-01, CONF-02, TEST-01..04 — see REQUIREMENTS.md -->

### Out of Scope

- Multi-vehicle / multi-config-entry support — singleton by design for apartment use case
- Native OCPP server — uses existing OCPP HA integration as backend
- Billing / invoice generation — not a payment tool

## Context

- Home Assistant custom integration installed via HACS
- Target hardware: EVPoint OCPP charger, 3-phase or 1-phase, Bulgaria apartment
- Bulgaria electricity: cheap night tariff from ~23:00 to ~06:00 (learned from sensor history)
- Apartment main breaker: typically 25A or 32A per phase; charger must share with household
- No cloud component — fully local, recorder-based learning

## Constraints

- **HA compatibility**: Must work with HA core releases — no private APIs
- **HACS**: Repo structure must pass HACS validator (hacs.json at root)
- **Singleton**: One config entry only — not designed for multi-EV households
- **Blocking calls**: Recorder queries run in executor — never inline in `_async_update_data`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Finish-mode select is raise-and-allow during session | Reverting silently was the Phase-1 "lost night" bug | ✓ Good |
| `chargingProfileId` reused across pushes | Replaces previous profile, prevents stack buildup | ✓ Good |
| Night window learned from recorder, not live sensor | Smooth look-ahead even when tariff sensor lags | ✓ Good |
| `translations/en.json` is byte-for-byte copy of `strings.json` | HA platform requirement | ✓ Good |
| `OptionsFlow()` inherits `config_entry` as read-only property | Setting it raised 500; fixed in v0.7.1 | ✓ Good |
| Safety margin triggers `charge_max_now` override | Deficit + safety always beat finish mode | ✓ Good |
| ASAP early-return positioned after `too_late`, before deficit gate | Ensures done/too_late guards fire; deficit/safety correctly bypassed only for ASAP | ✓ Good |
| `PlanInputs.asap_current` defaults to 0 (not max_a) | Test-backward-compat: 30+ existing callers unaffected; coordinator always passes explicit value | ✓ Good |
| `_next_night_end` uses `<= now` boundary | Exactly-at-night-end is treated as past and rolls to tomorrow — open-interval contract matches `_latest_night_end_before` | ✓ Good |
| Night Only `finish_mode` resolved before `night_hours` computation | Pitfall 5: resolving finish_mode after night_hours silently fed departure-based anchor to Night Only | ✓ Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-09 after Phase 05 (planner-seam)*
