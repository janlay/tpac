"""Wi-Fi clients: one entity per MAC, Connected or Disconnected.

An unnamed client that has been unseen for a week is removed; a named one is kept.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from pathlib import Path

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entities import NetworkEndpoint, client_device_info, client_device_key, client_model

LOG = logging.getLogger(__name__)

# A Wi-Fi client with a random MAC is dropped after the retention period without being seen,
# unless the user has named its entity.  The period is the retention entity (number.py).
CLIENT_PRUNE_INTERVAL = timedelta(hours=1)
CLIENT_STORE_VERSION = 1
# A client that first appeared within this window counts as new.
NEW_CLIENT_WINDOW = 24 * 3600
# MAC -> name, written by an external tool (for example from the router's DHCP lease comments).
NAMES_FILE = "tpac_client_names.json"


async def async_setup_clients(hass, entry, async_add_entities) -> None:
    """Add an entity for each Wi-Fi client, and prune the unnamed ones that have gone quiet."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    clients = TpacClients(hass, coordinator, entry, async_add_entities)
    coordinator.clients = clients
    await clients.async_load()
    async_add_entities([NewWifiClients(coordinator, entry, clients)])
    await clients.async_update()
    entry.async_on_unload(coordinator.async_add_listener(lambda: hass.async_create_task(clients.async_update())))
    entry.async_on_unload(async_track_time_interval(hass, clients.prune, CLIENT_PRUNE_INTERVAL))
    await clients.prune()


class TpacClients:
    """Creates the client entities and removes unnamed ones that have gone quiet.

    ``last_seen`` (MAC -> Unix time) is kept in Home Assistant storage so that the
    seven-day rule survives restarts.
    """

    def __init__(self, hass, coordinator, entry, async_add_entities) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._entry = entry
        self._add = async_add_entities
        self._store = Store(hass, CLIENT_STORE_VERSION, f"{DOMAIN}_client_last_seen_{entry.entry_id}")
        self._first_store = Store(hass, CLIENT_STORE_VERSION, f"{DOMAIN}_client_first_seen_{entry.entry_id}")
        self._ap_store = Store(hass, CLIENT_STORE_VERSION, f"{DOMAIN}_client_last_ap_{entry.entry_id}")
        self._last_seen: dict[str, float] = {}
        # MAC -> Unix time of the first sighting.  Dropped with the client when it is pruned.
        self._first_seen: dict[str, float] = {}
        # MAC -> identity of the access point that last listed it.  Shared with the client entities.
        self._last_ap: dict[str, str] = {}
        self._known: set[str] = set()
        self._entities: dict[str, WifiClient] = {}
        self._names: dict[str, str] = {}
        self._names_mtime: float | None = None

    async def async_load(self) -> None:
        self._last_seen = dict(await self._store.async_load() or {})
        self._first_seen = dict(await self._first_store.async_load() or {})
        self._last_ap = dict(await self._ap_store.async_load() or {})

    def new_count(self, now: float) -> int:
        """Clients whose first sighting is within the last NEW_CLIENT_WINDOW seconds."""
        cutoff = now - NEW_CLIENT_WINDOW
        return sum(1 for first in self._first_seen.values() if first >= cutoff)

    def _present(self) -> dict[str, str]:
        """MAC -> identity of the access point that lists it now."""
        return {
            (station.get("mac") or "").lower(): device.get("identity", "")
            for device in self._coordinator.data.get("devices", [])
            for station in device.get("stations") or []
            if station.get("mac")
        }

    def _read_names(self, known_mtime: float | None) -> tuple[float | None, dict[str, str]] | None:
        """Blocking: the names file when it differs from ``known_mtime``, or None when it does not."""
        path = Path(self._hass.config.path(NAMES_FILE))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == known_mtime:
            return None
        if mtime is None:
            return None, {}
        try:
            return mtime, {mac.lower(): name for mac, name in json.loads(path.read_text()).items()}
        except (OSError, ValueError) as exc:
            LOG.warning("cannot read %s: %s", path, exc)
            return mtime, {}

    async def async_refresh_names(self) -> None:
        """Rename the entities when the names file changes."""
        result = await self._hass.async_add_executor_job(self._read_names, self._names_mtime)
        if result is None:
            return
        self._names_mtime, self._names = result
        for mac, entity in self._entities.items():
            entity.set_name(self._names.get(mac, mac))

    async def async_update(self) -> None:
        """Called on every coordinator update: pick up name changes, then record and add clients."""
        await self.async_refresh_names()
        self.add_new()

    def add_new(self) -> None:
        """Record what is present and add new entities."""
        now = time.time()
        for mac, identity in self._present().items():
            self._last_seen[mac] = now
            if mac not in self._first_seen:
                self._first_seen[mac] = now
                self._first_store.async_delay_save(lambda: self._first_seen, 60)
            if self._last_ap.get(mac) != identity:
                self._last_ap[mac] = identity
                self._ap_store.async_delay_save(lambda: self._last_ap, 60)
        # Every known client gets an entity, including those not present now, so that they are listed as disconnected after a restart.
        fresh = sorted(set(self._last_seen) - self._known)
        self._known.update(fresh)
        if fresh:
            entities = [WifiClient(self._coordinator, self._entry, mac, self._last_ap, self._names.get(mac, mac)) for mac in fresh]
            self._entities.update(zip(fresh, entities))
            self._add(entities)
        self._store.async_delay_save(lambda: self._last_seen, 60)

    def known(self) -> list[str]:
        return sorted(self._known)

    def name_of(self, mac: str) -> str:
        return self._names.get(mac, mac)

    async def drop(self, mac: str) -> bool:
        """Remove the stored history of a client that is not connected, and its live entities.  False if it is connected."""
        if self._coordinator.controller.is_connected(mac):
            return False
        self._coordinator.controller.forget_client(mac)
        self._known.discard(mac)
        live = [self._entities.pop(mac, None)]
        self._last_seen.pop(mac, None)
        self._first_seen.pop(mac, None)
        self._last_ap.pop(mac, None)
        for entity in live:
            if entity is not None and entity.hass is not None:
                await entity.async_remove(force_remove=True)
        self._store.async_delay_save(lambda: self._last_seen, 60)
        self._first_store.async_delay_save(lambda: self._first_seen, 60)
        self._ap_store.async_delay_save(lambda: self._last_ap, 60)
        await self._hass.async_add_executor_job(self._coordinator.controller.save_state)
        return True

    async def prune(self, _now=None) -> None:
        """Remove unnamed client entities not seen for the retention period."""
        cutoff = time.time() - self._coordinator.controller.retention_seconds
        registry = er.async_get(self._hass)
        present = self._present()
        for mac, seen in list(self._last_seen.items()):
            if mac in present or seen > cutoff:
                continue
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{self._entry.entry_id}_client_{mac}")
            entity = registry.async_get(entity_id) if entity_id else None
            if entity is not None and entity.name is not None:
                # The user named it in Home Assistant: keep it and its history.
                continue
            if entity is not None:
                registry.async_remove(entity_id)
                LOG.info("removed client %s, unseen for 7 days", mac)
            self._known.discard(mac)
            self._entities.pop(mac, None)
            del self._last_seen[mac]
            self._first_seen.pop(mac, None)
            self._first_store.async_delay_save(lambda: self._first_seen, 60)
            self._last_ap.pop(mac, None)
            self._ap_store.async_delay_save(lambda: self._last_ap, 60)
        self._store.async_delay_save(lambda: self._last_seen, 60)


class WifiClient(NetworkEndpoint, SensorEntity):
    """A Wi-Fi client seen by an access point.  Connected while any AP lists it."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["connected", "disconnected"]
    _attr_translation_key = "client_status"
    # A client with no icon chosen in Home Assistant is probably an unidentified device.
    _attr_icon = "mdi:account-question"

    def __init__(self, coordinator, entry, mac: str, last_ap: dict[str, str], name: str) -> None:
        super().__init__(coordinator, entry, mac, name)
        self._entry = entry
        self._mac = mac
        self._last_ap = last_ap

    def _handle_coordinator_update(self) -> None:
        # The address can change while the client stays connected; keep the device's model line current.
        ip = self._station.get("ip")
        model = client_model(ip)
        if self._attr_device_info.get("model") != model and self.hass is not None:
            self._attr_device_info = client_device_info(self.coordinator, self._entry, self._mac, self._attr_device_info.get("name"), ip)
            device = dr.async_get(self.hass).async_get_device_by_identifier((DOMAIN, client_device_key(self._entry, self._mac)), self._entry.entry_id)
            if device is not None:
                dr.async_get(self.hass).async_update_device(device.id, model=model)
        super()._handle_coordinator_update()

    def set_name(self, name: str) -> None:
        """Rename the client, for example to its DHCP lease comment.  The name is the device's, so the entity follows it."""
        if self._attr_device_info.get("name") == name:
            return
        self._attr_device_info = client_device_info(self.coordinator, self._entry, self._mac, name)
        if self.hass is not None:
            device = dr.async_get(self.hass).async_get_device_by_identifier((DOMAIN, client_device_key(self._entry, self._mac)), self._entry.entry_id)
            if device is not None:
                dr.async_get(self.hass).async_update_device(device.id, name=name)
            self.async_write_ha_state()

    @property
    def _station(self) -> dict:
        for device in self.coordinator.data.get("devices", []):
            for station in device.get("stations") or []:
                if (station.get("mac") or "").lower() == self._mac:
                    return {**station, "ap": device.get("identity")}
        return {}

    @property
    def available(self) -> bool:
        # Not tied to the coordinator's last refresh: before the first report a client is "disconnected", not "unavailable".
        return True

    @property
    def native_value(self) -> str:
        return "connected" if self._station else "disconnected"

    @property
    def extra_state_attributes(self) -> dict:
        station = self._station
        last_ap = self._last_ap.get(self._mac)
        left_at = self.coordinator.data.get("left_at", {}).get(self._mac)
        return {
            "mac": self._mac,
            **({"last_ap": last_ap} if last_ap else {}),
            **({"left_at": int(left_at)} if left_at and not station else {}),
            **{key: station[key] for key in ("ap", "ssid", "slot", "rssi", "ip", "since") if station.get(key) not in (None, "")},
        }


class NewWifiClients(CoordinatorEntity, SensorEntity):
    """How many Wi-Fi clients appeared for the first time in the last 24 hours."""

    _attr_has_entity_name = True
    _attr_name = "New Wi-Fi clients"
    _attr_icon = "mdi:account-plus"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, clients: TpacClients) -> None:
        super().__init__(coordinator)
        self._clients = clients
        self._attr_unique_id = f"{entry.entry_id}_new_wifi_clients"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def native_value(self) -> int:
        return self._clients.new_count(time.time())

    @property
    def extra_state_attributes(self) -> dict:
        return {"window_hours": NEW_CLIENT_WINDOW // 3600}
