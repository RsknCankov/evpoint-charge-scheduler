"""WR-05 — narrowed recorder-learner exception handling (D-01, from 03-REVIEW).

Both recorder learners (``_async_learn_night_window`` and ``_async_learn_prices``)
wrap the blocking ``state_changes_during_period`` executor job. Before this plan
each caught a bare ``except Exception`` and degraded to debug — which also
silently swallowed genuine programming bugs (a TypeError, an AttributeError),
hiding them behind ``night_window_source: configured`` / ``price_source: pending``.

These tests pin the narrowed behaviour:

* an INTENDED recorder-unavailable / query-failure type (HomeAssistantError,
  sqlalchemy SQLAlchemyError, RuntimeError "recorder not running") is caught —
  the learner returns, the learned values stay ``None``, the configured clock is
  used, and the coordinator still loads and plans (graceful degrade, T-04-01);
* an UNEXPECTED type (a TypeError standing in for a real code bug) is NOT
  swallowed — it propagates out of the learner so the bug surfaces instead of
  masquerading as "no history learned".

The setup mirrors ``test_session_end.py``: a full ``async_setup`` of a
MockConfigEntry with the tariff + price sensors wired, then the learner is
invoked directly with the executor job monkeypatched to raise.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evpoint_charge_scheduler.const import DOMAIN

TARIFF_ENTITY = "sensor.tariff"
PRICE_ENTITY = "sensor.price"


def _make_entry() -> MockConfigEntry:
    """Config with tariff + price sensors wired so both learners are live."""
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
            "tariff_sensor": TARIFF_ENTITY,
            "night_tariff_value": "night",
            "price_sensor": PRICE_ENTITY,
        },
    )


async def _setup(hass: HomeAssistant):
    hass.config.time_zone = "Europe/Sofia"
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Sofia"))
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


class _StubRecorder:
    """Minimal recorder stand-in whose executor entry point raises ``exc``."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def async_add_executor_job(self, *_args, **_kwargs):
        raise self._exc


def _patch_executor_to_raise(monkeypatch, exc: BaseException) -> None:
    """Make get_instance(hass).async_add_executor_job raise ``exc``.

    Both learners reach the recorder through
    ``get_instance(self.hass).async_add_executor_job(state_changes_during_period, ...)``.
    The test hass has no recorder registered, so the real ``get_instance`` would
    itself raise ``KeyError`` before the executor call — which would defeat the
    point of injecting a *specific* exception type. We therefore replace
    ``get_instance`` with one returning a stub recorder whose executor job raises
    exactly ``exc``, so the learner's narrowed ``except`` is what's under test.
    """
    from homeassistant.components import recorder

    stub = _StubRecorder(exc)
    # Patch the name the coordinator's learner methods import at call time.
    monkeypatch.setattr(recorder, "get_instance", lambda _hass: stub)


# --- Intended-error: graceful degrade -------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        HomeAssistantError("recorder not ready"),
        RuntimeError("recorder not running"),
    ],
)
async def test_night_window_learner_degrades_on_intended_error(
    hass: HomeAssistant, monkeypatch, exc
) -> None:
    """An intended recorder failure leaves learned window None, source configured."""
    coordinator = await _setup(hass)
    coordinator._learned_night_start = None
    coordinator._learned_night_end = None

    _patch_executor_to_raise(monkeypatch, exc)

    # Must NOT raise — the learner swallows the intended failure and returns.
    await coordinator._async_learn_night_window()

    assert coordinator._learned_night_start is None
    assert coordinator._learned_night_end is None

    # The coordinator still loads and plans on the configured clock.
    await coordinator.async_refresh()
    assert coordinator.data["night_window_source"] == "configured"


async def test_night_window_learner_degrades_on_sqlalchemy_error(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A sqlalchemy SQLAlchemyError (DB-layer failure) degrades gracefully too."""
    from sqlalchemy.exc import SQLAlchemyError

    coordinator = await _setup(hass)
    _patch_executor_to_raise(monkeypatch, SQLAlchemyError("db gone"))

    await coordinator._async_learn_night_window()

    assert coordinator._learned_night_start is None
    assert coordinator._learned_night_end is None


@pytest.mark.parametrize(
    "exc",
    [
        HomeAssistantError("recorder not ready"),
        RuntimeError("recorder not running"),
    ],
)
async def test_price_learner_degrades_on_intended_error(
    hass: HomeAssistant, monkeypatch, exc
) -> None:
    """An intended recorder failure leaves learned prices None, source pending."""
    coordinator = await _setup(hass)
    coordinator._learned_night_price = None
    coordinator._learned_day_price = None

    _patch_executor_to_raise(monkeypatch, exc)

    await coordinator._async_learn_prices()

    assert coordinator._learned_night_price is None
    assert coordinator._learned_day_price is None

    await coordinator.async_refresh()
    assert coordinator.data["price_source"] == "pending"


# --- Unexpected-error: NOT swallowed --------------------------------------


async def test_night_window_learner_propagates_unexpected_error(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A TypeError (real bug) is NOT caught — it propagates out of the learner."""
    coordinator = await _setup(hass)
    _patch_executor_to_raise(monkeypatch, TypeError("bug: wrong arg type"))

    with pytest.raises(TypeError):
        await coordinator._async_learn_night_window()


async def test_price_learner_propagates_unexpected_error(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A TypeError (real bug) is NOT caught — it propagates out of the learner."""
    coordinator = await _setup(hass)
    _patch_executor_to_raise(monkeypatch, TypeError("bug: wrong arg type"))

    with pytest.raises(TypeError):
        await coordinator._async_learn_prices()
