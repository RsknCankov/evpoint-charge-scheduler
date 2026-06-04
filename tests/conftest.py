"""Shared pytest fixtures for the EVPoint Charge Scheduler suite."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load custom_components/ during tests.

    `enable_custom_integrations` is provided by
    pytest-homeassistant-custom-component (PHACC). Wiring it autouse now is
    harmless for the direct-instantiation baseline test (which never calls
    async_setup) and is required the moment Phase 3/4 add full-setup tests.
    """
    yield
