"""Load-balancer coverage — the pure throttle ladder + both charger stop paths.

Closes D-05, the single largest coverage hole in the suite: the pure
``load_balancer.balance()`` had ZERO tests. Two distinct sub-concerns, each with
its own established analog:

* **Sub-concern A — pure ``balance()`` matrix (no hass).** Mirrors
  ``test_finish_mode_seam.py``: a keyword-only ``_li(...)`` builder over a pure
  seam function with stacked ``@pytest.mark.parametrize``. Pins all four
  ``throttle_reason`` outcomes (``unrestricted`` / ``throttled_by_apartment`` /
  ``apartment_load_too_high`` / ``smart_charging_pause``), the
  ``available_current = max(0, total_limit - headroom - int(apartment_current))``
  expression (the ``int()`` truncation and the ``max(0, ...)`` floor are
  load-bearing for parity), and the **safety invariant** — the dynamic output
  never exceeds the configured per-phase amp cap, so a spoofed apartment read can
  only *reduce* the charge cap (T-04-04: apartment always wins).

* **Sub-concern B — both charger stop paths on the real OCPP payload (needs
  hass).** Mirrors ``test_session_end.py``'s ``MockConfigEntry`` + ``freeze_time``
  + ``Europe/Sofia`` setup. Drives ``coordinator._apply_to_charger`` directly
  under a frozen clock and asserts, via ``async_mock_service`` spies, that:
    - a charger-switch present drop-to-0 SUPPRESSES the 0-amp OCPP push
      (``stop_via_switch``), calls switch ``turn_off``, and resets
      ``_last_applied_current`` to 0;
    - ``current_only`` (no switch) PUSHES a profile with ``limit: 0``;
    - after a drop-to-0, the next non-zero target RE-PUSHES the profile
      (``_last_applied_current == 0 != dynamic_current``).
  The captured non-zero payload is asserted against the OCPP shape (the
  ``chargingProfileId`` / ``chargingProfilePurpose`` / schedule-period limit).

Sub-concern A has no ``@pytest.mark.asyncio`` and no hass — plain sync unit tests
importing only ``.const`` / ``.models`` / ``load_balancer``.
"""

from __future__ import annotations

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.evpoint_charge_scheduler.const import (
    DOMAIN,
    THROTTLE_APARTMENT_HIGH,
    THROTTLE_BY_APARTMENT,
    THROTTLE_SMART_PAUSE,
    THROTTLE_UNRESTRICTED,
)
from custom_components.evpoint_charge_scheduler.load_balancer import balance
from custom_components.evpoint_charge_scheduler.models import LoadInputs, Output


# =============================================================================
# Sub-concern A — pure balance() throttle-ladder + safety-invariant matrix
# =============================================================================


def _li(
    *,
    planned_current: int = 16,
    total_limit: int = 32,
    headroom: int = 0,
    apartment_current: float = 0.0,
    min_a: int = 6,
) -> LoadInputs:
    """Keyword-only LoadInputs builder — an unrestricted 16 A pass-through unless
    overridden (32 A cap, no headroom, no apartment load)."""
    return LoadInputs(
        planned_current=planned_current,
        total_limit=total_limit,
        headroom=headroom,
        apartment_current=apartment_current,
        min_a=min_a,
    )


# --- The four throttle_reason outcomes ---------------------------------------


def test_throttle_smart_pause_when_planner_pauses() -> None:
    """planned_current <= 0 -> dynamic 0, smart_charging_pause (the planner's
    own pause wins before any apartment math)."""
    out = balance(_li(planned_current=0))
    assert out.dynamic_current == 0
    assert out.throttle_reason == THROTTLE_SMART_PAUSE


def test_throttle_apartment_high_when_no_room_for_min() -> None:
    """available < min_a -> dynamic 0, apartment_load_too_high. 32 - 0 - 28 = 4,
    below the 6 A floor."""
    out = balance(_li(planned_current=16, apartment_current=28.0, min_a=6))
    assert out.available_current == 4
    assert out.dynamic_current == 0
    assert out.throttle_reason == THROTTLE_APARTMENT_HIGH


def test_throttle_by_apartment_caps_to_available() -> None:
    """min_a <= available < planned -> dynamic == available, throttled_by_apartment.
    32 - 0 - 22 = 10, between 6 (min) and 16 (planned)."""
    out = balance(_li(planned_current=16, apartment_current=22.0, min_a=6))
    assert out.available_current == 10
    assert out.dynamic_current == 10
    assert out.throttle_reason == THROTTLE_BY_APARTMENT


def test_throttle_unrestricted_when_room_to_spare() -> None:
    """available >= planned -> dynamic == planned, unrestricted (the no-apartment
    pass-through)."""
    out = balance(_li(planned_current=16, apartment_current=0.0))
    assert out.available_current == 32
    assert out.dynamic_current == 16
    assert out.throttle_reason == THROTTLE_UNRESTRICTED


@pytest.mark.parametrize(
    "apartment_current, expected_dynamic, expected_reason",
    [
        (0.0, 16, THROTTLE_UNRESTRICTED),  # full room -> pass-through
        (22.0, 10, THROTTLE_BY_APARTMENT),  # squeezed -> capped to available
        (28.0, 0, THROTTLE_APARTMENT_HIGH),  # < min_a -> stop
    ],
)
def test_throttle_ladder_matrix(
    apartment_current: float, expected_dynamic: int, expected_reason: str
) -> None:
    """The throttle ladder across rising apartment load (planner active)."""
    out = balance(_li(planned_current=16, apartment_current=apartment_current))
    assert out.dynamic_current == expected_dynamic
    assert out.throttle_reason == expected_reason


# --- available_current: int() truncation + max(0, ...) floor -----------------


def test_available_current_subtracts_headroom_and_apartment() -> None:
    """available_current = total_limit - headroom - int(apartment_current)."""
    out = balance(_li(total_limit=32, headroom=4, apartment_current=10.0))
    assert out.available_current == 18  # 32 - 4 - 10


def test_available_current_truncates_fractional_apartment() -> None:
    """int() TRUNCATES (floors toward zero) a fractional apartment read — 22.9 A
    becomes 22 A, so available is 10, not 9. Pins the int() truncation."""
    out = balance(_li(total_limit=32, headroom=0, apartment_current=22.9, min_a=6))
    assert out.available_current == 10  # 32 - 0 - int(22.9)=22 -> 10
    assert out.dynamic_current == 10
    assert out.throttle_reason == THROTTLE_BY_APARTMENT


def test_available_current_floors_at_zero_when_over_limit() -> None:
    """An apartment draw above the limit can't drive available negative —
    max(0, ...) floors it at 0. Pins the max(0, ...) floor."""
    out = balance(_li(total_limit=32, headroom=4, apartment_current=40.0, min_a=6))
    assert out.available_current == 0  # max(0, 32 - 4 - 40) == max(0, -12)
    assert out.dynamic_current == 0
    assert out.throttle_reason == THROTTLE_APARTMENT_HIGH


# --- Safety invariant: dynamic never exceeds the per-phase cap (D-05/T-04-04) -


@pytest.mark.parametrize("planned_current", [0, 6, 16, 32, 64])
@pytest.mark.parametrize("total_limit", [16, 32])
@pytest.mark.parametrize("headroom", [0, 2, 6])
@pytest.mark.parametrize("apartment_current", [0.0, 5.5, 15.0, 30.0, 100.0])
def test_safety_invariant_dynamic_never_exceeds_per_phase_cap(
    planned_current: int,
    total_limit: int,
    headroom: int,
    apartment_current: float,
) -> None:
    """SAFETY INVARIANT (D-05, threat T-04-04): across the whole matrix the
    dynamic output never exceeds the configured per-phase cap (total_limit -
    headroom). A spoofed/high apartment read can only REDUCE the charge cap,
    never raise it — apartment always wins."""
    out = balance(
        _li(
            planned_current=planned_current,
            total_limit=total_limit,
            headroom=headroom,
            apartment_current=apartment_current,
        )
    )
    assert out.dynamic_current <= total_limit - headroom
    # And it is never negative either.
    assert out.dynamic_current >= 0


# =============================================================================
# Sub-concern B — both charger stop paths on the real OCPP payload (needs hass)
# =============================================================================

OCPP_DOMAIN = "ocpp"
OCPP_SERVICE = "set_charge_rate"
OCPP_SET_RATE = f"{OCPP_DOMAIN}.{OCPP_SERVICE}"
DEVID = "EVPoint-1"
SWITCH_ENTITY = "switch.evpoint_charger"
PROFILE_ID = 8


def _make_entry(*, with_switch: bool) -> MockConfigEntry:
    """A MockConfigEntry wired with the OCPP set-rate service + devid. The
    charger switch is added conditionally (mirrors the with_power_sensor idiom in
    test_session_end.py) so the same builder drives both stop paths:
    switch present (suppress 0-amp push) vs current_only (push limit:0)."""
    data = {
        "voltage": 230,
        "phases": 3,
        "min_current": 6,
        "max_current": 16,
        "total_current_limit": 32,
        "safety_headroom": 0,
        "night_start": "23:00",
        "night_end": "07:00",
        "battery_capacity": 60,
        "charging_loss": 1.1,
        "safety_margin_hours": 0.5,
        "ocpp_set_rate_service": OCPP_SET_RATE,
        "ocpp_devid": DEVID,
        "charging_profile_id": PROFILE_ID,
    }
    if with_switch:
        data["charger_switch"] = SWITCH_ENTITY
    return MockConfigEntry(domain=DOMAIN, data=data)


async def _setup(hass: HomeAssistant, *, with_switch: bool):
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))
    entry = _make_entry(with_switch=with_switch)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


def _ocpp_calls(calls):
    """Return the OCPP set_charge_rate calls captured by the spy."""
    return [c for c in calls if c.domain == OCPP_DOMAIN and c.service == OCPP_SERVICE]


def _schedule_limit(call):
    """Pull the chargingSchedulePeriod[0].limit out of a captured OCPP call."""
    return call.data["custom_profile"]["chargingSchedule"][
        "chargingSchedulePeriod"
    ][0]["limit"]


async def test_switch_present_drop_to_zero_suppresses_ocpp_push(
    hass: HomeAssistant,
) -> None:
    """Charger-switch present: a drop to 0 A is stopped by the switch turn_off,
    so NO 0-amp OCPP push goes out, AND _last_applied_current is reset to 0 so
    the next non-zero target re-pushes the profile on resume."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass, with_switch=True)
        ocpp_calls = async_mock_service(hass, OCPP_DOMAIN, OCPP_SERVICE)
        switch_calls = async_mock_service(hass, "switch", "turn_off")

        # Prime a running session at 16 A so the drop-to-0 is a real transition.
        await coordinator._apply_to_charger(16)
        await hass.async_block_till_done()
        assert coordinator._last_applied_current == 16

        # Drop to 0 — the switch stops; the 0-amp push must be suppressed.
        await coordinator._apply_to_charger(0)
        await hass.async_block_till_done()

        # No OCPP call carried a 0-amp limit (stop_via_switch suppressed it).
        zero_pushes = [c for c in _ocpp_calls(ocpp_calls) if _schedule_limit(c) == 0]
        assert zero_pushes == []
        # The switch turn_off DID fire.
        assert len(switch_calls) == 1
        assert switch_calls[0].data["entity_id"] == SWITCH_ENTITY
        # And the tracker reset to 0 (so resume re-pushes).
        assert coordinator._last_applied_current == 0


async def test_current_only_drop_to_zero_pushes_limit_zero(
    hass: HomeAssistant,
) -> None:
    """current_only (no switch): a drop to 0 A PUSHES an OCPP profile with
    limit:0 so the charger stops drawing instead of holding the last cap."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass, with_switch=False)
        ocpp_calls = async_mock_service(hass, OCPP_DOMAIN, OCPP_SERVICE)

        await coordinator._apply_to_charger(16)
        await hass.async_block_till_done()
        assert coordinator._last_applied_current == 16

        await coordinator._apply_to_charger(0)
        await hass.async_block_till_done()

        zero_pushes = [c for c in _ocpp_calls(ocpp_calls) if _schedule_limit(c) == 0]
        assert len(zero_pushes) == 1
        push = zero_pushes[0]
        assert push.data["limit_amps"] == 0
        assert coordinator._last_applied_current == 0


async def test_resume_after_drop_to_zero_repushes_profile(
    hass: HomeAssistant,
) -> None:
    """Resume re-push: after a drop-to-0 (switch path, _last_applied_current==0),
    the next non-zero target re-pushes a fresh OCPP profile because
    0 != dynamic_current. The re-pushed payload matches the OCPP shape."""
    with freeze_time("2026-06-04T23:30:00+03:00"):
        coordinator = await _setup(hass, with_switch=True)
        async_mock_service(hass, "switch", "turn_off")
        async_mock_service(hass, "switch", "turn_on")
        # Spy on OCPP from the start so the priming push at 16 A actually lands
        # (and sets _last_applied_current=16); otherwise service_not_found is
        # caught and the tracker never advances, breaking the drop-to-0 setup.
        ocpp_calls = async_mock_service(hass, OCPP_DOMAIN, OCPP_SERVICE)

        # Run at 16, then drop to 0 (switch stop, tracker -> 0).
        await coordinator._apply_to_charger(16)
        await hass.async_block_till_done()
        assert coordinator._last_applied_current == 16
        await coordinator._apply_to_charger(0)
        await hass.async_block_till_done()
        assert coordinator._last_applied_current == 0

        # Drive a fresh non-zero target — the resume re-push. Only the new push
        # past this point is the one under test.
        ocpp_calls.clear()
        await coordinator._apply_to_charger(12)
        await hass.async_block_till_done()

        pushes = _ocpp_calls(ocpp_calls)
        assert len(pushes) == 1
        push = pushes[0]
        # Fresh non-zero push went out (resume re-push).
        assert push.data["limit_amps"] == 12
        assert _schedule_limit(push) == 12
        assert coordinator._last_applied_current == 12
        # And the payload matches the OCPP profile shape.
        profile = push.data["custom_profile"]
        assert profile["chargingProfileId"] == PROFILE_ID
        assert profile["chargingProfilePurpose"] == "ChargePointMaxProfile"
        assert profile["chargingProfileKind"] == "Relative"
        assert push.data["devid"] == DEVID
