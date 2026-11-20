"""Connected status: on while Home Assistant holds the BLE link (Smart Life is blocked then)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MoesConfigEntry
from .entity import MoesEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MoesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([MoesConnected(entry.runtime_data)])


class MoesConnected(MoesEntity, BinarySensorEntity):
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device) -> None:
        super().__init__(device, "connected")

    @property
    def available(self) -> bool:
        return True  # always meaningful: "off" also covers "not reachable"

    @property
    def is_on(self) -> bool:
        return self._device.connected
