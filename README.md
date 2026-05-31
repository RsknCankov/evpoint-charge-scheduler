# EVPoint Charge Scheduler for Home Assistant

<img src="brands/icon.png" alt="EVPoint Charge Scheduler" width="128" align="right">

A Home Assistant custom integration that plans EV charging around your **departure
time, target SoC, electricity tariff, and apartment load**. It tells your OCPP
charger when to start, what current to draw, and when to stop — automatically
prioritising the night tariff and protecting the apartment's main breaker.

It runs as **single charging sessions**: you enter the inputs (battery capacity,
current SoC, target SoC, departure, finish mode), press **Start**, and the
integration drives the charger until the target is reached. Outside of a session
the planner still shows what it *would* do, but never touches the charger.

## Features

- **Departure-aware planning.** Tell it when you're leaving and the SoC you
  want; it computes the energy needed and figures out the schedule.
- **Night-tariff first.** Charging is deferred to the night window whenever
  possible. The day tariff is only used when night-only would miss the target,
  and even then at the minimum rate that lets night-at-max finish the job.
- **Apartment-priority load balancing.** Tracks an apartment current sensor
  and throttles or pauses the charger in real time so total draw stays under
  your configured main-breaker limit (with a safety headroom for spikes).
- **Configurable charger.** Works with any charger that exposes an OCPP service
  to set the current limit, plus a switch entity to start/stop the transaction.
- **Exposes its decisions.** Read-only sensors let you see why charging is
  paused, throttled, or running at any given moment.

## Running a session

1. Open the integration's device card or the dashboard example below.
2. Fill in (or accept the previous values):
   - **Battery capacity** — kWh of your car's pack. Prefilled with the value you used last time.
   - **Current SoC** — what the battery is at right now (or wire up an external SoC sensor in the options flow).
   - **Target SoC** — what you want it charged to.
   - **Departure time** — when you need the car ready.
   - **Finish mode** — `asap`, `end_of_night`, or `departure`.
3. Press **Start charging session**.
4. The session ends automatically when the target SoC is reached, or when you press **Stop charging session**. Current SoC clears at end (since it'll be stale next trip); the other inputs persist for the next session.

> While a session is active the inputs above (battery capacity, current SoC, target SoC, departure time, finish mode) are **locked** — edits are ignored and snap back to the running value. Press **Stop charging session** to change them.

## How it decides

While a session is active, every 30 seconds (and on relevant state changes) it:

1. Computes `energy_needed = (target_soc - current_soc) / 100 * battery_kwh * loss`.
2. Computes how many hours of night tariff exist between now and departure.
3. Computes the deficit: `max(0, energy_needed - night_hours * max_power)`.
4. Picks an action:
   - `asap` + night tariff → `charge_max_now` at max current.
   - `end_of_night` / `departure` → `charge_gentle`: the *slowest* current that
     still finishes by the deadline, spreading the charge across the window
     instead of bursting at max (gentler on the battery and the supply cable).
   - Day tariff with `deficit > 0` → `charge_day_supplement` at the
     minimum rate that, combined with night-at-max, will hit the target.
   - Day tariff with no deficit → `wait_for_night`, charger off.
   - A `deficit` or slack below the safety margin overrides everything back to
     `charge_max_now`.
5. Caps that target by `(total_limit - headroom - apartment_current)` so the
   apartment always wins. If the apartment leaves less than the charger's
   minimum current, charging pauses entirely.
6. Sends the resulting limit / start / stop to your charger via OCPP.

## Installation via HACS

1. In Home Assistant, open HACS → Integrations → ⋮ → **Custom repositories**.
2. Add this repo's URL, category **Integration**, then click **Add**.
3. Search for "EVPoint Charge Scheduler" in HACS and install.
4. Restart Home Assistant.
5. Settings → Devices & Services → **Add Integration** → EVPoint Charge Scheduler.

## Configuration

Every field in the config flow is **optional**. Provide as much or as little
as you have wired up and the integration degrades gracefully:

| Field | If omitted |
| --- | --- |
| **Tariff sensor** | Night vs day is derived from the configured night-start / night-end clock window. When a tariff sensor **is** configured, the integration also learns the real night window from its recorder history (refreshed daily) and uses that for all planning math — see "Learned night window" below. |
| **Apartment current sensor** | Load balancing is disabled — the charger uses the full configured limit. |
| **Car SoC sensor** | A manual `Current SoC` number entity is created for you to keep up to date. |
| **Charger switch entity** | The integration sets the current limit but doesn't start/stop the transaction. When a switch **is** configured, charging is stopped by switching it off, and the integration skips the redundant 0-amp OCPP push. When it's omitted, the session is stopped by pushing a `limit_amps: 0` OCPP profile instead. |
| **OCPP service / devid** | "Advisory mode" — decisions are exposed via sensors but no commands are sent. Both the service name **and** the OCPP `devid` must be set for the integration to push charging profiles; missing either disables that path. Hook your own automations onto `sensor.dynamic_target_current` / `sensor.recommended_action`. |

Other fields (battery capacity, voltage, phases, min/max current, total
limit, headroom, night-tariff times, safety margin) have sensible defaults
but are still worth setting to match your hardware.

All of these can be edited later via the integration's **Configure** button.

## Finish mode

Controls *when* the charge is timed to complete. Default is `asap` (the original
behaviour).

| Mode | When charging finishes | Best for |
| --- | --- | --- |
| `asap` (default) | Whenever it naturally finishes, at **max current**, usually shortly after night tariff begins. Car may sit at high SoC for hours. | Maximum cost saving, maximum robustness to delays. |
| `end_of_night` | Around night-tariff end, charging **gently across the whole night window** at the slowest current that still finishes in time. | Same cost saving as ASAP, but the slowest practical current — gentler on the battery and the supply cable, and finishes near night-end so it sits at high SoC only briefly. |
| `departure` | Around departure time, charging **gently across all the time available**. May cross into day tariff. | A warm battery at departure (useful in winter); gentlest current, at the cost of paying day rates for some energy. |

Internally, `end_of_night` and `departure` charge at a *gentle* current —
`energy_needed ÷ available window` — instead of max. As the window shrinks the
rate is recomputed each cycle, so it self-corrects if charging falls behind. If
the need is tiny it floors at the charger's minimum current and starts later so
it still finishes near the deadline. `sensor.gentle_target_current` shows the
planned slow rate, and `sensor.latest_start_time` shows when it plans to start.
If a deficit or slack below the safety margin appears, all modes fall back to
charging at max — gentle charging is the polite default, not a hard schedule.

The mode is also exposed as a writable `select.<name>_finish_mode` dropdown, so
you can flip it per-trip from the dashboard or an automation without re-opening
the integration's options flow. The select takes precedence over the config
value once it has been set; the config-flow field acts as the initial seed.

## Learned night window

When a **tariff sensor** is configured, the integration doesn't just trust the
night-start / night-end times you typed into the config flow — it learns the
*real* window from the sensor's recorder history. Once a day (and at startup) it
scans the last 14 days of the sensor's state changes, takes the typical
time-of-day of each day→night and night→day transition, and uses those for all
the planning math (`night_hours_available`, the deficit, and the gentle-charging
window). This keeps the schedule accurate even if your configured clock values
are stale, and means you don't have to keep them in sync by hand.

It degrades gracefully: with no tariff sensor, no `recorder`, a sensor excluded
from recording, or fewer than a couple of days of history, it silently falls
back to the configured clock values. `sensor.<name>_night_window_source` tells
you which is currently in use, and `sensor.<name>_learned_night_start` /
`_learned_night_end` show what was detected. The live night/day state always
comes straight from the sensor; the learned window only improves the planner's
look-ahead.

## OCPP service payload

When a current limit needs to be applied, the integration calls the configured
OCPP service with the following payload (built dynamically from the planner's
decision and your config):

```yaml
action: ocpp.set_charge_rate
data:
  devid: <ocpp_devid from config>
  limit_amps: <computed current in amps>
  limit_watts: <amps × voltage × √3 for 3-phase, or × 1 for 1-phase>
  custom_profile:
    chargingProfileId: <charging_profile_id from config, default 8>
    stackLevel: 0
    chargingProfileKind: Relative
    chargingProfilePurpose: ChargePointMaxProfile
    chargingSchedule:
      chargingRateUnit: A
      chargingSchedulePeriod:
        - startPeriod: 0
          limit: <amps>
```

The `chargingProfileId` is reused for every update so the charger replaces the
existing profile rather than stacking new ones. If you have other automations
also installing OCPP profiles, pick a `chargingProfileId` that doesn't collide
with theirs.

## Entities created

**Writable inputs (you set these per trip — last value is prefilled):**
- `number.<name>_battery_capacity` (kWh)
- `number.<name>_current_soc` (only when no external SoC sensor is configured)
- `number.<name>_target_soc`
- `datetime.<name>_departure_time`
- `select.<name>_finish_mode` — `asap`, `end_of_night`, or `departure`

**Session controls:**
- `button.<name>_start_charging_session`
- `button.<name>_stop_charging_session`
- `binary_sensor.<name>_charging_session_active`

**Read-only sensors:**
- `sensor.<name>_energy_needed` (kWh)
- `sensor.<name>_hours_to_departure` (h)
- `sensor.<name>_night_hours_available` (h)
- `sensor.<name>_day_hours_available` (h)
- `sensor.<name>_max_charge_power` (kW)
- `sensor.<name>_slack_hours` (h)
- `sensor.<name>_day_energy_deficit` (kWh)
- `sensor.<name>_day_charging_current` (A)
- `sensor.<name>_available_current` (A) — apartment headroom for the EV
- `sensor.<name>_dynamic_target_current` (A) — actual current pushed to the charger
- `sensor.<name>_recommended_action` — one of: `idle` (no active session),
  `done`, `too_late`, `charge_max_now`, `charge_gentle`,
  `charge_day_supplement`, `wait_for_night`, `wait_for_start_time`
- `sensor.<name>_gentle_target_current` (A) — the planned slow charging rate
  under `end_of_night` / `departure`; `unavailable` in `asap` mode
- `sensor.<name>_throttle_reason` — `unrestricted`, `smart_charging_pause`,
  `apartment_load_too_high`, `throttled_by_apartment`
- `sensor.<name>_plan_status` — `ok`, `already_at_target`, `too_late`,
  `insufficient_time`
- `sensor.<name>_control_mode` — `active` (both OCPP service and switch wired),
  `current_only`, `switch_only`, or `advisory` (nothing wired — integration
  only computes, your own automations act)
- `sensor.<name>_tariff_source` — `sensor` if a tariff entity is configured,
  `schedule` if night/day is being derived from the configured clock window
- `sensor.<name>_learned_night_start` / `sensor.<name>_learned_night_end` —
  the night window learned from the tariff sensor's history (`HH:MM`), or
  `unavailable` until enough history exists
- `sensor.<name>_night_window_source` — `learned` when the planner is using the
  history-derived window, `configured` when falling back to the clock values
- `sensor.<name>_finish_mode` — the currently active finish mode (`asap`,
  `end_of_night`, or `departure`)
- `sensor.<name>_latest_start_time` — when charging is planned to begin under
  the active finish mode. `unavailable` when mode is `asap` (charging starts
  immediately on entering the night window)

## Lovelace example

The entities are listed in the input order: capacity → current SoC → target →
departure → finish mode → Start.

```yaml
type: entities
title: EV Charging
entities:
  - entity: number.evpoint_charge_scheduler_battery_capacity
  - entity: number.evpoint_charge_scheduler_current_soc
  - entity: number.evpoint_charge_scheduler_target_soc
  - entity: datetime.evpoint_charge_scheduler_departure_time
  - entity: select.evpoint_charge_scheduler_finish_mode
  - entity: button.evpoint_charge_scheduler_start_charging_session
  - entity: button.evpoint_charge_scheduler_stop_charging_session
  - type: divider
  - entity: binary_sensor.evpoint_charge_scheduler_charging_session_active
  - entity: sensor.evpoint_charge_scheduler_recommended_action
  - entity: sensor.evpoint_charge_scheduler_dynamic_target_current
  - entity: sensor.evpoint_charge_scheduler_throttle_reason
  - entity: sensor.evpoint_charge_scheduler_energy_needed
  - entity: sensor.evpoint_charge_scheduler_night_hours_available
  - entity: sensor.evpoint_charge_scheduler_plan_status
```

## Assumptions worth verifying

- **The apartment current sensor excludes the EV charger.** If your sensor is
  the main meter (and so already includes EV draw), the load-balancing math
  needs adjusting — open an issue or fork.
- **The total current limit is per-phase.** On a 3-phase 32 A charger drawing
  32 A on each phase, the apartment-priority math compares apartment amps on
  the worst phase to a per-phase ceiling. If your apartment sensor reports
  the sum of phases instead, change the limit accordingly.
- **The tariff sensor's "night" value matches the configured string.** Some
  utilities use `night`, others use `off_peak`, `low`, or a localised string.
  Set this exactly to what your sensor reports.

## License

MIT

## Brand icon

The icon and logo files live in [`brands/`](brands/). To make the icon show
up in the Home Assistant frontend (Settings → Devices & Services and the HACS
listing), submit a pull request to
[home-assistant/brands](https://github.com/home-assistant/brands) adding the
PNG files under:

```
custom_integrations/evpoint_charge_scheduler/icon.png
custom_integrations/evpoint_charge_scheduler/icon@2x.png
custom_integrations/evpoint_charge_scheduler/logo.png        (optional)
custom_integrations/evpoint_charge_scheduler/logo@2x.png     (optional)
```

The source `icon.svg` is included if you want to tweak the design before
submitting.
