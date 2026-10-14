"""Sensors for TPAC statistics."""

from __future__ import annotations


from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HUB_MANUFACTURER, HUB_MODEL, HUB_URL
from .coordinator import VERSION
from .entities import NetworkService
from .access_points import async_setup_access_points
from .clients import async_setup_clients
from .ssid import access_points, async_track_ssids, clients

DESCRIPTIONS = (
    SensorEntityDescription(
        key="packets",
        name="CAPWAP discovery requests",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="sources",
        name="Connected AP addresses",
        icon="mdi:access-point-network",
    ),
    SensorEntityDescription(
        key="service_version",
        name="Version",
        icon="mdi:tag",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up TPAC sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(TpacSensor(coordinator, entry, description) for description in DESCRIPTIONS)

    await async_setup_access_points(hass, entry, coordinator, async_add_entities)
    await async_setup_clients(hass, entry, async_add_entities)
    async_track_ssids(
        coordinator,
        entry,
        async_add_entities,
        lambda ssid: [WlanProfile(coordinator, entry, ssid)],
    )


class WlanProfile(NetworkService, SensorEntity):
    """An SSID's status: "Available" while any access point provides it, otherwise "Unavailable".

    The count of access points is the access_points attribute, so a change in the count is not a state change.
    """

    _attr_name = None

    def __init__(self, coordinator, entry, ssid: str) -> None:
        super().__init__(coordinator, entry, ssid, "status")

    @property
    def native_value(self) -> str:
        return "Available" if access_points(self.coordinator, self.ssid) else "Unavailable"

    @property
    def extra_state_attributes(self) -> dict:
        # The security settings are attributes, not entities, so that their changes stay out of the Activity.
        wlan = self.wlan
        return {
            "ssid": self.ssid,
            "access_points": access_points(self.coordinator, self.ssid),
            "clients": clients(self.coordinator, self.ssid),
            "authentication": wlan.get("auth"),
            "cipher": wlan.get("cipher"),
            "hidden": bool(wlan.get("hidden")),
        }


class TpacSensor(CoordinatorEntity, SensorEntity):
    """Represent one TPAC statistic."""

    entity_description: SensorEntityDescription

    def __init__(self, coordinator, entry, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="TPAC",
            manufacturer=HUB_MANUFACTURER,
            model=HUB_MODEL,
            sw_version=f"v{VERSION}",
            configuration_url=HUB_URL,
        )

    @property
    def native_value(self):
        stats = self.coordinator.data["stats"]
        if self.entity_description.key == "packets":
            return stats.get("packets", 0)
        if self.entity_description.key == "sources":
            return len(stats.get("sources", []))
        health = self.coordinator.data["health"]
        return health.get("version", "unknown")

    @property
    def extra_state_attributes(self):
        stats = self.coordinator.data["stats"]
        if self.entity_description.key == "sources":
            return {
                "addresses": sorted(
                    source["address"]
                    for source in stats.get("sources", [])
                    if source.get("address")
                )
            }
        if self.entity_description.key == "service_version":
            # The panel reads the profile list off this attribute instead of
            # making its own request, since it has no TPAC API key.
            return {"profiles": self.coordinator.data.get("profiles", [])}
        return {
            "bytes": stats.get("bytes", 0),
            "last_received": stats.get("last_received"),
            "untracked_source_packets": stats.get("untracked_source_packets", 0),
        }
