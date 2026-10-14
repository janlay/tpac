"""The AC model and version that TPAC announces to access points.  Both are kept across restarts."""

from __future__ import annotations

import re

from homeassistant.components.text import TextEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import AC_NAME, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Add the simulated AC model and version to the TPAC device."""
    controller = hass.data[DOMAIN][entry.entry_id].controller
    async_add_entities([
        SimulatedAcText(controller, entry, "model", "Simulating AC model", AC_NAME),
        SimulatedAcText(controller, entry, "version", "Simulating AC version", "7.0"),
    ])


class SimulatedAcText(RestoreEntity, TextEntity):
    """One part of the AC identity.  Its value is applied to the controller at once."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:router-network"

    def __init__(self, controller, entry, key: str, name: str, default: str) -> None:
        self._controller = controller
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_simulating_ac_{key}"
        self._attr_native_value = default
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", ""):
            self._attr_native_value = last.state
        if self._key == "model":
            # An earlier version kept "TL-R470GP-AC 7.0" in this entity.  Split it into model and version.
            match = re.match(r"^(.+?) (\d+\.\d+)$", self._attr_native_value)
            if match:
                self._attr_native_value = match.group(1)
                self._controller.ac_identity.version = match.group(2)
        self._apply(self._attr_native_value)

    async def async_set_value(self, value: str) -> None:
        self._apply(value)
        self._attr_native_value = value
        self.async_write_ha_state()

    def _apply(self, value: str) -> None:
        setattr(self._controller.ac_identity, self._key, value.strip())
