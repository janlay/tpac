"""The grace period before a client's departure is recorded.  It is kept across restarts."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .stations import DISASSOC_GRACE


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Add the disassociation grace period to the TPAC device."""
    controller = hass.data[DOMAIN][entry.entry_id].controller
    async_add_entities([DisassocGrace(controller, entry), RetentionDays(controller, entry)])


class DisassocGrace(RestoreEntity, NumberEntity):
    """Seconds a client must stay away from every access point before its departure is recorded."""

    _attr_has_entity_name = True
    _attr_name = "Disassociation grace period"
    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, controller, entry) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry.entry_id}_disassoc_grace"
        self._attr_native_value = DISASSOC_GRACE
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self._attr_native_value = float(last.state)
            except ValueError:
                pass
        self._controller.disassoc_grace = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._controller.disassoc_grace = value
        self._attr_native_value = value
        self.async_write_ha_state()


class RetentionDays(RestoreEntity, NumberEntity):
    """Days a client or access point may go unseen before it is removed, and its departure record is dropped."""

    _attr_has_entity_name = True
    _attr_name = "Retention period"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 365
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.DAYS

    def __init__(self, controller, entry) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry.entry_id}_retention_days"
        self._attr_native_value = float(controller.retention_days)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self._attr_native_value = float(last.state)
            except ValueError:
                pass
        self._controller.retention_days = int(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._controller.retention_days = int(value)
        self._attr_native_value = value
        self.async_write_ha_state()
