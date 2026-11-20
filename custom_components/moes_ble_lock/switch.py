"""Auto-lock switch (DP 33). Off = latch opens and stays open; on = locks immediately."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MoesConfigEntry
from .device import CONNECT_ERRORS, LockUnavailable
from .entity import MoesEntity
from .lib.protocol import AuthError


async def async_setup_entry(
    hass: HomeAssistant, entry: MoesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([MoesAutoLock(entry.runtime_data)])


class MoesAutoLock(MoesEntity, SwitchEntity):
    _attr_translation_key = "auto_lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device) -> None:
        super().__init__(device, "auto_lock")

    @property
    def is_on(self) -> bool | None:
        return self._device.state.auto_lock

    async def _set(self, enabled: bool) -> None:
        try:
            await self._device.async_set_auto_lock(enabled)
        except (AuthError, LockUnavailable, *CONNECT_ERRORS) as err:
            raise HomeAssistantError(f"Could not change auto-lock: {err}") from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
