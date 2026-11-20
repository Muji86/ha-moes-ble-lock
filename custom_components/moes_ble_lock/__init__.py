"""MOES Tuya BLE Lock — fully local Bluetooth control."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CHECK_CODE,
    CONF_DEVICE_ID,
    CONF_IDLE_TIMEOUT,
    CONF_LOCAL_KEY,
    CONF_POLL_INTERVAL,
    CONF_UUID,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
)
from .device import MoesLockDevice
from .lib.protocol import Credentials

PLATFORMS = [Platform.BINARY_SENSOR, Platform.LOCK, Platform.SENSOR, Platform.SWITCH]

type MoesConfigEntry = ConfigEntry[MoesLockDevice]


async def async_setup_entry(hass: HomeAssistant, entry: MoesConfigEntry) -> bool:
    creds = Credentials(
        local_key=entry.data[CONF_LOCAL_KEY],
        uuid=entry.data[CONF_UUID],
        device_id=entry.data[CONF_DEVICE_ID],
        check_code=entry.data[CONF_CHECK_CODE],
    )
    device = MoesLockDevice(
        hass,
        entry,
        entry.data[CONF_ADDRESS],
        creds,
        idle_timeout=entry.options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
    )
    entry.runtime_data = device
    entry.async_on_unload(device.async_start_advert_tracking())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    poll = timedelta(minutes=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    entry.async_on_unload(async_track_time_interval(hass, device.async_poll, poll))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    # First state read in the background so a missing lock never blocks HA startup.
    entry.async_create_background_task(hass, device.async_poll(), "moes_ble_lock initial poll")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MoesConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: MoesConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
