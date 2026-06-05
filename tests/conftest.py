"""Shared pytest fixtures for the EVPoint Charge Scheduler suite."""

import pytest
from homeassistant.util import dt as dt_util


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load custom_components/ during tests.

    `enable_custom_integrations` is provided by
    pytest-homeassistant-custom-component (PHACC). Wiring it autouse now is
    harmless for the direct-instantiation baseline test (which never calls
    async_setup) and is required the moment Phase 3/4 add full-setup tests.
    """
    yield


@pytest.fixture(autouse=True)
def reset_default_timezone():
    """Restore dt_util's default time zone around every test (D-04).

    Several tests pin ``dt_util.set_default_time_zone(Europe/Sofia)`` to drive
    the planner under a concrete local clock. Because ``DEFAULT_TIME_ZONE`` is
    module-global process state, a test that sets it leaks the default into the
    next test — silently, and worse, order-dependently. That latent leak is
    benign only while the suite runs in a fixed alphabetical order; the moment
    Wave 3 enables pytest-randomly's shuffle (plan 04-05), an alphabetically
    "later" test that assumed UTC could suddenly inherit Europe/Sofia and flip.

    This autouse fixture captures the default time zone before the test and
    restores it afterwards, so each test starts from the same baseline and no
    set_default_time_zone call can cross a test boundary. Only the fixture is
    added here — the pytest-randomly dependency lands in plan 04-05.
    """
    original = dt_util.DEFAULT_TIME_ZONE
    yield
    dt_util.set_default_time_zone(original)
