"""Clock-boundary matrix over the coordinator-shell time math (D-07 / TEST-03).

Two layers, both deterministic (D-09): a frozen/injected clock pinned to
Europe/Sofia (relying on the 04-01 conftest tz-reset fixture so the Sofia pin
never leaks under pytest-randomly), and no live-HA network.

1. The pure ``@staticmethod`` helpers on ``SmartEVChargingCoordinator`` are
   called directly with crafted tz-aware datetimes — no hass needed:

   * ``_night_hours_between`` — the 5-minute walk, the ``wraps = ns >= ne``
     midnight-cross branch, and the ``end <= start -> 0.0`` zero-length guard.
   * ``_latest_night_end_before`` — returns only a future time in ``(now, target]``
     or ``None`` (a candidate that rolls back a day past ``now``, or a candidate
     ``<= now``, both yield ``None``).
   * ``_is_in_night_window`` — the at-departure boundary inclusivity contract.

2. EU spring-forward (last Sun March, 03:00->04:00) and fall-back (last Sun Oct,
   04:00->03:00) drive a FULL coordinator under ``freeze_time`` +
   ``set_default_time_zone("Europe/Sofia")``; the window length is read off
   ``coordinator.data["night_hours_available"]`` and ``gentle_start`` off
   ``coordinator.data["latest_start_time"]`` — which MUST stay tz-aware per the
   CLAUDE.md ``SensorDeviceClass.TIMESTAMP`` gotcha.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import (
    DOMAIN,
    FINISH_MODE_DEPARTURE,
    FINISH_MODE_END_OF_NIGHT,
)
from custom_components.evpoint_charge_scheduler.coordinator import (
    SmartEVChargingCoordinator,
)

SOFIA = ZoneInfo("Europe/Sofia")
NIGHT_START = time(23, 0)
NIGHT_END = time(7, 0)


# --- Layer 1: pure @staticmethod clock math ----------------------------------


def test_night_hours_between_midnight_wrap_counts_full_window() -> None:
    """A 23:00->07:00 window walked across 00:00 counts the wrap branch (8h)."""
    start = datetime(2026, 3, 10, 23, 0, tzinfo=SOFIA)
    end = datetime(2026, 3, 11, 7, 0, tzinfo=SOFIA)
    hours = SmartEVChargingCoordinator._night_hours_between(
        start, end, NIGHT_START, NIGHT_END
    )
    # 23:00->07:00 = 8h; the 5-min walk samples [start, end) so ~8h of night.
    assert hours > 7.5
    assert hours == pytest.approx(8.0, abs=0.1)


def test_night_hours_between_partial_wrap_only_counts_night_portion() -> None:
    """A window straddling night-end counts only the in-window minutes."""
    # 05:00 (in night) -> 09:00 (day). Only 05:00->07:00 = 2h is night.
    start = datetime(2026, 3, 11, 5, 0, tzinfo=SOFIA)
    end = datetime(2026, 3, 11, 9, 0, tzinfo=SOFIA)
    hours = SmartEVChargingCoordinator._night_hours_between(
        start, end, NIGHT_START, NIGHT_END
    )
    assert hours == pytest.approx(2.0, abs=0.1)


def test_night_hours_between_zero_length_returns_zero() -> None:
    """end <= start -> exactly 0.0 (the zero-length guard)."""
    start = datetime(2026, 3, 11, 23, 0, tzinfo=SOFIA)
    assert (
        SmartEVChargingCoordinator._night_hours_between(
            start, start, NIGHT_START, NIGHT_END
        )
        == 0.0
    )
    earlier_end = start - timedelta(hours=1)
    assert (
        SmartEVChargingCoordinator._night_hours_between(
            start, earlier_end, NIGHT_START, NIGHT_END
        )
        == 0.0
    )


def test_night_hours_between_end_none_returns_zero() -> None:
    """A None end (no target_finish) returns 0.0, never raises."""
    start = datetime(2026, 3, 11, 23, 0, tzinfo=SOFIA)
    assert (
        SmartEVChargingCoordinator._night_hours_between(
            start, None, NIGHT_START, NIGHT_END
        )
        == 0.0
    )


def test_night_hours_between_non_wrapping_window() -> None:
    """A daytime window (ns < ne) uses the non-wrap branch."""
    # Night window 01:00->05:00 (does NOT wrap). Walk 00:00->06:00 -> 4h night.
    start = datetime(2026, 3, 11, 0, 0, tzinfo=SOFIA)
    end = datetime(2026, 3, 11, 6, 0, tzinfo=SOFIA)
    hours = SmartEVChargingCoordinator._night_hours_between(
        start, end, time(1, 0), time(5, 0)
    )
    assert hours == pytest.approx(4.0, abs=0.1)


def test_latest_night_end_before_returns_future_candidate() -> None:
    """A valid night_end in (now, target] is returned unchanged."""
    now = datetime(2026, 3, 10, 23, 0, tzinfo=SOFIA)
    target = datetime(2026, 3, 11, 18, 0, tzinfo=SOFIA)  # departure 18:00
    got = SmartEVChargingCoordinator._latest_night_end_before(
        target, NIGHT_END, now
    )
    assert got == datetime(2026, 3, 11, 7, 0, tzinfo=SOFIA)


def test_latest_night_end_before_candidate_at_or_before_now_returns_none() -> None:
    """A candidate that rolls back a day to <= now yields None."""
    # target 06:00, night_end 07:00 -> candidate 07:00 > target so it rolls back
    # a day to the previous 07:00, which is <= now -> None.
    now = datetime(2026, 3, 11, 5, 0, tzinfo=SOFIA)
    target = datetime(2026, 3, 11, 6, 0, tzinfo=SOFIA)
    assert (
        SmartEVChargingCoordinator._latest_night_end_before(
            target, NIGHT_END, now
        )
        is None
    )


def test_latest_night_end_before_candidate_exactly_now_returns_none() -> None:
    """Boundary: candidate == now is NOT in the open interval (now, target]."""
    now = datetime(2026, 3, 11, 7, 0, tzinfo=SOFIA)
    target = datetime(2026, 3, 11, 7, 0, tzinfo=SOFIA)
    assert (
        SmartEVChargingCoordinator._latest_night_end_before(
            target, NIGHT_END, now
        )
        is None
    )


def test_is_in_night_window_at_departure_boundary_inclusivity() -> None:
    """The window is [start, end): start inclusive, end exclusive."""
    # Wrapping window 23:00->07:00.
    at_start = datetime(2026, 3, 11, 23, 0, tzinfo=SOFIA)
    at_end = datetime(2026, 3, 11, 7, 0, tzinfo=SOFIA)
    just_before_end = datetime(2026, 3, 11, 6, 59, tzinfo=SOFIA)
    assert (
        SmartEVChargingCoordinator._is_in_night_window(
            at_start, NIGHT_START, NIGHT_END
        )
        is True
    )
    # end is exclusive: exactly 07:00 is day.
    assert (
        SmartEVChargingCoordinator._is_in_night_window(
            at_end, NIGHT_START, NIGHT_END
        )
        is False
    )
    assert (
        SmartEVChargingCoordinator._is_in_night_window(
            just_before_end, NIGHT_START, NIGHT_END
        )
        is True
    )


# --- _next_night_end forward-looking boundary tests (D-08) ------------------


def test_next_night_end_before_tonight_returns_today() -> None:
    """now=03:00, night_end=07:00 -> today's 07:00 (future)."""
    now = datetime(2026, 3, 11, 3, 0, tzinfo=SOFIA)
    result = SmartEVChargingCoordinator._next_night_end(now, NIGHT_END)
    assert result == datetime(2026, 3, 11, 7, 0, tzinfo=SOFIA)
    assert result > now


def test_next_night_end_after_tonight_returns_tomorrow() -> None:
    """now=08:00, night_end=07:00 -> tomorrow's 07:00."""
    now = datetime(2026, 3, 11, 8, 0, tzinfo=SOFIA)
    result = SmartEVChargingCoordinator._next_night_end(now, NIGHT_END)
    assert result == datetime(2026, 3, 12, 7, 0, tzinfo=SOFIA)
    assert result > now


def test_next_night_end_exactly_at_night_end_returns_tomorrow() -> None:
    """now=07:00:00, night_end=07:00 -> candidate == now -> +1 day (D-08 boundary)."""
    now = datetime(2026, 3, 11, 7, 0, 0, tzinfo=SOFIA)
    result = SmartEVChargingCoordinator._next_night_end(now, NIGHT_END)
    assert result == datetime(2026, 3, 12, 7, 0, tzinfo=SOFIA)
    assert result > now


def test_next_night_end_always_strictly_future() -> None:
    """Result is always strictly > now, regardless of time-of-day."""
    for hour in [0, 1, 6, 7, 12, 22, 23]:
        now = datetime(2026, 6, 9, hour, 0, tzinfo=SOFIA)
        result = SmartEVChargingCoordinator._next_night_end(now, NIGHT_END)
        assert result > now, f"Not strictly future at hour={hour}: result={result}"


def test_next_night_end_preserves_tzinfo() -> None:
    """Result carries the same tzinfo as now (tz-aware invariant)."""
    now = datetime(2026, 3, 11, 3, 0, tzinfo=SOFIA)
    result = SmartEVChargingCoordinator._next_night_end(now, NIGHT_END)
    assert result.tzinfo is not None
    assert result.tzinfo == now.tzinfo


# --- Layer 2: full coordinator under DST transitions -------------------------


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
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


async def _setup(hass: HomeAssistant) -> SmartEVChargingCoordinator:
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator: SmartEVChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.session_active = True
    coordinator.target_soc = 80.0
    coordinator.current_soc = 40.0
    return coordinator


# EU 2026: spring-forward last Sun March = 29 Mar (02:59->04:00, no 03:xx);
#          fall-back  last Sun October = 25 Oct (03:59->03:00, 03:xx twice).
# The walk samples night by WALL-CLOCK time-of-day from now (23:30) to night-end
# (07:00) = 7.5 wall-clock night-hours. The key DST guarantee is that this stays
# 7.5 across BOTH transitions — a naive elapsed-seconds count would read 6.5 (lost
# hour) or 8.5 (gained hour) instead. Identical 7.5 on both sides proves the
# tz-aware walk neither double-counts the fall-back repeat nor drops the
# spring-forward skip.
@pytest.mark.parametrize(
    ("label", "frozen", "departure_offset_h", "expected_window_h"),
    [
        # Spring-forward: the night that contains the 29 Mar 03:00 skip.
        # Departure 18:00 same day; end_of_night targets 07:00.
        ("spring_forward", "2026-03-28T23:30:00+02:00", 18, 7.5),
        # Fall-back: the night that contains the 25 Oct 03:00->03:00 repeat.
        # Departure 18:00 same day; end_of_night targets 07:00.
        ("fall_back", "2026-10-24T23:30:00+03:00", 18, 7.5),
    ],
)
async def test_dst_transitions_window_and_tz_aware_gentle_start(
    hass: HomeAssistant,
    label: str,
    frozen: str,
    departure_offset_h: int,
    expected_window_h: float,
) -> None:
    """Across both DST edges: the wall-clock night-window length is stable AND
    gentle_start stays tz-aware (the SensorDeviceClass.TIMESTAMP invariant)."""
    with freeze_time(frozen):
        coordinator = await _setup(hass)
        coordinator.finish_mode = FINISH_MODE_END_OF_NIGHT
        coordinator.departure_time = dt_util.now() + timedelta(
            hours=departure_offset_h
        )

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        data = coordinator.data
        # Wall-clock night 23:00->07:00 contains a DST shift; the walk counts
        # wall-clock minutes, so the window length matches the expected hours.
        assert data["night_hours_available"] == pytest.approx(
            expected_window_h, abs=0.25
        )

        gentle_start = data["latest_start_time"]
        assert gentle_start is not None
        # The critical invariant: never a naive datetime (would break the
        # TIMESTAMP sensor / dashboard read-back).
        assert isinstance(gentle_start, datetime)
        assert gentle_start.tzinfo is not None
        # gentle_start lands on/around the night window for this departure.
        assert gentle_start <= coordinator.departure_time


async def test_dst_departure_mode_gentle_start_tz_aware(
    hass: HomeAssistant,
) -> None:
    """departure mode across spring-forward also yields a tz-aware gentle_start
    spanning all time-to-departure (not just the night window)."""
    with freeze_time("2026-03-28T23:30:00+02:00"):
        coordinator = await _setup(hass)
        coordinator.finish_mode = FINISH_MODE_DEPARTURE
        coordinator.departure_time = dt_util.now() + timedelta(hours=12)

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        gentle_start = coordinator.data["latest_start_time"]
        assert gentle_start is not None
        assert gentle_start.tzinfo is not None
        assert gentle_start <= coordinator.departure_time
