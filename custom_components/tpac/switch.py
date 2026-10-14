"""The LED of each access point, as a switch on the access point's device."""

from __future__ import annotations

import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .entities import access_point_data, add_access_point_entities


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Add an LED switch for each access point the controller serves, now and when it appears."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_access_point_entities(coordinator, entry, async_add_entities, lambda identity: ApLed(coordinator, entry, identity))


# How long a written state is shown before the AP's report is taken as the truth.
OPTIMISTIC_SECONDS = 60.0


class ApLed(CoordinatorEntity, SwitchEntity):
    """The access point's LED.  Its state is the LED the AP last reported, and turning it sets the LED."""

    _attr_has_entity_name = True
    _attr_name = "LED"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, identity: str) -> None:
        super().__init__(coordinator)
        self._identity = identity
        self._controller = coordinator.controller
        self._attr_unique_id = f"{entry.entry_id}_{identity}_led"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, identity)})
        # (on, when) of the last write until the AP's report agrees with it.  The switch shows the write at once.
        self._optimistic: tuple[bool, float] | None = None

    @property
    def available(self) -> bool:
        return bool(access_point_data(self.coordinator, self._identity).get("connected"))

    @property
    def is_on(self) -> bool:
        if self._optimistic is not None:
            return self._optimistic[0]
        return access_point_data(self.coordinator, self._identity).get("led") == "on"

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None:
            on, since = self._optimistic
            led_on = access_point_data(self.coordinator, self._identity).get("led") == "on"
            if led_on == on or time.monotonic() - since > OPTIMISTIC_SECONDS:
                self._optimistic = None
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict:
        return {"identity": self._identity}

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        ok = await self.hass.async_add_executor_job(self._controller.set_led, self._identity, on)
        if not ok:
            raise ServiceValidationError(f"{self._identity} is not adopted or not connected")
        self._optimistic = (on, time.monotonic())
        self.async_write_ha_state()
