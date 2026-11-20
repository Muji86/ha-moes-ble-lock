"""Battery (DP 8) and Bluetooth signal strength sensors."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MoesConfigEntry
from .entity import MoesEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MoesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([MoesBattery(entry.runtime_data), MoesSignal(entry.runtime_data)])


class MoesBattery(MoesEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device) -> None:
        super().__init__(device, "battery")

    @property
    def native_value(self) -> int | None:
        return self._device.state.battery


class MoesSignal(MoesEntity, SensorEntity):
    """RSSI of the lock's adverts at the adapter that hears it best (for placing a proxy)."""

    _attr_translation_key = "signal_strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device) -> None:
        super().__init__(device, "rssi")

    @property
    def native_value(self) -> int | None:
        return self._device.rssi

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rssi = self._device.rssi
        if rssi is None:
            quality = None
        elif rssi >= -70:
            quality = "excellent"
        elif rssi >= -80:
            quality = "good"
        elif rssi >= -88:
            quality = "marginal"
        else:
            quality = "poor"
        return {
            "adapter": self._device.rssi_adapter,
            "quality": quality,
            "last_advert": self._device.last_advert,
        }
