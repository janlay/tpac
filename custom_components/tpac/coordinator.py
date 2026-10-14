"""Coordinator for the access points that the controller serves."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, UPDATE_INTERVAL
from .controller import Controller

_LOGGER = logging.getLogger(__name__)
VERSION = json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]
# A change refreshes the data at once; bursts of changes are merged into one refresh per second.
REFRESH_COOLDOWN = 1.0


class TpacDataUpdateCoordinator(DataUpdateCoordinator):
    """Read the access points and the discovery counters of the controller."""

    def __init__(self, hass: HomeAssistant, controller: Controller) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=REFRESH_COOLDOWN, immediate=True),
        )
        self.controller = controller
        # Set by the client module: the Wi-Fi client registry.
        self.clients = None
        controller.on_change = lambda: hass.add_job(self.async_request_refresh)

    async def _async_update_data(self) -> dict:
        devices = self.controller.devices()
        profiles = self.controller.profiles.list()
        sources = sorted({device["source_ip"] for device in devices if device.get("source_ip")})
        return {
            "devices": devices,
            "profiles": profiles,
            "left_at": self.controller.left_at(),
            "stats": {
                "packets": self.controller.counters["discovery_requests"],
                "bytes": 0,
                "sources": [{"address": address} for address in sources],
                "last_received": self.controller.last_request_at,
                "untracked_source_packets": 0,
            },
            "health": {"version": VERSION},
        }
