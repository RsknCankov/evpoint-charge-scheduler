"""Phase-1 baseline regression test — the milestone through-line guard.

This is the project's first failing regression test (D-09). It reproduces the
user's "lost night" bug: with a (lingering) session active, picking ``departure``
on the dashboard silently reverts to the running mode (``asap``), so the
coordinator reports ``wait_for_night`` — a state that is *impossible* for
``departure`` mode (the ``departure`` branch can only emit ``charge_gentle`` or
``wait_for_start_time``).

Contract for the whole milestone:

* It MUST be **RED** against today's unmodified ``custom_components/`` code, for
  the RIGHT reason — the silent mode-revert in ``select.async_select_option``
  (``select.py:71-73``) leaves ``coordinator.finish_mode`` at ``"asap"``.
* It MUST stay **RED** after the Phase-2 logic-preserving seam lift (the select
  guard is untouched by the seam extraction), proving the lift changed no
  behaviour.
* It turns **GREEN** in Phase 3 when the locked-select revert is fixed (the pick
  is honoured / raises-and-allows).

Encoding note (load-bearing, see 01-RESEARCH.md §Open Questions RESOLVED): the
pick MUST flow through the **real** ``select`` entity under ``session_active=True``.
A test that hard-codes ``coordinator.finish_mode = "departure"`` bypasses the
revert and is a FALSE-GREEN baseline — the ``departure`` branch already cannot
emit ``wait_for_night``, so such a test passes today and proves nothing.

No ``@pytest.mark.asyncio`` decorator: ``asyncio_mode = auto`` (pyproject.toml,
plan 01-01) runs every ``async def test_*`` automatically.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import DOMAIN


@freeze_time("2026-06-04T14:00:00+03:00")  # tz-aware, DAY tariff (07:00 < 14:00 < 23:00)
async def test_departure_mode_never_waits_for_night(hass: HomeAssistant) -> None:
    """Selecting ``departure`` during an active session must be honoured.

    RED today: the locked-session guard in ``select.async_select_option``
    silently swallows the pick, so ``coordinator.finish_mode`` stays ``"asap"``
    and the day-tariff ``asap`` branch emits the impossible ``wait_for_night``
    for a ``departure``-intent session.
    """
    # Pin the timezone so the no-sensor night-window derivation is reproducible
    # under the frozen clock (the integration assumes tz-aware datetimes).
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))

    # Minimal config the decision math reads. No tariff sensor and no
    # charger/OCPP selectors -> graceful degradation: advisory mode and a
    # schedule-derived is_night_now (DAY at 14:00).
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
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
        },
    )
    entry.add_to_hass(hass)

    # Full integration setup so the REAL select entity exists and the
    # enable_custom_integrations autouse fixture (conftest, plan 01-01) applies.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Put the coordinator into the lingering-session state with a manual-SoC
    # need that produces NO deficit and AMPLE slack, so the pure finish-mode
    # path runs (no deficit/safety override masks the result):
    #   energy_needed = (80-40)/100 * 60 * 1.1 = 26.4 kWh
    #   max_kw        = sqrt(3) * 230 * 16 / 1000 ~= 6.37 kW
    #   hours_needed_at_max ~= 4.14 h; departure 18 h out -> slack ~= 13.9 h
    #     (>> safety_margin 0.5 h, so no safety override)
    #   night_hours over 18 h spans two 23:00->07:00 windows -> ~13 h at 6.37 kW
    #     >> 26.4 kWh, so deficit_kwh == 0 (no deficit override)
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    coordinator.departure_time = dt_util.now() + timedelta(hours=18)

    # Locate the REAL finish-mode select entity and drive its option-select
    # through the service registry so the production
    # FinishModeSelect.async_select_option path runs under session_active=True.
    ent_reg = er.async_get(hass)
    select_entity_id = ent_reg.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_finish_mode"
    )
    assert select_entity_id is not None, "finish-mode select entity was not created"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_entity_id, "option": "departure"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Assertion 1 — the RIGHT-reason failure: the user's pick must reach the
    # coordinator. FAILS today: the silent revert (select.py:71-73) returns
    # early, so finish_mode stays "asap".
    assert coordinator.finish_mode == "departure", (
        "user picked 'departure' through the real select under an active "
        f"session, but coordinator.finish_mode == {coordinator.finish_mode!r} "
        "(the locked-session guard silently reverted the pick)"
    )

    # Run one real coordinator cycle and read the resulting action.
    data = await coordinator._async_update_data()

    # Assertion 2 — the product invariant (MODE-02): departure mode can NEVER
    # report wait_for_night. FAILS today: with finish_mode reverted to "asap"
    # on a DAY-tariff cycle, the asap branch emits wait_for_night.
    assert data["recommended_action"] != "wait_for_night", (
        "departure-intent session produced the impossible "
        f"recommended_action={data['recommended_action']!r} "
        f"(finish_mode={data['finish_mode']!r}, "
        f"deficit={data['day_energy_deficit']}, slack={data['slack_hours']}, "
        f"is_night_now={data['is_night_now']})"
    )
