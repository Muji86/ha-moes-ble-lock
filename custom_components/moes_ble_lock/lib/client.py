"""Async BLE client for the MOES/Tuya BLE lock, built on bleak + the sans-IO protocol."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

RESPONSE_TIMEOUT = 10.0


@dataclass
class LockState:
    open: bool | None = None  # DP 47: True = latch open
    battery: int | None = None  # DP 8
    auto_lock: bool | None = None  # DP 33
    auto_lock_time: int | None = None  # DP 36 (s)
    alarm: str | None = None  # DP 21, last reported
    dps: dict[int, object] = field(default_factory=dict)

    def apply(self, dps: list[p.Dp]) -> None:
        for dp in dps:
            self.dps[dp.id] = dp.value
            if dp.id == p.DP_MOTOR_STATE:
                self.open = bool(dp.value)
            elif dp.id == p.DP_BATTERY:
                self.battery = int(dp.value)
            elif dp.id == p.DP_AUTO_LOCK:
                self.auto_lock = bool(dp.value)
            elif dp.id == p.DP_AUTO_LOCK_TIME:
                self.auto_lock_time = int(dp.value)
            elif dp.id == p.DP_ALARM and isinstance(dp.value, int):
                self.alarm = p.ALARMS[dp.value] if dp.value < len(p.ALARMS) else str(dp.value)


class TuyaBleLock:
    """One connection to the lock. Call `connect()`, use it, then `disconnect()`.

    `device` may be a bleak BLEDevice (preferred; required for Home Assistant) or an
    address string.
    """

    def __init__(
        self,
        device: BLEDevice | str,
        creds: p.Credentials,
        on_update: Callable[[LockState], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._device = device
        self._creds = creds
        self._on_update = on_update
        self._on_disconnect = on_disconnect
        self._client: BleakClient | None = None
        self._session: p.Session | None = None
        self._waiters: list[tuple[Callable[[object], bool], asyncio.Future]] = []
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._bad_frames = 0
        self.state = LockState()

    @property
    def device_info(self) -> p.DeviceInfo | None:
        return self._session.device_info if self._session else None

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected and self._session
                    and self._session.session_key)

    # ------------------------------------------------------------------ connection
    async def connect(
        self,
        connector: Callable[[Callable[[BleakClient], None]], Awaitable[BleakClient]] | None = None,
        timeout: float = 20.0,
    ) -> None:
        """Connect and authenticate.

        `connector(disconnected_callback)` may return an already-connected BleakClient;
        Home Assistant uses this to go through bleak_retry_connector / Bluetooth proxies.
        Without it, a plain BleakClient is used with a few retries.
        """
        if connector is not None:
            client = await connector(self._handle_disconnect)
        else:
            for attempt in range(1, 4):
                client = BleakClient(self._device, disconnected_callback=self._handle_disconnect,
                                     timeout=timeout)
                try:
                    await client.connect()
                    break
                except (TimeoutError, asyncio.TimeoutError, OSError) as err:
                    _LOGGER.warning("connect attempt %d failed: %r", attempt, err)
                    if attempt == 3:
                        raise ConnectionError(
                            "could not connect; is Smart Life (or another client) "
                            "connected to the lock?") from err
                    await asyncio.sleep(2 * attempt)
        self._client = client
        mtu = getattr(client, "mtu_size", 23) or 23
        self._session = p.Session(self._creds, mtu=mtu)
        try:
            await client.start_notify(p.CHAR_NOTIFY, self._handle_notify)
            await self._handshake()
        except Exception:
            await self.disconnect()
            raise

    async def _handshake(self) -> None:
        s = self._session
        self._bad_frames = 0
        try:
            info = await self._request(s.device_info_request(),
                                       lambda e: isinstance(e, p.DeviceInfo))
        except (TimeoutError, asyncio.TimeoutError):
            if self._bad_frames:
                raise p.AuthError("lock reply could not be decrypted (wrong local_key?)") from None
            raise
        _LOGGER.debug("device info %s", info)
        pair = await self._request(s.pair_request(), lambda e: isinstance(e, p.PairResult))
        if pair.result not in (p.PAIR_OK, p.PAIR_ALREADY_BOUND):
            raise p.AuthError(f"lock rejected credentials (pair result {pair.result})")
        # Full state report follows the status request
        report = self._wait(lambda e: isinstance(e, p.DpReport)
                            and any(d.id == p.DP_BATTERY for d in e.dps))
        await self._write(s.status_request())
        await asyncio.wait_for(report, RESPONSE_TIMEOUT)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._session = None
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))
        self._waiters.clear()
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("error while disconnecting", exc_info=True)

    def _handle_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("lock disconnected")
        self._session = None
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))
        self._waiters.clear()
        if self._on_disconnect:
            self._on_disconnect()

    # ------------------------------------------------------------------ commands
    async def refresh(self) -> LockState:
        """Ask the lock for a full status report."""
        self._require()
        report = self._wait(lambda e: isinstance(e, p.DpReport)
                            and any(d.id == p.DP_BATTERY for d in e.dps))
        await self._write(self._session.status_request())
        await asyncio.wait_for(report, RESPONSE_TIMEOUT)
        return self.state

    async def unlock(self) -> None:
        """Momentary unlock (DP 71). The lock re-locks itself after DP 36 seconds."""
        self._require()
        await self._dp_command(*self._session.unlock_command())

    async def set_auto_lock(self, enabled: bool) -> None:
        """DP 33. False = latch opens and stays open; True = locks immediately."""
        self._require()
        await self._dp_command(*self._session.auto_lock_command(enabled))

    async def lock(self) -> None:
        await self.set_auto_lock(True)

    async def hold_open(self) -> None:
        await self.set_auto_lock(False)

    async def _dp_command(self, sn: int, frames: list[bytes]) -> None:
        res = await self._request(frames, lambda e: isinstance(e, p.DpCommandResult) and e.sn == sn)
        if res.result != 0:
            raise p.ProtocolError(f"lock refused command (result {res.result})")

    # ------------------------------------------------------------------ plumbing
    def _require(self) -> None:
        if not self.is_connected:
            raise ConnectionError("not connected")

    def _wait(self, pred: Callable[[object], bool]) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((pred, fut))
        return fut

    async def _request(self, frames: list[bytes], pred: Callable[[object], bool]):
        fut = self._wait(pred)
        await self._write(frames)
        try:
            return await asyncio.wait_for(fut, RESPONSE_TIMEOUT)
        finally:
            self._waiters = [w for w in self._waiters if w[1] is not fut]

    async def _write(self, frames: list[bytes]) -> None:
        async with self._write_lock:
            for f in frames:
                await self._client.write_gatt_char(p.CHAR_WRITE, f, response=False)

    def _handle_notify(self, _char, data: bytearray) -> None:
        if not self._session:
            return
        try:
            res = self._session.feed(bytes(data))
        except p.ProtocolError as err:
            self._bad_frames += 1
            _LOGGER.warning("bad frame from lock: %s", err)
            return
        if res.replies and self._client:
            task = asyncio.get_running_loop().create_task(self._write(res.replies))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        for ev in res.events:
            _LOGGER.debug("event %s", ev)
            if isinstance(ev, p.DpReport):
                self.state.apply(ev.dps)
                if self._on_update:
                    self._on_update(self.state)
            for pred, fut in list(self._waiters):
                if not fut.done() and pred(ev):
                    fut.set_result(ev)
