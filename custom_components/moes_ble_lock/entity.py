"""Base entity for the MOES lock."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .device import MoesLockDevice


class MoesEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: MoesLockDevice, key: str) -> None:
        self._device = device
        self._attr_unique_id = f"{device.address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, device.address)},
            name=device.name,
            manufacturer="MOES",
            model="ES830 BLE cabinet lock",
        )

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
