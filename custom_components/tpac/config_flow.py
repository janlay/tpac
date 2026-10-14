"""Config flow for TPAC: the one interface to observe."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_INTERFACE, DEFAULT_INTERFACE, DOMAIN


def _controller(hass):
    coordinator = next(iter(hass.data.get(DOMAIN, {}).values()), None)
    return coordinator.controller if coordinator else None


class TpacOptionsFlow(config_entries.OptionsFlow):
    """Disconnect grace period of one Wi-Fi client.  0 follows the device-wide period."""

    async def async_step_init(self, user_input=None):
        coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
        clients = coordinator.clients
        grace = dict(self.config_entry.options.get("client_grace", {}))
        if user_input is not None:
            mac, seconds = user_input["client"], int(user_input["seconds"])
            if seconds:
                grace[mac] = seconds
            else:
                grace.pop(mac, None)
            return self.async_create_entry(title="", data={**self.config_entry.options, "client_grace": grace})
        if clients is None or not clients.known():
            return self.async_abort(reason="no_clients")
        options = [selector.SelectOptionDict(value=mac, label=clients.name_of(mac)) for mac in clients.known()]
        current = ", ".join(f"{clients.name_of(mac)}: {seconds} s" for mac, seconds in grace.items()) or "-"
        return self.async_show_form(
            step_id="init",
            description_placeholders={"current": current},
            data_schema=vol.Schema(
                {
                    vol.Required("client"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Required("seconds", default=0): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
                    ),
                }
            ),
        )


class TpacConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Choose the host interface whose LLDP frames TPAC observes."""

    VERSION = 3

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TpacOptionsFlow()

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title=f"TPAC ({user_input[CONF_INTERFACE]})", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_INTERFACE, default=DEFAULT_INTERFACE): selector.TextSelector()}
            ),
        )

    async def async_step_discovery(self, discovery_info):
        """An access point that asked to join is waiting for adoption.  It appears under Discovered."""
        identity = discovery_info["identity"]
        await self.async_set_unique_id(identity)
        self._abort_if_unique_id_configured()
        self._identity = identity
        self._name = discovery_info["name"]
        self._address = discovery_info["address"]
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_adopt()

    async def async_step_adopt(self, user_input=None):
        if user_input is not None:
            controller = _controller(self.hass)
            if controller is None:
                return self.async_abort(reason="not_running")
            if not controller.adopt(self._identity):
                return self.async_abort(reason="not_discovered")
            return self.async_abort(reason="adopted")
        return self.async_show_form(
            step_id="adopt",
            description_placeholders={"name": self._name, "identity": self._identity, "address": self._address},
        )


async def async_start_discovery(hass, coordinator) -> None:
    """Start one discovery flow for each access point that is waiting and has no flow yet."""
    in_progress = {flow["context"].get("unique_id") for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)}
    for device in coordinator.data.get("devices", []):
        identity = device.get("identity")
        if device.get("status") != "pending" or not identity or identity in in_progress:
            continue
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DISCOVERY},
            data={
                "identity": identity,
                "name": device.get("model") or identity,
                "address": device.get("source_ip") or "",
            },
        )
