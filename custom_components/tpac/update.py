"""The software of each access point as an update entity, with the newest package of its model as the latest version."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiohttp import ClientError
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LATEST_TTL, async_fetch_package, async_latest_package, async_upgrade_ap
from .const import DOMAIN
from .entities import access_point_data, add_access_point_entities
from .firmware import parse_version

LOG = logging.getLogger(__name__)
CHECK_INTERVAL = timedelta(seconds=LATEST_TTL)
PROGRESS_INTERVAL = timedelta(seconds=2)
UPLOADING_PHASES = ("uploading", "installing")


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Add a firmware entity for each access point the controller serves, now and when it appears."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_access_point_entities(coordinator, entry, async_add_entities, lambda identity: ApFirmware(coordinator, entry, identity))


def version_text(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


class ApFirmware(CoordinatorEntity, UpdateEntity):
    """The software of an access point.  Only a package newer than the installed software is installed."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, coordinator, entry, identity: str) -> None:
        super().__init__(coordinator)
        self._identity = identity
        self._controller = coordinator.controller
        self._attr_unique_id = f"{entry.entry_id}_{identity}_firmware"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, identity)})
        self._latest = None
        self._unsub_progress = None

    @property
    def _installed(self) -> tuple[int, ...] | None:
        return parse_version(access_point_data(self.coordinator, self._identity).get("firmware", ""))

    @property
    def installed_version(self) -> str | None:
        installed = self._installed
        return version_text(installed) if installed else None

    @property
    def latest_version(self) -> str | None:
        if self._latest is None:
            return None
        return version_text(max(self._latest.version, self._installed or ()))

    @property
    def in_progress(self) -> bool:
        status = self._controller.upgrade_status(self._identity)
        return status is not None and status["phase"] in UPLOADING_PHASES

    @property
    def update_percentage(self) -> int | None:
        status = self._controller.upgrade_status(self._identity)
        return status["percent"] if status else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._async_check, CHECK_INTERVAL))
        self.hass.async_create_task(self._async_check())

    async def async_will_remove_from_hass(self) -> None:
        self._stop_progress()
        await super().async_will_remove_from_hass()

    async def _async_check(self, _now=None) -> None:
        device = access_point_data(self.coordinator, self._identity)
        model, hardware = device.get("model", ""), device.get("hardware", "")
        if not model or not hardware:
            return
        try:
            self._latest = await async_latest_package(self.hass, model, hardware)
        except (ClientError, RuntimeError, TimeoutError) as error:
            LOG.warning("firmware check for %s failed: %s", self._identity, error)
            return
        self.async_write_ha_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        installed = self._installed
        if self._latest is None or installed is None or self._latest.version <= installed:
            raise HomeAssistantError(f"no firmware newer than the installed one for {self._identity}")
        path = await async_fetch_package(self.hass, self._latest)
        await async_upgrade_ap(self.hass, self._identity, path.name, "")
        self._start_progress()
        self.async_write_ha_state()

    @callback
    def _start_progress(self) -> None:
        if self._unsub_progress is None:
            self._unsub_progress = async_track_time_interval(self.hass, self._async_progress, PROGRESS_INTERVAL)

    @callback
    def _async_progress(self, _now) -> None:
        self.async_write_ha_state()
        if not self.in_progress:
            self._stop_progress()

    @callback
    def _stop_progress(self) -> None:
        if self._unsub_progress is not None:
            self._unsub_progress()
            self._unsub_progress = None
