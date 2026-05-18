# EVPoint Charge Scheduler for Home Assistant

A Home Assistant custom integration that plans EV charging around your **departure
time, target SoC, electricity tariff, and apartment load**. It tells your OCPP
charger when to start, what current to draw, and when to stop — automatically
prioritising the night tariff and protecting the apartment's main breaker.

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

## How it decides

Every 30 seconds (and on relevant state changes) it:

1. Computes `energy_needed = (target_soc - current_soc) / 100 * battery_kwh * loss`.
2. Computes how many hours of night tariff exist between now and departure.
3. Computes the deficit: `max(0, energy_needed - night_hours * max_power)`.
4. Picks an action:
   - Currently night tariff → `charge_max_now` at max current.
   - Currently day tariff with `deficit > 0` → `charge_day_supplement` at the
     minimum rate that, combined with night-at-max, will hit the target.
   - Currently day tariff with no deficit → `wait_for_night`, charger off.
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
| **Tariff sensor** | Night vs day is derived from the configured night-start / night-end clock window. |
| **Apartment current sensor** | Load balancing is disabled — the charger uses the full configured limit. |
| **Car SoC sensor** | A manual `Current SoC` number entity is created for you to keep up to date. |
| **Charger switch entity** | The integration sets the current limit but doesn't start/stop the transaction. |
| **OCPP service / devid** | "Advisory mode" — decisions are exposed via sensors but no commands are sent. Both the service name **and** the OCPP `devid` must be set for the integration to push charging profiles; missing either disables that path. Hook your own automations onto `sensor.dynamic_target_current` / `sensor.recommended_action`. |

Other fields (battery capacity, voltage, phases, min/max current, total
limit, headroom, night-tariff times, safety margin) have sensible defaults
but are still worth setting to match your hardware.

All of these can be edited later via the integration's **Configure** button.

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

**Writable inputs (you set these per trip):**
- `number.<name>_target_soc`
- `number.<name>_current_soc` (only when no external SoC sensor is configured)
- `datetime.<name>_departure_time`
- `switch.<name>_smart_charging_enabled`

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
- `sensor.<name>_recommended_action` — one of: `disabled`, `done`, `too_late`,
  `charge_max_now`, `charge_day_supplement`, `wait_for_night`
- `sensor.<name>_throttle_reason` — `unrestricted`, `smart_charging_pause`,
  `apartment_load_too_high`, `throttled_by_apartment`
- `sensor.<name>_plan_status` — `ok`, `already_at_target`, `too_late`,
  `insufficient_time`
- `sensor.<name>_control_mode` — `active` (both OCPP service and switch wired),
  `current_only`, `switch_only`, or `advisory` (nothing wired — integration
  only computes, your own automations act)
- `sensor.<name>_tariff_source` — `sensor` if a tariff entity is configured,
  `schedule` if night/day is being derived from the configured clock window

## Lovelace example

```yaml
type: entities
title: EV Charging
entities:
  - entity: switch.evpoint_charge_scheduler_smart_charging_enabled
  - entity: datetime.evpoint_charge_scheduler_departure_time
  - entity: number.evpoint_charge_scheduler_target_soc
  - entity: number.evpoint_charge_scheduler_current_soc
  - type: divider
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
