"""Shared test helpers."""

from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant


async def fake_boot(hass: HomeAssistant) -> None:
    """Finish a boot the way hass.async_start() does, for a test hass set to not_running.

    async_start() itself can't be called (the test hass is already started), so:
    'start' event, then the startup jobs (HA 2026.9+ arms automations there; older HA
    has none), then RUNNING and the 'started' event.
    """
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()
    jobs = getattr(hass, "_startup_jobs", None)
    if jobs:
        for job in list(jobs):
            hass.async_run_hass_job(job.job, *job.args)
        jobs.clear()
        await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
