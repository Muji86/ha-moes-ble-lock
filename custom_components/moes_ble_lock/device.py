"""Connection manager for one lock: connects on demand, keeps the link briefly, polls."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .lib import protocol as p
from .lib.client import LockState, TuyaBleLock

_LOGGER = logging.getLogger(__name__)

RETRY_ERRORS = (BleakError, TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)
CONNECT_ERRORS = (*RETRY_ERRORS, p.ProtocolError)


class LockUnavailable(Exception):
    """The lock is not reachable right now."""


def make_connector(hass: HomeAssistant, address: str, name: str):
    """Connector for TuyaBleLock.connect() that goes through HA's Bluetooth stack/proxies."""

    async def connector(disconnected_callback):
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            raise LockUnavailable(
                f"{address} is not advertising (out of range, or Smart Life is connected)")
        return await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            name,
            disconnected_callback=disconnected_callback,
            ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                hass, address, connectable=True) or ble_device,
        )

    return connector


class MoesLockDevice:
    """Owns the BLE connection to one lock and the last known state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        creds: p.Credentials,
        idle_timeout: int,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.address = address
        self.name = entry.title
        self._creds = creds
        self._idle_timeout = idle_timeout
        self._lock: TuyaBleLock | None = None
        self._conn_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._reconnect_handle: asyncio.TimerHandle | None = None
        self._listeners: list[Callable[[], None]] = []
        self._stopped = False
        self.state = LockState()
        self.firmware: str | None = None
        self.last_contact: datetime | None = None
        # Advertisement reception (for placing adapters/proxies)
        self.rssi: int | None = None
        self.rssi_adapter: str | None = None
        self.last_advert: datetime | None = None
        self._last_rssi_notify = 0.0

    # ------------------------------------------------------------------ advertisements
    @callback
    def async_start_advert_tracking(self) -> Callable[[], None]:
        """Track the lock's adverts: signal strength and which adapter hears it best."""
        if info := bluetooth.async_last_service_info(self.hass, self.address, connectable=True):
            self._apply_advert(info)
        return bluetooth.async_register_callback(
            self.hass,
            self._on_advert,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    @callback
    def _apply_advert(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        self.rssi = info.rssi
        scanner = bluetooth.async_scanner_by_source(self.hass, info.source)
        self.rssi_adapter = scanner.name if scanner else info.source
        self.last_advert = dt_util.utcnow()

    @callback
    def _on_advert(self, info: bluetooth.BluetoothServiceInfoBleak,
                   _change: bluetooth.BluetoothChange) -> None:
        self._apply_advert(info)
        # Adverts arrive every ~1.5 s; refresh entities at most every 5 s
        now = self.hass.loop.time()
        if now - self._last_rssi_notify >= 5:
            self._last_rssi_notify = now
            self._notify()

    # ------------------------------------------------------------------ listeners
    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def async_wait_for(self, predicate: Callable[[LockState], bool], timeout: float) -> bool:
        """Wait until the lock reports a state matching `predicate` (or time out)."""
        if predicate(self.state):
            return True
        fut = self.hass.loop.create_future()

        @callback
        def _check() -> None:
            if not fut.done() and predicate(self.state):
                fut.set_result(True)

        remove = self.async_add_listener(_check)
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False
        finally:
            remove()

    # ------------------------------------------------------------------ status
    @property
    def connected(self) -> bool:
        return bool(self._lock and self._lock.is_connected)

    @property
    def available(self) -> bool:
        if self.connected:
            return True
        if bluetooth.async_address_present(self.hass, self.address, connectable=True):
            return True
        # Not advertising while Smart Life holds it; keep last state for a while.
        return bool(self.last_contact and dt_util.utcnow() - self.last_contact < timedelta(hours=2))

    # ------------------------------------------------------------------ connection
    async def _ensure_connected(self) -> TuyaBleLock:
        async with self._conn_lock:
            if self._lock and self._lock.is_connected:
                return self._lock
            lock = TuyaBleLock(self.address, self._creds, on_update=self._on_update,
                               on_disconnect=self._on_disconnect)
            lock.state = self.state  # keep one state object across connections
            try:
                await lock.connect(connector=make_connector(self.hass, self.address, self.name))
            except p.AuthError:
                _LOGGER.error("%s rejected the stored credentials; starting reauth", self.name)
                self.entry.async_start_reauth(self.hass)
                raise
            info = lock.device_info
            if info and info.device_version != self.firmware:
                self.firmware = info.device_version
                dev_reg = dr.async_get(self.hass)
                for dev in dr.async_entries_for_config_entry(dev_reg, self.entry.entry_id):
                    dev_reg.async_update_device(dev.id, sw_version=self.firmware)
            self._lock = lock
            self.last_contact = dt_util.utcnow()
            _LOGGER.debug("%s connected and authenticated", self.name)
            self._notify()
            return lock

    @callback
    def _on_update(self, _state: LockState) -> None:
        self.last_contact = dt_util.utcnow()
        self._notify()

    @callback
    def _on_disconnect(self) -> None:
        _LOGGER.debug("%s disconnected", self.name)
        self._cancel_idle()
        self._notify()
        if self._idle_timeout == 0 and not self._stopped:
            self._schedule_reconnect(5)

    @callback
    def _schedule_idle(self) -> None:
        self._cancel_idle()
        if self._idle_timeout > 0:
            self._idle_handle = self.hass.loop.call_later(
                self._idle_timeout,
                lambda: self.entry.async_create_background_task(
                    self.hass, self.async_disconnect(), "moes_ble_lock idle disconnect"))

    @callback
    def _cancel_idle(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None

    @callback
    def _schedule_reconnect(self, delay: float) -> None:
        if self._reconnect_handle:
            self._reconnect_handle.cancel()

        async def _reconnect() -> None:
            try:
                await self._ensure_connected()
            except p.AuthError:
                return
            except (*CONNECT_ERRORS, LockUnavailable) as err:
                _LOGGER.debug("%s reconnect failed: %s", self.name, err)
                self._schedule_reconnect(min(delay * 2, 120))

        self._reconnect_handle = self.hass.loop.call_later(
            delay, lambda: self.entry.async_create_background_task(
                self.hass, _reconnect(), "moes_ble_lock reconnect"))

    async def async_disconnect(self) -> None:
        self._cancel_idle()
        lock, self._lock = self._lock, None
        if lock:
            await lock.disconnect()
        self._notify()

    async def async_stop(self) -> None:
        self._stopped = True
        if self._reconnect_handle:
            self._reconnect_handle.cancel()
        await self.async_disconnect()

    # ------------------------------------------------------------------ operations
    async def _run(self, op: Callable[[TuyaBleLock], object]) -> None:
        async with self._op_lock:
            lock = await self._ensure_connected()
            try:
                await op(lock)
            except RETRY_ERRORS:
                # one retry on a fresh connection (link may have dropped silently)
                await self.async_disconnect()
                lock = await self._ensure_connected()
                await op(lock)
            self.last_contact = dt_util.utcnow()
            self._schedule_idle()

    async def async_unlock(self) -> None:
        await self._run(lambda lock: lock.unlock())

    async def async_set_auto_lock(self, enabled: bool) -> None:
        await self._run(lambda lock: lock.set_auto_lock(enabled))

    async def async_lock(self) -> None:
        await self.async_set_auto_lock(True)

    async def async_poll(self, *_args) -> None:
        """Refresh state + battery. Connecting already yields a full status report."""
        if self._stopped:
            return
        if not self.connected and not bluetooth.async_address_present(
                self.hass, self.address, connectable=True):
            _LOGGER.debug("%s not advertising; skipping poll", self.name)
            self._notify()
            return
        try:
            async with self._op_lock:
                was_connected = self.connected
                lock = await self._ensure_connected()
                if was_connected:
                    await lock.refresh()
                self._schedule_idle()
        except p.AuthError:
            pass
        except (*CONNECT_ERRORS, LockUnavailable) as err:
            _LOGGER.debug("%s poll failed: %s", self.name, err)
        self._notify()
