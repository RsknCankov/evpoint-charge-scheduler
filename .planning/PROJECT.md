# EVPoint Charge Scheduler — Charging Mode Reliability

## What This Is

A HACS-installable Home Assistant custom integration that schedules EV charging for an
EVPoint OCPP charger around departure time, target SoC, electricity tariff, and apartment
load. It already ships at v0.7.1 with a full planner, load balancer, learned night window,
and cost-aware slow charging.

**This milestone is a fix-and-harden cycle, not new features.** The selected charging mode
is not reliably honored at runtime: a user who picks `departure` on the dashboard dropdown
can have the integration silently run the default `asap` instead, leaving the car
undercharged. The goal is to make the three finish modes (`asap`, `end_of_night`,
`departure`) behave exactly as specified, fix the session/lock interactions that swallow a
mode change, and lock the behaviour down with the project's first automated test suite.

## Core Value

When the user selects a charging mode, the charger does *exactly that* and reaches the
target SoC by the deadline — every time, and provably (tests), not by luck.

## Requirements

### Validated

<!-- Already shipped in v0.7.1 and confirmed working — inferred from the codebase map. -->

- ✓ Single `DataUpdateCoordinator` running every 30s + on tariff/apartment/SoC state changes — existing
- ✓ Smart planner: energy-needed, night-hours-available, deficit, gentle-charging math — existing
- ✓ Load balancer caps planner target by `total_limit - headroom - apartment_current` — existing
- ✓ Three finish modes implemented in the decision tree (`asap`, `end_of_night`, `departure`) — existing
- ✓ Deficit and safety overrides that bypass finish mode — existing
- ✓ Learned night window from tariff-sensor recorder history — existing
- ✓ Cost-aware slow charging from a learned day/night price ratio — existing
- ✓ Idempotent OCPP `set_charge_rate` push + charger switch on/off control — existing
- ✓ Graceful degradation when any external entity (sensor/switch/OCPP) is missing — existing
- ✓ Full HA entity surface: ~23 sensors, number/datetime/select/button/binary_sensor — existing
- ✓ Config + options flow, single config entry (singleton) — existing
- ✓ Session restore across HA restart via `binary_sensor.session_active` (RestoreEntity) — existing

### Active

<!-- This milestone's scope. Hypotheses until shipped and validated. -->

- [ ] The finish mode chosen on the dashboard dropdown is reliably applied to the coordinator — never silently reverted to the default
- [ ] Charging sessions have sane end conditions even with manually-entered SoC (no SoC sensor) — sessions don't linger active across days
- [ ] Editing a locked input during an active session gives the user clear feedback instead of silently vanishing
- [ ] The currently-active finish mode is always visible and trustworthy on the dashboard (matches what the coordinator is actually running)
- [ ] `departure` mode charges gently and continuously to target by departure time, ignoring tariff — verified end to end
- [ ] `end_of_night` mode reaches target by night-end (with cost-aware spill where configured) — verified end to end
- [ ] `asap` mode bursts at max during night and does not strand the car undercharged when the night window is short — verified end to end
- [ ] A pytest regression suite covers the coordinator decision tree across all modes, the override precedence, and the session/lock lifecycle

### Out of Scope

- New charging capabilities (solar/PV-surplus, multi-vehicle, hourly spot pricing) — this is a reliability milestone, defer features
- Coordinator rewrite or architecture change — fix in place; the single-coordinator design stays
- Supporting OCPP backends beyond the current `set_charge_rate`-style contract — not this milestone
- Brand-icon submission to `home-assistant/brands` — cosmetic, unrelated to mode behaviour
- Changing the manual-SoC model into automatic SoC inference — out of scope unless trivially required by the lifecycle fix

## Context

- **Reproduced failure:** User selected `departure` via the dashboard dropdown and pressed
  Start. The status showed "wait for night tariff" — a state that is *impossible* in
  `departure` mode (it only ever yields `charge_gentle` / `wait_for_start_time`). It then
  charged ~1 hour at max overnight and stopped, ~30 kWh short. This signature exactly
  matches the coordinator running the default `asap` mode instead of the selected
  `departure`.
- **Leading hypothesis (to confirm in diagnosis):** The user runs with **manually-entered
  SoC and no SoC sensor**, so the planner's `energy_needed` never advances during charging.
  A session therefore never auto-ends via the `done` branch (`energy_needed <= 0`) and can
  remain active across days. While a session is active, all inputs are locked, and
  `select.async_select_option` (`select.py:71-73`) *silently reverts* a mode change when
  `session_active` is true — so the `departure` pick was swallowed and `asap` kept running,
  with no feedback to the user.
- **Codebase map exists** at `.planning/codebase/` (committed). The headline concern from
  the map: **no automated test suite** — all planner/tariff/cost math is hand-validated,
  which is why this defect broke invisibly.
- Hand-written `CLAUDE.md` documents the decision tree, session lifecycle, and the
  input-lock design in detail and should be treated as the behavioural spec to fix *toward*.

## Constraints

- **Tech stack**: Python 3 / Home Assistant custom component, distributed via HACS — no change
- **Compatibility**: Single config entry (singleton); must keep restoring sessions across HA restart — don't break the resume path
- **Behaviour spec**: `CLAUDE.md`'s decision tree and lifecycle are the intended contract; fixes align code to spec (or update the spec deliberately, with rationale)
- **Conventions**: Sentence-case UI labels; `DEFAULT_*` in `const.py`; `vol.Optional` selectors; `strings.json` canonical and mirrored byte-for-byte to `translations/en.json`; idempotent service calls
- **Testing**: Project has no test harness yet — establishing one is part of this milestone, so it must run without a live HA instance (mock the coordinator's HA dependencies)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Treat this as a fix-and-harden milestone, not a feature cycle | All three modes failed identically ("doesn't charge when it should") — points at a shared, upstream defect, not new scope | — Pending |
| Include a pytest regression suite in scope | The bug broke invisibly because nothing tested the coordinator math; closing the no-tests gap is the durable fix | — Pending |
| Add a UX guardrail for mode/lock visibility | Silent revert of a dropdown change with no feedback is the proximate cause of the user's lost night | — Pending |
| Diagnose before fixing | The exact root cause (silent revert vs lingering session vs both) needs confirmation from code + logs before committing to a fix | — Pending |

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
*Last updated: 2026-06-04 after initialization*
