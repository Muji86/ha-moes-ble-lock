"""Lock entity: unlock = momentary BLE unlock, lock = auto-lock on (locks immediately)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MoesConfigEntry
from .device import CONNECT_ERRORS, LockUnavailable
from .entity import MoesEntity
from .lib.protocol import AuthError

MOTOR_TIMEOUT = 4.0  # seconds to wait for the latch to report its new position


async def async_setup_entry(
    hass: HomeAssistant, entry: MoesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([MoesLock(entry.runtime_data)])


class MoesLock(MoesEntity, LockEntity):
    _attr_name = None  # the device's main entity

    def __init__(self, device) -> None:
        super().__init__(device, "lock")

    @property
    def is_locked(self) -> bool | None:
        if self._device.state.open is None:
            return None
        return not self._device.state.open

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._device.state
        return {
            "connected": self._device.connected,
            "auto_lock": s.auto_lock,
            "auto_lock_time": s.auto_lock_time,
            "last_alarm": s.alarm,
        }

    async def _call(self, op, transient: str, want_open: bool) -> None:
        setattr(self, transient, True)
        self.async_write_ha_state()
        try:
            await op()
            # Stay "unlocking"/"locking" until the latch actually reports the new position
            # (the motor takes ~1 s after the command is accepted).
            await self._device.async_wait_for(lambda s: s.open is want_open, MOTOR_TIMEOUT)
        except AuthError as err:
            raise HomeAssistantError("Lock rejected the stored keys; reconfigure it") from err
        except LockUnavailable as err:
            raise HomeAssistantError(
                "Lock not reachable (out of range, or Smart Life is connected to it)") from err
        except CONNECT_ERRORS as err:
            raise HomeAssistantError(f"Lock command failed: {err}") from err
        finally:
            setattr(self, transient, False)
            self.async_write_ha_state()

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._call(self._device.async_unlock, "_attr_is_unlocking", want_open=True)

    async def async_lock(self, **kwargs: Any) -> None:
        await self._call(self._device.async_lock, "_attr_is_locking", want_open=False)
