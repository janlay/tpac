"""Access points: one status entity per AP, kept while the AP is known and removed when long gone.

An AP that is not listed is "offline", not missing.  Its entity is created at setup from the last
snapshot in storage, so Home Assistant does not show the restored registry entry as "unavailable".
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .entities import NetworkDevice, device_identity, is_access_point

# An AP that has reported within this many seconds is alive.
ALIVE_WITHIN = 60
# An access point that has not been listed for the retention period is removed, like an unnamed client.
AP_PRUNE_INTERVAL = timedelta(hours=1)
# Version 1 files hold a Unix time per AP; loading also accepts the snapshot format, so no migration is needed.
AP_STORE_VERSION = 1
# What an offline AP still shows: its last known state, as the panel and the device page use it.
SNAPSHOT_KEYS = (
    "source_mac", "base_mac", "serial_number", "source_ip", "protocol", "status", "profiles",
    "model", "system_name", "system_description", "chassis_id", "management_address", "wtp_name",
    "hardware", "firmware", "country", "max_radios", "radios_in_use", "mount_type", "channels",
    "port_id",
)


async def async_setup_access_points(hass, entry, coordinator, async_add_entities) -> None:
    """Create a status entity for each known access point, and prune the ones not seen for a week."""
    store = Store(hass, AP_STORE_VERSION, f"{DOMAIN}_ap_last_seen_{entry.entry_id}")
    # identity -> {"seen": Unix time, "device": last snapshot}.  Older files hold only the time.
    known_aps: dict[str, dict] = {}
    for identity, value in (await store.async_load() or {}).items():
        known_aps[identity] = value if isinstance(value, dict) else {"seen": float(value), "device": {}}
    # Access points already in the registry but never stored count as unseen since 1970.
    for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if entity.domain == "sensor" and entity.unique_id.startswith(f"{entry.entry_id}_"):
            identity = entity.unique_id[len(entry.entry_id) + 1:]
            # Addresses and MACs contain ":" or "."; the hub and client entities do not.
            if (":" in identity or "." in identity) and not identity.startswith(("client_", "ssid_")):
                known_aps.setdefault(identity, {"seen": 0.0, "device": {}})

    def record_listed() -> None:
        now = time.time()
        for device in coordinator.data.get("devices", []):
            identity = device_identity(device)
            if not identity or not is_access_point(device):
                continue
            snapshot = {key: device[key] for key in SNAPSHOT_KEYS if device.get(key) not in (None, "", [])}
            known_aps[identity] = {"seen": now, "device": snapshot}
        store.async_delay_save(lambda: known_aps, 60)

    record_listed()
    await _prune(hass, entry, coordinator, store, known_aps)

    created = set(known_aps)
    async_add_entities(
        AccessPoint(coordinator, entry, identity, known_aps[identity]["device"]) for identity in created
    )

    def add_new() -> None:
        record_listed()
        fresh = [
            device_identity(device) for device in coordinator.data.get("devices", [])
            if is_access_point(device) and device_identity(device) and device_identity(device) not in created
        ]
        if fresh:
            created.update(fresh)
            async_add_entities(
                AccessPoint(coordinator, entry, identity, known_aps[identity]["device"]) for identity in fresh
            )

    entry.async_on_unload(coordinator.async_add_listener(add_new))

    async def prune(_now=None) -> None:
        await _prune(hass, entry, coordinator, store, known_aps)
        created.difference_update([i for i in list(created) if i not in known_aps])

    entry.async_on_unload(async_track_time_interval(hass, prune, AP_PRUNE_INTERVAL))


async def _prune(hass, entry, coordinator, store, known_aps: dict[str, dict]) -> None:
    """Remove the registry entries of access points not listed for the retention period."""
    # Any listed device keeps its AP.  Restored APs are listed before they have a protocol, so only an
    # empty list counts as the service being down, not every AP being gone.
    present = {device_identity(device) for device in coordinator.data.get("devices", [])}
    if not present:
        return
    cutoff = time.time() - coordinator.controller.retention_seconds
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    for identity in [i for i in list(known_aps) if i not in present]:
        # An entry without a snapshot is from before snapshots existed; it is removed unless listed again.
        if known_aps[identity]["seen"] > cutoff and known_aps[identity]["device"]:
            continue
        for platform, unique_id in (
            ("sensor", f"{entry.entry_id}_{identity}"),
            ("switch", f"{entry.entry_id}_{identity}_led"),
        ):
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if entity_id:
                registry.async_remove(entity_id)
        # The device was shared by the status and LED entities; remove it once they are gone.
        device_key = identity if identity.count(":") == 5 else f"capwap:{identity}"
        matches = devices.async_get_devices(identifiers={(DOMAIN, device_key)})
        if matches and not er.async_entries_for_device(registry, matches[0].id):
            devices.async_remove_device(matches[0].id)
        del known_aps[identity]
    store.async_delay_save(lambda: known_aps, 60)


class AccessPoint(NetworkDevice, SensorEntity):
    """An access point's status: "online" while the TPAC service lists it, otherwise "offline"."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["online", "offline"]
    _attr_translation_key = "ap_status"
    _attr_name = None

    def __init__(self, coordinator, entry, identity: str, snapshot: dict) -> None:
        super().__init__(coordinator, entry, identity)
        self._identity = identity
        self._snapshot = snapshot
        device = self._device
        registry_id = (
            identity
            if device.get("source_mac") or device.get("base_mac")
            else f"capwap:{identity}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, registry_id)},
            name=(
                device.get("wtp_name")
                or device.get("system_name")
                or device.get("model")
                or f"TPAC AP {identity}"
            ),
            manufacturer="TP-LINK" if self._looks_like_tplink else None,
            model=device.get("model") or device.get("system_description") or None,
            configuration_url=(
                f"http://{device.get('management_address') or device.get('source_ip')}"
                if device.get("management_address") or device.get("source_ip")
                else None
            ),
        )

    @property
    def _live(self) -> dict:
        """The device as the service lists it now, or {} when it is not listed."""
        return next(
            (device for device in self.coordinator.data.get("devices", []) if device_identity(device) == self._identity),
            {},
        )

    @property
    def _device(self) -> dict:
        """The live device, or the last snapshot when the AP is not listed."""
        return self._live or self._snapshot

    @property
    def _looks_like_tplink(self) -> bool:
        identity = " ".join(
            str(self._device.get(key, ""))
            for key in ("system_name", "system_description", "chassis_id", "model")
        ).lower()
        return "tp-link" in identity or "tplink" in identity

    @property
    def native_value(self) -> str:
        return "online" if self._live else "offline"

    @property
    def extra_state_attributes(self) -> dict:
        live = self._live
        device = self._device
        attributes = {
            key: device[key]
            for key in (
                "source_mac",
                "protocol",
                "source_ip",
                "source_port",
                "base_mac",
                "serial_number",
                "model",
                "hardware",
                "firmware",
                "wtp_name",
                "chassis_id",
                "port_id",
                "system_description",
                "management_address",
                "ttl_seconds",
                "observed_at",
                "status",
                "profiles",
                "apply_status",
                "apply_slots",
                "max_radios",
                "radios_in_use",
                "mac_type",
                "mount_type",
                "channels",
                "stations",
                "led",
                "country",
                "connected",
                "last_report",
            )
            if device.get(key) not in (None, "", [])
        }
        # The panel and the event forwarder find an access point by its identity, offline or not.
        attributes["identity"] = self._identity
        # Connected and alive describe the live session only; an offline AP has neither.
        attributes["connected"] = bool(live.get("connected"))
        attributes["alive"] = bool(live) and bool(live.get("last_report")) and time.time() - live["last_report"] <= ALIVE_WITHIN
        if "last_report" in attributes and live:
            # The service sends Unix time; Home Assistant shows ISO 8601.
            attributes["last_report"] = datetime.fromtimestamp(attributes["last_report"], timezone.utc).isoformat()
        else:
            attributes.pop("last_report", None)
        # Derived as the device panel shows them: the SSIDs the AP provides.
        attributes["ssids"] = self._ssids()
        return attributes

    def _ssids(self) -> list[str]:
        """The enabled SSIDs of the profiles applied to this AP, the default profile first."""
        profiles = {profile["name"]: profile.get("wlans") or {} for profile in self.coordinator.data.get("profiles", [])}
        names = ["default", *(n for n in self._device.get("profiles") or [] if n != "default")]
        ssids: list[str] = []
        for name in names:
            for wlan in profiles.get(name, {}).values():
                if wlan.get("enabled", True) and wlan.get("ssid") and wlan["ssid"] not in ssids:
                    ssids.append(wlan["ssid"])
        return ssids
