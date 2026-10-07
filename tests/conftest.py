import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Test automations keep their time-trigger timers until the test's HA is torn down.

    Newer pytest-homeassistant-custom-component releases fail a test on any timer
    still scheduled at teardown; those timers are HA's own automation listeners.
    """
    return True
