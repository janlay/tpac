"""Base classes of TPAC entities, one per kind of network object.

| Object        | Base class        | Concrete class |
|---------------|-------------------|----------------|
| Access point  | NetworkDevice     | AccessPoint    |
| SSID          | NetworkService    | WlanProfile    |
| Wi-Fi client  | NetworkEndpoint   | WifiClient     |
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .ssid import unique_id, wlans_by_ssid


def device_identity(device: dict) -> str:
    """The key that names an access point: its source MAC, base MAC, serial number, or source address."""
    return (
        device.get("source_mac")
        or device.get("base_mac")
        or device.get("serial_number")
        or device.get("source_ip", "")
    ).lower()


class NetworkEntity(CoordinatorEntity):
    """Common base: the coordinator and the unique id, which each subclass builds."""

    def __init__(self, coordinator, unique_id_: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique_id_


class NetworkDevice(NetworkEntity):
    """An access point or one of its parts.  Entities of one access point share its device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key: str) -> None:
        super().__init__(coordinator, f"{entry.entry_id}_{key}")


class NetworkService(NetworkEntity):
    """An SSID.  Each SSID name is one device, linked to the TPAC hub; entities are named by what they show."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, ssid: str, key: str) -> None:
        super().__init__(coordinator, unique_id(entry, ssid, key))
        self.ssid = ssid
        hub = dr.async_get(coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"ssid:{ssid}")},
            name=ssid,
            manufacturer="TPAC",
            model="Wi-Fi network",
            via_device_id=hub.id if hub else None,
        )

    @property
    def wlan(self) -> dict:
        return wlans_by_ssid(self.coordinator).get(self.ssid, {})


def client_model(ip: str | None) -> str:
    """The device's model line.  The address is part of it, so the device info shows it."""
    return f"Wi-Fi client, {ip}" if ip else "Wi-Fi client"


def client_device_info(coordinator, entry, mac: str, name: str, ip: str | None = None) -> DeviceInfo:
    """Each Wi-Fi client is a device of its own, linked to the TPAC hub.  The MAC is its network connection."""
    hub = dr.async_get(coordinator.hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    return DeviceInfo(
        identifiers={(DOMAIN, client_device_key(entry, mac))},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        name=name,
        manufacturer="TPAC",
        model=client_model(ip),
        via_device_id=hub.id if hub else None,
    )


def client_device_key(entry, mac: str) -> str:
    return f"{entry.entry_id}_client_{mac}"


class NetworkEndpoint(NetworkEntity):
    """A Wi-Fi client.  Its own device under the TPAC hub; its entities are named by what they show."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, mac: str, name: str) -> None:
        super().__init__(coordinator, f"{entry.entry_id}_client_{mac}")
        self._attr_device_info = client_device_info(coordinator, entry, mac, name)


def is_access_point(device: dict) -> bool:
    """An access point that the controller discovered over CAPWAP."""
    return device.get("protocol") == "capwap"


def access_point_data(coordinator, identity: str) -> dict:
    """The coordinator's data of one access point, or an empty dict while it is not served."""
    return next(
        (item for item in coordinator.data.get("devices", []) if device_identity(item) == identity),
        {},
    )


def add_access_point_entities(coordinator, entry, async_add_entities, create: Callable[[str], object]) -> None:
    """Add one entity per access point the controller serves, now and when it appears.  create(identity) builds it."""
    added: set[str] = set()

    def add_new() -> None:
        fresh = []
        for device in coordinator.data.get("devices", []):
            identity = device_identity(device)
            if identity and is_access_point(device) and identity not in added:
                added.add(identity)
                fresh.append(create(identity))
        if fresh:
            async_add_entities(fresh)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))
