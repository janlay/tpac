"""TPAC Home Assistant integration: observes LLDP and runs the software AC on UDP 5246."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .certificate import ensure_certificate
from .const import AC_FIRMWARE, AC_NAME, CONF_INTERFACE, DOMAIN, HUB_MANUFACTURER, HUB_MODEL, HUB_URL
from .controller import Controller
from .coordinator import VERSION, TpacDataUpdateCoordinator
from .config_flow import async_start_discovery
from .logbook import async_fire_tpac_event
from .entities import device_identity
from .firmware import (
    download_package, find_latest, image_version_record, package_path, parse_version, read_image,
    remove_old_packages, store_package, verify_image,
)

PLATFORMS = ["number", "sensor", "switch", "text"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the TPAC integration and its services."""
    hass.data.setdefault(DOMAIN, {})

    async def check_ap_update(call: ServiceCall) -> dict:
        return await _async_check_ap_update(hass, call.data["device"])

    hass.services.async_register(
        DOMAIN, "check_ap_update", check_ap_update,
        schema=vol.Schema({vol.Required("device"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    async def adopt_ap(call: ServiceCall) -> None:
        _controller(hass).adopt_or_raise(call.data["identity"].lower())

    async def forget_ap(call: ServiceCall) -> None:
        _controller(hass).forget_or_raise(call.data["identity"].lower())

    # The panel calls these names and passes "identity".
    identity_schema = vol.Schema({vol.Required("identity"): cv.string})
    hass.services.async_register(DOMAIN, "adopt", adopt_ap, schema=identity_schema)
    hass.services.async_register(DOMAIN, "forget", forget_ap, schema=identity_schema)
    await _async_register_panel(hass)

    async def import_profiles(call: ServiceCall) -> None:
        controller = _controller(hass)
        try:
            await hass.async_add_executor_job(controller.import_profiles, call.data["data"])
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error

    async def export_profiles(call: ServiceCall) -> dict:
        controller = _controller(hass)
        try:
            return await hass.async_add_executor_job(controller.export_profiles)
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error

    hass.services.async_register(
        DOMAIN, "import_profiles", import_profiles,
        schema=vol.Schema({vol.Required("data"): dict}),
    )
    hass.services.async_register(
        DOMAIN, "export_profiles", export_profiles,
        supports_response=SupportsResponse.ONLY,
    )

    async def _run(call: ServiceCall, action) -> None:
        """Run one controller call for the access point in the call, and report a refusal to the caller."""
        identity = call.data["identity"].lower()
        try:
            ok = await hass.async_add_executor_job(action, _controller(hass), identity, call)
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
        if not ok:
            raise ServiceValidationError(f"{identity} is not adopted or not connected")

    async def reboot_ap(call: ServiceCall) -> None:
        def action(controller, identity, _call):
            refusal = controller.reboot_refusal(identity)
            if refusal:
                raise ValueError(refusal)
            return controller.reboot(identity)

        await _run(call, action)

    async def factory_reset_ap(call: ServiceCall) -> None:
        await _run(call, lambda controller, identity, _call: controller.factory_reset(identity))

    async def assign_profiles(call: ServiceCall) -> None:
        names = call.data["names"]
        await _run(call, lambda controller, identity, _call: controller.assign_profiles(identity, names))

    async def set_country(call: ServiceCall) -> None:
        country = call.data["country"]
        await _run(call, lambda controller, identity, _call: controller.set_country(identity, country))

    ap_schema = vol.Schema({vol.Required("identity"): cv.string})
    hass.services.async_register(DOMAIN, "reboot", reboot_ap, schema=ap_schema)
    hass.services.async_register(DOMAIN, "factory_reset", factory_reset_ap, schema=ap_schema)
    hass.services.async_register(
        DOMAIN, "assign_profiles", assign_profiles,
        schema=vol.Schema({vol.Required("identity"): cv.string, vol.Required("names"): cv.ensure_list}),
    )
    hass.services.async_register(
        DOMAIN, "set_country", set_country,
        schema=vol.Schema({vol.Required("identity"): cv.string, vol.Required("country"): cv.string}),
    )

    async def upgrade_ap(call: ServiceCall) -> None:
        await _async_upgrade_ap(hass, call.data["device"], call.data["package"], call.data["identifier"])

    hass.services.async_register(
        DOMAIN, "upgrade_ap", upgrade_ap,
        schema=vol.Schema({
            vol.Required("device"): cv.string,
            vol.Required("package"): cv.string,
            vol.Required("identifier"): vol.All(cv.string, vol.Match(r"^[0-9a-fA-F]{40}$")),
        }),
    )
    return True


LOG = logging.getLogger(__name__)
PANEL_FILE = Path(__file__).parent / "static" / "tpac-panel.js"


def _panel_version() -> str:
    return hashlib.sha256(PANEL_FILE.read_bytes()).hexdigest()[:12]


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel script and add the TPAC page to the sidebar.  The script version is its content hash."""
    version = await hass.async_add_executor_job(_panel_version)
    url = f"/{DOMAIN}_static/tpac-panel.js"
    await hass.http.async_register_static_paths([StaticPathConfig(url, str(PANEL_FILE), cache_headers=False)])
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="tpac-panel",
        frontend_url_path=DOMAIN,
        module_url=f"{url}?v={version}",
        sidebar_title="TPAC",
        sidebar_icon="mdi:access-point-network",
        require_admin=True,
    )


def _controller(hass: HomeAssistant) -> Controller:
    coordinator = next(iter(hass.data[DOMAIN].values()), None)
    if coordinator is None:
        raise ServiceValidationError("TPAC is not set up")
    return coordinator.controller


async def _async_upgrade_ap(hass: HomeAssistant, identity: str, package: str, identifier: str) -> None:
    """Upload a verified package to an adopted AP.  The AP installs it and reboots."""
    path = Path(hass.config.path(DOMAIN, "firmware")) / package
    if not path.is_file():
        raise ServiceValidationError(f"no package {package}")
    image, record, reason = await hass.async_add_executor_job(_prepare_image, path)
    if reason:
        raise ServiceValidationError(reason)
    coordinator = next(iter(hass.data[DOMAIN].values()))
    if not coordinator.controller.upgrade(identity.lower(), image, record, bytes.fromhex(identifier)):
        raise ServiceValidationError("the access point is not adopted or not connected")


def _prepare_image(path: Path) -> tuple[bytes, bytes, str]:
    image = read_image(path)
    reason = verify_image(image)
    return image, image_version_record(path), (f"signature check failed: {reason}" if reason else "")


async def _async_check_ap_update(hass: HomeAssistant, identity: str) -> dict:
    """Compare an access point with the resource centre.  A newer package is downloaded and verified."""
    coordinator = next(iter(hass.data[DOMAIN].values()), None)
    device = next(
        (item for item in (coordinator.data["devices"] if coordinator else []) if device_identity(item) == identity.lower()),
        None,
    )
    if device is None:
        raise ServiceValidationError(f"no access point {identity}")
    model, hardware, firmware = device.get("model", ""), device.get("hardware", ""), device.get("firmware", "")
    current = parse_version(firmware)
    if not model or not hardware or current is None:
        raise ServiceValidationError("the access point did not report its model, hardware or software version")

    directory = Path(hass.config.path(DOMAIN, "firmware"))
    await hass.async_add_executor_job(remove_old_packages, directory)
    session = async_get_clientsession(hass)
    result = {"model": model, "hardware": hardware, "current": firmware, "latest": None, "update_available": False}
    latest = await find_latest(session, model, hardware)
    if latest is None or latest.version <= current:
        return result

    path = package_path(directory, latest.url)
    if not path.exists():
        data = await download_package(session, latest.url)
        path = await hass.async_add_executor_job(store_package, directory, latest.url, data)
    image = await hass.async_add_executor_job(read_image, path)
    reason = await hass.async_add_executor_job(verify_image, image)
    result.update(
        latest=".".join(map(str, latest.version)), update_available=True,
        package=path.name, verified=not reason, reason=reason,
    )
    return result


def _create_controller(config_dir: Path, interface: str) -> Controller:
    """Create the controller, which binds UDP 5246.  State lives in <config>/tpac/."""
    certificate, key = ensure_certificate(config_dir)
    return Controller(
        ac_name=AC_NAME,
        ac_firmware=AC_FIRMWARE,
        certificate=certificate,
        key=key,
        interface=interface,
        state_dir=config_dir / DOMAIN,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the controller on the configured interface."""
    interface = entry.data[CONF_INTERFACE]
    # Key generation and socket binding block, so they run outside the event loop.
    controller = await hass.async_add_executor_job(_create_controller, Path(hass.config.path()), interface)
    controller.client_grace = _client_grace(entry)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    coordinator = TpacDataUpdateCoordinator(hass, controller)
    controller.on_event = lambda record: hass.add_job(async_fire_tpac_event, hass, entry, record)
    hass.data[DOMAIN][entry.entry_id] = coordinator
    # The hub device exists before its entities, so SSID devices can name it with via_device_id.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="TPAC",
        manufacturer=HUB_MANUFACTURER,
        model=HUB_MODEL,
        sw_version=f"v{VERSION}",
        configuration_url=HUB_URL,
    )
    # Per-client entities replaced by the integration options (grace) and the device Delete action (forget).
    registry = er.async_get(hass)
    for retired in er.async_entries_for_config_entry(registry, entry.entry_id):
        if retired.unique_id.startswith(f"{entry.entry_id}_client_") and retired.unique_id.endswith(("_grace", "_forget")):
            registry.async_remove(retired.entity_id)
    threading.Thread(target=controller.serve_forever, name="tpac-capwap", daemon=True).start()
    entry.async_on_unload(controller.stop)
    await coordinator.async_config_entry_first_refresh()
    # A discovered access point waiting for adoption appears under Discovered; see config_flow.py.
    def on_update() -> None:
        hass.async_create_task(async_start_discovery(hass, coordinator))

    entry.async_on_unload(coordinator.async_add_listener(on_update))
    on_update()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the controller and unload the platforms."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True


def _client_grace(entry: ConfigEntry) -> dict[str, float]:
    """Per-client disconnect grace periods from the integration options.  A client without one follows the device-wide period."""
    return {mac: float(seconds) for mac, seconds in entry.options.get("client_grace", {}).items() if float(seconds)}


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed grace periods without reloading the integration, so access points stay joined."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        coordinator.controller.client_grace = _client_grace(entry)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device_entry) -> bool:
    """A Wi-Fi client's device can be removed from its page once the client is not connected."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    prefix = f"{entry.entry_id}_client_"
    for domain, key in device_entry.identifiers:
        if domain == DOMAIN and key.startswith(prefix) and coordinator and coordinator.clients is not None:
            return await coordinator.clients.drop(key[len(prefix):])
    return False
