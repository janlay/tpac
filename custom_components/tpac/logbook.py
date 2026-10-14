"""Describe TPAC events for Home Assistant's Activity (logbook), and fire them on the event bus."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, EVENT_TPAC
from .ssid import unique_id as ssid_unique_id

# Client events belong to the client; the SSID-attached ones go to the SSID's status entity when they name an SSID.
CLIENT_EVENTS = {"sta_disassoc", "client_ip", "client_connect", "client_roam"}
SSID_EVENTS = {"sta_disassoc", "client_connect"}


@callback
def async_describe_events(hass: HomeAssistant, async_describe_event) -> None:
    """The Activity line is the event name (a short variable-style word); the rest stays in the event data."""

    @callback
    def describe(event: Event) -> dict:
        data = event.data
        line = {"name": "TPAC", "message": data["event"]}
        if data.get("entity_id"):
            line["entity_id"] = data["entity_id"]
        return line

    async_describe_event(DOMAIN, EVENT_TPAC, describe)


@callback
def async_fire_tpac_event(hass: HomeAssistant, entry: ConfigEntry, record: dict) -> None:
    """Fire one controller event as ``tpac_event``, attached to the entity it belongs to."""
    registry = er.async_get(hass)
    name, data, identity = record["event"], record["data"], record["identity"]
    if name in SSID_EVENTS and data.get("ssid"):
        unique_id = ssid_unique_id(entry, data["ssid"], "status")
    elif name in CLIENT_EVENTS:
        unique_id = f"{entry.entry_id}_client_{identity}"
    else:
        unique_id = f"{entry.entry_id}_{identity}"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    payload = {"event": name, "identity": identity, "title": record["title"], "data": data}
    if entity_id:
        payload["entity_id"] = entity_id
    hass.bus.async_fire(EVENT_TPAC, payload)
